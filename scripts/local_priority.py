#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
local_priority.py —— 本地录播优先扫描（每日自动化的前置闸门）

在 run_pipeline.py 抓取 B站之前，先扫描本地已提供的录播目录
（D:\\录播\\25788785-岁己SUI\\YYYY-MM-DD\\*.mp4|flv），对「尚未在 state 中处理」的场次
优先做：ffmpeg 抽音频 → FunASR 转写 → 热词替换 → Gemini 切块总结 → 写 Word →
把占位写进 state（key=LOCAL-YYYYMMDD，date 推高一天），使后续 run_pipeline 不会重复抓这些场次。

目的：从根上杜绝「自动化抢跑本地已提供场次、重复消耗 token」。

判重规则（满足任一即视为已处理、跳过）：
  * state['processed'] 中存在 key `LOCAL-YYYYMMDD`；
  * 或存在某条目其 date == 该直播日期，或其 title 含该直播中文日期（如「2026年08月13日」）；
  * 或 TXT 目录已存在以 `YYYYMMDD-` 开头的同名转录文件（可能正被其它脚本处理）。

占位 date = live_date + 1 天：确保基线 baseline_ld > live_date，B站日后上传同场时
`live_date >= baseline_ld` 为 False 而被挡住（fetch_latest 用 >= 判定，相等会漏）。

用法:
  python local_priority.py            # 扫描并处理本地未处理场次
  python local_priority.py --dry-run  # 只看会处理哪些、跳过哪些，不实际处理
