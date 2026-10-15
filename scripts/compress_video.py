#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compress_video.py —— 总结完成后把原始录播视频压成更小的归档副本（按 config.compress 配置）

背景：原始录播（原画流）动辄 10~15GB。在「生成 MP3 归档」之后，用 NVIDIA NVENC HEVC
（H.265）硬件编码重新封装，在几乎不损失画质的前提下把体积压到目标大小附近。
RTX 20 系及以后支持 hevc_nvenc，速度远快于 CPU 编码。

设计原则（与 bili-live-summary 其它模块一致）：
- 默认（replace_original=false）：输出 `<原名>_compressed.mp4` 新文件，保留原始以便核对；
- replace_original=true：压缩成功后**直接永久删除原视频**（默认 replace_mode=delete，
  省空间、不进回收站、不备份、无需确认），压缩版保留在 `<原名>_compressed.mp4`；
  旧版 recycle 分支（备份+移回收站）已移除——本机回收站会堆积导致磁盘爆满；
- 幂等：目标已存在且体积合理则跳过；
- 不抛异常，避免影响总结主流程；失败仅记日志返回 None，绝不破坏原文件。

用法:
  python compress_video.py --src "D:\\录播\\...\\录制-xxx.mp4"
  python compress_video.py --src "..." --replace   # 压缩后替换原视频（直接永久删除原视频，不进回收站）
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import Logger, probe_duration, hms, force_unlink, force_rmtree

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


