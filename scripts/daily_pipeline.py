#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_pipeline.py —— 任意 B站主播直播录播 每日主编排（通用 skill，配置驱动）

默认由自动化每天触发一次，跑完整条流水线（具体触发时间由调用方/自动化决定）：

  1. 监测本地录播根目录（config.local.root）下的新录播文件
     （通常是 flv 或 mp4，直接放在根目录，不在日期子目录里）。
       · 已处理过（state 占位 / TXT 命中）的跳过；
       · 正在录制中的临时文件（大小不稳定 / 修改时间太新）跳过，避免截半。
  2. 若有新本地文件 → 直接走本地总结流程（FunASR 转写 → 热词 → Gemini 切块总结 → 写 Word）。
  3. 若没有新本地文件 → 去 B站合集（config.series：mid + series_id）
     搜索最近未处理的录播，下载并合并多 P 为单个视频，再走总结流程。
  4. 总结完成之后 → 把视频（MP4/FLV）转一份中质量音质的 MP3
     （192k / 44.1k，体积不大，音质满足文字识别即可，用户另有他用）。
  5. 以上全部结束后 → 把视频压成体积约 5GB 的 HEVC(H.265) MP4 文件。
     flv/mkv 等统一输出为 mp4，并放进 `根目录/YYYY-MM-DD/` 日期子目录。

最终每场成品只有三样：① AI 总结的 Word 文档  ② 中质量 MP3  ③ 压缩后的 HEVC MP4。

用法:
  python daily_pipeline.py              # 正式运行（监测→本地或B站→MP3→压缩）
  python daily_pipeline.py --dry-run    # 只打印会处理哪些、跳过哪些，不触碰任何文件
  python daily_pipeline.py --date 2026-08-24   # 只处理指定直播日期（定向重跑/排障）
