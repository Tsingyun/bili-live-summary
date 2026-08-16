#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_812.py —— 8/12 本地录播总结驱动（复用 regen_gemini_v2 的 Gemini 轮换逻辑）。

前置：本地 mp4 已抽音频为 wav 并由 transcribe.py 转写为 TXT/SRT
      （命名 = 20260812-和vr大家一起玩躲猫猫！ 2026年08月12日20点场.txt）

流程：
  1. 热词替换（转写后、送 AI 前，对 TXT/SRT 各一遍）
  2. Gemini 30 分钟切块总结（模型轮换 + RPM≤5 + 429 不重试 + 断点续跑 + 心跳锁）
  3. 追加到桌面《岁己直播总结_2026年8月.docx》（章节：和vr大家一起玩躲猫猫！ 2026年08月12日20点场）
  4. 成功后更新 state.json，把 8/12 标记为已处理（date 推到 2026-08-13 以推高基线，
     阻止日后真实 8/12 上传后被重新下载/总结；B站当时尚未上传故无 bvid）

用法:
  python run_812.py
  python run_812.py --test-one   # 只跑第 1 块并打印原始输出，用于格式核验
"""

import argparse
import datetime
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import regen_gemini_v2 as R
import summarize_gemini as S
import hotwords
from common import load_config, Logger, load_state, save_state

WORK = R.WORK
FNAME = '20260812-和vr大家一起玩躲猫猫！ 2026年08月12日20点场.txt'
DATE = '2026-08-12'
LOCK = os.path.join(WORK, '.run_812.lock')
HEARTBEAT = os.path.join(WORK, '.run_812.heartbeat')
PROGRESS = os.path.join(WORK, '.run_812_progress.json')


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
            print('[run_812] 已有实例在运行（心跳有效），退出')
            sys.exit(0)
        for p in (LOCK, HEARTBEAT):
            try:
                os.remove(p)
            except OSError:
                pass
        if not grab():
            print('[run_812] 锁仍被占用，退出')
            sys.exit(0)
    touch()

    cfg = load_config(os.path.join(WORK, 'config.json'))
    log = Logger(cfg, 'run_812')
    font = cfg['output'].get('font', '思源宋体CN Light')
    with open(R.PROMPT_ORIGINAL, 'r', encoding='utf-8') as f:
        sys_prompt = f.read().strip()

    rot = R.GeminiRotator(R.API_KEY, R.PROXY, R.MODELS, rpm=5, window=60,
                          max_per_model=20, log=log)
    progress = load_progress()
    txt_path = os.path.join(WORK, 'TXT', FNAME)
    srt_path = os.path.join(WORK, 'SRT', FNAME.replace('.txt', '.srt'))

    if args.test_one:
        chunks = S.load_and_chunk(txt_path, 30 * 60)
        c0 = chunks[0]
        label = f'{S.hms(c0[0])} - {S.hms(c0[0] + 30 * 60)}'
        user = R.build_user(label, 1, len(chunks), c0[1])
        out = rot.call(sys_prompt, user, tag='[TEST]')
        print('\n========== RAW OUTPUT (8/12 block1) ==========')
        print(out)
        print('========== END ==========')
        try:
            os.remove(LOCK)
            os.remove(HEARTBEAT)
        except Exception:
            pass
        sys.exit(0)

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
        log.error('[run_812] 8/12 总结未完全成功（可能配额耗尽），未更新 state，可重跑续跑')
        print('[run_812] INCOMPLETE')
        sys.exit(1)

    # --- 成功后更新 state.json：标记 8/12 已处理，阻止日后重复下载/总结 ---
    st = load_state(cfg)
    doc_path = S.doc_path_for(cfg, datetime.date(2026, 8, 12))
    st['processed']['LOCAL-20260812-vr'] = {
        'stages': ['transcribe', 'summarize', 'cleanup'],
        'title': '和vr大家一起玩躲猫猫！ 2026年08月12日20点场',
        # 故意设为 8/13：pick_videos 的基线 = max(已处理各场日期)，
        # 推到 8/13 后真实 8/12（live_date=8/12）满足不了 _live_date >= 基线，
        # 从而不被选中。原因：B站当时尚未上传，无真实 bvid；而 done() 按 bvid 判重，
        # 无法用合成 key 命中，只能靠基线日期挡。
        'date': '2026-08-13',
        'stem': '20260812-和vr大家一起玩躲猫猫！ 2026年08月12日20点场',
        'doc': doc_path,
        'local': True,
        'note': '本地 mp4 手动提供（D:/录播/25788785-岁己SUI/2026-08-12/录制-...-和vr大家一起玩躲猫猫！.mp4），'
                '未从B站下载；B站尚未上传故无bvid，已由基线日期挡住日后重抓',
    }
    st.setdefault('runs', []).append({
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'ok': 1, 'failed': [], 'seconds': 0,
        'note': '8/12 本地录播 Gemini 总结（模型轮换+限流）',
    })
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)
    log('已更新 state.json：8/12 标记为已处理（防重复下载/总结）')
    print('[run_812] DONE 8/12 总结完成并记入 state.json')


if __name__ == '__main__':
    main()