def compress_video(cfg, log, src_video, replace_original=False, archive_root=None, orig_members=None):
    """把 src_video 用 hevc_nvenc 压成更小的归档副本。

    返回输出文件路径；未启用 / 源不存在 / 转换失败返回 None。

    replace_original=False（默认）：输出 `<名>_compressed.mp4` 新文件，保留原始（安全）。
    replace_original=True：压缩成功后**直接永久删除原视频**（默认 replace_mode=delete，
        省空间、不进回收站、不备份、无需确认）；压缩版保留在 _compressed.mp4。
        旧版 recycle 分支（备份+移回收站）已移除——回收站堆积会撑爆磁盘。
        压缩失败不动原文件。
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
    if out_mode == 'mp4_folder':
        # 用户规则（2026-09-05）：压缩后的 MP4 统一输出到「录播根目录/MP4/」（自动创建）。
        # ★ 2026-09-08 起搁置：用户决定不用 MP4/ 文件夹，统一改用 date_subfolder（根/YYYY-MM-DD/）。
        # archive_root 优先（B站/rec站补档的源在临时目录，必须归档到录播根目录），否则用源所在盘根/本地 root。
        base_dir = archive_root or os.path.dirname(src_video)
        folder = cc.get('mp4_folder_name', 'MP4')
        if os.path.basename(base_dir) == folder:
            out_dir = base_dir
        else:
            out_dir = os.path.join(base_dir, folder)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, name + '_compressed' + out_ext)
        log('[compress] 输出到 MP4 统一目录：%s' % out_dir)
    elif out_mode == 'mp4_date_folder' and ymd:
        # 用户规则（2026-09-06）：压缩后的 MP4 输出到「录播根目录/MP4/YYYY-MM-DD/」——
        # 在 mp4_folder 基础上按直播日期再建一层文件夹。★ 2026-09-08 起搁置（同 mp4_folder，
        # 用户决定统一用 date_subfolder，产物直接进 根/YYYY-MM-DD/ 日期文件夹）。
        base_dir = archive_root or os.path.dirname(src_video)
        folder = cc.get('mp4_folder_name', 'MP4')
        date_folder = '%s-%s-%s' % (ymd[:4], ymd[4:6], ymd[6:8])
        out_dir = os.path.join(base_dir, folder, date_folder)
        os.makedirs(out_dir, exist_ok=True)
        out = os.path.join(out_dir, name + '_compressed' + out_ext)
        log('[compress] 输出到 MP4/日期子目录：%s' % out_dir)
    elif out_mode == 'date_subfolder' and ymd:
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

    # 幂等：已存在且**时长与源接近**才跳过。
    # ★ 2026-09-18：旧判据只看"体积 >1MB"。压缩是全流程里最耗时的单步（几十分钟），
    #   上一轮被杀极可能停在编码中途——留下一个体积已经几 GB、但时长只有几分钟的
    #   残缺 `_compressed.mp4`。旧逻辑下次直接当"已完成"跳过，且 replace 模式下还会
    #   把**完好的源视频删掉**，等于永久丢档。现在必须时长达标（≥源时长 98%）才算完成。
    if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
        out_dur = probe_duration(ffprobe, out) or 0
        if out_dur <= 0 or out_dur < dur * 0.98:
            log('[compress] 已有产物不完整（%.0fs/源 %.0fs），删除后重新压缩：%s'
                % (out_dur, dur, os.path.basename(out)), 'WARN')
            force_unlink(out, log)
        else:
            sz = os.path.getsize(out) // 1024 // 1024
            log('[compress] 已存在且完整，跳过：%s（%d MB）' % (os.path.basename(out), sz))
            if replace:
                _replace_by_mode(cfg, log, src_video, out)
            return out

    a_params = ['-c:a', 'copy']
    if audio_mode == 'aac':
        a_params = ['-c:a', 'aac', '-b:a', '%dk' % int(audio_kbps)]

    # 编码写入临时名（保留 .mp4 后缀，ffmpeg 需要据此选封装格式），
    # 校验通过后再 os.replace 到正式名——被杀只会留下 .part.mp4。
    tmp_out = out + '.part.mp4'
    if os.path.exists(tmp_out):
        force_unlink(tmp_out, log)          # 上次被杀留下的半成品

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
    ] + a_params + [tmp_out]
    # 超时按源时长自适应（NVENC 一般 3~8x 实时），上限 6 小时；避免卡死拖住整轮
    to = int(max(1800, min(6 * 3600, 1800 + dur * 3)))
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=to)
    except subprocess.TimeoutExpired:
        log.error('[compress] 编码超时（%ds）已终止，源文件保留：%s'
                  % (to, os.path.basename(src_video)))
        force_unlink(tmp_out, log)
        return None
    except Exception as e:
        log.error('[compress] 编码失败：%s' % e)
        force_unlink(tmp_out, log)
        return None

    # 产出校验：体积 + **时长必须与源一致**，通过后才原子改名到正式路径
    if not (os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 1024 * 1024):
        log.error('[compress] 转换后文件异常（空/过小）')
        force_unlink(tmp_out, log)
        return None
    out_dur = probe_duration(ffprobe, tmp_out) or 0
    if out_dur <= 0 or out_dur < dur * 0.98:
        log.error('[compress] 产物时长不足（%.0fs/源 %.0fs），判为失败并保留源文件'
                  % (out_dur, dur))
        force_unlink(tmp_out, log)
        return None
    try:
        os.replace(tmp_out, out)
    except Exception as e:
        log.error('[compress] 产物改名失败：%s' % e)
        return None
    if os.path.exists(out) and os.path.getsize(out) > 1024 * 1024:
        sz = os.path.getsize(out) // 1024 // 1024
        saved = (1 - os.path.getsize(out) / os.path.getsize(src_video)) * 100
        log('[compress] 完成（%d MB，节省 %.0f%%，%.0fs）：%s'
            % (sz, saved, __import__('time').time() - t0, os.path.basename(out)))
    else:
        log.error('[compress] 转换后文件异常')
        return None

    # replace_original：压缩成功后按需处理原文件（replace_mode 决定方式）
    if replace:
        ok = _replace_by_mode(cfg, log, src_video, out)
        if ok and orig_members:
            # ★ 2026-09-26 修复：多段合并场次 src_video 只是合并中间文件，原始 FLV 分段
            #   从未被碰过 → 每天堆积。压缩成功后一并删除原始分段（压缩产物已校验完整）。
            for m in orig_members:
                try:
                    if os.path.abspath(m) == os.path.abspath(out):
                        continue
                    if force_unlink(m, log):
                        log('[compress] 已删除原始分段（压缩版已就绪）：%s' % os.path.basename(m))
                    else:
                        log.error('[compress] 删除原始分段失败（会持续占磁盘）：%s'
                                  % os.path.basename(m))
                except Exception as e:
                    log.error('[compress] 删除原始分段异常：%s' % e)
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


# (2026-10-15) 回收站分支已移除：本机「安全删除 hook」会把 os.remove 软删进回收站，
# 原视频经回收站会堆积导致磁盘爆满；所有删除统一走 common.force_unlink（原生永久删除）。


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
            force_unlink(old)
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
            force_unlink(old)
            log('[compress] 备份超容量，删除最旧：%s' % os.path.basename(old))
        except Exception as e:
            log.error('[compress] 删除旧备份失败：%s' % e)
            break


def _replace_by_mode(cfg, log, src_video, compressed):
    """压缩成功后处理原视频：一律直接永久删除（用户规则 2026-10-15：省空间、不进回收站、不备份、无需确认）。

    旧的 recycle 分支（备份+移回收站）已移除——本机回收站会堆积导致磁盘爆满。
    无论 config.replace_mode 写 recycle 还是 delete，都走 _replace_direct_delete 永久删除。
    返回 True 表示原文件已移除；失败则保留原文件返回 False。
    """
    return _replace_direct_delete(cfg, log, src_video, compressed)


def _compressed_ok(cfg, src_video, compressed):
    """删源前的最后一道门禁：压缩产物必须「存在 + 体积合理 + 时长≈源」。
    删源不可逆，这里一律从严；取不到源时长时不阻断（只校验体积）。"""
    if not (os.path.exists(compressed) and os.path.getsize(compressed) > 1024 * 1024):
        return False, '产物不存在或过小'
    try:
        ffprobe = cfg['paths']['ffprobe']
    except Exception:
        return True, ''
    src_dur = probe_duration(ffprobe, src_video) or 0
    out_dur = probe_duration(ffprobe, compressed) or 0
    if src_dur > 0 and (out_dur <= 0 or out_dur < src_dur * 0.98):
        return False, '产物时长 %.0fs < 源时长 %.0fs 的 98%%' % (out_dur, src_dur)
    return True, ''


def _replace_direct_delete(cfg, log, src_video, compressed):
    """压缩成功且校验通过后：直接永久删除原视频（用户规则 2026-08-28：省空间，不进回收站/不备份）。

    仅在 compressed 校验通过后才删除；任何异常都保留原文件并返回 False（绝不误删）。
    """
    ok, why = _compressed_ok(cfg, src_video, compressed)
    if not ok:
        log.error('[compress] 压缩产物校验未通过（%s），保留原文件：%s'
                  % (why, os.path.basename(src_video)))
        return False
    # 必须检查返回值：force_unlink 失败时不抛异常（内部已兜底），若不判返回值就会
    # 误报「已删除」，20GB 的源视频悄悄留在磁盘上——这正是以前磁盘爆掉的原因之一。
    if force_unlink(src_video, log):
        log('[compress] 已直接删除原视频（压缩版已就绪）：%s' % os.path.basename(src_video))
        return True
    log.error('[compress] 删除原视频失败，原文件保留（会持续占用磁盘，需手工删除）：%s'
              % os.path.basename(src_video))
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
