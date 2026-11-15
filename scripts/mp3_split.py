#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mp3_split.py —— 归档 MP3 超过阈值时切成「上 / 下」多个 part（无损、可重跑）

★ 用户规则（2026-11-15）：「11.10 和 11.14 的 MP3 文件太大了，分成上下两个 part。
   以后遇到超过 500MB 的文件，也要这样切一下。」

设计要点
--------
1. **同一套逻辑两处用**：`mp3_export.export_mp3()` 生成归档 MP3 后自动调用
   `maybe_split()`；历史大文件用本脚本 CLI 手动补切。行为完全一致。
2. **无损**：`-c copy` 按时间切，不重编码，192k CBR 下「按时间对半」≈「按体积对半」。
   切点会先在中点 ±90s 内用 silencedetect 找静音，尽量不把一句话切成两半；
   找不到静音就退回精确等分（不会被探测失败卡住）。
3. **原子写**：每段先落 `<part>.part`（显式 `-f mp3`，见 2026-09-19 事故），
   全部段校验通过后统一 `os.replace` 提升为正式文件；中途失败不留下半截 part。
4. **幂等**：所有 part 已存在、时长达标且总时长与源相符 → 直接跳过。
5. **不删原件**：默认保留完整 MP3（`split_delete_original=false`）。想省空间
   需显式打开开关，或人工确认后移入回收站。
6. **不影响主流程**：任何异常都只记日志并返回，绝不抛到总结管线。

命名：`<原名>_Part1.mp3`（上）、`<原名>_Part2.mp3`（下）……
段数：n = max(2, ceil(文件大小 / 阈值))，保证每段都不超阈值；
      当前 6 小时左右的场次天然是 2 段（上下）。

CLI
---
    python mp3_split.py --config D:/岁己直播自动总结/config.json \
        --file "C:/Users/Tsing/DeskBox/文档/xxx.mp3" [--dry-run] [--force]
    python mp3_split.py --config ... --scan      # 扫描归档目录里所有超阈值 MP3
