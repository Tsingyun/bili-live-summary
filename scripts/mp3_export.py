#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mp3_export.py —— 总结后把录播视频转成「识别专用」低码率 MP3 归档（按 config.mp3_export 配置）

规则来源：用户 2026-08-19 要求「每次总结之后把视频转换成 MP3 文件，留档」。
★ 用户规则（2026-11-15）：归档 MP3 是送给千问做音频识别用的，「音质可以全面降低，
  满足普通的转录需求就可以」→ 默认 **16 kHz / 单声道 / 64 kbps**。
  · 阿里云百炼 qwen3-asr-flash / -filetrans 输入端统一重采样到 16 kHz，44.1 kHz
    多出的 8–22 kHz 全部被丢掉；录播 L/R 是单声道复制，-ac 1 零信息损失；
  · 人声可懂度集中在 8 kHz 以下，16 kHz 按 Nyquist 刚好覆盖；勿降到 8 kHz；
  · 64k 足够干净语音，耳语/ASMR/多人重叠可把 bitrate 调成 96k；
  · 唱歌/音乐听感会发闷——本产物只用于识别，需要收听版请另行单独导出。
★ 用户规则（2026-09-08）：归档 MP3 输出到「C:/Users/Tsing/DeskBox/文档」，与 Word 总结文档同目录
（由 config.mp3_export.out_dir 控制，早期曾放在录播根目录）。

与转写用的临时 16k 单声道 MP3 是两份独立产物：
  * 临时 16k MP3 由 cleanup.delete_mp3_after_success 管控，用完即删；
  * 本模块产出的是独立的归档 MP3，放在 mp3_export.out_dir 根目录（不与临时目录混用）。

文件名取源视频去扩展名后的名字，再剥掉 strip_prefix 列表里的前缀（如
'录制-25788785-' / '25788785-'），使输出文件以日期开头。
keep_in_date_subfolder=false 时直接放 out_dir 根（不按日期分子文件夹）。

★ 幂等规格门禁（2026-11-15）：补跑时若已有 MP3 的采样率/声道数不符合当前配置，
  会按新规格重导（旧文件在新 .part 校验通过前不删），让历史高码率归档也能自动降级。

★ 用户规则（2026-11-15）：归档 MP3 超过 500MB 时自动切成上下 Part。
切分由 mp3_split.py 完成（无损 -c copy）；本模块在「新生成」与「幂等跳过」
两条返回路径之前都会调 _maybe_split()，保证补跑也能补切历史大文件。
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
    """把 src_video 转成识别专用低码率 MP3（16k/单声道/64k），输出到 mp3_export.out_dir。

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

    # ★ 2026-11-15 用户规则：归档 MP3 只用于送千问识别，默认 16k / 单声道 / 64k
    bitrate = str(mcfg.get('bitrate', '64k'))
    ar = int(mcfg.get('ar', 16000))
    channels = int(mcfg.get('channels', 1))

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
        reason = ''
        if src_dur > 0:
            try:
                from common import probe_duration
                got = probe_duration(cfg['paths'].get('ffprobe'), out) or 0
                ok = got > 0 and got >= src_dur * 0.98
                if not ok:
                    reason = '时长不足'
            except Exception:
                ok = True
        # ★ 规格门禁：老规格（192k/44.1k/立体声）在补跑时自动按新规格重导
        if ok and not _spec_matches(cfg, cfg['paths'].get('ffprobe'), out, ar, channels):
            ok = False
            reason = '规格不符（非 %dHz/%dch/%s）' % (ar, channels, bitrate)
        if ok:
            log('[mp3_export] 归档 MP3 已存在且完整，跳过：%s' % os.path.basename(out))
            _maybe_split(cfg, log, out)
            return out
        log('[mp3_export] 已有 MP3 %s，重新转码：%s'
            % (reason or '不满足复用条件', os.path.basename(out)), 'WARN')
        # 只在「残缺」时先删；规格不符时保留旧文件，等新 .part 校验通过再 os.replace 覆盖
        if reason == '时长不足':
            try:
                from common import force_unlink
                force_unlink(out)
            except Exception:
                pass

    log('[mp3_export] 转识别专用 MP3（%dHz/%dch/%s）：%s -> %s'
        % (ar, channels, bitrate, os.path.basename(src_video), os.path.basename(out)))
    # 原子写：先落 <out>.part，校验通过再改名——被杀不会留下"看似完整"的半截 MP3
    tmp = out + '.part'
    try:
        from common import force_unlink as _fu
        _fu(tmp)
    except Exception:
        pass
    to = int(max(600, min(4 * 3600, 600 + (src_dur or 0) * 2)))
    try:
        # ★ 必须显式 -f mp3：临时名是 `<out>.part`，ffmpeg 从扩展名猜不出容器格式，
        #   会直接以 EINVAL(-22) 退出（Windows 上表现为 exit status 4294967274）。
        #   加了 -f 就与文件名无关，别再删掉它。
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', src_video, '-vn',
                        '-acodec', 'libmp3lame', '-ac', str(channels),
                        '-ar', str(ar), '-b:a', bitrate,
                        '-f', 'mp3', tmp], check=True, stdout=subprocess.DEVNULL,
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
        _maybe_split(cfg, log, out)
        return out
    log.error('[mp3_export] 转换后文件异常（可能秒退未真正转写）')
    return None


def _spec_matches(cfg, ffprobe, path, ar, channels):
    """幂等门禁：已有 MP3 的采样率/声道数是否符合当前配置。

    读不到（ffprobe 缺失 / 文件异常）就保守返回 True，避免补跑时反复重导。
    只比对采样率与声道数，不比对码率（CBR/VBR 波动会让判据不稳定）。
    """
    if not ffprobe:
        return True
    try:
        import json
        r = subprocess.run([ffprobe, '-v', 'error', '-select_streams', 'a:0',
                            '-show_entries', 'stream=sample_rate,channels',
                            '-of', 'json', path], check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        st = (json.loads(r.stdout.decode('utf-8', 'replace')) or {}).get('streams') or []
        if not st:
            return True
        got_ar = int(st[0].get('sample_rate') or 0)
        got_ch = int(st[0].get('channels') or 0)
        return got_ar in (0, ar) and got_ch in (0, channels)
    except Exception:
        return True


def _maybe_split(cfg, log, out):
    """★ 用户规则 2026-11-15：归档 MP3 超过阈值自动切成上下 Part。

    切分异常一律吞掉，绝不影响 MP3 归档 / 总结主流程（与压缩同级的旁路能力）。
    """
    try:
        import mp3_split
        return mp3_split.maybe_split(cfg, log, out)
    except Exception as e:
        log('[mp3_export] 自动切分异常（不影响归档）：%s' % e, 'WARN')
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
