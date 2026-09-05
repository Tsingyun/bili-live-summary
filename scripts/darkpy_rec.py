#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""rec.darkpy.cn 录播站客户端（第二道备份源）。

站点结构（2026-09-05 实测确认，勿凭猜测改动）
------------------------------------------
· 站点是 phpstudy + 自研 PHP 文件浏览器，端口 5144 是页面，5143 是文件服务。
· 目录列表：http://rec.darkpy.cn:5143/file.php
    必须带 dir / dir_s / dir_f / dir_file 四个参数（缺一则拿不到列表）：
      dir      = `<主播目录>/`           例：25788785-岁己SUI/
      dir_s    = 1
      dir_f    = `./file/<主播目录>`     例：./file/25788785-岁己SUI
      dir_file = {"1":".\\/file\\/"}
  （这几个参数取自页面隐藏字段 #dir_s/#dir_f/#dir_file，见 index.js 的拼接逻辑）
· 文件下载：http://rec.darkpy.cn:5143/file/<主播目录>/<文件名>.flv
  页面里还有个 ds.darkpy.cn 镜像域名，但 rec 域名直连实测可用（HEAD 200 + 正确 Content-Length）。
· 直接访问 /file/<目录>/ 会被 403（关闭了目录浏览），只能走 file.php。

文件命名：YYYYMMDD-HHMMSS-<序号>-<标题>.flv
一场直播常被切成多个 FLV（约每小时一段，或网络波动重连），需按时间间隙归组后合并。

对外接口
--------
    fetch_index(cfg, log)                 列出主播目录下全部 FLV
    files_for_date(files, ymd)            取某天的 FLV
    group_sessions(files, max_gap_sec)    按时间连续性归组为「场次」
    download_one(cfg, log, name, dest)    断点续传下载单个 FLV
    merge_flvs(cfg, log, paths, out_mp4)  无损合并（concat demuxer，不重编码）
    fetch_and_prepare(cfg, log, ymd)      一站式：列目录→归组→下载→合并→校验
