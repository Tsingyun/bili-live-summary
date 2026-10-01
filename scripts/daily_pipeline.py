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
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import (load_config, load_state, save_state, load_cookies, Logger,
                    probe_duration, stem_for, fetch_series,
                    force_unlink, force_rmtree, timeout_for_source,
                    run_tool, atomic_write_text)
import fetch_latest
import download_merge
import hotwords
import mp3_export
import compress_video
import backfill
import reorder_doc as RD
import summarize_gemini as S
import regen_gemini_v2 as R
import model_router as MR  # Gemini + ModelScope 混合模型路由
import cleanup
import bili_upload
import cleanup_originals  # 压完删原始 FLV 的闭环清扫（run_sweep）

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


def _log_err(log, msg):
    """记一条错误级日志。兼容两种 log 形态：Logger 对象（.error 方法）或
    普通 callable（第二参为级别字符串）。"""
    try:
        log.error(msg)
    except AttributeError:
        log(msg, 'ERROR')


def _tool_ready(path, log, name):
    """显式检查外部工具（ffmpeg / ffprobe）是否可用。

    合并强依赖这两个命令，缺失时必须**明确报错并说明后果**，而不是让后面的
    subprocess 抛一个看不懂的 FileNotFoundError。
    """
    if not path or not os.path.exists(path):
        _log_err(log, '[merge] 外部工具缺失：%s 未配置或路径不存在（%s）——'
                      '本轮无法判断片段时长/合并，相关片段将按独立场次处理（不会崩溃）。'
                      '请检查 config.paths.%s' % (name, path, name.lower()))
        return False
    return True


def _file_end(f, ffprobe, log):
    """估算单个片段的结束时刻，返回 (end_datetime, source)。

    source ∈ {'duration', 'mtime', 'none'}：
      · 'duration' —— ffprobe 拿到真实时长，最可靠；
      · 'mtime'    —— ffprobe 失败/缺失时回退用「文件最后修改时间」近似结束时刻
                      （录制器写完后不再改文件，mtime ≈ 录制结束时刻）。这是**降级**
                      手段，仅用于让跨午夜场次仍有机会被正确合并，日志会显式标注；
      · 'none'     —— 两种手段都失败，该片段无法参与间隙判断，**保守独立成场**，
                      绝不猜测性合并。
    """
    if ffprobe:
        try:
            d = probe_duration(ffprobe, f['path'])
            if d and d > 0:
                return f['start'] + datetime.timedelta(seconds=float(d)), 'duration'
        except Exception:
            pass
    try:
        mt = datetime.datetime.fromtimestamp(os.path.getmtime(f['path']))
        if mt > f['start']:
            return mt, 'mtime'
    except Exception:
        pass
    return None, 'none'


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
            low = fn.lower()
            # 排除压缩/合并产物及其半成品（2026-09-26 修复）：
            #  · 任意含 .part 的残留（压缩/合并被杀留下的 *.part.mp4）绝不能当新录播，
            #    否则会被 is_file_stable 误判成「录制中」，导致整场被永久跳过、源永不删；
            #  · 去掉全部尾缀扩展名后仍以 _compressed / _merged 结尾的（如
            #    xxx_compressed.mp4、xxx_merged_compressed.mp4、xxx_merged.mp4）也排除。
            if '.part' in low:
                continue
            core = re.sub(r'(\.\w+)+$', '', fn)
            if core.lower().endswith('_compressed') or core.lower().endswith('_merged'):
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

    # ------------------------------------------------------------------
    # 场次归组（2026-09-14 重构：不再以「日期变更」作为切分依据）
    #
    # 【旧实现的问题】先按文件名里的 8 位日期 by_date 硬分组，再在**同一天内部**
    # 用间隙阈值合并。断流重连后若录制跨过 0 点，后半段文件名日期变为次日，
    # 于是被丢进另一个日期桶，永远无法和前一晚的尾巴合并 → 一场直播被切成两个
    # session → 产出两份总结（用户反馈的问题）。
    #
    # 【新实现】全部候选文件**全局**按开始时间排序，只用「相邻片段的间隙阈值」
    # 判断是否同一场，与日历日期完全无关：
    #   · 跨午夜但连续（间隙 <= max_gap_sec）→ 归属前一场，合并后只出一份总结；
    #   · 同一天但间隔很久（如下午场 + 晚上场）→ 正确切成两场；
    #   · 场次归属日期 = chain 第一个片段的日期（即**开播那天**）。
    # ------------------------------------------------------------------
    files.sort(key=lambda x: x['start'])
    merge_on = bool(mc.get('enabled', True))
    if not _tool_ready(ffprobe, log, 'ffprobe'):
        ffprobe = None                      # 缺失时降级用 mtime，不中断流程

    # 每个片段的结束时刻（优先真实时长，失败回退 mtime，都失败则独立成场）
    end_of, dur = {}, {}
    degraded = []
    for f in files:
        end, src = _file_end(f, ffprobe, log)
        if end is None:
            degraded.append(f['fn'])
            end = f['start']                # 时长视作 0 → 间隙必然超限 → 独立成场
        f['_end_src'] = src
        end_of[f['path']] = end
        dur[f['path']] = (end - f['start']).total_seconds()
    if degraded:
        log('[merge] %d 个片段取不到时长且无法用 mtime 估算，保守按独立场次处理：%s'
            % (len(degraded), ', '.join(degraded)), 'WARN')
    if ffprobe is None and files:
        log('[merge] 已降级：用文件修改时间(mtime)近似片段结束时刻来判断间隙', 'WARN')

    # 允许轻微负间隙（录制器重连可能回退几秒/轻微重叠），默认 60s 内仍算同一场
    allow_overlap = int(mc.get('allow_overlap_sec', 60))
    excl = set(mc.get('exclude_dates') or [])
    if not merge_on:
        log('[merge] local_merge.enabled=false，每个片段独立成场（不合并）')

    def _excluded(f):
        return f['live_date'].isoformat() in excl

    chains, cur = [], [files[0]]
    for nxt in files[1:]:
        prev = cur[-1]
        gap = (nxt['start'] - end_of[prev['path']]).total_seconds()
        same_session = (
            merge_on
            and not _excluded(prev) and not _excluded(nxt)
            and prev['_end_src'] != 'none'          # 前一段结束时刻不可知 → 不敢合并
            and (-allow_overlap) <= gap <= max_gap
        )
        if same_session:
            cur.append(nxt)
        else:
            chains.append(cur)
            cur = [nxt]
    chains.append(cur)

    # 超短片段吸收：极短且紧贴/重叠的片段并入相邻长场，避免独立成场出废稿
    if merge_on:
        chains = _absorb_ultrashort(cfg, log, mc, files, chains, dur)

    # chain_index 按「开播日期」分组编号：跨午夜合并出的 chain 归属其首段的日期，
    # 保证同一天有多场时编号稳定，并与 state 里的占位 key 兼容。
    per_date_counter = {}
    for chain in chains:
        chain.sort(key=lambda x: x['start'])
        first, last = chain[0], chain[-1]
        ld = first['live_date']
        idx = per_date_counter.get(ld, 0)
        per_date_counter[ld] = idx + 1
        span_end = end_of[last['path']]
        crosses_midnight = any(f['live_date'] != ld for f in chain)
        sessions.append({'live_date': ld, 'clean_title': first['title'], 'hour': first['hour'],
                         'member_files': [f['path'] for f in chain], 'is_merged': len(chain) > 1,
                         'chain_index': idx, 'merged_src': None,
                         # date_seq = 该场次在「本日」内的序号，只用于生成 Word/TXT 标题；
                         # chain_index 可能会被 _next_free_key 顺延（避让旧占位），
                         # 两者必须分开，否则顺延会让标题凭空多出「（第2场）」。
                         'date_seq': idx,
                         'crosses_midnight': crosses_midnight,
                         'span_start': first['start'], 'span_end': span_end})
        if crosses_midnight:
            log('[merge] 跨午夜场次已合并为一场（归属 %s，共 %d 段，%s → %s）'
                % (ld.isoformat(), len(chain),
                   first['start'].strftime('%m-%d %H:%M:%S'),
                   span_end.strftime('%m-%d %H:%M:%S')))
    # 全局按开播时间排序输出，保证处理顺序即时间顺序
    sessions.sort(key=lambda s: s['span_start'])
    return sessions


