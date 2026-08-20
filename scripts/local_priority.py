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
from common import load_config, Logger, load_state, save_state

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


def parse_entry(dirpath):
    """解析一个本地日期目录，返回 (live_date, clean_title, hour, src_path) 或 None"""
    d = os.path.basename(dirpath.rstrip('/\\'))
    m = DIR_RE.match(d)
    if not m:
        return None
    live_date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for fn in sorted(os.listdir(dirpath)):
        if fn.lower().endswith(('.mp4', '.flv', '.mkv', '.mov')):
            src = os.path.join(dirpath, fn)
            mm = NAME_RE.match(fn)
            if mm:
                hour = int(mm.group(3)[:2])
                clean_title = mm.group(5).strip()
            else:
                clean_title = re.sub(r'\.\w+$', '', fn).strip()
                hour = 20
            return (live_date, clean_title, hour, src)
    return None


def _real_live_date(k, v):
    """从 key 或字段里还原真实直播日期。LOCAL-YYYYMMDD 占位被故意把 date 推高一天，
    因此真实日期应从 key 解析；BV 条目的 date/live_date 为真实直播日期。"""
    m = re.match(r'^LOCAL-(\d{8})', k)
    if m:
        y = m.group(1)
        return '%s-%s-%s' % (y[:4], y[4:6], y[6:])
    return v.get('live_date') or v.get('date')


def already_done(st, live_date, clean_title):
    ymd = live_date.strftime('%Y%m%d')
    cn_date = live_date.strftime('%Y年%m月%d日')
    if ('LOCAL-%s' % ymd) in st.get('processed', {}):
        return True, 'LOCAL-%s 占位已存在' % ymd
    for k, v in st.get('processed', {}).items():
        # 注意：LOCAL-YYYYMMDD 占位里的 date 被推高一天，不能用它做同日期判定
        if _real_live_date(k, v) == live_date.isoformat():
            return True, 'state 已有同日期条目 %s' % k
        if cn_date in (v.get('title') or ''):
            return True, 'state 已有同直播日期条目 %s' % k
    # 本地 TXT 已存在（可能正被其它脚本处理，避免重复转写）
    txt_dir = st.get('_dirs', {}).get('txt') or os.path.join(WORK, 'TXT')
    if os.path.isdir(txt_dir):
        for fn in os.listdir(txt_dir):
            if fn.startswith(ymd + '-') and fn.endswith('.txt'):
                return True, '本地 TXT 已存在（可能正在处理）'
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


def summarize_local_one(cfg, st, entry, log, progress):
    live_date, clean_title, hour, src = entry
    ymd = live_date.strftime('%Y%m%d')
    cn_date = live_date.strftime('%Y年%m月%d日')
    stem = '%s-%s %s%d点场' % (ymd, clean_title, cn_date, hour)
    fname = stem + '.txt'
    date_iso = live_date.isoformat()
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
    st['processed']['LOCAL-%s' % ymd] = {
        'stages': ['transcribe', 'summarize', 'cleanup'],
        'title': '%s %s%d点场' % (clean_title, cn_date, hour),
        'date': next_day,
        'live_date': date_iso,
        'stem': stem,
        'doc': S.doc_path_for(cfg, live_date),
        'local': True,
        'note': '本地录播优先扫描自动处理（%s），已写占位使自动化跳过' % os.path.basename(src),
    }
    st.setdefault('runs', []).append({
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'ok': 1, 'failed': [], 'seconds': 0,
        'note': '本地优先扫描处理 %s' % stem,
    })
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)
    log('已写 state 占位 LOCAL-%s（date=%s），自动化将跳过此场' % (ymd, next_day))

    # --- 总结后导出中质量 MP3 归档（按 config.mp3_export；与 16k 临时 MP3 无关）---
    mp3_export.export_for_local(cfg, log, src)

    try:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
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

    def alive():
        if not os.path.exists(LOCK):
            return False
        try:
            age = time.time() - float(open(HB, encoding='utf-8').read().strip() or '0')
        except Exception:
            age = 99999
        return age < 240

    def grab():
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except OSError:
            return False

    if not grab():
        if alive():
            print('[local_priority] 已有实例运行，退出')
            sys.exit(0)
        for p in (LOCK, HB):
            try:
                os.remove(p)
            except OSError:
                pass
        if not grab():
            print('[local_priority] 锁占用，退出')
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

        entries = []
        if os.path.isdir(LOCAL_ROOT):
            for d in sorted(os.listdir(LOCAL_ROOT)):
                dp = os.path.join(LOCAL_ROOT, d)
                if not os.path.isdir(dp):
                    continue
                e = parse_entry(dp)
                if not e:
                    continue
                entries.append(e)

        to_process, skipped = [], []
        for e in entries:
            live_date, clean_title, hour, src = e
            done, reason = already_done(st, live_date, clean_title)
            if done:
                skipped.append((e, reason))
            else:
                to_process.append(e)

        log('本地优先扫描：发现 %d 个本地场次，将处理 %d，跳过 %d'
            % (len(entries), len(to_process), len(skipped)))
        for e in skipped:
            log('  跳过 %s：%s' % (e[0][0].isoformat(), e[1]))
        for e in to_process:
            log('  将处理 %s %s (%s)' % (e[0].isoformat(), e[1], os.path.basename(e[3])))

        if args.dry_run:
            log('dry-run，未实际处理。')
            return

        progress = {}
        for e in to_process:
            touch()
            try:
                summarize_local_one(cfg, st, e, log, progress)
            except Exception as ex:
                log.error('处理 %s 失败：%s' % (e[0].isoformat(), ex))
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
