#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""backfill.py —— 缺失录播检查 + 补档编排（第二备份 rec.darkpy.cn / 第三 B站）。

用途（用户规则 2026-09-05）
--------------------------
1. 用「实际直播日期」（live_calendar 多源并集：rec站 / 本地 / B站标题）作基准；
2. 与 本地录播文件、Word 月度总结、state.done_dates 比对，找出缺失的直播日期
   （一天多场按天判断：当天有任何一场已总结/有录播就不算缺失；文件明显过小
   视为不完整，也归入缺失）；
3. 缺失的按 本地 → rec.darkpy.cn → B站 三级顺序补档：
   · rec.darkpy.cn：FLV 分段下载 → 按段结束时间归组 → concat 无损合并（不重编码）；
   · B站：下载合并多 P 后走同一总结流程（原 bilibili_fallback 逻辑，仅限定目标日期）；
4. 补档成功的场次复用 daily_pipeline.process_video_source（转写→热词→总结→Word→
   MP3→压缩），压缩后的 MP4 归档到「录播根目录/YYYY-MM-DD/」日期文件夹
   （out_dir_mode=date_subfolder，当前规则；MP3 留在录播根目录）；
5. 全程幂等/断点续跑：已下载不重下、已合并不重合并、转写/总结产物存在即跳过。

对外接口
--------
    analyze(cfg, st, log, start, end)
        返回逐日报告行 + 各来源日期集合，不做任何下载/写操作。
    stage(cfg, st, log, progress, ...)
        主编排：analyze → 逐缺失日补档（rec → B站）→ 汇总输出。
    命令行：python backfill.py [--from YYYY-MM-DD] [--to YYYY-MM-DD]
            [--report]  只打印检查结果（推荐先用）
            [--fill]     实际补档（不传 --report/--fill 时默认 report）
