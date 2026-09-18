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
                    stem_for, probe_duration, hms,
                    force_unlink, force_rmtree)


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
            force_unlink(lst)
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
            force_unlink(w)
        except Exception:
            pass
    try:
        force_unlink(lst)
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


def get_video_url(bvid, cid, cookies, log):
    """取 DASH 视频流里码率最高的一条（HEVC/AVC 均可，后续统一重封装 mp4）。"""
    for attempt in range(3):
        d = bili_get('https://api.bilibili.com/x/player/playurl',
                     {'bvid': bvid, 'cid': cid, 'qn': 0, 'fnval': 16, 'fnver': 0, 'fourk': 1},
                     cookies)
        if d.get('code') != 0:
            log.error(f'playurl code={d.get("code")} {d.get("message")}')
            time.sleep(5 * (2 ** attempt))
            continue
        dash = (d.get('data') or {}).get('dash') or {}
        videos = list(dash.get('video') or [])
        if not videos:
            log.error('该分P没有可用视频流')
            return None
        best = max(videos, key=lambda x: (x.get('bandwidth', 0), x.get('height', 0)))
        return best['baseUrl']
    return None


def download_video_one(cfg, video, log, max_seconds=None):
    """下载单期录播视频（最佳 DASH 视频+音频），多P合并为单个 MP4。

    供「B站兜底」路径使用：兜底时本地没有视频文件，需要从 B站把视频拉下来，
    才能走「总结→中质量MP3→压缩HEVC」的完整交付。返回合并后的 MP4 路径；失败返回 None。
    """
    P = cfg['paths']
    cookies = load_cookies(P.get('cookies'))
    bvid = video['bvid']
    stem = video.get('stem') or stem_for(video.get('pubdate', 0), video['title'])
    final = os.path.join(cfg['_dirs']['tmp'], 'bilibili', stem + '.mp4')

    info = fetch_view(bvid, cookies)
    pages = info.get('pages') or []
    # 期望总时长：用于校验"下载+合并"产物是否完整
    # ★ 2026-09-18：旧实现只按体积 >1MB 判断"已存在可复用"。B站兜底是最容易
    #   被杀在中途的一步（下 9GB + 合并），一旦停下就会留下一个体积已经很大、
    #   时长却只有前几个 P 的残缺 MP4，下次运行直接复用 → 总结缺一半。
    expect_dur = 0
    for p in pages:
        try:
            expect_dur += float(p.get('duration') or 0)
        except Exception:
            pass

    if os.path.exists(final) and os.path.getsize(final) > 1024 * 1024:
        got = probe_duration(P['ffprobe'], final) or 0
        if got > 0 and (expect_dur <= 0 or got >= expect_dur * 0.95):
            log(f'合并视频已存在且完整，跳过下载：{stem}')
            return final
        log(f'已有合并视频不完整（{hms(got)}/期望 {hms(expect_dur)}），删除重下：{stem}',
            'WARN')
        force_unlink(final)

    log(f'{bvid} 共 {len(pages)} 个分P（视频）：{info.get("title", "")[:50]}')
    if not pages:
        log.error(f'{bvid} 没有可用分P，跳过')
        return None

    work = os.path.join(cfg['_dirs']['tmp'], 'bilibili', bvid)
    os.makedirs(work, exist_ok=True)

    parts = []
    for pi, page in enumerate(pages, 1):
        cid = page['cid']
        log(f'  P{pi}/{len(pages)}  {hms(page.get("duration", 0))}  {page.get("part", "")[:40]}')
        v_url = get_video_url(bvid, cid, cookies, log)
        a_url = get_audio_url(bvid, cid, cookies, log)
        if not v_url or not a_url:
            log.error(f'  P{pi} 取流失败，整期中止')
            return None
        v_raw = aria2_download(P['aria2c'], v_url, work, f'p{pi:02d}.video.m4s', log)
        a_raw = aria2_download(P['aria2c'], a_url, work, f'p{pi:02d}.audio.m4s', log)
        if not v_raw or not a_raw:
            log.error(f'  P{pi} 下载失败，整期中止')
            return None
        mux = os.path.join(work, f'p{pi:02d}.mp4')
        r = run([P['ffmpeg'], '-y', '-hide_banner', '-loglevel', 'error',
                 '-i', v_raw, '-i', a_raw, '-c', 'copy', mux], timeout=900)
        if not (os.path.exists(mux) and os.path.getsize(mux) > 10240):
            log.error(f'  P{pi} 封装失败：{(r.stderr or "")[-300:]}')
            return None
        try:
            force_unlink(v_raw)
            force_unlink(a_raw)
        except Exception:
            pass
        parts.append(mux)
        log(f'  P{pi} 完成 {os.path.getsize(mux) // 1024 // 1024}MB')

    if not parts:
        return None

    t0 = time.time()
    ffmpeg = P['ffmpeg']
    # 先写临时名，校验时长达标后再原子改名（进程被杀只留 .part.mp4，不会被复用）
    tmp_final = final + '.part.mp4'
    try:
        force_unlink(tmp_final)
    except Exception:
        pass
    if len(parts) > 1:
        log(f'  合并 {len(parts)} 个分P → 单个 MP4 ...')
        lst = os.path.join(work, '_concat_list.txt')
        with open(lst, 'w', encoding='utf-8') as f:
            for p in parts:
                f.write("file '" + p.replace('\\', '/').replace("'", "'\\''") + "'\n")
        r = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                 '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', tmp_final], timeout=7200)
        try:
            force_unlink(lst)
        except Exception:
            pass
    else:
        r = run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                 '-i', parts[0], '-c', 'copy', tmp_final], timeout=7200)
    if not (os.path.exists(tmp_final) and os.path.getsize(tmp_final) > 1024 * 1024):
        log.error(f'  生成合并视频失败：{(r.stderr or "")[-300:]}')
        force_unlink(tmp_final)
        return None
    got_dur = probe_duration(P['ffprobe'], tmp_final) or 0
    if expect_dur > 0 and (got_dur <= 0 or got_dur < expect_dur * 0.95):
        log.error(f'  合并视频不完整（{hms(got_dur)}/期望 {hms(expect_dur)}），本次判失败，'
                  f'已下载分片保留可重试')
        force_unlink(tmp_final)
        return None
    try:
        os.replace(tmp_final, final)
    except Exception as e:
        log.error(f'  合并视频改名失败：{e}')
        force_unlink(tmp_final)
        return None
    dur = probe_duration(P['ffprobe'], final)
    log(f'  合并视频就绪：{os.path.basename(final)}  {hms(dur)}  '
        f'{os.path.getsize(final) // 1024 // 1024}MB  耗时 {time.time() - t0:.0f}s')
    for p in parts:
        try:
            force_unlink(p)
        except Exception:
            pass
    try:
        force_rmtree(work)
    except Exception:
        pass
    return final


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
            force_unlink(raw)
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
            force_unlink(p)
        except Exception:
            pass
    try:
        force_rmtree(work)
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
