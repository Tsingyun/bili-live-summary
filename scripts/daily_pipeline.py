#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_pipeline.py —— 任意 B站主播直播录播 每日主编排（通用 skill，配置驱动）

默认由自动化每天触发一次，跑完整条流水线（具体触发时间由调用方/自动化决定）：

  1. 监测本地录播目录（config.local.root）下的新录播文件（flv / mp4）。
     扫描范围 = **根目录 + 各 `YYYY-MM-DD` 日期子目录**：录制器录完会把最终文件
     移动进日期子目录，根目录常常为空，只扫根目录会漏掉所有录播（2026-08-31 修正）。
       · 已处理过（state 占位 / TXT 命中）的跳过；
       · 正在录制中的临时文件（大小不稳定 / 修改时间太新）跳过，避免截半。
  2. 若有新本地文件 → 直接走本地总结流程（FunASR 转写 → 热词 → Gemini 切块总结 → 写 Word）。
  3. 若没有新本地文件 / 本地处理后仍有缺失日期 → 按三级备份顺序补档：
       **本地 → rec.darkpy.cn（第二备份）→ B站（第三）**（用户规则 2026-09-05）。
       缺失判定依据「实际直播日期」日历（live_calendar：rec站文件日期 ∪ 本地文件日期
       ∪ B站标题日期），与 本地文件/Word月度总结/state.done_dates 比对得出。
       · rec.darkpy.cn：FLV 分段下载 → 按段结束时间归组 → concat 无损合并（不重编码）
         → 同一总结流程（backfill.py / darkpy_rec.py）；
       · B站：仅当 rec站也没有该日时才抓（bilibili_fallback，限定目标日期）。
  4. 总结完成之后 → 把视频（MP4/FLV）转一份中质量音质的 MP3
     （192k / 44.1k，落在 mp3_export.out_dir；岁己 = C:/Users/Tsing/DeskBox/文档，与 Word 同目录）。
  5. 压缩归档（本地录播 & rec站补档）：压成约 5GB 的 HEVC(H.265) MP4，
     输出到「录播根目录/YYYY-MM-DD/」日期文件夹（out_dir_mode=date_subfolder，
     当前规则，用户 2026-09-08：不用 MP4/ 文件夹，直接用日期命名文件夹）；
     压缩成功后按 compress.replace_mode 处理原视频（delete=直接永久删除）。
     B站兜底录播默认不压缩、原样保留 MP4（config.bilibili，见步骤3旧规则）。

最终每场成品：① AI 总结的 Word 文档  ② 中质量 MP3（mp3_export.out_dir，与 Word 同目录）
             ③ 压缩后 HEVC MP4（录播根目录/YYYY-MM-DD/；B站兜底=原样保留的 MP4）。

用法:
  python daily_pipeline.py              # 正式运行（监测→本地/rec/B站→MP3→压缩）
  python daily_pipeline.py --dry-run    # 只打印会处理哪些、跳过哪些，不触碰任何文件
  python daily_pipeline.py --date 2026-09-01   # 只处理指定直播日期（定向补档/重跑）
  python backfill.py --from 2026-09-01 --to 2026-09-30 [--fill]   # 缺失检查/补档