退出码 0 = 成功 / 无需切分；1 = 有文件切分失败。
"""
import argparse
import math
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

DEFAULT_THRESHOLD_MB = 500.0        # Windows 资源管理器口径：MB = MiB
# ★ 必须是 float：_cfg() 按 default 的类型转换，写成 int(500) 会把
#   「0.0001 MB」这类小数阈值截成 0 → 静默关闭切分（W10 端到端回归抓到）。
DEFAULT_SILENCE_WINDOW = 90.0       # 切点前后各找 90s 静音
DEFAULT_SILENCE_DB = -35            # 静音判定门限
DEFAULT_SILENCE_MIN = 0.4           # 静音最短时长（秒）
PART_SUFFIX = '_Part%d'


def _mi2b(mb):
    return int(float(mb) * 1024 * 1024)


def _cfg(mcfg, key, default):
    v = mcfg.get(key, default)
    try:
        return type(default)(v)
    except (TypeError, ValueError):
        return default


def _tool(cfg, key, fallback):
    return (cfg.get('paths') or {}).get(key) or fallback


def _probe(ffprobe, path):
    try:
        from common import probe_duration
        return probe_duration(ffprobe, path) or 0.0
    except Exception:
        return 0.0


def _part_paths(src, n):
    stem = os.path.splitext(src)[0]
    return [stem + PART_SUFFIX % i + os.path.splitext(src)[1]
            for i in range(1, n + 1)]


def _find_silence_near(ffmpeg, src, target, window, noise_db, min_dur):
    """在 target ±window 秒内找最靠近 target 的静音段中点；失败返回 None。"""
    start = max(0.0, target - window)
    length = min(window * 2, max(1.0, target + window - start))
    cmd = [ffmpeg, '-hide_banner', '-nostats',
           '-ss', '%.3f' % start, '-t', '%.3f' % length,
           '-i', src, '-af',
           'silencedetect=noise=%ddB:d=%.2f' % (noise_db, min_dur),
           '-f', 'null', '-']
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=120)
        text = (r.stderr or b'').decode('utf-8', errors='replace')
    except Exception:
        return None
    starts = [float(x) for x in re.findall(r'silence_start:\s*([\d.]+)', text)]
    ends = [float(x) for x in re.findall(r'silence_end:\s*([\d.]+)', text)]
    best, best_dist = None, None
    # -ss 在输入前 → ffmpeg 把输出时间戳重置为 0，静音时间是相对 start 的偏移。
    for s, e in zip(starts, ends):
        mid = start + (s + e) / 2.0
        if not (start <= mid <= start + length):
            continue
        d = abs(mid - target)
        if best_dist is None or d < best_dist:
            best, best_dist = mid, d
    return best


def _pick_cuts(cfg, log, src, duration, n, window, noise_db, min_dur):
    """理想等分点 + 静音微调，保证严格递增。"""
    ffmpeg = _tool(cfg, 'ffmpeg', r'D:\ffmpeg\bin\ffmpeg.exe')
    ideal = [duration * i / n for i in range(1, n)]
    if not os.path.exists(ffmpeg) or window <= 0:
        return ideal
    cuts = []
    for t in ideal:
        c = _find_silence_near(ffmpeg, src, t, window, noise_db, min_dur)
        if c is None or c <= 0 or c >= duration:
            c = t
        if cuts and c - cuts[-1] < 60:      # 静音微调不得把两段挤到 1 分钟以内
            c = t
        cuts.append(c)
        if abs(c - t) > 1:
            log('[mp3_split] 切点 %s 微调到附近静音（偏移 %+.1fs）'
                % (_hms(t), c - t))
    return cuts


def _hms(sec):
    sec = int(max(0, sec))
    return '%02d:%02d:%02d' % (sec // 3600, (sec // 60) % 60, sec % 60)


def _run_ffmpeg(ffmpeg, args, timeout):
    try:
        return subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error'] + args,
                              check=True, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError('ffmpeg 超时（%ds）' % timeout)
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or b'').decode('utf-8', errors='replace')[-300:]
        raise RuntimeError('ffmpeg 失败：%s' % tail)


def _safe_unlink(path):
    try:
        from common import force_unlink
        force_unlink(path)
    except Exception:
        try:
            os.remove(path)
        except Exception:
            pass


def split_mp3(cfg, log, src, *, force=False, threshold_mb=None,
              parts=0, delete_original=False, dry_run=False):
    """若 src 超过阈值则切分。返回 part 路径列表；无需切分返回 None；失败返回 []。"""
    mcfg = cfg.get('mp3_export') or {}
    if not os.path.exists(src):
        log('[mp3_split] 文件不存在，跳过：%s' % src, 'WARN')
        return None
    size = os.path.getsize(src)
    threshold = _mi2b(threshold_mb if threshold_mb is not None
                      else _cfg(mcfg, 'split_over_mb', DEFAULT_THRESHOLD_MB))
    if threshold <= 0:
        return None
    if size <= threshold:
        log('[mp3_split] 未超阈值（%.1f MB ≤ %d MB），不切：%s'
            % (size / 1048576.0, int(threshold / 1048576),
               os.path.basename(src)))
        return None

    ffmpeg = _tool(cfg, 'ffmpeg', r'D:\ffmpeg\bin\ffmpeg.exe')
    ffprobe = _tool(cfg, 'ffprobe', r'D:\ffmpeg\bin\ffprobe.exe')
    if not (os.path.exists(ffmpeg) and os.path.exists(ffprobe)):
        log('[mp3_split] ffmpeg/ffprobe 不可用，跳过切分', 'WARN')
        return None
    duration = _probe(ffprobe, src)
    if duration <= 0:
        log('[mp3_split] 无法读取时长，跳过切分：%s' % os.path.basename(src), 'WARN')
        return None

    auto_n = max(2, int(math.ceil(size / float(threshold))))
    n = int(parts) if parts and parts > 0 else auto_n
    if delete_original:
        n = max(n, auto_n)     # 要删原件时必须保证每段都 ≤ 阈值
    dsts = _part_paths(src, n)
    log('[mp3_split] %s：%.1f MB / %s → %d 段（阈值 %d MB，Part1=上 / Part%d=下）'
        % (os.path.basename(src), size / 1048576.0, _hms(duration), n,
           int(threshold / 1048576), n))

    # 幂等：parts 齐全且总时长达标
    if not force and all(os.path.exists(p) and os.path.getsize(p) > 10240 for p in dsts):
        ds = [_probe(ffprobe, p) for p in dsts]
        if all(d > 0 for d in ds) and abs(sum(ds) - duration) <= 3.0:
            log('[mp3_split] 各 part 已存在且完整，跳过')
            return dsts
        log('[mp3_split] 已有 part 不完整，重新切分', 'WARN')

    cuts = _pick_cuts(cfg, log, src, duration, n,
                      _cfg(mcfg, 'split_silence_window_sec', DEFAULT_SILENCE_WINDOW),
                      _cfg(mcfg, 'split_silence_db', DEFAULT_SILENCE_DB),
                      _cfg(mcfg, 'split_silence_min_sec', DEFAULT_SILENCE_MIN))
    bounds = [0.0] + cuts + [duration]
    segs = [(bounds[i], bounds[i + 1] - bounds[i]) for i in range(n)]
    if dry_run:
        for i, (ss, seg) in enumerate(segs, 1):
            log('[mp3_split][dry-run] Part%d  %s 起，时长 %s，预计 %.1f MB'
                % (i, _hms(ss), _hms(seg), size / 1048576.0 * seg / duration))
        return dsts

    # 全部段落先落到 .part，统一校验后才提升，避免留下半截文件
    tmps = [p + '.part' for p in dsts]
    for t in tmps:
        _safe_unlink(t)
    try:
        for i, ((ss, seg), tmp) in enumerate(zip(segs, tmps), 1):
            args = ['-ss', '%.3f' % ss, '-i', src]
            if i < n:                       # 最后一段直接到文件尾，避免漏掉尾巴
                args += ['-t', '%.3f' % seg]
            # ★ 临时名是 .part，必须显式 -f mp3，否则 ffmpeg 猜不出容器格式
            args += ['-c', 'copy', '-f', 'mp3', tmp]
            _run_ffmpeg(ffmpeg, args, timeout=max(600, int(seg * 2) + 300))
            if not (os.path.exists(tmp) and os.path.getsize(tmp) > 10240):
                raise RuntimeError('Part%d 产物异常（空/过小）' % i)
            log('[mp3_split]   Part%d 生成完毕：%s 起 / %s'
                % (i, _hms(ss), _hms(seg)))

        ds = [_probe(ffprobe, t) for t in tmps]
        if any(d <= 0 for d in ds):
            raise RuntimeError('part 时长探测失败：%s' % ds)
        if abs(sum(ds) - duration) > 3.0:
            raise RuntimeError('分段总时长 %.1fs 与源 %.1fs 偏差过大' % (sum(ds), duration))
        sizes = [os.path.getsize(t) for t in tmps]
        too_big = [i + 1 for i, s in enumerate(sizes) if s > threshold]
        if too_big:
            log('[mp3_split] 仍有 part 超过阈值（Part%s），可增大 split_parts'
                % ','.join(map(str, too_big)), 'WARN')
        for t, p in zip(tmps, dsts):
            os.replace(t, p)
    except Exception as e:
        log.error('[mp3_split] 切分失败，回滚所有临时段：%s' % e)
        for t in tmps:
            _safe_unlink(t)
        return []
    finally:
        for t in tmps:
            if os.path.exists(t):
                _safe_unlink(t)

    log('[mp3_split] 完成：%s'
        % ' / '.join('%s %.1fMB' % (os.path.basename(p), os.path.getsize(p) / 1048576.0)
                     for p in dsts))
    if delete_original:
        # 默认关闭：只有显式要求才删完整原件（parts 已校验无损覆盖全时长）
        _safe_unlink(src)
        log('[mp3_split] 按配置删除完整原件：%s' % os.path.basename(src), 'WARN')
    return dsts


def maybe_split(cfg, log, mp3_path):
    """给 mp3_export 调用的薄包装：读开关 + 吞掉所有异常，绝不影响总结主流程。"""
    mcfg = cfg.get('mp3_export') or {}
    if not mcfg.get('split_enabled', True):
        return None
    if not mp3_path or not os.path.exists(mp3_path):
        return None
    try:
        return split_mp3(cfg, log, mp3_path,
                         parts=_cfg(mcfg, 'split_parts', 0),
                         delete_original=bool(mcfg.get('split_delete_original', False)))
    except Exception as e:
        log('[mp3_split] 自动切分异常（不影响 MP3 归档）：%s' % e, 'WARN')
        return None


def main():
    ap = argparse.ArgumentParser(description='归档 MP3 超阈值自动切成上下 part')
    ap.add_argument('--config', default=None)
    ap.add_argument('--file', action='append', default=[], help='待切 MP3，可重复')
    ap.add_argument('--scan', action='store_true', help='扫描 mp3_export.out_dir 下所有超阈值 MP3')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--force', action='store_true', help='忽略幂等，强制重切')
    ap.add_argument('--threshold', type=float, default=None, help='覆盖阈值（MB）')
    ap.add_argument('--parts', type=int, default=0, help='指定段数（0=自动）')
    ap.add_argument('--delete-original', action='store_true',
                    help='切分成功后删除完整原件（默认关闭，谨慎）')
    args = ap.parse_args()

    import common
    cfg = common.load_config(args.config)
    log = common.Logger(cfg)
    files = list(args.file)
    if args.scan:
        out_dir = (cfg.get('mp3_export') or {}).get('out_dir') or cfg['_dirs']['root']
        thr = _mi2b(args.threshold if args.threshold is not None
                    else _cfg(cfg.get('mp3_export') or {}, 'split_over_mb',
                              DEFAULT_THRESHOLD_MB))
        for name in sorted(os.listdir(out_dir)):
            if not name.lower().endswith('.mp3') or re.search(r'_Part\d+\.mp3$', name, re.I):
                continue
            p = os.path.join(out_dir, name)
            if os.path.isfile(p) and os.path.getsize(p) > thr:
                files.append(p)
    if not files:
        print('没有待处理文件（可用 --file 指定，或 --scan 扫描归档目录）')
        return 0
    bad = 0
    for p in files:
        r = split_mp3(cfg, log, p, force=args.force,
                      threshold_mb=args.threshold, parts=args.parts,
                      delete_original=args.delete_original, dry_run=args.dry_run)
        if r == []:
            bad += 1
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