def _members_of(rec):
    """取占位记录里的成员文件名集合（用于判断两条记录是不是同一场）。"""
    try:
        return set(os.path.basename(x) for x in (rec.get('members') or []))
    except Exception:
        return set()


def _same_session_as_record(rec, session):
    """判断 state 里的占位记录是否就是「当前这一场」。

    同一个 live_date 可能残留多条占位（典型：跨午夜尾巴被合并进前一天时留下的
    `merged_into` 占位）。若只按 key 判定，前一天留下的占位会**永久挡住**当天
    真正的直播（2026-09-13 晚场即因此被整场跳过）。

    判定：占位记录的成员文件与当前场次有交集 → 同一场；无交集 → 同日另一场。
    记录里没有 members（很老的记录）时保守按「同一场」处理，保持旧行为。
    """
    prev = _members_of(rec)
    if not prev:
        return True
    cur = set(os.path.basename(p) for p in session['member_files'])
    return bool(prev & cur)


def _next_free_key(st, session, log=None):
    """为本场挑一个不与既有占位冲突的 key（返回顺延后的 chain_index）。

    目标 key 被占且成员无交集（= 同一天的另一场）时，把 chain_index 往后顺延，
    **绝不覆盖**旧记录——旧记录承载着「某段已并入哪场」的信息，覆盖会丢。
    """
    ld = session['live_date']
    ci = int(session.get('chain_index', 0))
    for _ in range(50):
        key = session_key(ld, ci)
        rec = (st.get('processed') or {}).get(key)
        if not rec or _same_session_as_record(rec, session):
            if ci != session.get('chain_index') and log:
                log('[monitor] %s 占位冲突（同日另有已处理场次），key 顺延为 %s'
                    % (ld.isoformat(), key))
            return ci
        ci += 1
    return ci


def already_done_local(cfg, st, session):
    key = session_key(session['live_date'], session['chain_index'])
    rec = (st.get('processed') or {}).get(key)
    if rec:
        if _same_session_as_record(rec, session):
            return True, '%s 占位已存在' % key
        # 占位属于同一天的另一场（如跨午夜尾巴的 merged_into 占位），不能挡住本场
        return False, ''
    fname = session_fname(session['live_date'], session['clean_title'],
                          session['hour'], session.get('date_seq', session['chain_index']))
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


def _sum_dur(cfg, paths):
    """各分段时长之和（用于合并产物的完整性判据）。取不到时按 0 计。"""
    ffprobe = (cfg.get('paths') or {}).get('ffprobe')
    total = 0.0
    for p in paths:
        try:
            total += probe_duration(ffprobe, p) or 0
        except Exception:
            pass
    return total


