#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mp3_export.py —— 总结后把录播视频转成中质量 MP3 归档（按 config.mp3_export 配置）

规则来源：用户 2026-08-19 要求「每次总结之后把视频转换成 MP3 文件，中质量即可，
留在 D:\录播\25788785-岁己SUI 根目录下」。

与转写用的临时 16k 单声道 MP3 完全无关：
  * 临时 16k MP3 由 cleanup.delete_mp3_after_success 管控，用完即删；
  * 本模块产出的是独立的中质量归档 MP3，放在 mp3_export.out_dir 根目录。

文件名取源视频去扩展名后的名字，再剥掉 strip_prefix 列表里的前缀（如
'录制-25788785-' / '25788785-'），使输出文件以日期开头。
keep_in_date_subfolder=false 时直接放 out_dir 根（不按日期分子文件夹）。
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Logger


def _strip_prefix(name, prefixes):
    """去掉开头的前缀，使文件名以日期开头。"""
    for p in (prefixes or []):
        if name.startswith(p):
            name = name[len(p):]
    return name


def export_mp3(cfg, log, src_video, default_stem=None):
    """把 src_video（本地录播 mp4/flv）转成中质量 MP3，输出到 mp3_export.out_dir。

    返回输出路径；未启用 / 源不存在 / 转换失败时返回 None（不抛异常，
    避免影响总结主流程）。

    幂等：若目标 MP3 已存在且 >10KB 视为已完成，直接跳过。
    """
    mcfg = cfg.get('mp3_export') or {}
    if not mcfg.get('enabled', False):
        log('[mp3_export] 未启用（mp3_export.enabled=false），跳过', 'WARN')
        return None
    if not src_video or not os.path.exists(src_video):
        log('[mp3_export] 源视频不存在，跳过：%s' % src_video, 'WARN')
        return None

    ffmpeg = cfg['paths']['ffmpeg']
    prefixes = mcfg.get('strip_prefix') or []
    base = os.path.basename(src_video)
    name = re.sub(r'\.(mp4|flv|mkv|mov|MP4|FLV|MKV|MOV)$', '', base, flags=re.I)
    name = _strip_prefix(name, prefixes)

    out_dir = mcfg.get('out_dir') or cfg.get('_dirs', {}).get('root', os.getcwd())
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, name + '.mp3')

    # 幂等：已存在且体积正常则跳过
    if os.path.exists(out) and os.path.getsize(out) > 10240:
        log('[mp3_export] 归档 MP3 已存在，跳过：%s' % os.path.basename(out))
        return out

    bitrate = str(mcfg.get('bitrate', '192k'))
    ar = int(mcfg.get('ar', 44100))
    log('[mp3_export] 转中质量 MP3：%s -> %s' % (os.path.basename(src_video), os.path.basename(out)))
    try:
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', src_video, '-vn',
                        '-acodec', 'libmp3lame', '-ar', str(ar), '-b:a', bitrate,
                        out], check=True)
    except Exception as e:
        log.error('[mp3_export] 转换失败：%s' % e)
        return None

    if os.path.exists(out) and os.path.getsize(out) > 10240:
        sz = os.path.getsize(out) // 1024 // 1024
        log('[mp3_export] 完成（%d MB）：%s' % (sz, os.path.basename(out)))
        return out
    log.error('[mp3_export] 转换后文件异常（可能秒退未真正转写）')
    return None


def export_for_local(cfg, log, src_video):
    """本地录播入口专用包装：直接调用 export_mp3。"""
    return export_mp3(cfg, log, src_video)
