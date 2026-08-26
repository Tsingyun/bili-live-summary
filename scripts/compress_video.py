#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress_video.py —— 总结完成后把原始录播视频压成更小的归档副本（按 config.compress 配置）

背景：原始录播（原画流）动辄 10~15GB。在「生成 MP3 归档」之后，用 NVIDIA NVENC HEVC
（H.265）硬件编码重新封装，在几乎不损失画质的前提下把体积压到目标大小附近。
RTX 20 系及以后支持 hevc_nvenc，速度远快于 CPU 编码。

设计原则（与 bili-live-summary 其它模块一致）：
- 默认（replace_original=false）：输出 `<原名>_compressed.mp4` 新文件，保留原始以便核对；
- replace_original=true：压缩成功并备份（与原视频同盘，默认 <盘>:\\live_raw_backup，
  可在 compress.backup_dir 覆盖）后，把原始视频可靠移入回收站（绝不裸删），压缩版保留在
  `<原名>_compressed.mp4`（flv/mkv 等源因容器兼容性问题也统一输出 mp4）；
- 幂等：目标已存在且体积合理则跳过；
- 不抛异常，避免影响总结主流程；失败仅记日志返回 None，绝不破坏原文件。

用法:
  python compress_video.py --src "D:\\录播\\...\\录制-xxx.mp4"
  python compress_video.py --src "..." --replace   # 压缩后删原视频（先备份）
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Logger, probe_duration, hms

# 从文件名里提取 8 位日期（20260824），用于「按日期子目录输出」
_DATE_RE = re.compile(r'(\d{8})')


def _date_from_name(src):
    fn = os.path.basename(src)
    # 文件名里可能有多个 8 位数字（如 UID 25788785 排在日期 20260824 前），
    # 必须找「第一个能解析成合法日期」的，而不是第一个 8 位数字。
    for m in _DATE_RE.finditer(fn):
        ymd = m.group(1)
        try:
            datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
            return ymd
        except Exception:
            continue
    return None


def _audio_codec_of(ffprobe, src):
    """返回音频流编码名，用于决定是否可无损 copy。"""
    try:
        out = os.popen('%s -hide_banner -v error -show_entries stream=codec_name '
                       '-select_streams a: 0 -of default=noprint_wrappers=1:nokey=1 "%s"'
                       % (ffprobe, src)).read().strip()
        return out
    except Exception:
        return ''


