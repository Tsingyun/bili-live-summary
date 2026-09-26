#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cleanup_originals.py —— 删除「已被压缩、但原始录播 FLV 没被删」的文件（一次性补救 + 复用门禁）。

根因（daily_pipeline 多段合并场次的 bug）：
  compress_video 只删传入的 src_video；对多段合并场次 src_video 是
  root/merged/<名>_merged.mp4（中间文件），原始 FLV 分段从没被碰过 → 每天堆积。

本脚本按与管线一致的「间隙归组」把原始分段聚成场次，只要该场次已产出
**完整** *_compressed.mp4（存在 + 体积>1MB + 有 ffprobe 时 时长≥各分段之和95%）
就判定可安全删除原始分段。门禁失败（无压缩产物 / 压缩不完整）的原文一律不动。

用法：
  python cleanup_originals.py            # 只读扫描，打印可删清单与可回收空间
  python cleanup_originals.py --apply   # 真正删除（force_unlink，失败只告警）
"""
import sys, os, re, json, datetime, argparse
SCRIPTS = r"C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts"
sys.path.insert(0, SCRIPTS)
import common
from common import load_config, probe_duration, force_unlink, Logger

NAME_RE = re.compile(
    r'^(?:录制-)?(?P<uid>\d+)-(?P<date>\d{8})-(?P<time>\d{6})-(?P<num>\d+)-'
    r'(?P<title>.+)\.(?P<ext>mp4|flv|mkv|mov|MP4|FLV|MKV|MOV)$')
_DATE_RE = re.compile(r'(\d{8})')
DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
MAX_GAP = 600


def ymd_of(fn):
    for m in _DATE_RE.finditer(fn):
        ymd = m.group(1)
        try:
            datetime.date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8]))
            return ymd
        except Exception:
            continue
    return None


def compressed_path_for(local_root, members):
    """镜像 compress_video 的 date_subfolder 命名，推算该场次压缩产物路径。"""
    first = members[0]
    base = re.sub(r'\.\w+$', '', os.path.basename(first['path']))
    ymd = ymd_of(first['fn'])
    if not ymd:
        return None
    date_folder = '%s-%s-%s' % (ymd[:4], ymd[4:6], ymd[6:8])
    cname = (base + '_compressed.mp4') if len(members) == 1 \
        else (base + '_merged_compressed.mp4')
    return os.path.join(local_root, date_folder, cname)


def run_sweep(cfg, log, dry_run=True):
    """扫描并删除「已产出完整 _compressed.mp4 但原始录播 FLV 仍在」的原文。

    门禁（与管线一致）：压缩产物存在 + 体积>1MB + ffprobe 时长≥各分段之和95%，
    任一不满足则保留原文。删除走 force_unlink（本机软删进回收站，可恢复）。
    幂等：原文已不在即跳过。可作为 daily_pipeline 的开跑前/收尾清扫步骤调用，
    也可独立 CLI 运行（python cleanup_originals.py [--apply]）。
    """
    local_root = (cfg.get('local') or {}).get('root') or ''
    ffprobe = (cfg.get('paths') or {}).get('ffprobe')
    project_root = cfg['_dirs']['root']
    merged_dir = os.path.join(project_root, 'merged')
    apply = not dry_run

    if not local_root or not os.path.isdir(local_root):
        print('本地根目录不存在：', local_root)
        return

    # ---- 收集原始媒体（根目录 + 一层 YYYY-MM-DD 日期子目录）----
    files = []
    scan_dirs = [local_root]
    for fn in sorted(os.listdir(local_root)):
        sub = os.path.join(local_root, fn)
        if os.path.isdir(sub) and DATE_DIR_RE.match(fn):
            scan_dirs.append(sub)
    for d in scan_dirs:
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        for fn in names:
            fp = os.path.join(d, fn)
            if not os.path.isfile(fp):
                continue
            if not fn.lower().endswith(('.flv', '.mp4', '.mkv', '.mov')):
                continue
            low = re.sub(r'\.\w+$', '', fn).lower()
            if low.endswith('_compressed') or low.endswith('_merged'):
                continue
            mm = NAME_RE.match(fn)
            if not mm:
                continue
            y, mo, dd = int(mm.group('date')[:4]), int(mm.group('date')[4:6]), \
                int(mm.group('date')[6:8])
            hh, mi, ss = int(mm.group('time')[:2]), int(mm.group('time')[2:4]), \
                int(mm.group('time')[4:6])
            start = datetime.datetime(y, mo, dd, hh, mi, ss)
            files.append({'path': fp, 'start': start, 'fn': fn})

    # ---- 时长 + 归组（间隙 <= MAX_GAP）----
    for f in files:
        f['dur'] = probe_duration(ffprobe, f['path']) if ffprobe else 0
        f['sz'] = os.path.getsize(f['path']) if os.path.lexists(f['path']) else 0
        f['end'] = f['start'] + datetime.timedelta(seconds=f['dur'])
    files.sort(key=lambda x: x['start'])
    chains, cur = [], []
    for f in files:
        if not cur:
            cur = [f]
            continue
        gap = (f['start'] - cur[-1]['end']).total_seconds()
        if -60 <= gap <= MAX_GAP:
            cur.append(f)
        else:
            chains.append(cur)
            cur = [f]
    if cur:
        chains.append(cur)

    # ---- 判定每场可否删原始分段 ----
    deletable, kept, kept_reason = [], [], {}
    for ch in chains:
        cp = compressed_path_for(local_root, ch)
        ok = False
        reason = ''
        if not cp:
            reason = '文件名解析不到日期'
        elif not (os.path.exists(cp) and os.path.getsize(cp) > 1024 * 1024):
            reason = '无完整压缩产物 %s' % (os.path.basename(cp) if cp else '')
        else:
            if ffprobe:
                cpd = probe_duration(ffprobe, cp) or 0
                total = sum(m['dur'] for m in ch)
                if total > 0 and (cpd <= 0 or cpd < total * 0.95):
                    reason = '压缩产物时长不足(%.0fs/源%.0fs)' % (cpd, total)
                else:
                    ok = True
            else:
                ok = True  # 无 ffprobe 时仅靠存在+体积门禁（压缩产物本就是原子改名落盘，可信）
        live = [m for m in ch if os.path.lexists(m['path']) and m['sz'] > 0]
        if ok and live:
            for m in live:
                deletable.append(m)
        else:
            if not live:
                reason = reason or '原文均已不在'
            if reason:
                kept_reason.setdefault(reason, 0)
                kept_reason[reason] += len(live)
            kept.extend(live)

    # ---- 合并中间文件残留（root/merged/<x>_merged.mp4）----
    merged_leftover = []
    if os.path.isdir(merged_dir):
        for fn in sorted(os.listdir(merged_dir)):
            if not fn.lower().endswith('_merged.mp4'):
                continue
            fp = os.path.join(merged_dir, fn)
            if not os.path.isfile(fp):
                continue
            x = re.sub(r'_merged\.mp4$', '', fn)
            ymd = ymd_of(x)
            if not ymd:
                continue
            date_folder = '%s-%s-%s' % (ymd[:4], ymd[4:6], ymd[6:8])
            cp = os.path.join(local_root, date_folder, x + '_compressed.mp4')
            if os.path.exists(cp) and os.path.getsize(cp) > 1024 * 1024:
                merged_leftover.append(fp)

    # ---- 报告 ----
    del_mb = sum(m['sz'] for m in deletable) // 1024 // 1024
    ml_mb = sum(os.path.getsize(p) for p in merged_leftover) // 1024 // 1024
    print('=' * 70)
    print('录播根目录: %s' % local_root)
    print('ffprobe   : %s' % ('有' if ffprobe else '无(仅靠存在+体积门禁)'))
    print('原始文件  : %d 个 (%.1f GB)' % (len(files),
          sum(m['sz'] for m in files) / 1024.0 ** 3))
    print('场次归组  : %d 场' % len(chains))
    print('-' * 70)
    print('【可删除原始分段】%d 个，回收约 %d MB (%.2f GB)'
          % (len(deletable), del_mb, del_mb / 1024.0))
    for m in deletable:
        print('   DEL  %s  (%d MB)' % (m['path'], m['sz'] // 1024 // 1024))
    if merged_leftover:
        print('【可删除合并残留】%d 个，回收约 %d MB' % (len(merged_leftover), ml_mb))
        for p in merged_leftover:
            print('   DEL  %s' % p)
    print('-' * 70)
    print('【保留（无完整压缩产物，不删）】%d 个' % len(kept))
    for r, n in sorted(kept_reason.items(), key=lambda x: -x[1]):
        print('   %3d 个 : %s' % (n, r))
    # 列出体积 >=1MB 但保留的原文（最可能是"还没压/压缩失败"），便于用户知悉
    big_kept = [m for m in kept if m['sz'] > 1024 * 1024]
    if big_kept:
        print('   其中体积 >=1MB 的未处理原文：')
        for m in big_kept:
            print('   KEEP %s  (%d MB)' % (m['path'], m['sz'] // 1024 // 1024))
    print('=' * 70)

    if not apply:
        print('（dry-run，未删除。加 --apply 才真正删除）')
        return

    # ---- 真正删除 ----
    n_ok = 0
    for m in deletable:
        if force_unlink(m['path'], log):
            n_ok += 1
            print('已删 %s' % m['path'])
        else:
            print('删除失败(保留) %s' % m['path'])
    for p in merged_leftover:
        if force_unlink(p, log):
            print('已删合并残留 %s' % p)
        else:
            print('删除失败(保留) %s' % p)
    print('删除完成：原始分段 %d/%d 成功' % (n_ok, len(deletable)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None, help='直接指定 config.json 路径')
    ap.add_argument('--apply', action='store_true', help='真正删除（默认 dry-run 只报告）')
    args = ap.parse_args()
    cfg = common.load_config(args.config)
    log = Logger(cfg, 'cleanup_originals')
    run_sweep(cfg, log, dry_run=not args.apply)


if __name__ == '__main__':
    main()