"""

import argparse
import datetime
import json
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
                    probe_duration, stem_for, fetch_series,
                    force_unlink, force_rmtree)
import fetch_latest
import download_merge
import hotwords
import mp3_export
import compress_video
import backfill
import reorder_doc as RD
import summarize_gemini as S
import regen_gemini_v2 as R
import cleanup

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认本地录制文件名正则（录制器命名：录制-UID-YYYYMMDD-HHMMSS-序号-标题.扩展名）。
# 可用 config.local.name_regex 覆盖；自定义正则必须含命名组 date(YYYYMMDD)、time(HHMMSS)、title。
DEFAULT_NAME_PATTERN = r'^(?:录制-)?(?P<uid>\d+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<num>\d+)-(?P<title>.+)\.(?P<ext>mp4|flv|mkv|mov|MP4|FLV|MKV|MOV)$'
DEFAULT_NAME_RE = re.compile(DEFAULT_NAME_PATTERN)
MEDIA_EXT = ('.mp4', '.flv', '.mkv', '.mov')

# 录制器实际行为（2026-08-31 确认）：录制中的临时文件在根目录，录完后最终文件会被
# 移动进「根目录/YYYY-MM-DD/」日期子目录，导致根目录反而为空、本地监测扫不到任何东西。
# 故监测范围 = 根目录 + 各 YYYY-MM-DD 日期子目录（只看一层，不递归更深）。
_DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


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


def _absorb_ultrashort(cfg, log, mc, flist, chains, dur):
    """把极短片段并入相邻长场，避免独立成场出废稿（用户规则 2026-09-06）。

    判定：chain 总时长 <= ultrashort_sec 视为超短；若与另一 chain 时间重叠或间隙
    绝对值 <= absorb_window_sec，则吸收进该相邻 chain（按时间序合并文件，后续
    prepare_session 仍以 is_merged 续压）。找不到相邻长场则保留为独立场次。
    """
    us = int(mc.get('ultrashort_sec', 0) or 0)
    if us <= 0:
        return chains
    win = int(mc.get('absorb_window_sec', 600) or 600)

    def chain_dur(ch):
        return sum(dur.get(f['path'], 0) for f in ch)

    def ch_start(ch):
        return ch[0]['start']

    def ch_end(ch):
        last = ch[-1]
        return last['start'] + datetime.timedelta(seconds=dur.get(last['path'], 0))

    anchors = [ch for ch in chains if chain_dur(ch) > us]
    shorts = [ch for ch in chains if chain_dur(ch) <= us]
    if not anchors or not shorts:
        return chains

    new_chains = list(anchors)
    for sh in shorts:
        s0, e0 = ch_start(sh), ch_end(sh)
        best, best_dist = None, None
        for a in new_chains:
            a0, a1 = ch_start(a), ch_end(a)
            if (s0 < a1) and (a0 < e0):          # 时间重叠
                dist = 0
            else:
                gap = (a0 - e0).total_seconds() if a0 >= e0 else (s0 - a1).total_seconds()
                dist = abs(gap)
            if dist <= win and (best_dist is None or dist < best_dist):
                best, best_dist = a, dist
        if best is not None:
            merged = sorted(best + sh, key=lambda f: f['start'])
            i = new_chains.index(best)
            new_chains[i] = merged
            log('[merge] 超短片段(%ds)并入相邻长场：%s'
                % (int(chain_dur(sh)), os.path.basename(sh[0]['path'])))
        else:
            new_chains.append(sh)
    new_chains.sort(key=lambda ch: ch_start(ch))
    return new_chains


# ---------------------------------------------------------------------------
# 步骤 1：监测本地新录播（根目录 + 日期子目录）
# ---------------------------------------------------------------------------
def scan_root_sessions(cfg, log):
    """扫描本地录播目录，按直播实际日期分组、相邻连续段合并为 chain，返回待处理 session 列表。

    扫描范围（2026-08-31 修正）：**根目录 + 各 `YYYY-MM-DD` 日期子目录**（只看一层）。
    录制器会把录完的最终文件移动进日期子目录，根目录常常是空的，只扫根目录会漏掉所有录播。

    日期子目录里同时会有两类「不是新录播」的文件，必须排除：
      · 压缩产物 `<原名>_compressed.mp4`——注意默认正则的 title 组会吃掉 `_compressed`
        从而误匹配，必须显式过滤；
      · B站兜底归档的 MP4（命名不符合录制器规则，会被正则自然过滤掉）。

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

    # 扫描目录集合：根目录 + 一层 YYYY-MM-DD 日期子目录
    scan_dirs = [local_root]
    for fn in sorted(os.listdir(local_root)):
        sub = os.path.join(local_root, fn)
        if os.path.isdir(sub) and _DATE_DIR_RE.match(fn):
            scan_dirs.append(sub)
    if len(scan_dirs) > 1:
        log('[monitor] 扫描范围：根目录 + %d 个日期子目录' % (len(scan_dirs) - 1))

    files = []
    seen = set()
    for scan_d in scan_dirs:
        for fn in sorted(os.listdir(scan_d)):
            fp = os.path.join(scan_d, fn)
            if not os.path.isfile(fp):
                continue
            if not fn.lower().endswith(MEDIA_EXT):
                continue
            # 压缩产物不是新录播（默认正则的 title 组会吃掉 _compressed 从而误匹配）
            if re.sub(r'\.\w+$', '', fn).lower().endswith('_compressed'):
                continue
            if fn in seen:
                continue
            mm = name_re.match(fn)
            if not mm:
                log('[monitor] 文件 %s 不匹配命名规则，跳过' % fn, 'WARN')
                continue
            seen.add(fn)
            ymd = mm.group('date')
            hhmmss = mm.group('time')
            title = mm.group('title').strip()
            y, mo, dd = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
            hh, mi, ss = int(hhmmss[:2]), int(hhmmss[2:4]), int(hhmmss[4:6])
            live_date = datetime.date(y, mo, dd)
            start = datetime.datetime(y, mo, dd, hh, mi, ss)
            files.append({'path': fp, 'start': start, 'live_date': live_date,
                          'title': title, 'hour': hh, 'fn': fn})
    if not files:
        return sessions

    by_date = {}
    for f in files:
        by_date.setdefault(f['live_date'], []).append(f)

    excl = set(mc.get('exclude_dates') or [])
    for ld, flist in sorted(by_date.items()):
        flist.sort(key=lambda x: x['start'])
        dur = {f['path']: probe_duration(ffprobe, f['path']) for f in flist}
        if ld.isoformat() in excl:
            chains = [[f] for f in flist]
        elif any(dur.get(f['path'], 0) <= 0 for f in flist):
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
        # 超短片段吸收：极短且紧贴/重叠的片段并入相邻长场，避免独立成场出废稿
        chains = _absorb_ultrashort(cfg, log, mc, flist, chains, dur)
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
    if not (txt_dir and os.path.isdir(txt_dir)
            and os.path.exists(os.path.join(txt_dir, fname))):
        return False, ''

    # TXT 存在还不够：必须 Word 里已有对应章节才算真完成。
    # 否则「转写成功、但总结中途失败（崩溃 / 被杀 / 写锁抢占失败）」会留下 TXT 却没有章节，
    # 旧逻辑据此判定已处理 → 该场被永久跳过（2026-08-31 的 8/29 场即踩此坑）。
    try:
        heading = fname.split('-', 1)[1].rsplit('.txt', 1)[0] if '-' in fname else fname[:-4]
        doc_path = S.doc_path_for(cfg, session['live_date'])
        if not os.path.exists(doc_path):
            return False, ''
        from docx import Document as _Docx
        if S.has_section(_Docx(doc_path), heading):
            return True, 'Word 已存在该章节'
        return False, ''
    except Exception:
        # 校验出问题时按「未完成」更安全：重跑时 process_session 内部还有
        # has_section 二次把关，不会重复写章节。
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
            force_unlink(list_path)
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
# B站录播 MP4 归档（用户规则 2026-08-29：本地没录播而B站有时，跑完流程要留下 MP4）
# ---------------------------------------------------------------------------
def _archive_bilibili_mp4(cfg, log, src_video, date_iso):
    """把 B站下载的录播 MP4 归档留存（不做压缩，原样保留）。

    用户规则（2026-08-29）：本地磁盘没有这一天录播、但 B站有时，跑完流程之后
    要留下 B站录播的 MP4 文件；这类 MP4 不需要再压缩。

    下载产物默认落在 workdir/tmp/bilibili/（临时目录，不适合长期留存），
    故按直播日期归档到「录播根目录/YYYY-MM-DD/」——与本地压缩版同一个视频库。
    该目录不在根目录监测的扫描范围内（只扫根级文件），不会被误判成新录播。

    返回归档后的路径；任何失败都保留原文件、绝不删除。
    """
    try:
        local_root = (cfg.get('local') or {}).get('root') or ''
        if local_root:
            out_dir = os.path.join(local_root, date_iso)
        else:
            out_dir = os.path.join(cfg['_dirs']['root'], 'bilibili', date_iso)
        os.makedirs(out_dir, exist_ok=True)
        dst = os.path.join(out_dir, os.path.basename(src_video))
        if os.path.abspath(dst) == os.path.abspath(src_video):
            return src_video
        if os.path.exists(dst) and os.path.getsize(dst) >= os.path.getsize(src_video):
            log('[bilibili] 归档 MP4 已存在：%s' % dst)
            try:
                force_unlink(src_video)
            except Exception:
                pass
            return dst
        shutil.move(src_video, dst)
        log('[bilibili] 已保留 B站录播 MP4 → %s' % dst)
        return dst
    except Exception as e:
        log.error('[bilibili] 归档 MP4 失败（原文件保留，不删除）：%s' % e)
        return src_video


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

    # 转写完成 → 16k 单声道音频已完成使命（TXT/SRT 已落盘），立即释放 ~0.5GB。
    # 关键改动（2026-09-01）：不再等「总结成功」才删。原先写在成功路径上，
    # 一旦 Gemini 配额耗尽 return False，每场就永久留下 0.5GB 的死文件。
    cleanup.after_session(cfg, log, stem=stem, src_kind=src_kind)

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
        # 失败也要清干净：ASR 切块、B站下载残留不因失败而赖在磁盘上
        cleanup.after_session(cfg, log, stem=stem, src_kind=src_kind)
        return False

    # 4) 总结后导出中质量 MP3（192k/44.1k，供文字识别/其他用途）
    mp3_export.export_for_local(cfg, log, src_video)

    # 5) 压缩归档：
    #    · local  —— 压成 ~5GB HEVC MP4（out_dir_mode=date_subfolder → 根/YYYY-MM-DD/，
    #                当前规则；源已在日期目录则复用，不重复建层）
    #    · darkpy —— 与本地一致（用户规则 2026-09-05：rec站补档也要同等压缩归档）；
    #                压缩开关被关掉时按 B站规则保留原始 MP4 归档（不删）。
    #    · bilibili —— 按用户规则不压缩，直接保留原 MP4
    local_root = (cfg.get('local') or {}).get('root') or ''
    cc = cfg.get('compress') or {}
    bz = cfg.get('bilibili') or {}
    if src_kind in ('local', 'darkpy') and cc.get('enabled', True):
        replace = cc.get('replace_original', False) and src_kind == 'local'
        if src_kind == 'darkpy':
            # rec站源在临时目录，压缩成功后源文件按本地规则删除（省盘）；
            # 压缩失败则保留，下次重跑时 merge 幂等跳过、直接续压。
            replace = cc.get('replace_original', False)
        compress_video.compress_video(cfg, log, src_video, replace_original=replace,
                                      archive_root=local_root or None)
    elif src_kind in ('darkpy', 'bilibili'):
        log('[compress] %s 源：compress.enabled=false，按 B站规则保留原始 MP4 不压缩' % src_kind)
        if bz.get('keep_mp4', True):
            _archive_bilibili_mp4(cfg, log, src_video, date_iso)
        elif src_kind == 'darkpy':
            # 用户没关 keep_mp4，但此处被压缩关闭分支触达——保守保留，不删
            log('[compress] darkpy 源 keep_mp4=false 但压缩关闭，保留原始文件避免丢档', 'WARN')

    # 收尾兜底：16k 音频 / ASR 切块 / B站已归档副本（正常在转写后已删，这里再确认一次）
    cleanup.after_session(cfg, log, stem=stem, src_kind=src_kind)

    # B站源：按配置保留/删除下载视频（默认保留，归档到 录播根目录/YYYY-MM-DD/）
    if src_kind == 'bilibili':
        if bz.get('keep_mp4', True):
            _archive_bilibili_mp4(cfg, log, src_video, date_iso)
        else:
            try:
                if os.path.exists(src_video):
                    # 必须判返回值：这是 ~9GB 的下载文件，误报「已删除」会让垃圾一直堆着
                    if force_unlink(src_video, log):
                        log('[bilibili] 已删除临时下载视频：%s' % os.path.basename(src_video))
                    else:
                        log.error('[bilibili] 删除临时下载视频失败（不影响成品，但会占磁盘）：%s'
                                  % os.path.basename(src_video))
            except Exception as e:
                log.error('[bilibili] 删除临时下载视频失败（不影响成品）：%s' % e)

    try:
        RD.reorder()
    except Exception as e:
        log('重排 Word 失败（不影响总结）：%s' % e)
    return True