def prepare_session(cfg, log, session):
    """为 session 准备好单一视频源（merged_src）。单文件直接用；多段连续则合并。"""
    if not session['is_merged']:
        session['merged_src'] = session['member_files'][0]
        return session['merged_src']
    merged_dir = os.path.join(cfg['_dirs']['root'], 'merged')
    os.makedirs(merged_dir, exist_ok=True)
    first_base = re.sub(r'\.\w+$', '', os.path.basename(session['member_files'][0]))
    out_path = os.path.join(merged_dir, first_base + '_merged.mp4')

    # 复用已有合并产物必须校验「时长接近各分段之和」，不能只看体积 >1MB。
    # 旧实现只看体积：上次被杀留下的半截合并文件会被当成已完成直接送去转写，
    # 结果就是「一场直播只总结了一半」。这里不达标就重新合并（合并函数自身幂等）。
    exp = _sum_dur(cfg, session['member_files'])
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024 * 1024:
        got = probe_duration(cfg['paths']['ffprobe'], out_path) or 0
        if got > 0 and (exp <= 0 or got >= exp * 0.95):
            log('[merge] 合并视频已存在且完整（%s），跳过'
                % _hms_short(got))
            session['merged_src'] = out_path
            return out_path
        log('[merge] 已有合并产物不完整（%.0fs/期望 %.0fs），重新合并：%s'
            % (got, exp, os.path.basename(out_path)), 'WARN')

    log('[merge] 合并 %d 段连续录播为单一时间线：%s'
        % (len(session['member_files']),
           ', '.join(os.path.basename(p) for p in session['member_files'])))
    t0 = time.time()
    res = merge_videos(cfg, log, session, out_path)
    if not res:
        # 关键：合并失败**绝不退回「逐段分别总结」**——那会给一场直播产出多份
        # 残缺总结。这里整场跳过且不写完成标记，下次运行自动重试（幂等）。
        _log_err(log, '[merge] 合并失败，本场整场跳过、不写完成标记（下次自动重试），'
                      '不会逐段分别总结：%s'
                      % ', '.join(os.path.basename(p) for p in session['member_files']))
        return None
    log('[merge] 合并完成（%.0fs）：%s' % (time.time() - t0, os.path.basename(out_path)))
    session['merged_src'] = res
    return res


def _hms_short(sec):
    sec = int(sec or 0)
    return '%02d:%02d:%02d' % (sec // 3600, (sec % 3600) // 60, sec % 60)


def _merged_ok(out_path, ffprobe, min_dur=0.0):
    """校验合并产物是否可用：非空、时长 > 0，且（给了 min_dur 时）>= min_dur。"""
    if not os.path.exists(out_path) or os.path.getsize(out_path) <= 0:
        return False
    if not ffprobe:
        return True
    try:
        got = probe_duration(ffprobe, out_path) or 0
    except Exception:
        return False
    if got <= 0:
        return False
    return min_dur <= 0 or got >= min_dur


def merge_videos(cfg, log, session, out_path):
    """多段连续录播合并为单一视频（优先 concat demuxer 不重编码，失败回退重编码）。

    **外部依赖（显式声明）**：本函数强依赖 `config.paths.ffmpeg` 与
    `config.paths.ffprobe` 两个外部命令。
      · ffmpeg  —— 真正执行合并（concat demuxer / filter_complex 两条路径）；
      · ffprobe —— 合并后校验产物时长，防止产出 0 字节/损坏/半截文件被当成成功。
    任一缺失或执行失败都会记录明确错误并返回 None，**由调用方决定跳过整场**，
    绝不会退化成「逐段分别总结」。

    ★ 2026-09-18 加固：先写 `<out>.part.mp4`，校验时长（>= 各分段之和 95%）通过后
    再原子改名到正式路径；ffmpeg 调用统一带超时。这样进程被杀只会留下 .part，
    正式文件要么不存在、要么完整——杜绝"半截合并视频被复用"。
    """
    ffmpeg = cfg['paths']['ffmpeg']
    ffprobe = cfg['paths']['ffprobe']
    if not _tool_ready(ffmpeg, log, 'ffmpeg'):
        _log_err(log, '[merge] 缺少 ffmpeg，无法把 %d 段录播合并为单一时间线；'
                      '本场跳过（不会逐段总结），请检查 config.paths.ffmpeg 后重跑。'
                      % len(session['member_files']))
        return None
    if not _tool_ready(ffprobe, log, 'ffprobe'):
        log('[merge] 缺少 ffprobe，合并后无法校验产物时长，将改用「文件非空」作为成功判据', 'WARN')
        ffprobe = None

    members = list(session['member_files'])
    # ★ 2026-09-26 修复：录制中断会留下 0 字节占位片段，concat 对空文件会失败。
    #   直接丢弃空成员（它们不含任何内容），仅当全部为空才放弃合并。
    nonempty = [p for p in members if os.path.exists(p) and os.path.getsize(p) > 0]
    if len(nonempty) < len(members):
        dropped = [os.path.basename(p) for p in members if p not in nonempty]
        log('[merge] 丢弃 %d 个空片段(0字节,疑似录制中断残留)：%s'
            % (len(dropped), ', '.join(dropped)), 'WARN')
    members = nonempty
    if not members:
        _log_err(log, '[merge] 所有成员均为空文件，放弃合并：%s'
                 % ', '.join(os.path.basename(p) for p in session['member_files']))
        return None

    exp = _sum_dur(cfg, members)
    min_dur = exp * 0.95 if exp > 0 else 0.0
    tmp_out = out_path + '.part.mp4'
    try:
        force_unlink(tmp_out)
    except Exception:
        pass
    to = int(max(1200, min(6 * 3600, 1200 + exp * 2)))

    list_path = out_path + '.list.txt'
    try:
        with open(list_path, 'w', encoding='utf-8') as f:
            for p in members:
                f.write("file '%s'\n" % p.replace("'", "'\\''"))
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'concat', '-safe', '0', '-i', list_path,
                        '-c', 'copy', tmp_out], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=to)
        if _merged_ok(tmp_out, ffprobe, min_dur):
            os.replace(tmp_out, out_path)
            return out_path
        log('[merge] 无损合并产物不完整（%s），尝试重编码合并'
            % _hms_short(probe_duration(ffprobe, tmp_out) if ffprobe else 0), 'WARN')
    except subprocess.TimeoutExpired:
        _log_err(log, '[merge] 无损合并超时（%ds）已终止，尝试重编码合并' % to)
    except Exception as e:
        log('[merge] concat demuxer 失败（%s），尝试重编码合并' % e, 'WARN')
    finally:
        try:
            force_unlink(list_path)
        except Exception:
            pass
        try:
            force_unlink(tmp_out)
        except Exception:
            pass

    try:
        n = len(members)
        ins = []
        for p in members:
            ins += ['-i', p]
        vmap = ''.join('[%d:v]' % i for i in range(n))
        amap = ''.join('[%d:a]' % i for i in range(n))
        fc = "%s%sconcat=n=%d:v=1:a=1[v][a]" % (vmap, amap, n)
        to2 = int(max(2400, min(12 * 3600, 2400 + exp * 4)))
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error'] + ins +
                       ['-filter_complex', fc, '-map', '[v]', '-map', '[a]',
                        '-c:v', 'libx264', '-preset', 'veryfast',
                        '-c:a', 'aac', '-b:a', '192k', tmp_out],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=to2)
        if _merged_ok(tmp_out, ffprobe, min_dur):
            os.replace(tmp_out, out_path)
            return out_path
        log.error('[merge] 重编码合并产物仍不完整，放弃本场')
    except subprocess.TimeoutExpired:
        _log_err(log, '[merge] 重编码合并超时，放弃本场')
    except Exception as e2:
        _log_err(log, '[merge] 重编码合并也失败：%s' % e2)
    try:
        force_unlink(tmp_out)
    except Exception:
        pass
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
        ffprobe = (cfg.get('paths') or {}).get('ffprobe')
        src_dur = probe_duration(ffprobe, src_video) or 0
        if os.path.exists(dst) and os.path.getsize(dst) >= os.path.getsize(src_video):
            # 体积够大还要时长对得上，否则可能是上次被杀留下的截断副本
            dst_dur = probe_duration(ffprobe, dst) or 0
            if src_dur <= 0 or dst_dur >= src_dur * 0.98:
                log('[bilibili] 归档 MP4 已存在：%s' % dst)
                try:
                    force_unlink(src_video)
                except Exception:
                    pass
                return dst
            log('[bilibili] 归档副本时长不足（%.0fs/源 %.0fs），重新归档'
                % (dst_dur, src_dur), 'WARN')
            force_unlink(dst)
        # 跨盘时 shutil.move 是 copy+unlink：被杀会留下截断副本。
        # 先复制到 .part 再原子改名，成功后才删源。
        tmp = dst + '.part'
        try:
            force_unlink(tmp)
        except Exception:
            pass
        if os.path.dirname(os.path.abspath(dst)) == os.path.dirname(os.path.abspath(src_video)):
            shutil.move(src_video, dst)          # 同盘：rename，原子
        else:
            shutil.copy2(src_video, tmp)
            got = probe_duration(ffprobe, tmp) or 0
            if src_dur > 0 and (got <= 0 or got < src_dur * 0.98):
                _log_err(log, '[bilibili] 归档副本校验失败（时长不符），保留原文件不删')
                force_unlink(tmp)
                return src_video
            os.replace(tmp, dst)
            force_unlink(src_video)
        log('[bilibili] 已保留 B站录播 MP4 → %s' % dst)
        return dst
    except Exception as e:
        log.error('[bilibili] 归档 MP4 失败（原文件保留，不删除）：%s' % e)
        return src_video