"""
import datetime
import os
import re
import subprocess
import time

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

FILE_PORT_FALLBACK = 5143


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def cfg_darkpy(cfg):
    d = cfg.get('darkpy') or {}
    return {
        'enabled': d.get('enabled', False),
        'host': (d.get('host') or 'rec.darkpy.cn').rstrip('/'),
        'port': int(d.get('port') or 5144),
        'file_port': int(d.get('file_port') or FILE_PORT_FALLBACK),
        'room_dir': d.get('room_dir') or '',
        'max_gap_sec': int(d.get('max_gap_sec') or 900),
        'min_session_sec': int(d.get('min_session_sec') or 600),
        'timeout': int(d.get('timeout') or 30),
        'retries': int(d.get('retries') or 3),
    }


def _file_base(cfg):
    d = cfg_darkpy(cfg)
    return 'http://%s:%d' % (d['host'], d['file_port'])


# ---------------------------------------------------------------------------
# 列表
# ---------------------------------------------------------------------------
# 实测结构（2026-09-05 抓取确认）：
#   <span class='sp'>20260704-195123-522-标题.flv</span></a></div>
#   <div id = "echo_size">3.31GB</div>
#   <div id = "echo_time">2026-07-04 20:51</div>
# 注意：span.sp 没有 id 属性；echo_time 是该分段的「结束时间」（mtime），
# 与文件名里的开始时间相差约 1 小时，可用来精确计算段与段之间的间隙。
_ENTRY_RE = re.compile(
    r"<span class='sp'[^>]*>(?P<name>[^<]+?\.flv)</span>.*?"
    r"<div id\s*=\s*\"echo_size\">(?P<size>[^<]*)</div>.*?"
    r"<div id\s*=\s*\"echo_time\">(?P<time>[^<]*)</div>", re.S)
# 兜底：从下载链接里抽（页面含多组同名链接，取唯一名）
_HREF_RE = re.compile(r"file%2F[^\"']*?%2F([^\"']+?\.flv)")

_MTIME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")


def _size_gb(s):
    m = re.match(r"([\d.]+)\s*(GB|MB|KB|B)?", (s or '').strip(), re.I)
    if not m:
        return 0.0
    v = float(m.group(1))
    u = (m.group(2) or 'B').upper()
    if u == 'GB':
        return v
    if u == 'MB':
        return v / 1024.0
    if u == 'KB':
        return v / 1024.0 / 1024.0
    return v / (1024.0 ** 3)


def _fetch_index_impl(cfg, log):
    """拉取主播目录下全部 FLV（真实实现，带缓存的是 fetch_index）。

    返回 [{'name','ymd','hms','title','size','size_gb','mtime'}]，按文件名升序。
    优先用渲染条目（含大小/时间），不足时回退到下载链接抽取（只有文件名）。
    """
    d = cfg_darkpy(cfg)
    if not d['room_dir']:
        log('[darkpy] 未配置 darkpy.room_dir，无法列目录', 'WARN')
        return []
    if requests is None:
        log('[darkpy] 缺少 requests 库', 'ERROR')
        return []

    url = '%s/file.php' % _file_base(cfg)
    params = {
        'dir': d['room_dir'] + '/',
        'dir_s': '1',
        'dir_f': './file/%s' % d['room_dir'],
        'dir_file': '{"1":".\\/file\\/"}',
        'user': '',
    }
    last = None
    for i in range(d['retries']):
        try:
            r = requests.get(url, params=params, timeout=d['timeout'])
            r.raise_for_status()
            html = r.text
            break
        except Exception as e:
            last = e
            if i < d['retries'] - 1:
                time.sleep(3 * (i + 1))
    else:
        log.error('[darkpy] 列目录失败：%s' % last)
        return []

    out = {}
    for m in _ENTRY_RE.finditer(html):
        name = m.group('name')
        out[name] = {
            'name': name,
            'ymd': '',
            'size': m.group('size').strip(),
            'size_gb': _size_gb(m.group('size')),
            'mtime': m.group('time').strip(),
        }
    if len(out) < max(1, html.count('.flv') // 8):
        # 渲染条目明显偏少（正则没吃全）→ 回退到下载链接抽取（只有文件名）
        from urllib.parse import unquote
        for enc in _HREF_RE.findall(html):
            n = unquote(enc)
            if n not in out:
                out[n] = {'name': n, 'ymd': '', 'size': '', 'size_gb': 0.0, 'mtime': ''}
        if out:
            log('[darkpy] 用兜底方式补齐解析，当前 %d 个文件' % len(out), 'WARN')

    for name, rec in out.items():
        mm = re.match(r'^(?P<ymd>\d{8})-(?P<hms>\d{6})-\d+-(?P<title>.+)\.flv$', name)
        if mm:
            rec['ymd'] = mm.group('ymd')
            rec['hms'] = mm.group('hms')
            rec['title'] = mm.group('title')
            try:
                rec['start'] = datetime.datetime.strptime(
                    mm.group('ymd') + mm.group('hms'), '%Y%m%d%H%M%S')
            except ValueError:
                rec['start'] = None
        else:
            rec.setdefault('hms', '')
            rec.setdefault('title', name)
            rec['start'] = None
        # 分段结束时间（站点给的 mtime），用于精确计算段间间隙
        mt = _MTIME_RE.search(rec.get('mtime') or '')
        rec['end'] = None
        if mt:
            try:
                rec['end'] = datetime.datetime(
                    int(mt.group(1)), int(mt.group(2)), int(mt.group(3)),
                    int(mt.group(4)), int(mt.group(5)), int(mt.group(6) or 0))
            except ValueError:
                rec['end'] = None
        rec['end'] = rec['end'] or rec.get('start')

    files = sorted(out.values(), key=lambda x: x['name'])
    log('[darkpy] 列目录成功：%d 个 FLV' % len(files))
    return files


def files_for_date(files, ymd):
    """取某天（YYYYMMDD 或 YYYY-MM-DD）的 FLV，按文件名升序。"""
    ymd = ymd.replace('-', '')
    return [f for f in files if f.get('ymd') == ymd]


# ---------------------------------------------------------------------------
# 场次归组：一场直播可能被切成多段（按「段结束时间」算间隙，支持跨午夜）
# ---------------------------------------------------------------------------
def group_sessions(files, max_gap_sec=900, log=None):
    """对一批文件（同一天或全局）按时间连续性归组为「场次」。返回场次列表。

    间隙算法：后一段的 start - 前一段的 end（end 优先取站点给的 mtime，
    即该分段实际结束时刻；取不到才退回用 start）。这样「1 小时一段、首尾相接」
    的标准录播（如 195123→205121）间隙约 0 秒，会被正确并成一场；
    而真正的两场（下午场 / 晚上场）间隔数小时，会被拆开。

    跨午夜的场次（23:xx 开始、00:xx 还有一段）不在同一天文件名下，
    只要把「全量文件」喂进来做全局归组（见 group_all_sessions），
    仍能正确并成一场，再以第一段的日期作为场次日期。
    """
    if not files:
        return []
    usable = [f for f in files if f.get('start')]
    if len(usable) != len(files):
        if log:
            log('[darkpy] 有 %d 个文件解析不出时间，按独立场次处理'
                % (len(files) - len(usable)), 'WARN')
    ordered = sorted(files, key=lambda x: (x.get('start') or datetime.datetime.min,
                                           x['name']))
    sessions, cur = [], [ordered[0]]
    for nxt in ordered[1:]:
        prev = cur[-1]
        prev_end = prev.get('end') or prev.get('start')
        if prev_end and nxt.get('start'):
            gap = (nxt['start'] - prev_end).total_seconds()
            # 允许小幅负间隙（时间戳有秒级误差/录制重叠）
            if -120 <= gap <= max_gap_sec:
                cur.append(nxt)
                continue
        sessions.append(cur)
        cur = [nxt]
    sessions.append(cur)
    return sessions


def group_all_sessions(files, max_gap_sec=900, log=None):
    """全局归组（跨日期）。返回与 group_sessions 相同结构的场次列表。"""
    return group_sessions(files, max_gap_sec, log)


def sessions_for_date(files, ymd, max_gap_sec=900, log=None):
    """取某天的「场次」列表（场次以第一段的文件名日期为准）。

    例如 20260901-000110 是 8/31 深夜场的尾巴，全局归组后归属 2026-08-31，
    不会误当成 9/1 的一场。
    """
    ymd = ymd.replace('-', '')
    sessions = group_sessions(files, max_gap_sec, log)
    return [s for s in sessions
            if s and s[0].get('ymd') == ymd]


# ---------------------------------------------------------------------------
# 下载（断点续传）
# ---------------------------------------------------------------------------
def file_url(cfg, name):
    d = cfg_darkpy(cfg)
    from urllib.parse import quote
    return '%s/file/%s/%s' % (_file_base(cfg), quote(d['room_dir']), quote(name))


def download_one(cfg, log, name, dest, expect_bytes=0):
    """断点续传下载单个 FLV。返回 dest 或 None。

    已存在且大小一致（或 >= 期望值 98%）则跳过，满足幂等要求。
    """
    if requests is None:
        log.error('[darkpy] 缺少 requests 库')
        return None
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + '.part'

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        have = os.path.getsize(dest)
        if expect_bytes and have >= expect_bytes * 0.98:
            log('[darkpy] 已存在，跳过下载：%s（%.2f GB）' % (name, have / 1024 ** 3))
            return dest
        if not expect_bytes:
            log('[darkpy] 已存在，跳过下载：%s' % name)
            return dest

    url = file_url(cfg, name)
    resume = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {'Range': 'bytes=%d-' % resume} if resume else {}
    mode = 'ab' if resume else 'wb'
    if resume:
        log('[darkpy] 续传 %s（已有 %.2f GB）' % (name, resume / 1024 ** 3))
    else:
        log('[darkpy] 下载 %s' % name)

    try:
        r = requests.get(url, headers=headers, stream=True, timeout=60)
        if r.status_code not in (200, 206):
            log.error('[darkpy] 下载失败 HTTP %s：%s' % (r.status_code, name))
            return None
        total = int(r.headers.get('Content-Length') or 0)
        if expect_bytes and total and abs(total + resume - expect_bytes) > expect_bytes * 0.05 \
                and resume == 0:
            # 仅提示，不阻断（站点大小是四舍五入的显示值）
            log('[darkpy] 大小与列表显示值有差异（列表 %.2f GB / 实际 %.2f GB）'
                % (expect_bytes / 1024 ** 3, total / 1024 ** 3), 'WARN')
        t0 = time.time()
        got = resume
        with open(part, mode) as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
        if expect_bytes and got < expect_bytes * 0.9:
            log.error('[darkpy] 下载不完整 %.2f/%.2f GB：%s' % (got / 1024 ** 3,
                                                                expect_bytes / 1024 ** 3, name))
            return None
        os.replace(part, dest)
        log('[darkpy] 下载完成 %s（%.2f GB，%.0fs）' % (name, got / 1024 ** 3, time.time() - t0))
        return dest
    except Exception as e:
        log.error('[darkpy] 下载异常 %s：%s' % (name, e))
        return None


# ---------------------------------------------------------------------------
# 合并（优先无损封装，不重编码）
# ---------------------------------------------------------------------------
def _probe(cfg, path):
    ffprobe = cfg['paths']['ffprobe']
    try:
        r = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', path],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=180)
        return float((r.stdout or '0').strip() or 0)
    except Exception:
        return 0.0


def _stream_info(cfg, path):
    """取视频/音频编码参数，用于校验各分段是否一致。"""
    ffprobe = cfg['paths']['ffprobe']
    try:
        r = subprocess.run([ffprobe, '-v', 'error', '-show_entries',
                            'stream=codec_name,width,height,r_frame_rate',
                            '-of', 'csv=p=0', path],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=180)
        return (r.stdout or '').strip().replace('\n', ' | ')
    except Exception:
        return ''


def merge_flvs(cfg, log, paths, out_mp4):
    """合并多个 FLV 为单个 MP4。优先 concat demuxer（-c copy，不重编码）。

    返回 (out_mp4, 是否无损)。失败返回 (None, False)。
    """
    if not paths:
        return None, False
    ffmpeg, ffprobe = cfg['paths']['ffmpeg'], cfg['paths']['ffprobe']
    os.makedirs(os.path.dirname(out_mp4), exist_ok=True)

    if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1024 * 1024 \
            and _probe(cfg, out_mp4) > 0:
        log('[darkpy] 合并文件已存在，跳过：%s' % os.path.basename(out_mp4))
        return out_mp4, True

    # 合并前校验：段数 / 大小 / 参数一致性
    log('[darkpy] 合并前校验：%d 段' % len(paths))
    for p in paths:
        sz = os.path.getsize(p) / 1024 ** 3 if os.path.exists(p) else 0
        log('    %.2f GB  %s' % (sz, os.path.basename(p)))
    infos = {_stream_info(cfg, p) for p in paths}
    if len(infos) > 1:
        log('[darkpy] 各分段编码参数不完全一致，仍尝试无损合并：%s'
            % ' || '.join(list(infos)[:3]), 'WARN')

    if len(paths) == 1:
        # 单段：直接封装为 mp4（不重编码）
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', paths[0], '-c', 'copy', '-movflags', '+faststart',
                        out_mp4], check=False)
        if os.path.exists(out_mp4) and _probe(cfg, out_mp4) > 0:
            log('[darkpy] 单段直接封装完成：%s' % os.path.basename(out_mp4))
            return out_mp4, True
        return None, False

    list_path = out_mp4 + '.list.txt'
    try:
        with open(list_path, 'w', encoding='utf-8') as f:
            for p in paths:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'concat', '-safe', '0', '-i', list_path,
                        '-c', 'copy', '-movflags', '+faststart', out_mp4], check=False)
        if os.path.exists(out_mp4) and _probe(cfg, out_mp4) > 0:
            log('[darkpy] 无损合并完成（concat copy）：%s（%s）'
                % (os.path.basename(out_mp4), _hms(_probe(cfg, out_mp4))))
            return out_mp4, True
        log('[darkpy] concat 无损合并未产出有效文件，回退重编码合并', 'WARN')
    except Exception as e:
        log('[darkpy] concat 合并异常（%s），回退重编码' % e, 'WARN')
    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass

    # 回退：filter concat 重编码（保证一定出片，代价是慢 + 有损）
    try:
        ins = []
        for p in paths:
            ins += ['-i', p]
        n = len(paths)
        fc = '%s%sconcat=n=%d:v=1:a=1[v][a]' % (
            ''.join('[%d:v]' % i for i in range(n)),
            ''.join('[%d:a]' % i for i in range(n)), n)
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error'] + ins +
                       ['-filter_complex', fc, '-map', '[v]', '-map', '[a]',
                        '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                        '-c:a', 'aac', '-b:a', '192k',
                        '-movflags', '+faststart', out_mp4], check=False)
        if os.path.exists(out_mp4) and _probe(cfg, out_mp4) > 0:
            log('[darkpy] 重编码合并完成（有损回退）：%s' % os.path.basename(out_mp4))
            return out_mp4, False
    except Exception as e:
        log.error('[darkpy] 重编码合并也失败：%s' % e)
    return None, False


def _hms(sec):
    sec = int(sec or 0)
    return '%02d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


# ---------------------------------------------------------------------------
# 完整性检查
# ---------------------------------------------------------------------------
def check_session(cfg, log, session):
    """检查一场的各分段是否「够完整」。返回 (ok, 说明)。

    判定：① 每段都能取到时长；② 总时长 >= min_session_sec；
          ③ 无时长为 0 的段。用于识别「明显缺失/截断」的分段。
    """
    d = cfg_darkpy(cfg)
    min_sec = d['min_session_sec']
    total, bad = 0.0, []
    for f in session:
        p = f.get('_local')
        if not p or not os.path.exists(p):
            bad.append('%s（未下载）' % f['name'])
            continue
        dur = _probe(cfg, p)
        if dur <= 0:
            bad.append('%s（无法读取时长）' % f['name'])
        total += dur
    if bad:
        return False, '分段不可读：' + '、'.join(bad[:3])
    if total < min_sec:
        return False, '总时长 %.0f 分钟 < 阈值 %d 分钟，疑似不完整' % (total / 60, min_sec // 60)
    return True, '共 %d 段，合计 %s' % (len(session), _hms(total))


# ---------------------------------------------------------------------------
# 一站式：列目录 → 归组 → 下载 → 合并 → 校验
# ---------------------------------------------------------------------------
_INDEX_CACHE = {'t': 0.0, 'files': None}


def fetch_index(cfg, log, force=False, cache_ttl=600):
    """拉取主播目录下全部 FLV（带 10 分钟缓存；force=True 强制刷新）。"""
    if not force and _INDEX_CACHE['files'] is not None \
            and time.time() - _INDEX_CACHE['t'] < cache_ttl:
        return _INDEX_CACHE['files']
    files = _fetch_index_impl(cfg, log)
    if files:
        _INDEX_CACHE['t'] = time.time()
        _INDEX_CACHE['files'] = files
    return files


def fetch_and_prepare(cfg, log, ymd, work_dir, session_index=0):
    """为指定日期取回一场完整录播（MP4）。返回 dict 或 None。

    {'src_video','flv_paths','n_segments','duration','lossless','note',
     'complete','title','hms','first_name','sessions_total'}

    同一天多场（session_index）：0 = 当天第一场，1 = 第二场，以此类推。
    归组基于**全量全局文件**（跨午夜尾巴归属到开播那天，不会误判成新场次）。

    幂等/断点：
    · 合并产物已存在且可读 → 直接复用，不再下载 FLV（省流量）；
    · 已下载的 FLV 不重下（.part Range 续传）；
    · 合并成功后按 keep_flv_after_merge 决定是否删除 FLV（默认删，省盘）。
    """
    d = cfg_darkpy(cfg)
    if not d['enabled']:
        return None
    files = fetch_index(cfg, log)
    day = files_for_date(files, ymd)
    if not day:
        log('[darkpy] %s 在该站没有录播文件' % ymd)
        return None
    sessions = sessions_for_date(files, ymd, d['max_gap_sec'], log)
    log('[darkpy] %s 共 %d 个 FLV（全局归组后当天 %d 场）' % (ymd, len(day), len(sessions)))
    if session_index >= len(sessions):
        log('[darkpy] %s 只有 %d 场，请求第 %d 场越界' % (ymd, len(sessions), session_index + 1))
        return None
    session = sessions[session_index]
    first = session[0]
    base = re.sub(r'\.flv$', '', first['name'])
    os.makedirs(work_dir, exist_ok=True)
    out_mp4 = os.path.join(work_dir, base + '.mp4')

    def _mk_result(merged, flv_paths, lossless, note, ok):
        return {'src_video': merged, 'flv_paths': flv_paths,
                'n_segments': len(flv_paths), 'duration': _probe(cfg, merged),
                'lossless': lossless, 'note': note, 'complete': ok,
                'title': first.get('title') or base, 'hms': first.get('hms') or '',
                'first_name': first['name'], 'sessions_total': len(sessions)}

    # 合并产物已存在 → 直接复用（不重下 FLV）；顺手清理残留 FLV
    if os.path.exists(out_mp4) and os.path.getsize(out_mp4) > 1024 * 1024 \
            and _probe(cfg, out_mp4) > 0:
        log('[darkpy] 合并产物已存在，复用：%s' % os.path.basename(out_mp4))
        return _mk_result(out_mp4, [], True, '复用已有合并产物', True)

    flv_paths = []
    for f in session:
        dest = os.path.join(work_dir, f['name'])
        got = download_one(cfg, log, f['name'], dest,
                           expect_bytes=int(f.get('size_gb', 0) * 1024 ** 3))
        if not got:
            log.error('[darkpy] %s 下载失败，本场放弃' % f['name'])
            return None
        f['_local'] = got
        flv_paths.append(got)

    ok, note = check_session(cfg, log, session)
    if not ok:
        log('[darkpy] %s 完整性检查未通过：%s' % (ymd, note), 'WARN')

    merged, lossless = merge_flvs(cfg, log, flv_paths, out_mp4)
    if not merged:
        return None

    # 合并成功 → FLV 已完成使命，按配置删除（默认删，断点靠 out_mp4）
    if not d.get('keep_flv_after_merge', False):
        try:
            from common import force_unlink
        except Exception:
            force_unlink = os.remove
        for p in flv_paths:
            try:
                if os.path.exists(p):
                    force_unlink(p)
            except Exception as e:
                log('[darkpy] FLV 清理失败（不影响成品）：%s' % e, 'WARN')
    return _mk_result(merged, flv_paths, lossless, note, ok)
