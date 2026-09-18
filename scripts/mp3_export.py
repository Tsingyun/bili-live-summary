#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mp3_export.py —— 总结后把录播视频转成中质量 MP3 归档（按 config.mp3_export 配置）

规则来源：用户 2026-08-19 要求「每次总结之后把视频转换成 MP3 文件，中质量即可，留档」。
★ 用户规则（2026-09-08）：归档 MP3 输出到「C:/Users/Tsing/DeskBox/文档」，与 Word 总结文档同目录
（由 config.mp3_export.out_dir 控制，早期曾放在录播根目录）。

与转写用的临时 16k 单声道 MP3 完全无关：
  * 临时 16k MP3 由 cleanup.delete_mp3_after_success 管控，用完即删；
  * 本模块产出的是独立的中质量归档 MP3，放在 mp3_export.out_dir 根目录（不与临时目录混用）。

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

    # 幂等：已存在且**时长与源接近**才跳过。
    # ★ 2026-09-18：旧判据只看体积 >10KB，转码中途被杀留下的残缺 MP3（可能已几十 MB）
    #   会被当成已完成，归档里就永久留着一个听不完的文件。改为时长达标（≥源 98%）。
    src_dur = 0.0
    try:
        from common import probe_duration
        src_dur = probe_duration(cfg['paths'].get('ffprobe'), src_video) or 0.0
    except Exception:
        pass
    if os.path.exists(out) and os.path.getsize(out) > 10240:
        ok = True
        if src_dur > 0:
            try:
                from common import probe_duration
                got = probe_duration(cfg['paths'].get('ffprobe'), out) or 0
                ok = got > 0 and got >= src_dur * 0.98
            except Exception:
                ok = True
        if ok:
            log('[mp3_export] 归档 MP3 已存在且完整，跳过：%s' % os.path.basename(out))
            return out
        log('[mp3_export] 已有 MP3 时长不足，重新转码：%s' % os.path.basename(out), 'WARN')
        try:
            from common import force_unlink
            force_unlink(out)
        except Exception:
            pass

    bitrate = str(mcfg.get('bitrate', '192k'))
    ar = int(mcfg.get('ar', 44100))
    log('[mp3_export] 转中质量 MP3：%s -> %s' % (os.path.basename(src_video), os.path.basename(out)))
    # 原子写：先落 <out>.part，校验通过再改名——被杀不会留下"看似完整"的半截 MP3
    tmp = out + '.part'
    try:
        from common import force_unlink as _fu
        _fu(tmp)
    except Exception:
        pass
    to = int(max(600, min(4 * 3600, 600 + (src_dur or 0) * 2)))
    try:
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', src_video, '-vn',
                        '-acodec', 'libmp3lame', '-ar', str(ar), '-b:a', bitrate,
                        tmp], check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=to)
    except subprocess.TimeoutExpired:
        log.error('[mp3_export] 转换超时（%ds）已终止：%s' % (to, os.path.basename(src_video)))
        _safe_unlink(tmp)
        return None
    except Exception as e:
        log.error('[mp3_export] 转换失败：%s' % e)
        _safe_unlink(tmp)
        return None

    if not (os.path.exists(tmp) and os.path.getsize(tmp) > 10240):
        log.error('[mp3_export] 转换后文件异常（可能秒退未真正转写）')
        _safe_unlink(tmp)
        return None
    # 时长达标才提升为正式归档文件
    if src_dur > 0:
        try:
            from common import probe_duration
            got = probe_duration(cfg['paths'].get('ffprobe'), tmp) or 0
            if got <= 0 or got < src_dur * 0.98:
                log.error('[mp3_export] 产物时长不足（%.0fs/源 %.0fs），判为失败' % (got, src_dur))
                _safe_unlink(tmp)
                return None
        except Exception:
            pass
    try:
        os.replace(tmp, out)
    except Exception as e:
        log.error('[mp3_export] 归档改名失败：%s' % e)
        return None

    if os.path.exists(out) and os.path.getsize(out) > 10240:
        sz = os.path.getsize(out) // 1024 // 1024
        log('[mp3_export] 完成（%d MB）：%s' % (sz, os.path.basename(out)))
        return out
    log.error('[mp3_export] 转换后文件异常（可能秒退未真正转写）')
    return None


def _safe_unlink(path):
    try:
        from common import force_unlink
        force_unlink(path)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass


def export_for_local(cfg, log, src_video):
    """本地录播入口专用包装：直接调用 export_mp3。"""
    return export_mp3(cfg, log, src_video)