"""

import argparse
import datetime
import os
import re
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regen_gemini_v2 as R
import summarize_gemini as S
import hotwords
import transcribe as T
import reorder_doc as RD
import mp3_export
from common import load_config, Logger, load_state, save_state, probe_duration

WORK = R.WORK
# 本地录播根目录（用户提供的本地 mp4/flv 放这里，按 YYYY-MM-DD 分子目录）
LOCAL_ROOT = r'D:\录播\25788785-岁己SUI'

# 文件名前缀：25788785-20260813-200106-472-标题.mp4 （也兼容 「录制-」前缀）
NAME_RE = re.compile(r'^(?:录制-)?(\d+)-(\d{8})-(\d{6})-(\d+)-(.+)\.(mp4|flv|mkv|mov|MP4|FLV)$')
DIR_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')


def wait_network(log, cfg, timeout=900):
    hosts = ['generativelanguage.googleapis.com']
    if cfg:
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
        for h in hosts:
            try:
                socket.gethostbyname(h)
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


def scan_sessions(cfg, log, root=LOCAL_ROOT):
    """扫描本地录播根目录，把每个日期目录下的文件按「时间连续」分组为若干 session。

    连续判定：按文件名里的时间(HHMMSS)排序，相邻两段若「前一段结束时间 ≈ 后一段开始时间」
    （间隙 <= max_gap_sec，且能取到时长）则视为同一场（网络波动断开重连），归入同一 chain。
    间隙过大或取不到时长 → 视为独立场次，各自成 chain。

    返回 list[dict]，每个 session 含：
      live_date, clean_title, hour, member_files, is_merged, chain_index, merged_src(=None)
    """
    sessions = []
    if not os.path.isdir(root):
        return sessions
    mc = cfg.get('local_merge') or {}
    if not mc.get('enabled', True):
        log('[merge] local_merge 未启用，按「每目录仅首个文件」旧逻辑处理', 'WARN')
        for d in sorted(os.listdir(root)):
            dp = os.path.join(root, d)
            if not os.path.isdir(dp):
                continue
            e = _parse_first(d)
            if e:
                ld, title, hour, src = e
                sessions.append(_mk_session(ld, title, hour, [src], 0))
        return sessions

    max_gap = int(mc.get('max_gap_sec', 600))
    ffprobe = cfg['paths']['ffprobe']
    for d in sorted(os.listdir(root)):
        dp = os.path.join(root, d)
        if not os.path.isdir(dp):
            continue
        m = DIR_RE.match(d)
        if not m:
            continue
        live_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        files = []
        for fn in sorted(os.listdir(dp)):
            if not fn.lower().endswith(('.mp4', '.flv', '.mkv', '.mov')):
                continue
            mm = NAME_RE.match(fn)
            if not mm:
                continue
            hh, mi, ss = int(mm.group(3)[0:2]), int(mm.group(3)[2:4]), int(mm.group(3)[4:6])
            start = datetime.datetime(live_date.year, live_date.month, live_date.day, hh, mi, ss)
            files.append({'path': os.path.join(dp, fn), 'start': start,
                          'title': mm.group(5).strip(), 'hour': hh})
        if not files:
            continue
        excl = set(mc.get('exclude_dates') or [])
        if live_date.isoformat() in excl:
            # 显式排除：该日期强制不合并，每段独立成场（用于用户确认是「多场独立直播」的情况）
            log('[merge] %s 在 exclude_dates 中，强制不合并（每段独立成场）' % live_date.isoformat(), 'WARN')
            chains = [[f] for f in files]
        else:
            dur = {f['path']: probe_duration(ffprobe, f['path']) for f in files}
            files.sort(key=lambda x: x['start'])
            # 时长缺失 → 保守地每段独立，避免误合并
            if any(dur.get(f['path'], 0) <= 0 for f in files):
                log('[merge] %s 有文件取不到时长，按独立场次处理（不合并）' % d, 'WARN')
                chains = [[f] for f in files]
            else:
                chains, cur = [], [files[0]]
                for nxt in files[1:]:
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
            sessions.append(_mk_session(live_date, first['title'], first['hour'],
                                        [f['path'] for f in chain], idx))
    return sessions


def _parse_first(d):
    """降级模式：取目录里按名排序的第一个媒体文件（保持旧行为）。"""
    dp = os.path.join(LOCAL_ROOT, d)
    m = DIR_RE.match(d)
    if not m:
        return None
    live_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for fn in sorted(os.listdir(dp)):
        if fn.lower().endswith(('.mp4', '.flv', '.mkv', '.mov')):
            src = os.path.join(dp, fn)
            mm = NAME_RE.match(fn)
            if mm:
                hour = int(mm.group(3)[:2]); clean_title = mm.group(5).strip()
            else:
                clean_title = re.sub(r'\.\w+$', '', fn).strip(); hour = 20
            return (live_date, clean_title, hour, src)
    return None


def _mk_session(live_date, title, hour, member_files, chain_index):
    return {
        'live_date': live_date,
        'clean_title': title,
        'hour': hour,
        'member_files': member_files,
        'is_merged': len(member_files) > 1,
        'chain_index': chain_index,
        'merged_src': None,
    }


def session_key(live_date, chain_index):
    ymd = live_date.strftime('%Y%m%d')
    return 'LOCAL-%s' % ymd if chain_index == 0 else 'LOCAL-%s-%d' % (ymd, chain_index + 1)


def session_fname(session):
    ld = session['live_date']
    ymd = ld.strftime('%Y%m%d')
    cn = ld.strftime('%Y年%m月%d日')
    suffix = '' if session['chain_index'] == 0 else '（第%d场）' % (session['chain_index'] + 1)
    stem = '%s-%s %s%d点场%s' % (ymd, session['clean_title'], cn, session['hour'], suffix)
    return stem + '.txt'


def prepare_session(cfg, log, session):
    """为 session 准备好可用于转写的单一视频源（merged_src）。
    单文件：直接指向原文件；多文件连续：合并到 merged 工作目录（幂等）。"""
    if not session['is_merged']:
        session['merged_src'] = session['member_files'][0]
        return session['merged_src']
    merged_dir = os.path.join(cfg['_dirs']['root'], 'merged')
    os.makedirs(merged_dir, exist_ok=True)
    first_base = re.sub(r'\.\w+$', '', os.path.basename(session['member_files'][0]))
    out_path = os.path.join(merged_dir, first_base + '_merged.mp4')
    if os.path.exists(out_path) and os.path.getsize(out_path) > 1024 * 1024:
        log('[merge] 合并视频已存在，跳过重新合并：%s' % os.path.basename(out_path))
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
    """把多段连续录播合并为一个视频。优先 concat demuxer（极快、不重编码）；
    若容器/编码不一致导致失败，回退到 concat filter 重编码（慢但稳）。"""
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
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0 and (probe_duration(ffprobe, out_path) or 0) > 0:
            return out_path
    except Exception as e:
        log('[merge] concat demuxer 失败（%s），尝试重编码合并' % e, 'WARN')
    finally:
        try:
            os.remove(list_path)
        except Exception:
            pass
    # 回退：重编码合并（同 recorder 通常命中上面的快路径，这里兜底）
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
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0 and (probe_duration(ffprobe, out_path) or 0) > 0:
            return out_path
    except Exception as e2:
        log.error('[merge] 重编码合并也失败：%s' % e2)
    return None


def _real_live_date(k, v):
    """从 key 或字段里还原真实直播日期。LOCAL-YYYYMMDD 占位被故意把 date 推高一天，
    因此真实日期应从 key 解析；BV 条目的 date/live_date 为真实直播日期。"""
    m = re.match(r'^LOCAL-(\d{8})', k)
    if m:
        y = m.group(1)
        return '%s-%s-%s' % (y[:4], y[4:6], y[6:])
    return v.get('live_date') or v.get('date')


def already_done(st, session):
    key = session_key(session['live_date'], session['chain_index'])
    if key in st.get('processed', {}):
        return True, '%s 占位已存在' % key
    # 精确 TXT 命中（仅本 session 的 fname），避免误吞同一日期的其它场次
    fname = session_fname(session)
    txt_dir = st.get('_dirs', {}).get('txt') or os.path.join(WORK, 'TXT')
    if os.path.isdir(txt_dir) and os.path.exists(os.path.join(txt_dir, fname)):
        return True, '本场 TXT 已存在（可能已处理/并行处理）'
    return False, ''


def extract_audio(cfg, log, src, mp3_path):
    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000:
        log('音频已存在，跳过抽取')
        return
    ffmpeg = cfg['paths']['ffmpeg']
    log('开始抽取音频：%s' % os.path.basename(src))
    t0 = time.time()
    subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                   '-i', src, '-vn', '-ac', '1', '-ar', '16000', mp3_path], check=True)
    log('  音频抽取完成（%.0fs）' % (time.time() - t0))


def summarize_local_one(cfg, st, session, log, progress):
    live_date = session['live_date']
    clean_title = session['clean_title']
    hour = session['hour']
    ymd = live_date.strftime('%Y%m%d')
    cn_date = live_date.strftime('%Y年%m月%d日')
    fname = session_fname(session)
    stem = fname[:-4]  # 去掉 .txt
    date_iso = live_date.isoformat()
    src = session['merged_src']
    if not src or not os.path.exists(src):
        log.error('session 缺少可用的合并源文件，跳过')
        return False
    mp3_path = os.path.join(cfg['_dirs']['mp3'], stem + '.mp3')
    txt_path = os.path.join(cfg['_dirs']['txt'], fname)
    srt_path = os.path.join(cfg['_dirs']['srt'], fname.replace('.txt', '.srt'))

    extract_audio(cfg, log, src, mp3_path)
    if not (os.path.exists(txt_path) and os.path.getsize(txt_path) > 200 and os.path.exists(srt_path)):
        log('加载 FunASR 模型并转写 ...')
        model = T.load_model(cfg, log)
        T.transcribe_one(cfg, mp3_path, model, log)
    else:
        log('转写结果已存在，跳过')

    if cfg.get('hotwords', {}).get('enabled', True):
        if os.path.exists(txt_path):
            hotwords.apply_to_file(txt_path, cfg, log)
        if os.path.exists(srt_path):
            hotwords.apply_to_file(srt_path, cfg, log)

    with open(R.PROMPT_ORIGINAL, 'r', encoding='utf-8') as f:
        sys_prompt = f.read().strip()
    font = cfg['output'].get('font', '思源宋体CN Light')
    rot = R.GeminiRotator(R.API_KEY, R.PROXY, R.MODELS, rpm=5, window=60,
                          max_per_model=20, log=log)
    ok = R.process_session(rot, sys_prompt, cfg, fname, date_iso, font, log, progress, src_path=src)
    if not ok:
        log.error('本地场次 %s 总结未完全成功（可能配额耗尽），未写占位，可重跑' % stem)
        return False

    # date 推高一天：确保 baseline_ld > live_date，挡住 B站日后上传同场
    next_day = (live_date + datetime.timedelta(days=1)).isoformat()
    key = session_key(live_date, session['chain_index'])
    n_parts = len(session['member_files'])
    merge_note = '合并 %d 段连续录播后' % n_parts if session['is_merged'] else ''
    st['processed'][key] = {
        'stages': ['transcribe', 'summarize', 'cleanup'],
        'title': '%s %s%d点场' % (clean_title, cn_date, hour),
        'date': next_day,
        'live_date': date_iso,
        'stem': stem,
        'doc': S.doc_path_for(cfg, live_date),
        'local': True,
        'merged': session['is_merged'],
        'parts': n_parts,
        'members': [os.path.basename(p) for p in session['member_files']],
        'note': '本地录播优先扫描自动处理（%s%s），已写占位使自动化跳过'
                % (merge_note, os.path.basename(src)),
    }
    st.setdefault('runs', []).append({
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'ok': 1, 'failed': [], 'seconds': 0,
        'note': '本地优先扫描处理 %s' % stem,
    })
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)
    log('已写 state 占位 %s（date=%s），自动化将跳过此场' % (key, next_day))

    # --- 总结后导出中质量 MP3 归档（按 config.mp3_export；与 16k 临时 MP3 无关）---
    mp3_export.export_for_local(cfg, log, src)

    try:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
    except Exception:
        pass
    # 合并产出的中间视频：按配置决定是否保留
    mc = cfg.get('local_merge') or {}
    if not mc.get('keep_merged', True) and session['is_merged'] and src and os.path.exists(src):
        try:
            os.remove(src)
            log('[merge] 已删除中间合并视频（keep_merged=false）')
        except Exception:
            pass
    try:
        RD.reorder()
    except Exception as e:
        log('重排 Word 失败（不影响总结）：%s' % e)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()

    LOCK = os.path.join(WORK, '.local_priority.lock')
    HB = os.path.join(WORK, '.local_priority.heartbeat')

    def pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)  # Windows: signal 0 仅探测进程是否存在
            return True
        except (ProcessLookupError, OSError):
            return False

    def grab():
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except OSError:
            # 锁已存在：判定持有者是否仍存活
            try:
                pid = int(open(LOCK, encoding='utf-8').read().strip() or '0')
            except Exception:
                pid = 0
            if pid_alive(pid):
                return False  # 确有实例在跑
            # 持有者已死（残留锁，通常因 sandbox 删文件受限删不掉）：覆盖夺回
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
        print('[local_priority] 已有实例运行，退出')
        sys.exit(0)

    def touch():
        try:
            with open(HB, 'w', encoding='utf-8') as f:
                f.write(str(int(time.time())))
        except Exception:
            pass

    try:
        cfg = load_config(os.path.join(WORK, 'config.json'))
        log = Logger(cfg, 'local_priority')
        if not args.dry_run and not wait_network(log, cfg):
            sys.exit(3)
        st = load_state(cfg)

        sessions = scan_sessions(cfg, log, LOCAL_ROOT)

        to_process, skipped = [], []
        for s in sessions:
            done, reason = already_done(st, s)
            if done:
                skipped.append((s, reason))
            else:
                to_process.append(s)

        log('本地优先扫描：发现 %d 个待处理会话（来自本地录播目录），将处理 %d，跳过 %d'
            % (len(sessions), len(to_process), len(skipped)))
        for s in skipped:
            log('  跳过 %s：%s' % (s[0]['live_date'].isoformat(), s[1]))
        for s in to_process:
            parts = len(s['member_files'])
            tag = ('合并 %d 段连续录播' % parts) if s['is_merged'] else '单文件'
            log('  将处理 %s %s [%s] (%s)'
                % (s['live_date'].isoformat(), s['clean_title'], tag,
                   ', '.join(os.path.basename(p) for p in s['member_files'])))

        if args.dry_run:
            log('dry-run，未实际处理。')
            return

        progress = {}
        for s in to_process:
            touch()
            src = prepare_session(cfg, log, s)
            if not src:
                log.error('准备源文件失败，跳过：%s' % s['live_date'].isoformat())
                continue
            try:
                summarize_local_one(cfg, st, s, log, progress)
            except Exception as ex:
                log.error('处理 %s 失败：%s' % (s['live_date'].isoformat(), ex))
            time.sleep(int(cfg.get('summarize', {}).get('inter_video_delay_sec', 15)))
        log('本地优先扫描完成')
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
