#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_local_813_815.py —— 本地录播 8/13 + 8/15 总结驱动（复用 GeminiRotator，首选 3.7-flash）。

背景：8/15 之前那次后台任务丢失、零产物，需重跑；8/13 是本次新增需求。
两场都在本地 mp4，B站尚未上传、无 bvid，故走「本地提供」路径，并在 state 写入占位条目
把基线日期顶高，使日后 B站若上传也不会被自动化重复下载/总结。

顺序：先 8/15 再 8/13，最后调用 reorder_doc.py 按直播日期重排 Word 章节。
（同进程顺序执行，避免两个脚本并发写同一 docx 触发写锁超时导致某一场被跳过。）

每场流程：ffmpeg 抽音频 → FunASR 带时间轴转写(TXT/SRT) → 热词替换 →
          Gemini 30 分钟切块总结(模型轮换+限流) → 追加到桌面 Word → 更新 state 防重复。
"""
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
from common import load_config, Logger, load_state, save_state

WORK = R.WORK
LOCK = os.path.join(WORK, '.run_local_813_815.lock')
HEARTBEAT = os.path.join(WORK, '.run_local_813_815.heartbeat')
PROGRESS = os.path.join(WORK, '.run_local_813_815_progress.json')
REORDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'reorder_doc.py')

# 任务定义：(mp4, stem(无扩展名), date, state_key, state_date, title)
JOBS = [
    dict(
        mp4=r'D:\录播\25788785-岁己SUI\2026-08-15\录制-25788785-20260815-200622-302-陪你这个猪过周六！.mp4',
        stem='20260815-陪你这个猪过周六！ 2026年08月15日20点场',
        date='2026-08-15',
        state_key='LOCAL-20260815',
        state_date='2026-08-16',
        title='陪你这个猪过周六！ 2026年08月15日20点场',
    ),
    dict(
        mp4=r'D:\录播\25788785-岁己SUI\2026-08-13\25788785-20260813-200106-472-陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥.mp4',
        stem='20260813-陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥 2026年08月13日20点场',
        date='2026-08-13',
        state_key='LOCAL-20260813',
        state_date='2026-08-14',
        title='陪你这个猪过周四！-陪你这个猪看滴嘟虾∥鱼粥 2026年08月13日20点场',
    ),
]


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


def extract_audio(cfg, log, mp3_path, mp4):
    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 1000:
        log(f'音频已存在，跳过抽取：{os.path.basename(mp3_path)}')
        return
    ffmpeg = cfg['paths']['ffmpeg']
    log(f'开始抽取音频：{os.path.basename(mp4)}')
    t0 = time.time()
    subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                   '-i', mp4, '-vn', '-ac', '1', '-ar', '16000', mp3_path],
                  check=True)
    log(f'  音频抽取完成（{time.time() - t0:.0f}s）：{mp3_path}')


def transcribe(cfg, log, mp3_path, fname):
    txt_path = os.path.join(cfg['_dirs']['txt'], fname)
    srt_path = os.path.join(cfg['_dirs']['srt'], fname.replace('.txt', '.srt'))
    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 200 and os.path.exists(srt_path):
        log(f'转写结果已存在，跳过：{fname}')
        return
    log('加载 FunASR 模型并转写 ...')
    model = T.load_model(cfg, log)
    T.transcribe_one(cfg, mp3_path, model, log)


def run_one(job, cfg, log, rot, font, sys_prompt, progress):
    FNAME = job['stem'] + '.txt'
    DATE = job['date']
    MP4 = job['mp4']
    txt_path = os.path.join(WORK, 'TXT', FNAME)
    srt_path = os.path.join(WORK, 'SRT', FNAME.replace('.txt', '.srt'))
    mp3_path = os.path.join(cfg['_dirs']['mp3'], job['stem'] + '.mp3')

    touch()
    extract_audio(cfg, log, mp3_path, MP4)
    touch()
    transcribe(cfg, log, mp3_path, FNAME)
    touch()

    if cfg.get('hotwords', {}).get('enabled', True):
        if os.path.exists(txt_path):
            hotwords.apply_to_file(txt_path, cfg, log)
        if os.path.exists(srt_path):
            hotwords.apply_to_file(srt_path, cfg, log)

    touch()
    ok = R.process_session(rot, sys_prompt, cfg, FNAME, DATE, font, log, progress)
    if not ok:
        log.error(f"[{job['date']}] 总结未完全成功（可能配额耗尽），未更新 state，可重跑续跑")
        return False

    st = load_state(cfg)
    doc_path = S.doc_path_for(cfg, datetime.date.fromisoformat(DATE))
    st['processed'][job['state_key']] = {
        'stages': ['transcribe', 'summarize', 'cleanup'],
        'title': job['title'],
        'date': job['state_date'],
        'stem': job['stem'],
        'doc': doc_path,
        'local': True,
        'note': f"本地 mp4 手动提供（{MP4}），未从B站下载；B站尚未上传故无bvid，"
                f"已由基线日期挡住日后重抓（state date={job['state_date']}）",
    }
    st.setdefault('runs', []).append({
        'at': datetime.datetime.now().isoformat(timespec='seconds'),
        'ok': 1, 'failed': [], 'seconds': 0,
        'note': f"{job['date']} 本地录播 Gemini 总结（模型轮换+限流）",
    })
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)
    log(f"已更新 state.json：{job['date']} 标记为已处理（防重复下载/总结）")

    try:
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
            log(f'已清理中间 mp3：{os.path.basename(mp3_path)}')
    except Exception as e:
        log(f'清理 mp3 失败（不影响结果）：{e}')
    return True


def main():
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
            print('[run_local_813_815] 已有实例在运行（心跳有效），退出')
            sys.exit(0)
        for p in (LOCK, HEARTBEAT):
            try:
                os.remove(p)
            except OSError:
                pass
        if not grab():
            print('[run_local_813_815] 锁仍被占用，退出')
            sys.exit(0)
    touch()

    cfg = load_config(os.path.join(WORK, 'config.json'))
    log = Logger(cfg, 'run_local_813_815')
    font = cfg['output'].get('font', '思源宋体CN Light')
    with open(R.PROMPT_ORIGINAL, 'r', encoding='utf-8') as f:
        sys_prompt = f.read().strip()

    rot = R.GeminiRotator(R.API_KEY, R.PROXY, R.MODELS, rpm=5, window=60,
                          max_per_model=20, log=log)
    progress = load_progress()

    results = {}
    for job in JOBS:
        touch()
        try:
            ok = run_one(job, cfg, log, rot, font, sys_prompt, progress)
            results[job['date']] = ok
        except Exception as e:
            import traceback
            log.error(f"{job['date']} 异常：{e}")
            traceback.print_exc()
            results[job['date']] = False
        finally:
            touch()

    # 无论两场是否都成功，都尝试按日期重排 Word（缺场也不影响已写入的章节）
    try:
        touch()
        log('开始按日期重排 Word 章节 ...')
        subprocess.run([sys.executable, REORDER], check=True)
        log('Word 章节重排完成')
    except Exception as e:
        log.error(f'Word 重排失败：{e}')

    for p in (LOCK, HEARTBEAT):
        try:
            os.remove(p)
        except OSError:
            pass

    print('[run_local_813_815] DONE |', {k: ('OK' if v else 'INCOMPLETE') for k, v in results.items()})


if __name__ == '__main__':
    main()