# ---------------------------------------------------------------------------
# 步骤 3：B站兜底
# ---------------------------------------------------------------------------
def bilibili_fallback(cfg, st, log, cookies, progress, only_dates=None):
    """本地/rec站都没有的缺失日期，去 B站合集抓对应录播（第三备份源）。

    only_dates：限定只处理这些直播日期（由 backfill 缺失补档驱动）；
                None 时保持旧行为（抓最新未处理）。
    返回成功处理的直播日期列表。
    """
    s = cfg['series']
    horizon = fetch_latest.scan_horizon(cfg, st)
    archives = fetch_series(s['mid'], s['series_id'], cookies, stop_before_pubdate=horizon)
    log('B站兜底：扫描到 %d 期，最新：%s' % (len(archives),
        (archives[0]['title'][:50] if archives else '无')))
    done_dates = set(st.get('done_dates', []))
    if only_dates is not None:
        want = set(only_dates)
        picked = [a for a in archives
                  if a.get('_live_date') and a['_live_date'].isoformat() in want
                  and a['_live_date'].isoformat() not in done_dates]
        # 标题解析不出日期的，尝试用 pubdate 前一日近似兜底（避免漏）
        for a in archives:
            if a in picked or a.get('_live_date'):
                continue
            pd = datetime.date.fromtimestamp(a.get('pubdate', 0))
            if pd.isoformat() in want and pd.isoformat() not in done_dates:
                a['_live_date'] = pd
                picked.append(a)
        log('B站兜底：限定 %d 个缺失日期，匹配到 %d 期' % (len(want), len(picked)))
    else:
        picked = fetch_latest.pick_videos(cfg, st, archives, log)
        # 已处理过的日期不再从 B站抓（本地/此前已处理）
        picked = [a for a in picked if a.get('_live_date') is None
                  or a['_live_date'].isoformat() not in done_dates]
    if not picked:
        log('B站兜底：无新录播，收工。')
        return []
    ok_dates = []
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
            ok_dates.append(a['date'])
        else:
            log.error('%s 处理失败（保留临时视频，下次重试）：%s' % (bvid, src))
    return ok_dates


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


