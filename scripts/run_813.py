#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_813.py —— 8/13 本地录播总结驱动（复用 regen_gemini_v2 的 Gemini 轮换逻辑）。

前置：本地 mp4 在 D:\\录播\\25788785-岁己SUI\\2026-08-13\\
本脚本会自己完成：ffmpeg 抽音频 → FunASR 带时间轴转写(TXT/SRT) → 热词替换 →
Gemini 30 分钟切块总结(首选 3.7-flash，轮换+限流) → 追加到桌面 Word →
更新 state 防重复 → 调用 reorder_doc 按日期重排整篇（8/13 插到 8/12 后、8/15 前）。

用法:
  python run_813.py
  python run_813.py --test-one   # 只跑第 1 块并打印原始输出，用于格式核验
"""

import argparse
import datetime
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regen_gemini_v2 as R
import summarize_gemini as S
import hotwords
import transcribe as T
import reorder_doc as RD
from common import load_config, Logger, load_state, save_state

WORK = R.WORK
MP4 = r'D:\录播\25788785-岁己SUI\2026-08-13\25788785-20260813-200106-472-陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥.mp4'
STEM = '20260813-陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥 2026年08月13日20点场'
FNAME = STEM + '.txt'
DATE = '2026-08-13'
LOCK = os.path.join(WORK, '.run_813.lock')
HEARTBEAT = os.path.join(WORK, '.run_813.heartbeat')
PROGRESS = os.path.join(WORK, '.run_813_progress.json')


def touch():
    try:
        with open(HEARTBEAT, 'w', encoding='utf-8') as f:
            f.write(str(int(time.time())))
    except Exception:
        pass


def load_progress():
    if os.path.exists(PROGRESS):
        try:
            with open(PROGRESS, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_progress(d):
    tmp = PROGRESS + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, PROGRESS)


def extract_audio(cfg, log, mp3_path):
    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000:
        log(f'音频已存在，跳过抽取：{os.path.basename(mp3_path)}')
        return
    ffmpeg = cfg['paths']['ffmpeg']
    log(f'开始抽取音频：{os.path.basename(MP4)}')
    t0 = time.time()
    subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                   '-i', MP4, '-vn', '-ac', '1', '-ar', '16000', mp3_path],
                  check=True)
    log(f'  音频抽取完成（{time.time() - t0:.0f}s）：{mp3_path}')


def transcribe(cfg, log, mp3_path):
    txt_path = os.path.join(cfg['_dirs']['txt'], FNAME)
    srt_path = os.path.join(cfg['_dirs']['srt'], FNAME.replace('.txt', '.srt'))
    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 200 and os.path.exists(srt_path):
        log(f'转写结果已存在，跳过：{STEM}')
        return
    log('加载 FunASR 模型并转写 ...')
    model = T.load_model(cfg, log)
    T.transcribe_one(cfg, mp3_path, model, log)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--test-one', action='store_true')
    args = ap.parse_args()

    # 单实例锁（心跳机制，抗 Windows PID 复用 / 环境回收）
    def holder_alive():
        if not os.path.exists(LOCK):
            return False
        try:
            age = time.time() - float(open(HEARTBEAT, encoding='utf-8').read().strip() or '0')
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
        if holder_alive():
            print('[run_813] 已有实例在运行（心跳有效），退出')
            sys.exit(0)
        for p in (LOCK, HEARTBEAT):
            try:
                os.remove(p)
            except OSError:
                pass
        if not grab():
            print('[run_813] 锁仍被占用，退出')
            sys.exit(0)
    touch()

    cfg = load_config(os.path.join(WORK, 'config.json'))
    log = Logger(cfg, 'run_813')
    font = cfg['output'].get('font', '思源宋体CN Light')
    with open(R.PROMPT_ORIGINAL, 'r', encoding='utf-8') as f:
        sys_prompt = f.read().strip()

    rot = R.GeminiRotator(R.API_KEY, R.PROXY, R.MODELS, rpm=5, window=60,
                          max_per_model=20, log=log)
    progress = load_progress()
    txt_path = os.path.join(WORK, 'TXT', FNAME)
    srt_path = os.path.join(WORK, 'SRT', FNAME.replace('.txt', '.srt'))
    mp3_path = os.path.join(cfg['_dirs']['mp3'], STEM + '.mp3')

    if args.test_one:
        extract_audio(cfg, log, mp3_path)
        transcribe(cfg, log, mp3_path)
        chunks = S.load_and_chunk(txt_path, 30 * 60)
        c0 = chunks[0]
        label = f'{S.hms(c0[0])} - {S.hms(c0[0] + 30 * 60)}'
        user = R.build_user(label, 1, len(chunks), c0[1])
        out = rot.call(sys_prompt, user, tag='[TEST]')
        print('\n========== RAW OUTPUT (8/13 block1) ==========')
        print(out)
        print('========== END ==========')
        try:
            os.remove(LOCK)
            os.remove(HEARTBEAT)
        except Exception:
            pass
        sys.exit(0)

    # --- 抽音频 + 转写 ---
    touch()
    extract_audio(cfg, log, mp3_path)
    touch()
    transcribe(cfg, log, mp3_path)
    touch()

    # --- 热词替换（转写后、送 AI 前）---
    if cfg.get('hotwords', {}).get('enabled', True):
        if os.path.exists(txt_path):
            hotwords.apply_to_file(txt_path, cfg, log)
        if os.path.exists(srt_path):
            hotwords.apply_to_file(srt_path, cfg, log)

    ok = False
    try:
        touch()
        ok = R.process_session(rot, sys_prompt, cfg, FNAME, DATE, font, log, progress)
    finally:
        try:
            os.remove(LOCK)
        except Exception:
            pass
        try:
            os.remove(HEARTBEAT)
        except Exception:
            pass

    if not ok:
        log.error('[run_813] 8/13 总结未完全成功（可能配额耗尽），未更新 state，可重跑续跑')
        print('[run_813] INCOMPLETE')
        sys.exit(1)

    # --- 成功后更新 state.json：标记 8/13 已处理，阻止日后重复下载/总结 ---
    st = load_state(cfg)
    doc_path = S.doc_path_for(cfg, datetime.date(2026, 8, 13))
    st['processed']['LOCAL-20260813'] = {
        'stages': ['transcribe', 'summarize', 'cleanup'],
        'title': '陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥 2026年08月13日20点场',
        # 真实日期 2026-08-13。当前基线已由 8/15 顶到 2026-08-15，
        # 8/13 的 live_date=2026-08-13 < 基线，自动化不会被选中；加此占位明确"已本地处理"。
        'date': '2026-08-13',
        'stem': '20260813-陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥 2026年08月13日20点场',
        'doc': doc_path,
        'local': True,
        'note': '本地 mp4 手动提供（D:/录播/25788785-岁己SUI/2026-08-13/25788785-20260813-...-陪你这个猪过周四！-...∥鱼粥.mp4），'
                '未从B站下载；自动化基线(8/15)已高于本场，不会被重抓',
    }
    st.setdefault('runs', []).append({
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'ok': 1, 'failed': [], 'seconds': 0,
        'note': '8/13 本地录播 Gemini 总结（模型轮换+限流）',
    })
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)
    log('已更新 state.json：8/13 标记为已处理（防重复下载/总结）')

    # --- 按日期重排整篇 Word（8/13 插到 8/12 后、8/15 前）---
    try:
        RD.reorder()
        log('已按日期重排 Word 章节')
    except Exception as e:
        log(f'重排 Word 失败（不影响总结结果）：{e}')

    # --- 清理抽取出的 mp3（源 mp4 留在 D:\录播，TXT/SRT 保留）---
    try:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
            log(f'已清理中间 mp3：{os.path.basename(mp3_path)}')
    except Exception as e:
        log(f'清理 mp3 失败（不影响结果）：{e}')

    print('[run_813] DONE 8/13 总结完成并记入 state.json，已重排 Word')


if __name__ == '__main__':
    main()