"""
import datetime
import os
import re

# 明显不完整阈值：单日本地录播视频总大小 < 该值则视为录制失败/片段，不当作已覆盖
MIN_COVER_VIDEO_MB = 20
_HEAD_DATE_RE = re.compile(r'(20\d{2})年(\d{1,2})月(\d{1,2})日')
MEDIA_EXT = ('.mp4', '.flv', '.mkv', '.mov')


def cfg_backfill(cfg):
    b = cfg.get('backfill') or {}
    return {
        'enabled': b.get('enabled', True),
        # 默认补档窗口：从 since_live_date 到今天（只补该主播的近期目标范围）
        'start_date': (b.get('start_date') or
                       (cfg.get('latest_mode') or {}).get('since_live_date') or ''),
        'end_date': b.get('end_date') or '',
        'max_dates_per_run': int(b.get('max_dates_per_run') or 3),
        'min_cover_video_mb': int(b.get('min_cover_video_mb') or MIN_COVER_VIDEO_MB),
    }


def window(cfg, only_date=None, frm=None, to=None):
    """决定检查/补档的日期窗口。返回 (start_iso, end_iso)。"""
    if only_date:
        return only_date, only_date
    b = cfg_backfill(cfg)
    start = frm or b['start_date']
    end = to or b['end_date'] or datetime.date.today().isoformat()
    # 校验格式，非法则抛给调用方
    datetime.date.fromisoformat(start)
    datetime.date.fromisoformat(end)
    return start, end


def _norm(ymd):
    return '%s-%s-%s' % (ymd[:4], ymd[4:6], ymd[6:8]) if len(ymd) == 8 else None


# ---------------------------------------------------------------------------
# 已覆盖来源 1：Word 月度文档（章节标题含 日期+N点场）
# ---------------------------------------------------------------------------
def word_covered_dates(cfg, log, start, end):
    """扫描 [start,end] 所在月份的 Word 总结文档，返回已出现章节的日期集合。"""
    out = set()
    try:
        import docx
    except Exception as e:
        log('[backfill] 无 python-docx，跳过 Word 比对（%s）' % e, 'WARN')
        return out

    # 需要检查的月份：start..end 逐月
    cur = datetime.date.fromisoformat(start)
    last = datetime.date.fromisoformat(end)
    while cur <= last:
        # 复用 summarize_gemini 的月度路径构造
        try:
            import summarize_gemini as SG
            p = SG.doc_path_for(cfg, cur)
        except Exception:
            p = ''
        if p and os.path.exists(p):
            try:
                d = docx.Document(p)
                for para in d.paragraphs:
                    t = (para.text or '').strip()
                    if not t or '点场' not in t:
                        continue
                    m = _HEAD_DATE_RE.search(t[:60])
                    if m:
                        out.add('%04d-%02d-%02d' % (int(m.group(1)), int(m.group(2)),
                                                     int(m.group(3))))
            except Exception as e:
                log('[backfill] 读 Word %s 失败：%s' % (os.path.basename(p), e), 'WARN')
        # 下个月
        y, m = cur.year, cur.month
        cur = datetime.date(y + 1, 1, 1) if m == 12 else datetime.date(y, m + 1, 1)
    if out:
        log('[backfill] Word 月度文档覆盖 %d 个日期' % len(out))
    return out


# ---------------------------------------------------------------------------
# 已覆盖来源 2：本地录播文件（含「过小=不完整」判定）
# ---------------------------------------------------------------------------
def local_status(cfg, log, start, end):
    """扫描本地录播（根目录 + YYYY-MM-DD 子目录），返回 {date: {'bytes':n,'files':[]}}。"""
    root = (cfg.get('local') or {}).get('root') or ''
    res = {}
    if not root or not os.path.isdir(root):
        return res
    dirs = [root] + [os.path.join(root, d) for d in os.listdir(root)
                     if os.path.isdir(os.path.join(root, d))
                     and re.match(r'^\d{4}-\d{2}-\d{2}$', d)]
    for sd in dirs:
        try:
            names = os.listdir(sd)
        except Exception:
            continue
        for n in names:
            if not n.lower().endswith(MEDIA_EXT):
                continue
            # 媒体文件名里的 8 位日期用于归日。date_subfolder 模式下压缩产物
            # *_compressed.mp4 就落在该日期子目录里，会被正常计入（本地已覆盖）。
            m = re.search(r'(20\d{2})(\d{2})(\d{2})', n)
            d = _norm('%s%s%s' % (m.group(1), m.group(2), m.group(3))) if m else None
            if not d or not (start <= d <= end):
                continue
            full = os.path.join(sd, n)
            try:
                sz = os.path.getsize(full)
            except Exception:
                sz = 0
            res.setdefault(d, {'bytes': 0, 'files': []})
            res[d]['bytes'] += sz
            res[d]['files'].append(n)
    return res


# ---------------------------------------------------------------------------
# 主分析：逐日判定 covered / missing
# ---------------------------------------------------------------------------
def analyze(cfg, st, log, start, end, sources=None):
    """返回 (rows, sources_used)。

    row = {'date', 'status', 'detail', 'darkpy_flv', 'darkpy_sess', 'darkpy_gb',
           'local_bytes', 'bilibili_live': bool}

    status ∈ {'covered', 'local_pending', 'local_incomplete', 'missing'}
      · covered          该日已处理/已总结/本地录播完整
      · local_pending    本地有视频但未总结（应走本地重试，不从 rec 重下）
      · local_incomplete 本地文件过小（疑似 bug 产物）→ 归入缺失，需补档
      · missing          本地没有该日录播 → 待补档
    """
    import live_calendar
    # 实际直播日期（sources 可由调用方限定，避免无谓拉 B站全集）
    dates, used = live_calendar.actual_live_dates(
        cfg, log, start_date=start, end_date=end, sources=sources)
    if not dates:
        log('[backfill] %s~%s 窗口内未发现任何直播日期（来源：%s）'
            % (start, end, used or '无'))
        return [], used

    done = set(st.get('done_dates', []))
    word = word_covered_dates(cfg, log, start, end)
    loc = local_status(cfg, log, start, end)
    # rec站当天场次信息（供「缺失后有没有得补」判断；只统计归属当天的分段，
    # 跨午夜尾巴归前一场，不计入本日段数）
    try:
        import darkpy_rec as D
        dd = D.cfg_darkpy(cfg)
        dp_files = D.fetch_index(cfg, log) if dd['enabled'] else []
    except Exception as e:
        log('[backfill] rec站索引失败：%s' % e, 'WARN')
        dp_files = []
    dp_by = {}
    if dp_files:
        # 先全局归组一次（跨午夜尾巴归到开播日），再按天取场次
        all_sess = D.group_all_sessions(dp_files, dd['max_gap_sec'], log)
        for sess in all_sess:
            iso = _norm(sess[0].get('ymd') or '')
            if not iso or not (start <= iso <= end):
                continue
            gb = sum(x.get('size_gb', 0) for x in sess)
            e = dp_by.setdefault(iso, {'flv': 0, 'sess': 0, 'gb': 0.0})
            e['flv'] += len(sess)
            e['sess'] += 1
            e['gb'] = round(e['gb'] + gb, 2)

    min_cover = cfg_backfill(cfg)['min_cover_video_mb'] * 1024 * 1024
    rows = []
    for d in dates:
        local = loc.get(d, {'bytes': 0, 'files': []})
        row = {'date': d, 'darkpy_flv': 0, 'darkpy_sess': 0, 'darkpy_gb': 0.0,
               'local_bytes': local['bytes'], 'local_files': local['files']}
        if d in dp_by:
            row.update(darkpy_flv=dp_by[d]['flv'], darkpy_sess=dp_by[d]['sess'],
                       darkpy_gb=dp_by[d]['gb'])
        if d in done:
            row['status'], row['detail'] = 'covered', '已处理（state.done_dates）'
        elif d in word:
            row['status'], row['detail'] = 'covered', 'Word 月度文档已有该日章节'
        elif local['bytes'] >= min_cover:
            row['status'] = 'local_pending'
            row['detail'] = '本地有视频但未总结（%.2f GB），走本地重试'
            row['detail'] = row['detail'] % (local['bytes'] / 1024 ** 3)
        elif local['bytes'] > 0:
            row['status'] = 'local_incomplete'
            row['detail'] = '本地文件过小（%.1f MB），疑似录制 bug/片段，需补档' \
                            % (local['bytes'] / 1024 ** 2)
        else:
            row['status'] = 'missing'
            row['detail'] = '本地缺失'
        rows.append(row)
    log('[backfill] %s~%s：实际直播 %d 天，缺失/待补 %d 天'
        % (start, end, len(dates),
           sum(1 for r in rows if r['status'] in ('missing', 'local_incomplete'))))
    return rows, used


# ---------------------------------------------------------------------------
# 报告打印
# ---------------------------------------------------------------------------
def print_report(cfg, log, rows):
    """简洁逐行结果（覆盖/缺失情况 + rec站是否有货），供人读与日志用。"""
    if not rows:
        print('（窗口内没有实际直播日期）')
        return
    print()
    print('缺失检查结果')
    print('-' * 70)
    for r in rows:
        tag = {'covered': '已覆盖', 'local_pending': '本地待总结',
               'local_incomplete': '本地片段', 'missing': '缺失'}[r['status']]
        line = '%-10s [%s] %s' % (r['date'], tag, r['detail'])
        if r['status'] in ('missing', 'local_incomplete'):
            line += '  | rec站: %d场/%d段/%.2fGB' % (r['darkpy_sess'], r['darkpy_flv'],
                                                     r['darkpy_gb']) \
                if r['darkpy_flv'] else '  | rec站: 无该日'
        print(line)
    print('-' * 70)


def _final_lines(rows, done, was_missing=None):
    """输出用户要求的逐日结论行，如：
    2026-09-01：本地缺失 → rec.darkpy.cn → 成功
    2026-09-05：本地缺失 → 无源（rec站/B站都没有）→ 等上传后自动补
    """
    was_missing = was_missing or set()
    lines = []
    for r in rows:
        d = r['date']
        if d in done:
            if d in was_missing:
                src = 'rec.darkpy.cn' if r['darkpy_flv'] else 'B站'
                lines.append('%s：本地缺失 → %s → 成功' % (d, src))
            else:
                lines.append('%s：%s，无需补档' % (d, r['detail']))
        elif r['status'] == 'local_pending':
            lines.append('%s：%s（本地已有视频，下轮自动重试）' % (d, r['detail']))
        elif r['status'] == 'covered':
            lines.append('%s：%s，无需补档' % (d, r['detail']))
        else:
            if r['darkpy_flv']:
                lines.append('%s：%s → rec.darkpy.cn → 失败（见日志，可重跑）' % (d, r['detail']))
            else:
                lines.append('%s：%s → rec站无该日 → 等 B站上传后自动补' % (d, r['detail']))
    return lines


# ---------------------------------------------------------------------------
# 编排：补齐缺失（rec.darkpy.cn → B站）
# ---------------------------------------------------------------------------
def stage(cfg, st, log, progress, cookies=None, only_date=None, frm=None, to=None,
          dry_run=False, do_bilibili=True):
    """主编排：analyze → 逐缺失日补档（rec → B站）→ 返回逐日结论行列表。

    由 daily_pipeline 在本地扫描之后调用；也可由 backfill.py 命令行单独驱动。
    """
    import daily_pipeline as DP   # 延迟导入，避免循环依赖（本模块被 daily_pipeline import）
    b = cfg_backfill(cfg)
    if not b['enabled'] and not only_date:
        log('[backfill] 未启用（backfill.enabled=false），跳过缺失补档')
        return []
    start, end = window(cfg, only_date, frm, to)

    # 快路径日历：rec站 + 本地（不拉 B站全集，避免每早白扫 ~100s）
    # 实际直播日历：默认用 config.live_calendar.sources（rec站 ∪ 本地 ∪ B站标题），
    # 保证「只有 B站有、rec站没有」的日期也能被识别成缺失并走第三备份。
    rows, used = analyze(cfg, st, log, start, end)
    if not rows:
        log('[backfill] 窗口内无直播日期（来源：%s）' % (used or '无'))
        return []
    print_report(cfg, log, rows)

    targets = [r for r in rows if r['status'] in ('missing', 'local_incomplete')]
    if not targets:
        log('[backfill] 窗口内无缺失，无需补档')
        return _final_lines(rows, set(st.get('done_dates', [])), set())
    if dry_run:
        log('[backfill] dry-run：缺失 %d 天，跳过实际补档' % len(targets))
        miss_set = {r['date'] for r in targets}
        lines = []
        for r in rows:
            if r['date'] in miss_set:
                if r['darkpy_flv']:
                    lines.append('%s：%s → rec.darkpy.cn → 待补档（dry-run，未下载）'
                                 % (r['date'], r['detail']))
                else:
                    lines.append('%s：%s → rec站无该日 → 待源站上传（dry-run，未下载）'
                                 % (r['date'], r['detail']))
            elif r['status'] == 'covered':
                lines.append('%s：%s，无需补档' % (r['date'], r['detail']))
            else:
                lines.append('%s：%s' % (r['date'], r['detail']))
        return lines

    log('[backfill] 待补档 %d 天，顺序：rec.darkpy.cn → B站' % len(targets))
    done = set(st.get('done_dates', []))

    # ---- 第一优先源：rec.darkpy.cn ----
    rec_rows = [r for r in targets if r['darkpy_sess'] > 0]
    for r in rec_rows:
        date_iso = r['date']
        if date_iso in done:
            continue
        import darkpy_rec as D
        work_dir = (cfg.get('darkpy') or {}).get('work_dir') or \
            os.path.join(cfg['_workdir'], '_darkpy')
        for sidx in range(r['darkpy_sess']):
            if date_iso in done:
                break
            log('=' * 66)
            log('[backfill] rec站补档 %s 第 %d/%d 场' % (date_iso, sidx + 1,
                                                        r['darkpy_sess']))
            log('=' * 66)
            try:
                res = D.fetch_and_prepare(cfg, log, date_iso, work_dir,
                                          session_index=sidx)
            except Exception as e:
                log.error('[backfill] rec站取 %s 场次 %d 异常：%s'
                          % (date_iso, sidx + 1, e))
                continue
            if not res:
                log.error('[backfill] rec站 %s 场次 %d 下载/合并失败'
                          % (date_iso, sidx + 1))
                continue
            if _process_darkpy_session(cfg, st, DP, date_iso, res, sidx,
                                       log, progress):
                DP.mark_darkpy_done(st, cfg, date_iso, res, sidx)
            else:
                log.error('[backfill] %s 场次 %d 处理未完成（保留中间产物，可重跑）'
                          % (date_iso, sidx + 1))

    # ---- 仍未覆盖、rec站确实没有的 → B站兜底（限定目标日期） ----
    done = set(st.get('done_dates', []))
    remaining = [r['date'] for r in targets if r['date'] not in done]
    if remaining and do_bilibili:
        log('[backfill] rec站补齐后仍缺 %d 天，转 B站兜底：%s'
            % (len(remaining), remaining))
        try:
            DP.bilibili_fallback(cfg, st, log, cookies, progress,
                                 only_dates=remaining)
        except Exception as e:
            log.error('[backfill] B站兜底异常：%s' % e)

    # ---- 最终结论行 ----
    rows2, _ = analyze(cfg, st, log, start, end)
    done = set(st.get('done_dates', []))
    lines = _final_lines(rows2, done, {r['date'] for r in targets})
    log('=' * 66)
    log('[backfill] 补档结果')
    for ln in lines:
        log('[backfill] ' + ln)
    return lines


def _process_darkpy_session(cfg, st, DP, date_iso, res, sidx, log, progress):
    """把 rec站合好的 MP4 送入统一总结流水线（转写→热词→总结→Word→MP3→压缩）。"""
    live_date = datetime.date.fromisoformat(date_iso)
    title = res.get('title') or ''
    # 轻量清洗文件名非法字符（res.title 来自 FLV 标题，可能含 / : 等）
    clean_title = re.sub(r'[\\/:*?"<>|]', '', title).strip() or 'rec录播'
    hour = 0
    hms = res.get('hms') or ''
    if len(hms) >= 2 and hms[:2].isdigit():
        hour = int(hms[:2])
    fname = DP.session_fname(live_date, clean_title, hour, sidx)
    log('[backfill] 送入总结流水线：%s' % fname)
    return DP.process_video_source(cfg, st, res['src_video'], date_iso, fname,
                                   'darkpy', log, progress)


def main():
    import argparse
    import common
    ap = argparse.ArgumentParser(description='缺失录播检查 + 补档（rec.darkpy.cn → B站）')
    ap.add_argument('--from', dest='frm', default=None, help='开始日期 YYYY-MM-DD')
    ap.add_argument('--to', default=None, help='结束日期 YYYY-MM-DD')
    ap.add_argument('--date', default=None, help='只检查/补档指定日期')
    ap.add_argument('--report', action='store_true', help='只打印检查报告（默认）')
    ap.add_argument('--fill', action='store_true', help='实际补档')
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--config', default=None)
    args = ap.parse_args()

    import sys
    import common as _common
    cfg_path = args.config
    if not cfg_path:
        wd = args.workdir or os.environ.get('BILI_WORKDIR')
        if wd:
            cfg_path = os.path.join(wd, 'config.json')
    cfg = _common.load_config(cfg_path)
    log = _common.Logger(cfg, 'backfill')
    st = _common.load_state(cfg)
    cookies = _common.load_cookies(cfg['paths'].get('cookies'))

    report = args.report or (not args.fill)
    lines = stage(cfg, st, log, {}, cookies=cookies,
                  only_date=args.date, frm=args.frm, to=args.to,
                  dry_run=report, do_bilibili=args.fill)
    print()
    print('== 补档结论 ==')
    for ln in lines:
        print('  ' + ln)
    sys.exit(0)


if __name__ == '__main__':
    main()