def compress_video(cfg, log, src_video, replace_original=False, archive_root=None):
    """把 src_video 用 hevc_nvenc 压成更小的归档副本。

    返回输出文件路径；未启用 / 源不存在 / 转换失败返回 None。

    replace_original=False（默认）：输出 `<名>_compressed.mp4` 新文件，保留原始（安全）。
    replace_original=True：压缩成功后先备份原文件（与原视频同盘，默认 <盘>:\\live_raw_backup，
        可在 compress.backup_dir 覆盖；按 max_backup_files/max_backup_gb 自动收敛），
        再可靠移入回收站，压缩版保留在 _compressed.mp4。压缩失败不动原文件。
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

    base_noext, ext = os.path.splitext(src_video)
    name = os.path.basename(base_noext)
    # HEVC 在 flv/mkv 等容器兼容性差，统一输出为 mp4（hvc1）以保证可播放
    out_ext = '.mp4'
    out_mode = cc.get('out_dir_mode', 'next_to_source')
    ymd = _date_from_name(src_video)
    if out_mode == 'date_subfolder' and ymd:
        # 用户规则（2026-08-24）：压缩后的 MP4 放进「根目录/YYYY-MM-DD/」日期子目录；
        # 但若源文件已经位于该日期子目录内（录制器已把原文件放进 根/YYYY-MM-DD/），
        # 则直接复用该目录，不要再新建一层 根/YYYY-MM-DD/YYYY-MM-DD/。
        date_folder = '%s-%s-%s' % (ymd[:4], ymd[4:6], ymd[6:8])
        parent = os.path.dirname(src_video)
        if os.path.basename(parent) == date_folder:
            out_dir = parent
        else:
            base_dir = archive_root or parent
            out_dir = os.path.join(base_dir, date_folder)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, name + '_compressed' + out_ext)
        log('[compress] 输出到日期子目录：%s' % out_dir)
    else:
        out = os.path.join(os.path.dirname(src_video), name + '_compressed' + out_ext)

    replace = replace_original or cc.get('replace_original', False)

    # 幂等：已存在且体积正常则跳过
    if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
        sz = os.path.getsize(out) // 1024 // 1024
        log('[compress] 已存在，跳过：%s（%d MB）' % (os.path.basename(out), sz))
        if replace:
            _replace_backup_then_delete(cfg, log, src_video, out)
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
        saved = (1 - os.path.getsize(out) / os.path.getsize(src_video)) * 100
        log('[compress] 完成（%d MB，节省 %.0f%%）：%s'
            % (sz, saved, os.path.basename(out)))
    else:
        log.error('[compress] 转换后文件异常')
        return None

    # replace_original：压缩成功后先备份原文件，再删除原视频（用回收站，不裸删）
    if replace:
        ok = _replace_backup_then_delete(cfg, log, src_video, out)
        if not ok:
            log('[compress] 原文件保留（压缩版已就绪）：%s' % os.path.basename(out))
    return out


def _default_backup_dir(src_video):
    """备份目录：默认与原视频同盘（避免塞满系统盘 C:）。

    例：原视频在 D:\\录播\\... → D:\\live_raw_backup\\。
    可在 compress.backup_dir 显式覆盖（建议指向大容量存档盘）。
    """
    drive = os.path.splitdrive(os.path.abspath(src_video))[0]
    if drive:
        return os.path.join(drive + os.sep, 'live_raw_backup')
    return os.path.join(os.path.dirname(os.path.abspath(src_video)), 'live_raw_backup')


def _send_to_recycle_bin(log, path):
    """可靠地把文件移入系统回收站（绝不裸删）。

    返回 True 表示成功；失败抛异常由调用方处理（保留原文件）。
    Windows 用 VB Microsoft.VisualBasic.FileIO.FileSystem.DeleteFile(SendToRecycleBin)，
    对超大文件可靠（旧实现用 COM ParseName 对超长中文路径/大文件静默失败，导致假成功）。
    """
    if os.name == 'nt':
        ps = ('Add-Type -AssemblyName Microsoft.VisualBasic;'
              '[Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile('
              '"' + path.replace('\\', '\\\\') + '",'
              '"OnlyErrorDialogs","SendToRecycleBin")')
        subprocess.run(['powershell', '-NoProfile', '-Command', ps], check=True)
    elif sys.platform == 'darwin':
        subprocess.run(['osascript', '-e',
                        'tell application "Finder" to delete POSIX file "%s"' % path],
                       check=True)
    else:
        subprocess.run(['gio', 'trash', path], check=True)
    return True


def _prune_backups(log, backup_dir, cc):
    """备份目录收敛：按文件数 / 总容量上限删除最旧备份，避免备份盘被撑爆。

    备份副本是冗余（原件已压成 _compressed.mp4，必要时还可从回收站找回），
    故直接 os.remove，不再进回收站（避免无限套娃）。
    """
    try:
        files = [os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
                 if os.path.isfile(os.path.join(backup_dir, f))]
    except Exception:
        return
    if not files:
        return
    files.sort(key=lambda p: os.path.getmtime(p))
    max_files = int(cc.get('max_backup_files', 3))
    max_bytes = float(cc.get('max_backup_gb', 60)) * 1024.0 ** 3
    while len(files) > max_files:
        old = files.pop(0)
        try:
            os.remove(old)
            log('[compress] 备份超量，删除最旧：%s' % os.path.basename(old))
        except Exception as e:
            log.error('[compress] 删除旧备份失败：%s' % e)
            break
    while files:
        total = sum(os.path.getsize(f) for f in files)
        if total <= max_bytes or len(files) <= 1:
            break
        old = files.pop(0)
        try:
            os.remove(old)
            log('[compress] 备份超容量，删除最旧：%s' % os.path.basename(old))
        except Exception as e:
            log.error('[compress] 删除旧备份失败：%s' % e)
            break


def _replace_backup_then_delete(cfg, log, src_video, compressed):
    """压缩成功且校验通过后：备份原文件（与原视频同盘），再可靠移入回收站。

    返回 True 表示原文件已移入回收站（压缩版保留在 compressed 路径）；
    任意环节失败都保留原文件并返回 False（绝不裸 rm）。
    """
    cc = cfg.get('compress') or {}
    backup_dir = cc.get('backup_dir') or _default_backup_dir(src_video)
    os.makedirs(backup_dir, exist_ok=True)
    backup = os.path.join(backup_dir, os.path.basename(src_video))
    # 1) 备份原文件（冗余副本，用于极端情况下的原片找回）
    try:
        shutil.copy2(src_video, backup)
        log('[compress] 已备份原文件到：%s' % backup)
    except Exception as e:
        log.error('[compress] 备份失败，放弃删除原文件：%s' % e)
        return False
    # 2) 收敛备份目录，防止备份盘被撑爆
    try:
        _prune_backups(log, backup_dir, cc)
    except Exception as e:
        log.error('[compress] 备份收敛异常（不影响原文件）：%s' % e)
    # 3) 可靠移入回收站（Windows VB / macOS Finder / Linux gio）
    try:
        _send_to_recycle_bin(log, src_video)
        log('[compress] 已把原视频移入回收站：%s' % src_video)
        return True
    except Exception as e:
        log.error('[compress] 回收站删除失败，原文件保留（请手动清理）：%s' % e)
        return False


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