def mark_darkpy_done(st, cfg, date_iso, res, session_index=0):
    """rec.darkpy.cn 补档成功后的占位写入（与 LOCAL 占位同语义）。

    · key = DARKPY-YYYYMMDD（多场追加 -N），date 故意 +1 天挡住 B站日后上传的同场；
    · live_date 存真实直播日期，done_dates 只收录一次。
    """
    ld = datetime.date.fromisoformat(date_iso)
    ymd = ld.strftime('%Y%m%d')
    key = 'DARKPY-%s' % ymd if session_index == 0 else 'DARKPY-%s-%d' % (ymd, session_index + 1)
    base = os.path.basename(res['src_video'])
    stem = base[:-4]
    title = res.get('title') or stem
    rec = st['processed'].setdefault(key, {'stages': []})
    rec.update({
        'stages': ['transcribe', 'summarize', 'cleanup', 'compress'],
        'title': '%s %s' % (title, ld.strftime('%Y年%m月%d日')),
        'date': (ld + datetime.timedelta(days=1)).isoformat(),
        'live_date': ld.isoformat(),
        'stem': stem,
        'doc': S.doc_path_for(cfg, ld),
        'darkpy': True,
        'lossless': res.get('lossless', False),
        'parts': res.get('n_segments', 0),
        'note': 'daily_pipeline rec.darkpy.cn 补档（%s）' % base,
    })
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
    ap.add_argument('--from', dest='frm', default=None,
                    help='缺失补档窗口开始（YYYY-MM-DD）；默认 backfill.start_date/since_live_date')
    ap.add_argument('--to', default=None,
                    help='缺失补档窗口结束（YYYY-MM-DD）；默认今天')
    ap.add_argument('--no-backfill', action='store_true',
                    help='跳过缺失检查/补档（rec.darkpy.cn → B站），只做本地')
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

        # ---- 步骤 0：开跑前先清一遍垃圾 + 磁盘水位检查 ----
        # （上一轮如果中途失败，9GB 的下载残留 / ASR 切块会一直躺在 tmp 里）
        cu = cleanup.cfg_cleanup(cfg)
        if cu.get('enabled', True) and cu.get('sweep_on_start', True):
            cleanup.sweep(cfg, log, dry_run=args.dry_run)
        if not args.dry_run:
            cleanup.check_disk_guard(cfg, log, dry_run=False)

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
            if not args.no_backfill:
                log('---- 步骤 3（dry-run）：缺失检查报告 ----')
                backfill.stage(cfg, st, log, {}, cookies=cookies,
                               only_date=args.date, frm=args.frm, to=args.to,
                               dry_run=True, do_bilibili=False)
            log('dry-run，未实际处理。')
            return

        # ---- 步骤 2：本地优先总结 ----
        progress = {}
        if to_process:
            log('---- 步骤 2：本地录播优先总结 ----')
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
            touch()

        # ---- 步骤 3：缺失检查 + 补档（本地 → rec.darkpy.cn → B站）----
        # 用户规则（2026-09-05）：每天对照「实际直播日历」找缺失，rec.darkpy.cn 优先于 B站。
        if not args.no_backfill:
            log('---- 步骤 3：缺失检查/补档（backfill） ----')
            backfill.stage(cfg, st, log, progress, cookies=cookies,
                           only_date=args.date, frm=args.frm, to=args.to,
                           dry_run=False, do_bilibili=True)
        else:
            log('---- 步骤 3：已跳过（--no-backfill） ----')

        # ---- 收尾清扫：无论成败，把本轮产生的中间文件清干净 ----
        try:
            if cleanup.cfg_cleanup(cfg).get('sweep_on_finish', True):
                cleanup.sweep(cfg, log, dry_run=args.dry_run)
        except Exception as e:
            log('收尾清扫失败（不影响交付物）：%s' % e, 'WARN')

        log('daily_pipeline 本次完成')
    finally:
        try:
            force_unlink(LOCK)
        except Exception:
            pass
        try:
            force_unlink(HB)
        except Exception:
            pass


if __name__ == '__main__':
    main()
