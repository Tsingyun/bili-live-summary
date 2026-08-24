#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress_video.py —— 总结完成后把原始录播视频压成更小的归档副本（按 config.compress 配置）

背景：原始录播（原画流）动辄 10~15GB。在「生成 MP3 归档」之后，用 NVIDIA NVENC HEVC
（H.265）硬件编码重新封装，在几乎不损失画质的前提下把体积压到目标大小附近。
RTX 20 系及以后支持 hevc_nvenc，速度远快于 CPU 编码。

设计原则（与 bili-live-summary 其它模块一致）：
- 默认不覆盖原始文件，输出 `<原名>_compressed<ext>` 新文件，保留原始以便核对；
- 幂等：目标已存在且体积合理则跳过；
- 不抛异常，避免影响总结主流程；失败仅记日志返回 None。

用法:
  python compress_video.py --src "D:\\录播\\...\录制-xxx.mp4"
  python compress_video.py --src "..." --replace   # 直接覆盖原文件（需先备份，见说明）
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Logger, probe_duration, hms


def _audio_codec_of(ffprobe, src):
    """返回音频流编码名，用于决定是否可无损 copy。"""
    try:
        out = os.popen('%s -hide_banner -v error -show_entries stream=codec_name '
                       '-select_streams a:0 -of default=noprint_wrappers=1:nokey=1 "%s"'
                       % (ffprobe, src)).read().strip()
        return out
    except Exception:
        return ''


def compress_video(cfg, log, src_video, replace_original=False):
    """把 src_video 用 hevc_nvenc 压成更小的归档副本。

    返回输出文件路径；未启用 / 源不存在 / 转换失败返回 None。

    replace_original=False（默认）：输出 src_video 同目录的 `<名>_compressed<ext>` 新文件，
        保留原始文件，安全（不破坏原始数据）。
    replace_original=True：直接覆盖 src_video（调用方需先备份，本函数不负责备份）。
    """
    cc = cfg.get('compress') or {}
    if not cc.get('enabled', False):
        log('[compress] 未启用（compress.enabled=false），跳过', 'WARN')
        return None
    if not src_video or not os.path.exists(src_video):
        log('[compress] 源视频不存在，跳过：%s' % src_video, 'WARN')
        return None

    ffmpeg = cfg['paths']['ffmpeg']
    ffprobe = cfg['paths']['ffprobe']

    dur = probe_duration(ffprobe, src_video) or 0
    if dur <= 0:
        log('[compress] 取不到时长，跳过', 'WARN')
        return None

    codec = cc.get('codec', 'hevc_nvenc')
    target_gb = float(cc.get('target_size_gb', 5))
    target_bytes = target_gb * 1024.0 ** 3
    audio_kbps = float(cc.get('audio_kbps', 192))
    # 目标总码率（含音频）再扣掉音频，得到视频目标码率
    total_kbps = (target_bytes * 8.0) / dur / 1000.0
    video_kbps = max(int(total_kbps - audio_kbps), 200)
    cq = int(cc.get('cq', 24))              # NVENC 恒定质量（越小越清晰，24 已很清晰）
    preset = cc.get('preset', 'p4')         # p4 = balanced；p1 最快/p7 最慢最省体积
    audio_mode = cc.get('audio_mode', 'copy')  # copy = 原始音频直接复用；aac = 重编码 192k

    base, ext = os.path.splitext(src_video)
    if replace_original or cc.get('replace_original', False):
        out = src_video
    else:
        out = base + '_compressed' + ext

    # 幂等：已存在且体积正常则跳过
    if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
        sz = os.path.getsize(out) // 1024 // 1024
        log('[compress] 已存在，跳过：%s（%d MB）' % (os.path.basename(out), sz))
        return out

    a_params = ['-c:a', 'copy']
    if audio_mode == 'aac':
        a_params = ['-c:a', 'aac', '-b:a', '%dk' % int(audio_kbps)]

    log('[compress] 开始：%s（%s，目标≈%dMB，视频目标码率 %dk）'
        % (os.path.basename(src_video), hms(dur), int(target_gb * 1024), video_kbps))
    t0 = __import__('time').time()
    cmd = [
        ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
        '-err_detect', 'ignore_err', '-fflags', '+genpts',
        '-i', src_video,
        '-c:v', codec, '-preset', preset, '-tune', 'hq',
        '-rc', 'vbr', '-cq:v', str(cq),
        '-b:v', '%dk' % video_kbps,
        '-maxrate', '%dk' % int(video_kbps * 1.8),
        '-bufsize', '%dk' % int(video_kbps * 2),
        '-spatial-aq', '1', '-temporal-aq', '1',
        '-tag:v', 'hvc1',
    ] + a_params + [out]
    try:
        subprocess.run(cmd, check=True)
    except Exception as e:
        log.error('[compress] 编码失败：%s' % e)
        return None

    if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
        sz = os.path.getsize(out) // 1024 // 1024
        log('[compress] 完成（%d MB，节省 %.0f%%）：%s'
            % (sz, (1 - os.path.getsize(out) / os.path.getsize(src_video)) * 100,
               os.path.basename(out)))
        return out
    log.error('[compress] 转换后文件异常')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--replace', action='store_true')
    ap.add_argument('--config', default=None)
    args = ap.parse_args()
    from common import load_config
    cfg = load_config(args.config)
    log = Logger(cfg, 'compress')
    compress_video(cfg, log, args.src, replace_original=args.replace)


if __name__ == '__main__':
    main()