# ---------------------------------------------------------------------------
# 核心：对「一个视频源」跑完 转写→总结→MP3→压缩
# ---------------------------------------------------------------------------
def extract_16k_audio(cfg, log, src, dst):
    """抽取 16k 单声道音频（转写用）。已存在且非空则跳过。

    原子写：先落到 <dst>.part，成功后 os.replace；被杀只会留下 .part，
    不会让一个半截音频被下次运行当成「已抽好」。
    """
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return
    ffmpeg = cfg['paths']['ffmpeg']
    log('抽取 16k 转写用音频：%s' % os.path.basename(src))
    t0 = time.time()
    tmp = dst + '.part'
    to = timeout_for_source(cfg['paths']['ffprobe'], src, base=1800)
    try:
        # ★ 必须显式 -f mp3：临时名是 `<dst>.part`，ffmpeg 从扩展名猜不出容器格式，
        #   会直接以 EINVAL(-22) 退出（Windows 上表现为 exit status 4294967274）。
        #   加了 -f 就与文件名无关，别再删掉它。
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', src, '-vn', '-ac', '1', '-ar', '16000',
                        '-f', 'mp3', tmp],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=to)
        if not (os.path.exists(tmp) and os.path.getsize(tmp) > 1000):
            raise RuntimeError('音频抽取产物异常（空文件）')
        os.replace(tmp, dst)
    except subprocess.TimeoutExpired:
        _log_err(log, '[audio] 抽取 16k 音频超时（%ds）已终止：%s'
                 % (to, os.path.basename(src)))
        try:
            force_unlink(tmp)
        except Exception:
            pass
        raise
    except Exception:
        try:
            force_unlink(tmp)
        except Exception:
            pass
        raise
    log('  音频抽取完成（%.0fs）' % (time.time() - t0))