"""

import argparse
import datetime
import os
import re
import shutil
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import (load_config, load_state, save_state, load_cookies, Logger,
                    probe_duration, stem_for, fetch_series)
import fetch_latest
import download_merge
import hotwords
import mp3_export
import compress_video
import reorder_doc as RD
import summarize_gemini as S
import regen_gemini_v2 as R

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认本地录制文件名正则（录制器命名：录制-UID-YYYYMMDD-HHMMSS-序号-标题.扩展名）。
# 可用 config.local.name_regex 覆盖；自定义正则必须含命名组 date(YYYYMMDD)、time(HHMMSS)、title。
DEFAULT_NAME_PATTERN = r'^(?:录制-)?(?P<uid>\d+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<num>\d+)-(?P<title>.+)\.(?P<ext>mp4|flv|mkv|mov|MP4|FLV|MKV|MOV)$'
DEFAULT_NAME_RE = re.compile(DEFAULT_NAME_PATTERN)
MEDIA_EXT = ('.mp4', '.flv', '.mkv', '.mov')


def resolve_config_arg(args):
    """根据 --config / --workdir / 环境变量 / skill 同级 _workdir.json 解析 config.json 路径。"""
    if getattr(args, 'config', None):
        return args.config
    wd = getattr(args, 'workdir', None) or os.environ.get('BILI_WORKDIR')
    if wd:
        return os.path.join(wd, 'config.json')
    ptr = os.path.join(SCRIPT_DIR, '_workdir.json')
    if os.path.exists(ptr):
        try:
            d = json.load(open(ptr, encoding='utf-8'))
            cand = os.path.join(d['workdir'], 'config.json')
            if os.path.exists(cand):
                return cand
        except Exception:
            pass
    raise RuntimeError('无法确定工作目录：请传 --workdir/--config、设 BILI_WORKDIR，'
                       '或在 skill 目录放 _workdir.json')


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def session_key(live_date, chain_index):
    ymd = live_date.strftime('%Y%m%d')
    return 'LOCAL-%s' % ymd if chain_index == 0 else 'LOCAL-%s-%d' % (ymd, chain_index + 1)


def session_fname(live_date, clean_title, hour, chain_index):
    ymd = live_date.strftime('%Y%m%d')
    cn = live_date.strftime('%Y年%m月%d日')
    suffix = '' if chain_index == 0 else '（第%d场）' % (chain_index + 1)
    stem = '%s-%s %s%d点场%s' % (ymd, clean_title, cn, hour, suffix)
    return stem + '.txt'


def wait_network(log, cfg, timeout=900):
    hosts = ['api.bilibili.com', 'generativelanguage.googleapis.com']
    h = cfg.get('summarize', {}).get('api_host')
    if h:
        try:
            from urllib.parse import urlparse
            hh = urlparse(h).hostname
            if hh:
                hosts.append(hh)
        except Exception:
            pass
    deadline = time.time() + timeout
    delay = 10
    while time.time() < deadline:
        ok = True
        for hst in hosts:
            try:
                socket.gethostbyname(hst)
            except Exception:
                ok = False
                break
        if ok:
            return True
        log('网络尚未就绪，%ds 后重试 ...' % delay, 'WARN')
        time.sleep(delay)
        delay = min(delay * 2, 120)
    log.error('等待网络超时')
    return False


def is_file_stable(path, log, delay=8, min_age_sec=120):
    """判断文件是否「已写完、不在录制中」。

    · 太新（mtime 距现在 < min_age_sec）→ 大概率还在写，视为不稳定；
    · 两次 stat 之间大小不变 → 稳定（录制中文件大小会持续增长）。
    两者都满足才返回 True。
    """
    try:
        st1 = os.stat(path)
    except Exception:
        return False
    age = time.time() - st1.st_mtime
    if age < min_age_sec:
        return False
    sz1 = st1.st_size
    time.sleep(delay)
    try:
        st2 = os.stat(path)
    except Exception:
        return False
    return st2.st_size == sz1


# ---------------------------------------------------------------------------
# 步骤 1：监测本地根目录新录播
# ---------------------------------------------------------------------------
def scan_root_sessions(cfg, log):
    """扫描本地录播根目录（直接是 flv/mp4 文件，不在日期子目录），
    按直播实际日期分组、相邻连续段合并为 chain，返回待处理 session 列表。

    同时过滤：已处理（state 占位 / TXT 命中）、正在录制中（不稳定）的文件。
    """
    sessions = []
    local_root = (cfg.get('local') or {}).get('root') or ''
    if not local_root or not os.path.isdir(local_root):
        log('[monitor] 本地根目录未配置或不存在（config.local.root=%s），跳过本地监测' % local_root, 'WARN')
        return sessions
    mc = cfg.get('local_merge') or {}
    max_gap = int(mc.get('max_gap_sec', 600))
    ffprobe = cfg['paths']['ffprobe']
    name_re = re.compile((cfg.get('local') or {}).get('name_regex') or DEFAULT_NAME_PATTERN)

    files = []
    for fn in sorted(os.listdir(local_root)):
        fp = os.path.join(local_root, fn)
        if not os.path.isfile(fp):
            continue
        if not fn.lower().endswith(MEDIA_EXT):
            continue
        mm = name_re.match(fn)
        if not mm:
            log('[monitor] 根目录文件 %s 不匹配命名规则，跳过' % fn, 'WARN')
            continue
        ymd = mm.group('date')
        hhmmss = mm.group('time')
        title = mm.group('title').strip()
        y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
        hh, mi, ss = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
        live_date = datetime.date(y, mo, d)
        start = datetime.datetime(y, mo, d, hh, mi, ss)
        files.append({'path': fp, 'start': start, 'live_date': live_date,
                      'title': title, 'hour': hh, 'fn': fn})
    if not files:
        return sessions

    by_date = {}
    for f in files:
        by_date.setdefault(f['live_date'], []).append(f)

    excl = set(mc.get('exclude_dates') or [])
    for ld, flist in sorted(by_date.items()):
        if ld.isoformat() in excl:
            chains = [[f] for f in flist]
        else:
            dur = {f['path']: probe_duration(ffprobe, f['path']) for f in flist}
            flist.sort(key=lambda x: x['start'])
            if any(dur.get(f['path'], 0) <= 0 for f in flist):
                log('[merge] %s 有文件取不到时长，按独立场次（不合并）' % ld.isoformat(), 'WARN')
                chains = [[f] for f in flist]
            else:
                chains, cur = [], [flist[0]]
                for nxt in flist[1:]:
                    prev = cur[-1]
                    prev_end = prev['start'] + datetime.timedelta(seconds=dur[prev['path']])
                    gap = (nxt['start'] - prev_end).total_seconds()
                    if 0 <= gap <= max_gap:
                        cur.append(nxt)
                    else:
                        chains.append(cur)
                        cur = [nxt]
                chains.append(cur)
        for idx, chain in enumerate(chains):
            first = chain[0]
            sessions.append({'live_date': ld, 'clean_title': first['title'], 'hour': first['hour'],
                             'member_files': [f['path'] for f in chain], 'is_merged': len(chain) > 1,
                             'chain_index': idx, 'merged_src': None})
    return sessions


def already_done_local(cfg, st, session):
    key = session_key(session['live_date'], session['chain_index'])
    if key in st.get('processed', {}):
        return True, '%s 占位已存在' % key
    fname = session_fname(session['live_date'], session['clean_title'],
                          session['hour'], session['chain_index'])
    txt_dir = cfg['_dirs'].get('txt') or st.get('_dirs', {}).get('txt')
    if txt_dir and os.path.isdir(txt_dir) and os.path.exists(os.path.join(txt_dir, fname)):
        return True, '本场 TXT 已存在'
    return False, ''


def prepare_session(cfg, log, session):
    """为 session 准备好单一视频源（merged_src）。单文件直接用；多段连续则合并。"""
    if not session['is_merged']:
        session['merged_src'] = session['member_files'][0]
        return session['merged_src']
    merged_dir = os.path.join(cfg['_dirs']['root'], 'merged')
    os.makedirs(merged_dir, exist_ok=True)
    first_base = re.sub(r'\.\w+$', '', os.path.basename(session['member_files'][0]))
    out_path = os.path.join(merged_dir, first_base + '_merged.mp4')
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024 * 1024:
        log('[merge] 合并视频已存在，跳过：%s' % os.path.basename(out_path))
        session['merged_src'] = out_path
        return out_path
    log('[merge] 合并 %d 段连续录播为单一时间线：%s'
        % (len(session['member_files']),
           ', '.join(os.path.basename(p) for p in session['member_files'])))
    t0 = time.time()
    res = merge_videos(cfg, log, session, out_path)
    if not res:
        return None
    log('[merge] 合并完成（%.0fs）：%s' % (time.time() - t0, os.path.basename(out_path)))
    session['merged_src'] = res
    return res


def merge_videos(cfg, log, session, out_path):
    """多段连续录播合并为单一视频（优先 concat demuxer 不重编码，失败回退重编码）。"""
    ffmpeg = cfg['paths']['ffmpeg']
    ffprobe = cfg['paths']['ffprobe']
    list_path = out_path + '.list.txt'
    try:
        with open(list_path, 'w', encoding='utf-8') as f:
            for p in session['member_files']:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'concat', '-safe', '0', '-i', list_path,
                        '-c', 'copy', out_path], check=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0 \
                and (probe_duration(ffprobe, out_path) or 0) > 0:
            return out_path
    except Exception as e:
        log('[merge] concat demuxer 失败（%s），尝试重编码合并' % e, 'WARN')
    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass
    try:
        n = len(session['member_files'])
        ins = []
        for p in session['member_files']:
            ins += ['-i', p]
        vmap = ''.join('[%d:v]' % i for i in range(n))
        amap = ''.join('[%d:a]' % i for i in range(n))
        fc = "%s%sconcat=n=%d:v=1:a=1[v][a]" % (vmap, amap, n)
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error'] + ins +
                       ['-filter_complex', fc, '-map', '[v]', '-map', '[a]',
                        '-c:v', 'libx264', '-preset', 'veryfast',
                        '-c:a', 'aac', '-b:a', '192k', out_path], check=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0 \
                and (probe_duration(ffprobe, out_path) or 0) > 0:
            return out_path
    except Exception as e2:
        log.error('[merge] 重编码合并也失败：%s' % e2)
    return None


# ---------------------------------------------------------------------------
# 核心：对「一个视频源」跑完 转写→总结→MP3→压缩
# ---------------------------------------------------------------------------
def extract_16k_audio(cfg, log, src, dst):
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return
    ffmpeg = cfg['paths']['ffmpeg']
    log('抽取 16k 转写用音频：%s' % os.path.basename(src))
    t0 = time.time()
    subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                   '-i', src, '-vn', '-ac', '1', '-ar', '16000', dst], check=True)
    log('  音频抽取完成（%.0fs）' % (time.time() - t0))


def process_video_source(cfg, st, src_video, date_iso, fname, src_kind, log, progress):
    """对单个视频源跑完整总结流水线，返回 True/False。

    src_kind: 'local' 或 'bilibili'（影响压缩后是否删除原文件、占位写法）。
    """
    live_date = datetime.date.fromisoformat(date_iso)
    stem = fname[:-4]
    mp3_16k = os.path.join(cfg['_dirs']['mp3'], stem + '.mp3')
    txt_path = os.path.join(cfg['_dirs']['txt'], fname)
    srt_path = os.path.join(cfg['_dirs']['srt'], fname.replace('.txt', '.srt'))

    # 1) 抽取 16k 音频 + FunASR 转写（带时间轴）
    extract_16k_audio(cfg, log, src_video, mp3_16k)
    if not (os.path.exists(txt_path) and os.path.getsize(txt_path) > 200 and os.path.exists(srt_path)):
        import transcribe as T
        log('加载 FunASR 模型并转写 ...')
        model = T.load_model(cfg, log)
        T.transcribe_one(cfg, mp3_16k, model, log)
    else:
        log('转写结果已存在，跳过')

    # 2) 热词纠正（转写后、送 AI 前）
    if cfg.get('hotwords', {}).get('enabled', True):
        if os.path.exists(txt_path):
            hotwords.apply_to_file(txt_path, cfg, log)
        if os.path.exists(srt_path):
            hotwords.apply_to_file(srt_path, cfg, log)

    # 3) Gemini 逐 30 分钟块总结 → 写 Word（含章节元数据 + 话题分隔线）
    sys_prompt = R.load_prompt(cfg)
    font = cfg['output'].get('font', '思源宋体CN Light')
    rot = R.GeminiRotator(R.get_api_key(cfg), cfg['summarize'].get('proxy') or R.PROXY,
                          R.MODELS, rpm=5, window=60, max_per_model=20, log=log)
    ok = R.process_session(rot, sys_prompt, cfg, fname, date_iso, font, log, progress,
                           src_path=src_video)
    if not ok:
        log.error('%s 总结未完全成功（可能配额耗尽），未写完成标记，可重跑' % stem)
        return False

    # 4) 总结后导出中质量 MP3（192k/44.1k，供文字识别/其他用途）
    mp3_export.export_for_local(cfg, log, src_video)

    # 5) 压缩成 ~5GB HEVC MP4（按 config.compress；默认输出到 根/YYYY-MM-DD/ 日期子目录）
    cc = cfg.get('compress') or {}
    replace = (src_kind == 'local') and cc.get('replace_original', False)
    local_root = (cfg.get('local') or {}).get('root') or ''
    compress_video.compress_video(cfg, log, src_video, replace_original=replace,
                                  archive_root=local_root or None)

    # 清理 16k 临时音频（与中质量归档 MP3 无关）
    if cfg['cleanup'].get('delete_mp3_after_success', True):
        try:
            os.remove(mp3_16k)
        except Exception:
            pass

    # B站源是临时下载文件，压缩成功后即可删除（压缩版已是交付物）
    if src_kind == 'bilibili':
        try:
            if os.path.exists(src_video):
                os.remove(src_video)
                log('[bilibili] 已删除临时下载视频：%s' % os.path.basename(src_video))
        except Exception as e:
            log('[bilibili] 删除临时下载视频失败（不影响成品）：%s' % e)

    try:
        RD.reorder()
    except Exception as e:
        log('重排 Word 失败（不影响总结）：%s' % e)
    return True


# ---------------------------------------------------------------------------
# 步骤 3：B站兜底
# ---------------------------------------------------------------------------
def bilibili_fallback(cfg, st, log, cookies, progress):
    """本地没有新录播时，去 B站合集抓最近未处理的录播，下载合并视频后走总结流程。"""
    s = cfg['series']
    horizon = fetch_latest.scan_horizon(cfg, st)
    archives = fetch_series(s['mid'], s['series_id'], cookies, stop_before_pubdate=horizon)
    log('B站兜底：扫描到 %d 期，最新：%s' % (len(archives),
        (archives[0]['title'][:50] if archives else '无')))
    picked = fetch_latest.pick_videos(cfg, st, archives, log)
    # 已处理过的日期不再从 B站抓（本地/此前已处理）
    done_dates = set(st.get('done_dates', []))
    picked = [a for a in picked if a.get('_live_date') is None
              or a['_live_date'].isoformat() not in done_dates]
    if not picked:
        log('B站兜底：无新录播，收工。')
        return

    log('B站兜底：本次待处理 %d 期' % len(picked))
    for a in picked:
        # fetch_series 返回的原始 dict 只有 _live_date（datetime.date），
        # 没有字符串 date 键；补上供后续 log / process_video_source / mark_bilibili_done 使用
        a['date'] = a['_live_date'].isoformat()
        bvid = a['bvid']
        log('=' * 68)
        log('B站兜底处理 %s  %s  %s' % (bvid, a['date'], a['title'][:46]))
        log('=' * 68)
        src = download_merge.download_video_one(cfg, a, log)
        if not src:
            log.error('%s 视频下载/合并失败，跳过' % bvid)
            continue
        stem = stem_for(a.get('pubdate', 0), a['title'])
        fname = stem + '.txt'
        ok = process_video_source(cfg, st, src, a['date'], fname, 'bilibili', log, progress)
        if ok:
            mark_bilibili_done(st, cfg, a, fname)
        else:
            log.error('%s 处理失败（保留临时视频，下次重试）：%s' % (bvid, src))


# ---------------------------------------------------------------------------
# 状态写入
# ---------------------------------------------------------------------------
def mark_local_done(st, cfg, session, fname):
    live_date = session['live_date']
    chain_index = session['chain_index']
    next_day = (live_date + datetime.timedelta(days=1)).isoformat()
    key = session_key(live_date, chain_index)
    st['processed'][key] = {
        'stages': ['transcribe', 'summarize', 'cleanup'],
        'title': '%s %s%d点场' % (session['clean_title'],
                                  live_date.strftime('%Y年%m月%d日'), session['hour']),
        'date': next_day,
        'live_date': live_date.isoformat(),
        'stem': fname[:-4],
        'doc': S.doc_path_for(cfg, live_date),
        'local': True,
        'merged': session['is_merged'],
        'parts': len(session['member_files']),
        'members': [os.path.basename(p) for p in session['member_files']],
        'note': 'daily_pipeline 本地优先处理（%s）' % os.path.basename(session['member_files'][0]),
    }
    _finalize_state(st, cfg, live_date)


def mark_bilibili_done(st, cfg, video, fname):
    bvid = video['bvid']
    ld = datetime.date.fromisoformat(video['date'])
    rec = st['processed'].setdefault(bvid, {'stages': []})
    rec.update({'title': video['title'], 'date': video['date'],
                'pubdate': video.get('pubdate', 0), 'stem': fname[:-4],
                'doc': S.doc_path_for(cfg, ld), 'bilibili': True,
                'note': 'daily_pipeline B站兜底处理'})
    for stage in ('transcribe', 'summarize', 'cleanup'):
        if stage not in rec['stages']:
            rec['stages'].append(stage)
    _finalize_state(st, cfg, ld)


def _finalize_state(st, cfg, live_date):
    st.setdefault('done_dates', [])
    iso = live_date.isoformat()
    if iso not in st['done_dates']:
        st['done_dates'].append(iso)
    st.setdefault('runs', []).append({
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'ok': 1, 'failed': [], 'seconds': 0,
    })
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', default=None,
                    help='只处理指定直播日期（YYYY-MM-DD）；不指定则处理全部未处理')
    ap.add_argument('--workdir', default=None,
                    help='工作目录（含 config.json）；不传则按 BILI_WORKDIR / _workdir.json / BILI_LIVE_CONFIG 推断')
    ap.add_argument('--config', default=None, help='直接指定 config.json 路径（优先级最高）')
    args = ap.parse_args()

    cfg_path = resolve_config_arg(args)
    # 单实例锁（避免并发抢 Gemini 配额）
    LOCK = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), '.daily_pipeline.lock')
    HB = os.path.join(os.path.dirname(os.path.abspath(cfg_path)), '.daily_pipeline.heartbeat')

    def pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, OSError):
            return False

    def grab():
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except OSError:
            try:
                pid = int(open(LOCK, encoding='utf-8').read().strip() or '0')
            except Exception:
                pid = 0
            if pid_alive(pid):
                return False
            try:
                fd = os.open(LOCK, os.O_WRONLY | os.O_TRUNC)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except OSError:
                return False
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True

    if not grab():
        print('[daily_pipeline] 已有实例运行，退出')
        sys.exit(0)

    def touch():
        try:
            with open(HB, 'w', encoding='utf-8') as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    try:
        cfg = load_config(cfg_path)
        log = Logger(cfg, 'daily')
        if not args.dry_run and not wait_network(log, cfg):
            sys.exit(3)
        st = load_state(cfg)
        cookies = load_cookies(cfg['paths'].get('cookies'))

        # ---- 步骤 1：监测本地根目录 ----
        sessions = scan_root_sessions(cfg, log)
        if args.date:
            sessions = [s for s in sessions if s['live_date'].isoformat() == args.date]

        # 过滤已处理 + 跳过录制中文件
        to_process, skipped = [], []
        for s in sessions:
            done, reason = already_done_local(cfg, st, s)
            if done:
                skipped.append((s, reason))
                continue
            # 录制中（不稳定）整 chain 跳过
            unstable = [p for p in s['member_files'] if not is_file_stable(p, log)]
            if unstable:
                skipped.append((s, '录制中（%s 尚未写完），跳过'
                                % ', '.join(os.path.basename(p) for p in unstable)))
                continue
            to_process.append(s)

        log('监测本地根目录：发现 %d 个待处理会话，将处理 %d，跳过 %d'
            % (len(sessions), len(to_process), len(skipped)))
        for s in skipped:
            log('  跳过 %s：%s' % (s[0]['live_date'].isoformat(), s[1]))
        for s in to_process:
            tag = ('合并 %d 段' % len(s['member_files'])) if s['is_merged'] else '单文件'
            log('  将处理 %s %s [%s] (%s)'
                % (s['live_date'].isoformat(), s['clean_title'], tag,
                   ', '.join(os.path.basename(p) for p in s['member_files'])))

        if args.dry_run:
            log('dry-run，未实际处理。')
            return

        progress = {}
        if to_process:
            # ---- 步骤 2：本地优先总结 ----
            for s in to_process:
                touch()
                src = prepare_session(cfg, log, s)
                if not src:
                    log.error('准备源文件失败，跳过：%s' % s['live_date'].isoformat())
                    continue
                fname = session_fname(s['live_date'], s['clean_title'], s['hour'], s['chain_index'])
                try:
                    ok = process_video_source(cfg, st, src, s['live_date'].isoformat(),
                                             fname, 'local', log, progress)
                    if ok:
                        mark_local_done(st, cfg, s, fname)
                except Exception as ex:
                    log.error('处理 %s 失败：%s' % (s['live_date'].isoformat(), ex))
                time.sleep(int(cfg.get('summarize', {}).get('inter_video_delay_sec', 15)))
            log('本地优先扫描完成')
        else:
            # ---- 步骤 3：B站兜底 ----
            bilibili_fallback(cfg, st, log, cookies, progress)

        log('daily_pipeline 本次完成')
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass
        try:
            os.remove(HB)
        except Exception:
            pass


if __name__ == '__main__':
    main()
