#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 2：下载 B站录播音频，多分P自动合并为单个 MP3。

流程：view API 取全部分P → playurl 取 DASH 最高码率音轨 → aria2c 16线程下载
      → ffmpeg 逐P转 m4a → 多P则 concat 合并 → 统一输出单个 MP3（16kHz 单声道）

用法:
  python download_merge.py --bvid BV1iL3m6oEyU
  python download_merge.py --pending WORKDIR/tmp/pending.json
  python download_merge.py --bvid BVxxx --max-seconds 600   # 只取前10分钟（联调用）
"""

import argparse
import json
import os
import subprocess
import sys
import time

from common import (load_config, load_cookies, bili_get, fetch_view, Logger,
                    stem_for, probe_duration, hms)


def run(cmd, timeout=1800):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=timeout)


def get_audio_url(bvid, cid, cookies, log):
    """取 DASH 音轨里码率最高的一条。"""
    for attempt in range(3):
        d = bili_get('https://api.bilibili.com/x/player/playurl',
                     {'bvid': bvid, 'cid': cid, 'qn': 0, 'fnval': 16, 'fnver': 0, 'fourk': 1},
                     cookies)
        if d.get('code') != 0:
            log.error(f'playurl code={d.get("code")} {d.get("message")}')
            time.sleep(5 * (2 ** attempt))
            continue
        dash = (d.get('data') or {}).get('dash') or {}
        audios = list(dash.get('audio') or [])
        # flac / dolby 兜底
        if dash.get('flac') and dash['flac'].get('audio'):
            audios.append(dash['flac']['audio'])
        if not audios:
            log.error('该分P没有可用音轨')
            return None
        best = max(audios, key=lambda x: x.get('bandwidth', 0))
        return best['baseUrl']
    return None


def aria2_download(aria2c, url, out_dir, out_name, log, retries=3):
    dst = os.path.join(out_dir, out_name)
    if os.path.exists(dst) and os.path.getsize(dst) > 10240:
        log(f'  已存在，跳过下载：{out_name}')
        return dst
    for attempt in range(1, retries + 1):
        r = run([aria2c, '-x', '16', '-s', '16', '-k', '1M', '--allow-overwrite=true',
                 '--auto-file-renaming=false', '--console-log-level=warn',
                 '--header', 'Referer: https://www.bilibili.com/',
                 '--header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                 '-d', out_dir, '-o', out_name, url], timeout=3600)
        if os.path.exists(dst) and os.path.getsize(dst) > 10240:
            return dst
        log.error(f'  下载失败（第 {attempt}/{retries} 次）：{(r.stderr or r.stdout or "")[-300:]}')
        if attempt < retries:
            time.sleep(5 * (2 ** attempt))
    return None


def to_m4a(ffmpeg, src, dst, log):
    r = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
             '-i', src, '-vn', '-c:a', 'copy', '-movflags', '+faststart', dst], timeout=900)
    if os.path.exists(dst) and os.path.getsize(dst) > 10240:
        return dst
    log.error(f'  转 m4a 失败：{(r.stderr or "")[-300:]}')
    return None


def concat_to_mp3(ffmpeg, parts, dst, log, max_seconds=None):
    """多个 m4a 合并为单个 MP3。优先 concat demuxer，失败则退回逐段解码拼接。"""
    work = os.path.dirname(dst)
    lst = os.path.join(work, '_concat_list.txt')
    with open(lst, 'w', encoding='utf-8') as f:
        for p in parts:
            f.write("file '" + p.replace('\\', '/').replace("'", "'\\''") + "'\n")

    base = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', lst]
    tail = ['-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '64k']
    if max_seconds:
        tail = ['-t', str(int(max_seconds))] + tail
    r = run(base + tail + [dst], timeout=7200)
    if os.path.exists(dst) and os.path.getsize(dst) > 10240:
        try:
            os.remove(lst)
        except Exception:
            pass
        return dst

    log.error(f'  concat 合并失败，改用逐段解码拼接：{(r.stderr or "")[-300:]}')
    # 兜底：每段解码成 wav 再拼
    wavs = []
    for i, p in enumerate(parts):
        w = os.path.join(work, f'_part{i:02d}.wav')
        run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', p,
             '-vn', '-ac', '1', '-ar', '16000', w], timeout=3600)
        if os.path.exists(w):
            wavs.append(w)
    if not wavs:
        return None
    with open(lst, 'w', encoding='utf-8') as f:
        for w in wavs:
            f.write("file '" + w.replace('\\', '/') + "'\n")
    r = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'concat', '-safe', '0',
             '-i', lst] + (['-t', str(int(max_seconds))] if max_seconds else []) +
            ['-c:a', 'libmp3lame', '-b:a', '64k', dst], timeout=7200)
    for w in wavs:
        try:
            os.remove(w)
        except Exception:
            pass
    try:
        os.remove(lst)
    except Exception:
        pass
    return dst if os.path.exists(dst) and os.path.getsize(dst) > 10240 else None


def single_to_mp3(ffmpeg, src, dst, log, max_seconds=None):
    cmd = [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src]
    if max_seconds:
        cmd += ['-t', str(int(max_seconds))]
    cmd += ['-vn', '-ac', '1', '-ar', '16000', '-c:a', 'libmp3lame', '-b:a', '64k', dst]
    r = run(cmd, timeout=7200)
    if os.path.exists(dst) and os.path.getsize(dst) > 10240:
        return dst
    log.error(f'  转 MP3 失败：{(r.stderr or "")[-300:]}')
    return None


def download_one(cfg, video, log, max_seconds=None):
    """下载单期录播 → 返回合并好的 MP3 路径。"""
    P = cfg['paths']
    cookies = load_cookies(P.get('cookies'))
    bvid = video['bvid']
    stem = video.get('stem') or stem_for(video.get('pubdate', 0), video['title'])
    mp3_path = os.path.join(cfg['_dirs']['mp3'], stem + '.mp3')

    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 10240:
        log(f'MP3 已存在，跳过下载：{stem}')
        return mp3_path

    info = fetch_view(bvid, cookies)
    pages = info.get('pages') or []
    log(f'{bvid} 共 {len(pages)} 个分P：{info.get("title","")[:50]}')

    work = os.path.join(cfg['_dirs']['tmp'], bvid)
    os.makedirs(work, exist_ok=True)

    parts = []
    for pi, page in enumerate(pages, 1):
        cid = page['cid']
        log(f'  P{pi}/{len(pages)}  {hms(page.get("duration", 0))}  {page.get("part","")[:40]}')
        url = get_audio_url(bvid, cid, cookies, log)
        if not url:
            log.error(f'  P{pi} 取流失败，整期中止')
            return None
        raw = aria2_download(P['aria2c'], url, work, f'p{pi:02d}.m4s', log)
        if not raw:
            log.error(f'  P{pi} 下载失败，整期中止')
            return None
        m4a = to_m4a(P['ffmpeg'], raw, os.path.join(work, f'p{pi:02d}.m4a'), log)
        if not m4a:
            return None
        try:
            os.remove(raw)
        except Exception:
            pass
        parts.append(m4a)
        sz = os.path.getsize(m4a) / 1024 / 1024
        log(f'  P{pi} 完成 {sz:.0f}MB')

    if not parts:
        return None

    t0 = time.time()
    if len(parts) > 1:
        log(f'  合并 {len(parts)} 个分P → 单个 MP3 ...')
        out = concat_to_mp3(P['ffmpeg'], parts, mp3_path, log, max_seconds)
    else:
        log('  单P，直接转 MP3 ...')
        out = single_to_mp3(P['ffmpeg'], parts[0], mp3_path, log, max_seconds)

    if not out:
        log.error('  生成 MP3 失败')
        return None

    dur = probe_duration(P['ffprobe'], out)
    log(f'  MP3 就绪：{os.path.basename(out)}  {hms(dur)}  '
        f'{os.path.getsize(out) / 1024 / 1024:.0f}MB  耗时 {time.time() - t0:.0f}s')

    # 清理分P临时文件
    for p in parts:
        try:
            os.remove(p)
        except Exception:
            pass
    try:
        os.rmdir(work)
    except Exception:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--bvid', default=None)
    ap.add_argument('--pending', default=None)
    ap.add_argument('--max-seconds', type=int, default=None, help='只保留前 N 秒（联调用）')
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = Logger(cfg, 'download')

    if args.bvid:
        cookies = load_cookies(cfg['paths'].get('cookies'))
        info = fetch_view(args.bvid, cookies)
        videos = [{'bvid': args.bvid, 'title': info['title'], 'pubdate': info.get('pubdate', 0)}]
    else:
        path = args.pending or os.path.join(cfg['_dirs']['tmp'], 'pending.json')
        with open(path, 'r', encoding='utf-8') as f:
            videos = json.load(f)

    ok = 0
    for v in videos:
        try:
            if download_one(cfg, v, log, args.max_seconds):
                ok += 1
        except Exception as e:
            log.error(f'{v.get("bvid")} 下载异常：{e}')
    log(f'下载完成 {ok}/{len(videos)}')
    sys.exit(0 if ok == len(videos) else 1)


if __name__ == '__main__':
    main()