def process_video_source(cfg, st, src_video, date_iso, fname, src_kind, log, progress, member_files=None):
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
    rot = MR.build_rotator(cfg, log)  # 混合降级链（未配 ModelScope key 时自动退化为纯 Gemini）
    ok = R.process_session(rot, sys_prompt, cfg, fname, date_iso, font, log, progress,
                           src_path=src_video)
    if not ok:
        log.error('%s 总结未完全成功（可能配额耗尽），未写完成标记，可重跑' % stem)
        # 失败也要清干净：ASR 切块、B站下载残留不因失败而赖在磁盘上
        cleanup.after_session(cfg, log, stem=stem, src_kind=src_kind)
        return False

    # 4) 总结后导出中质量 MP3（192k/44.1k，供文字识别/其他用途）
    #    4)~5) 的产物（归档 MP3 / 压缩 MP4）失败**不影响总结成品**：TXT/SRT/Word 已落盘，
    #    state 占位照写；下次重跑时转写与总结都会跳过，只补做 MP3/压缩（幂等）。
    try:
        mp3_export.export_for_local(cfg, log, src_video)
    except Exception as e:
        _log_err(log, '[mp3_export] 归档 MP3 异常（不影响总结）：%s' % e)

    # 5) 压缩归档：
    #    · local  —— 压成 ~5GB HEVC MP4（out_dir_mode=date_subfolder → 根/YYYY-MM-DD/，
    #                当前规则；源已在日期目录则复用，不重复建层）
    #    · darkpy —— 与本地一致（用户规则 2026-09-05：rec站补档也要同等压缩归档）；
    #                压缩开关被关掉时按 B站规则保留原始 MP4 归档（不删）。
    #    · bilibili —— 按用户规则不压缩，直接保留原 MP4
    local_root = (cfg.get('local') or {}).get('root') or ''
    cc = cfg.get('compress') or {}
    bz = cfg.get('bilibili') or {}
    compressed_path = None  # 压缩产物路径，供「步骤6 上传小号」使用
    try:
        if src_kind in ('local', 'darkpy') and cc.get('enabled', True):
            replace = cc.get('replace_original', False) and src_kind == 'local'
            if src_kind == 'darkpy':
                # rec站源在临时目录，压缩成功后源文件按本地规则删除（省盘）；
                # 压缩失败则保留，下次重跑时 merge 幂等跳过、直接续压。
                replace = cc.get('replace_original', False)
            compressed_path = compress_video.compress_video(cfg, log, src_video, replace_original=replace,
                                          archive_root=local_root or None,
                                          orig_members=(member_files if src_kind == 'local' else None))
        elif src_kind in ('darkpy', 'bilibili'):
            log('[compress] %s 源：compress.enabled=false，按 B站规则保留原始 MP4 不压缩' % src_kind)
            if bz.get('keep_mp4', True):
                _archive_bilibili_mp4(cfg, log, src_video, date_iso)
            elif src_kind == 'darkpy':
                # 用户没关 keep_mp4，但此处被压缩关闭分支触达——保守保留，不删
                log('[compress] darkpy 源 keep_mp4=false 但压缩关闭，保留原始文件避免丢档', 'WARN')
    except Exception as e:
        _log_err(log, '[compress] 压缩归档异常（不影响总结成品，源文件保留）：%s' % e)

    # 收尾兜底：16k 音频 / ASR 切块 / B站已归档副本（正常在转写后已删，这里再确认一次）
    try:
        cleanup.after_session(cfg, log, stem=stem, src_kind=src_kind)
    except Exception as e:
        log('收尾清理异常（不影响交付物）：%s' % e, 'WARN')

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
        # 必须重排「本场实际写入的月度文档」，不能落回 reorder_doc 的默认文档（曾导致当月章节顺序错乱）
        RD.reorder(S.doc_path_for(cfg, live_date))
    except Exception as e:
        log('重排 Word 失败（不影响总结）：%s' % e)

    # 步骤 6（可选）：上传压缩副本到 B站小号
    #   · 默认直接传现有 HEVC 副本（用户 2026-09-26 确认：先拿真实 HEVC 录播试传，
    #     H.264 转码仅作兜底，block_hevc=true 才阻断）；
    #   · 失败不影响总结成品：TXT/SRT/Word/MP3/压缩 MP4 已全部落盘，state 占位照写，
    #     下次重跑时只补做上传（幂等，已上传标记会跳过）。
    try:
        uc = bili_upload.load_upload_cfg(cfg)
        if uc.get('enabled'):
            up_src = compressed_path if (compressed_path and os.path.exists(compressed_path)) \
                else (src_video if src_kind == 'bilibili' else None)
            if up_src and os.path.exists(up_src):
                cookies = load_cookies(cfg['paths'].get('cookies'))
                bili_upload.upload_one(cfg, log, uc, up_src,
                                       {'date_iso': date_iso, 'fname': fname,
                                        'src_kind': src_kind}, cookies, st=st)
            else:
                log('[upload] 无可上传视频（压缩产物缺失且非 B站源），跳过', 'WARN')
        else:
            log('[upload] 未启用（upload.enabled=false）')
    except Exception as e:
        _log_err(log, '[upload] 上传小号异常（不影响总结成品）：%s' % e)
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
    # ★ 2026-09-18：B站合集接口会风控（HTTP 412 / code=-352），或代理抖动导致请求失败。
    #   这里必须自己兜住：B站只是第三备份，拿不到集合列表时应该"本轮跳过并告警"，
    #   而不是把异常抛到 main 让整轮（含收尾清扫与状态落盘）中断。
    try:
        archives = fetch_series(s['mid'], s['series_id'], cookies, stop_before_pubdate=horizon)
    except Exception as e:
        log.error('B站兜底：拉取合集失败（风控/网络），本轮跳过 B站源：%s' % e)
        return []
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
        # ★ 2026-09-18：单期失败必须与其它期隔离——否则一期取流失败/风控会中断
        #   后面所有期（旧实现里 download_video_one 抛异常就会跳掉整个循环）。
        try:
            src = download_merge.download_video_one(cfg, a, log)
            if not src:
                log.error('%s 视频下载/合并失败，跳过（已下载分片保留，下次续传）' % bvid)
                continue
            stem = stem_for(a.get('pubdate', 0), a['title'])
            fname = stem + '.txt'
            ok = process_video_source(cfg, st, src, a['date'], fname, 'bilibili', log, progress)
            if ok:
                mark_bilibili_done(st, cfg, a, fname)
                ok_dates.append(a['date'])
            else:
                log.error('%s 处理失败（保留临时视频，下次重试）：%s' % (bvid, src))
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error('B站兜底 %s 处理异常（跳过该期，继续后续）：%s' % (bvid, e))
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
# 产物质量门禁（2026-09-24 新增）
# ---------------------------------------------------------------------------
def _healthcheck(cfg, log, dry_run=False):
    """对输出目录的 Word 总结做三项自检并就地修复：

      ① 格式错误：金句应为「灰色斜体 10pt」，正文应「粗体 11pt」；
         markdown 标记（`**`、`__`、`_“…”_`）必须已剥净。
      ② 异常内容：模型回显的提示词（如「输入文本每一行都以 [HH:MM:SS] 开头…」）
         与元话语开场白（「好的，这是对输入文本进行分段重组…」）必须清除。
      ③ 热词校验：文档里不得残留热词表的「原词」（英文按词边界匹配，避免 NPC 命中 PC）。

    纯离线，不消耗网络与模型额度；任何异常都只告警，绝不影响交付物。
    """
    try:
        import glob
        import docx_healthcheck as HC
    except Exception as e:
        log('未加载产物质量检查模块，跳过：%s' % e, 'WARN')
        return
    out_dir = (cfg.get('output') or {}).get('doc_dir')
    if not out_dir or not os.path.isdir(out_dir):
        log('输出目录不存在，跳过产物质量检查', 'WARN')
        return
    docs = [d for d in glob.glob(os.path.join(out_dir, '*直播总结_*.docx'))
            if not HC.is_backup_file(os.path.basename(d))]
    if not docs:
        return
    try:
        rules, protect = HC.resolve_rules(cfg)
    except Exception:
        rules, protect = [], []
    font = (cfg.get('output') or {}).get('font', '思源宋体CN Light')

    total_fixed, problems = 0, 0
    for d in docs:
        try:
            rep = HC.check_doc(d, rules, protect, fix=not dry_run, font=font, log=log)
        except Exception as e:
            log('  [质检] %s 检查失败：%s' % (os.path.basename(d), e), 'WARN')
            continue
        c = rep['counts']
        if rep['total']:
            log('  [质检] %s：格式 %d / markdown %d / 异常内容 %d / 热词残留 %d / 结构提示 %d%s'
                % (os.path.basename(d), c.get('quote_format', 0), c.get('md_residue', 0),
                   c.get('abnormal', 0), c.get('hotword_residue', 0),
                   c.get('structure', 0),
                   ('（已修复 %d 处）' % rep['fixed']) if rep['fixed'] else ''))
            if rep.get('blocking'):
                problems += 1
            if c.get('abnormal'):
                for it in rep['issues']['abnormal'][:3]:
                    log('    · 异常内容已处理：%s' % it['text'][:60], 'WARN')
            if c.get('hotword_residue'):
                agg = {}
                for it in rep['issues']['hotword_residue']:
                    k = (it['orig'], it['repl'])
                    agg[k] = agg.get(k, 0) + 1
                top = sorted(agg.items(), key=lambda x: -x[1])[:5]
                log('    · 热词未覆盖（建议补进 hotwords.md）：%s'
                    % '、'.join('%s→%s x%d' % (a, b, n) for (a, b), n in top), 'WARN')
            total_fixed += rep['fixed']
    if total_fixed:
        log('产物质量检查：共修复 %d 处' % total_fixed)
    elif problems:
        log('产物质量检查：发现问题但未修复（dry-run）', 'WARN')
    else:
        log('产物质量检查：全部通过')


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
    wd = os.path.dirname(os.path.abspath(cfg_path))
    # 单实例锁（避免并发抢 Gemini 配额）；心跳文件用于判断持有者是否**真的还活着**
    LOCK = os.path.join(wd, '.daily_pipeline.lock')
    HB = os.path.join(wd, '.daily_pipeline.heartbeat')
    STAGE = os.path.join(wd, '.daily_pipeline.stage')

    # 判活逻辑统一在 common 里（与 run_scheduled.py 共用，避免两处实现漂移）：
    # ★ 2026-09-18：旧实现只用 os.kill(pid,0) 判活。本机 PID 复用很快，
    #   上一轮被环境回收后其 PID 常被无关进程顶替 → 误判「仍存活」→
    #   新的一轮直接 `已有实例运行，退出`（exit 0），当天**什么都不会产出**，
    #   且日志上看不出任何异常。这是"可重复运行"最隐蔽的破坏点。
    #   现在要求「PID 存在 且 心跳新鲜」双条件，心跳过期即判孤儿并抢占。
    pid_alive = common.pid_alive
    read_lock_pid = common.read_lock_pid
    lock_owner_alive = lambda: common.lock_owner_alive(wd)     # noqa: E731

    def write_hb(stage=None):
        """刷新心跳（必要时同时写阶段检查点）。"""
        try:
            with open(HB, 'w', encoding='utf-8') as f:
                f.write('%d %d %s' % (os.getpid(), int(time.time()), stage or ''))
        except Exception:
            pass
        if stage:
            try:
                with open(STAGE, 'w', encoding='utf-8') as f:
                    f.write('%s\tpid=%d\t%s\n'
                            % (datetime.datetime.now().isoformat(timespec='seconds'),
                               os.getpid(), stage))
            except Exception:
                pass

    def _write_lock():
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(fd, ('%d %s' % (os.getpid(),
                                 datetime.datetime.now().isoformat(timespec='seconds'))).encode())
        os.close(fd)

    def grab():
        try:
            _write_lock()
            write_hb('启动')
            return True
        except OSError:
            if lock_owner_alive():
                return False
            # 持有者已死 / 心跳过期 → 孤儿锁，抢占
            print('[daily_pipeline] 检测到孤儿锁（持有者已不在或心跳过期），接管')
            try:
                force_unlink(LOCK)
            except Exception:
                pass
            for _ in range(10):
                try:
                    _write_lock()
                    write_hb('启动（接管孤儿锁）')
                    return True
                except OSError:
                    time.sleep(0.3)
            return False

    def release_lock():
        """只在锁**确实是本进程持有**时释放，避免误删别人的锁。"""
        if read_lock_pid(LOCK) != os.getpid():
            return
        for p in (LOCK, HB, STAGE):
            try:
                force_unlink(p)
            except Exception:
                pass

    if not grab():
        print('[daily_pipeline] 已有实例运行（心跳有效），退出')
        sys.exit(0)

    # ---- 心跳线程：无论卡在哪一步（长 ffmpeg/ASR/Gemini），都持续证明自己活着 ----
    hb_stop = {'v': False}

    def _hb_loop():
        while not hb_stop['v']:
            for _ in range(60):
                if hb_stop['v']:
                    return
                time.sleep(1)
            try:
                with open(HB, 'w', encoding='utf-8') as f:
                    f.write('%d %d' % (os.getpid(), int(time.time())))
            except Exception:
                pass

    _hb_thread = threading.Thread(target=_hb_loop, daemon=True)
    _hb_thread.start()

    def touch(stage=None):
        write_hb(stage)

    # ---- 信号：把 SIGTERM/SIGBREAK 转成优雅退出，让 finally 能释放锁 ----
    def _on_term(signum, _frame):
        try:
            write_hb('收到信号 %s，准备退出' % signum)
        except Exception:
            pass
        raise KeyboardInterrupt('signal %s' % signum)

    for _sig in ('SIGTERM', 'SIGBREAK'):
        try:
            signal.signal(getattr(signal, _sig), _on_term)
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
        # 开跑前也扫一遍：补删上一轮（被宿主回收/中断）残留的「已压缩原文」
        # —— 这是"压完却漏删"的根因缺口，见 cleanup_originals.run_sweep
        try:
            cleanup_originals.run_sweep(cfg, log, dry_run=args.dry_run)
        except Exception as e:
            log.error('开跑前原始FLV清扫异常（不影响后续）：%s' % e)
        if not args.dry_run:
            cleanup.check_disk_guard(cfg, log, dry_run=False)

        if not args.dry_run and not wait_network(log, cfg):
            sys.exit(3)
        st = load_state(cfg)
        cookies = load_cookies(cfg['paths'].get('cookies'))

        # ---- 步骤 0.5：确保「当月」文档已建立 ----
        # ★ 归档语义（2026-10-02 起）：往月文档只读、原样归档；新总结只写「直播日期
        #   所属月份」的文档，绝不跨月混写。每月 1 日第一次跑管线时自动新建当月文档。
        if not args.dry_run:
            try:
                _mdoc, _mcreated = S.ensure_month_doc(cfg, log)
                log('[步骤0.5] 当月归档文档：%s（%s）'
                    % (os.path.basename(_mdoc), '新建' if _mcreated else '已存在'))
            except Exception as e:
                # 建文档失败不该拖垮整轮：真正写入时 append_section 还会再兜一次
                log.error('当月文档初始化失败（不影响后续，写入时自动兜底）：%s' % e)

        # ---- 步骤 1：监测本地根目录 ----
        touch('步骤1 监测本地录播')
        sessions = scan_root_sessions(cfg, log)
        if args.date:
            sessions = [s for s in sessions if s['live_date'].isoformat() == args.date]

        # 占位 key 顺延：同一 live_date 可能已有旧占位（典型是跨午夜尾巴留下的
        # merged_into 占位），不能让它顶掉当天真正的直播，也不能覆盖旧记录。
        for s in sessions:
            s['chain_index'] = _next_free_key(st, s, log)

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
        # ★ 2026-10-01：从录播站下载落位的文件 mtime 必然是「刚刚」，会被
        #   is_file_stable 判成「录制中」→ 整场跳过、本次运行零产出，但退出码
        #   仍是 0，看起来像正常跑完。这类"静默空转"必须显式告警。
        if sessions and not to_process:
            unstable_all = all('录制中' in (r or '') for _, r in skipped)
            if unstable_all:
                log('本次 0 产出：全部 %d 场因「录制中/文件不稳定」被跳过。'
                    '若这些文件其实是刚下载的成品（mtime 很新），属误判——'
                    '等 %d 秒后重跑即可；正式拉片请用 rec_fetch_date.py'
                    '（会自动把 mtime 还原为真实录制收尾时刻）'
                    % (len(skipped), 125), 'WARN')
        for s in to_process:
            tag = ('合并 %d 段%s' % (len(s['member_files']),
                                    '（跨午夜）' if s.get('crosses_midnight') else '')
                   ) if s['is_merged'] else '单文件'
            log('  将处理 %s %s [%s] (%s)'
                % (s['live_date'].isoformat(), s['clean_title'], tag,
                   ', '.join(os.path.basename(p) for p in s['member_files'])))

        if args.dry_run:
            if not args.no_backfill:
                log('---- 步骤 3（dry-run）：缺失检查报告 ----')
                try:
                    backfill.stage(cfg, st, log, {}, cookies=cookies,
                                   only_date=args.date, frm=args.frm, to=args.to,
                                   dry_run=True, do_bilibili=False)
                except Exception as e:
                    log.error('缺失检查（dry-run）异常：%s' % e)
            log('dry-run，未实际处理。')
            # dry-run 也做一次**只读**体检，便于提前发现文档问题（绝不修改文件）
            try:
                _healthcheck(cfg, log, dry_run=True)
            except Exception as e:
                log('产物质量检查（dry-run）异常：%s' % e, 'WARN')
            return

        # ---- 步骤 2：本地优先总结 ----
        # ★ 2026-09-19：这里以前写死 `progress = {}`，于是 process_session 内部的
        #   「[缓存命中]」续跑逻辑**从来没生效过**——中途失败（如某块全模型失败、
        #   或写 docx 被占用）后重跑，会从第 1 块重新调用大模型，白烧一轮额度。
        #   现在从磁盘读回进度：process_session 成功写完 docx 时会 pop 掉该日期，
        #   所以正常跑完不会留脏缓存，只有失败残留才会被复用（正是我们要的语义）。
        progress = R.load_progress(os.path.join(wd, '.regen_gemini_v2_progress.json'))
        if progress:
            log('载入总结续跑进度：%s' % ', '.join(sorted(progress.keys())))
        if to_process:
            log('---- 步骤 2：本地录播优先总结 ----')
            for s in to_process:
                touch('步骤2 本地总结 %s' % s['live_date'].isoformat())
                src = prepare_session(cfg, log, s)
                if not src:
                    log.error('准备源文件失败，跳过：%s' % s['live_date'].isoformat())
                    continue
                # 标题用 date_seq（本日场次序号）而非 chain_index——后者可能因
                # 避让旧占位被顺延，用它会让标题凭空多出「（第2场）」。
                fname = session_fname(s['live_date'], s['clean_title'], s['hour'],
                                      s.get('date_seq', s['chain_index']))
                try:
                    ok = process_video_source(cfg, st, src, s['live_date'].isoformat(),
                                             fname, 'local', log, progress,
                                             member_files=s['member_files'])
                    if ok:
                        mark_local_done(st, cfg, s, fname)
                except KeyboardInterrupt:
                    raise                      # 优雅退出信号：交给外层 finally 收尾
                except Exception as ex:
                    log.error('处理 %s 失败：%s' % (s['live_date'].isoformat(), ex))
                time.sleep(int(cfg.get('summarize', {}).get('inter_video_delay_sec', 15)))
            log('本地优先扫描完成')

        # ---- 步骤 3：缺失检查 + 补档（本地 → rec.darkpy.cn → B站）----
        # 用户规则（2026-09-05）：每天对照「实际直播日历」找缺失，rec.darkpy.cn 优先于 B站。
        # ★ 2026-09-18：整段包 try/except。此前这里一旦抛异常（如 B站合集接口风控、
        #   单个日期数据异常），会直接穿透 main 跳过收尾清扫，且日志停在半截、
        #   看不出是"失败"还是"没跑完"。现在失败也会继续走收尾并明确记录。
        if not args.no_backfill:
            log('---- 步骤 3：缺失检查/补档（backfill） ----')
            touch('步骤3 缺失检查/补档')
            try:
                backfill.stage(cfg, st, log, progress, cookies=cookies,
                               only_date=args.date, frm=args.frm, to=args.to,
                               dry_run=False, do_bilibili=True)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log.error('缺失检查/补档异常（已完成的部分已写入 state，可重跑续做）：%s' % e)
        else:
            log('---- 步骤 3：已跳过（--no-backfill） ----')

        # ---- 步骤 4：产物质量门禁（格式检查 / 异常内容清理 / 热词校验）----
        # 2026-09-24 新增：Word 写完后再自检一遍并就地修复，见 _healthcheck 说明。
        touch('步骤4 产物质量检查')
        try:
            _healthcheck(cfg, log, dry_run=args.dry_run)
        except Exception as e:
            log('产物质量检查失败（不影响交付物）：%s' % e, 'WARN')

        # ---- 收尾清扫：无论成败，把本轮产生的中间文件清干净 ----
        touch('收尾清扫')
        try:
            if cleanup.cfg_cleanup(cfg).get('sweep_on_finish', True):
                cleanup.sweep(cfg, log, dry_run=args.dry_run)
        except Exception as e:
            log('收尾清扫失败（不影响交付物）：%s' % e, 'WARN')
        # 收尾补扫：本回合（含历史遗留）压出 _compressed 的原始 FLV 一律删掉
        # —— 漏删必删，杜绝"压完却堆着原始FLV 最后靠手动删"的事故
        try:
            cleanup_originals.run_sweep(cfg, log, dry_run=args.dry_run)
        except Exception as e:
            log('原始FLV清扫失败（不影响交付物）：%s' % e, 'WARN')

        # ---- 模型用量汇报（每次运行结束必附；仅附加输出，失败不影响交付物）----
        try:
            for _u in R.format_usage_report():
                log(_u)
        except Exception as e:
            log('模型用量汇报失败（不影响交付物）：%s' % e, 'WARN')

        log('daily_pipeline 本次完成')
    except KeyboardInterrupt:
        print('[daily_pipeline] 收到中断信号，已释放锁并退出（产物均为原子写，可从断点续跑）')
    finally:
        hb_stop['v'] = True
        try:
            release_lock()
        except Exception:
            pass


if __name__ == '__main__':
    main()
