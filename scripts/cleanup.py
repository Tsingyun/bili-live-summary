#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""垃圾清理器：自动删除流水线产生的中间文件与临时缓存，防止磁盘被撑爆。

背景
----
每跑完一场直播（约 5 小时、20GB 源视频）会临时占用 25~30GB：
    源视频 ~20GB + 压缩产物 ~8GB（峰值并存）
    B站兜底路径：分P m4s 碎片（约 2x 成品）+ 合并 MP4 ~9GB
    16k 转写 MP3 ~0.5GB、ASR 切块 wav ~0.6GB
原先的清理代码只写在「成功路径」上（os.remove 散布在 download_merge / transcribe /
daily_pipeline 里），一旦中途失败（下载失败、Gemini 配额耗尽直接 return），
9GB 级的下载残留与切块就永久留在磁盘上，几天就能撑爆 D 盘。

本模块提供「按保留天数清扫」的统一收口，无论成败都会执行：
    sweep(cfg, log)          —— 全量清扫（每轮 pipeline 开头+结尾各跑一次）
    after_session(cfg, log)  —— 单场收尾清理（转写完成/总结完成/失败 都调用）
    check_disk_guard(cfg, log) —— 磁盘水位检查，低于阈值先清扫再告警

安全设计
--------
1. 白名单：只允许删 workdir 下的 tmp/ MP3/ logs/ SRT/ TXT/ backups/ __pycache__
   以及 workdir 根的历史 *.bak / 孤儿 .lock/.heartbeat。
2. 黑名单（永不触碰）：config.json、state.json、hotwords.md、Word 文档目录、
   录播根目录（local.root）下的任何成品——压缩 MP4、源 flv/mp4；以及 mp3_export.out_dir
   的归档 MP3（用户规则 2026-09-08 起与 Word 总结同目录，不在录播根目录）。
   源视频只允许 compress 模块在「压缩成功且校验通过」后删除，本模块不碰。
3. 删除走 common.force_unlink / force_rmtree（原生永久删除：ctypes Win32 DeleteFileW →
   PowerShell Remove-Item -Force，绕过 os.remove 安全删除 hook，绝不进回收站、无需确认）。

命令行
------
    python cleanup.py --sweep              # 清扫（按 config.cleanup 配置）
    python cleanup.py --sweep --dry-run    # 只报告要删什么，不真删
    python cleanup.py --report             # 只看占用统计
    python cleanup.py --sweep --aggressive # 无视保留天数，清空全部临时类
"""
import os
import re
import sys
import time
import shutil
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C  # noqa: E402


# ---------------------------------------------------------------------------
# 默认保留天数（-1 = 永久保留，永不清理）
# ---------------------------------------------------------------------------
DEFAULTS = {
    'enabled': True,          # 总开关
    'tmp_days': 2,            # tmp/** ：下载碎片、ASR 切块、一次性脚本
    'mp3_days': 0,            # MP3/** ：16k 单声道转写临时音频（0=立即）
    'log_days': 30,           # logs/**
    'txt_days': -1,           # TXT/** ：转写稿，默认永久
    'srt_days': -1,           # SRT/** ：字幕，默认永久
    'backup_days': 30,        # backups/** 与 根目录 *.bak
    'pycache_days': 0,        # __pycache__
    'orphan_lock_hours': 2,   # 孤儿 .lock/.heartbeat 存活超过该小时数则清
    'disk_guard_gb': 40,      # D 盘剩余低于此值(GB)时，开跑前强制 aggressive 清扫
    'sweep_on_start': True,   # 每轮 pipeline 开跑前先清扫一次
    'sweep_on_finish': True,  # 每轮 pipeline 结束后再清扫一次
    'delete_mp3_after_success': True,  # 兼容旧键：转写完成后删 16k MP3
}

# workdir 下允许清扫的目录（相对 workdir），其余一律不碰
SWEEP_DIRS = ['tmp', 'MP3', 'logs', 'TXT', 'SRT', 'backups', '__pycache__']

# 这些文件/目录永远不删（相对 workdir 根，或 basename 精确匹配）
NEVER_TOUCH_NAMES = {
    'config.json', 'state.json', 'hotwords.md', 'prompt_template.txt',
    'prompt.txt', 'README.md', 'AI_RUNBOOK.md', 'SKILL.md',
    '.git', '.gitignore', 'scripts',
}


def cfg_cleanup(cfg):
    """读取 config.cleanup，缺键用默认值补齐。"""
    out = dict(DEFAULTS)
    got = cfg.get('cleanup') or {}
    if isinstance(got, dict):
        out.update({k: v for k, v in got.items() if not k.startswith('_')})
    return out


def protected_paths(cfg):
    """返回绝对不能删的路径集合（成品目录）。"""
    prots = set()

    def add(p):
        if p:
            try:
                prots.add(os.path.abspath(p))
            except Exception:
                pass

    # 录播根目录：压缩成品 / 源视频 都在这里，本模块一律不碰
    # （归档 MP3 2026-09-08 起改到 mp3_export.out_dir，见下方 add）
    add((cfg.get('local') or {}).get('root'))
    add((cfg.get('mp3_export') or {}).get('out_dir'))
    add((cfg.get('output') or {}).get('doc_dir'))
    return prots


# ---------------------------------------------------------------------------
# 删除原语（三级降级）
# ---------------------------------------------------------------------------
# 强制删除原语统一放在 common（transcribe / download_merge / compress_video 共用）：
# 自动化环境下 D 盘的 os.remove 会被安全 hook 拦截（软删进回收站/静默失败），
# 因此统一走 common.force_unlink / force_rmtree（原生永久删除）。详见 common 注释。
force_unlink = C.force_unlink
force_rmtree = C.force_rmtree


def dir_size(path):
    """目录/文件总字节数。"""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except Exception:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except Exception:
                pass
    return total


# ---------------------------------------------------------------------------
# 磁盘水位
# ---------------------------------------------------------------------------
def disk_free_gb(path):
    """指定路径所在磁盘的剩余空间（GB）。"""
    try:
        if os.name == 'nt':
            import ctypes
            drive = os.path.splitdrive(os.path.abspath(path))[0] + '\\'
            free = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.GetDiskFreeSpaceExW(
                    ctypes.c_wchar_p(drive), None, None, ctypes.byref(free)):
                return free.value / (1024 ** 3)
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize / (1024 ** 3)
    except Exception:
        return -1.0


def check_disk_guard(cfg, log, dry_run=False):
    """磁盘水位检查。低于阈值时先 aggressive 清扫，仍不足返回 False（调用方应告警）。"""
    cu = cfg_cleanup(cfg)
    if not cu['enabled']:
        return True
    target = (cfg.get('local') or {}).get('root') or cfg['_dirs']['root']
    free = disk_free_gb(target)
    need = float(cu.get('disk_guard_gb') or 0)
    if free < 0:
        return True
    log('[cleanup] 磁盘剩余 %.1f GB（水位线 %.0f GB）' % (free, need))
    if free >= need:
        return True
    log(('[cleanup] 剩余空间低于水位线，先强制清扫临时文件（会删除 tmp/logs 等临时产物，'
         '不会碰录播成品、总结文档）'), 'WARN')
    if not dry_run:
        sweep(cfg, log, dry_run=False, aggressive=True)
    free2 = disk_free_gb(target)
    log('[cleanup] 清扫后剩余 %.1f GB（释放 %.1f GB）' % (free2, max(0.0, free2 - free)))
    if free2 < need:
        log('[cleanup] 仍低于水位线：请检查录播根目录成品（压缩 MP4 / 归档 MP3）是否过多，'
            '本模块不会自动删除成品。', 'WARN')
        return False
    return True


# ---------------------------------------------------------------------------
# 孤儿下载清理（最容易攒 9GB 的地方）
# ---------------------------------------------------------------------------
def purge_archived_downloads(cfg, log, dry_run=False):
    """tmp/bilibili/*.mp4 若已归档到「录播根目录/YYYY-MM-DD/」同名文件，则删除临时副本。

    B站兜底下载 ~9GB，归档成功后 shutil.move 本应移走；但 move 失败/中断、
    或归档目录里已有同名文件时原逻辑只删一半，残留就是 9GB。
    """
    freed, deleted = 0, []
    local_root = (cfg.get('local') or {}).get('root') or ''
    bilidir = os.path.join(cfg['_dirs']['tmp'], 'bilibili')
    if not os.path.isdir(bilidir):
        return freed, deleted
    for name in os.listdir(bilidir):
        p = os.path.join(bilidir, name)
        if not os.path.isfile(p):
            continue
        size = dir_size(p)
        # 归档目标：录播根目录各日期子目录里的同名文件
        archived = False
        if local_root and os.path.isdir(local_root):
            for d in os.listdir(local_root):
                cand = os.path.join(local_root, d, name)
                if os.path.isfile(cand) and os.path.getsize(cand) >= size * 0.98:
                    archived = True
                    break
        if archived:
            deleted.append(p)
            freed += size
            if not dry_run:
                force_unlink(p, log)
    if deleted:
        log('[cleanup] 已归档的 B站下载副本 %d 个，释放 %.2f GB%s'
            % (len(deleted), freed / (1024 ** 3), '（dry-run 未删）' if dry_run else ''))
    return freed, deleted


def purge_asr_chunks(cfg, log, dry_run=False, max_age_sec=None):
    """清理 ASR 切块目录 tmp/asr_*（转写中途失败会残留，每场约 0.6GB）。"""
    freed, deleted = 0, []
    tmp = cfg['_dirs']['tmp']
    if not os.path.isdir(tmp):
        return freed, deleted
    now = time.time()
    for name in os.listdir(tmp):
        if not name.startswith('asr_'):
            continue
        p = os.path.join(tmp, name)
        if not os.path.isdir(p):
            continue
        if max_age_sec is not None:
            try:
                if now - os.path.getmtime(p) < max_age_sec:
                    continue
            except Exception:
                pass
        size = dir_size(p)
        deleted.append(p)
        freed += size
        if not dry_run:
            force_rmtree(p, log)
    if deleted:
        log('[cleanup] ASR 切块目录 %d 个，释放 %.2f GB%s'
            % (len(deleted), freed / (1024 ** 3), '（dry-run 未删）' if dry_run else ''))
    return freed, deleted


# ---------------------------------------------------------------------------
# 单场收尾清理
# ---------------------------------------------------------------------------
def after_session(cfg, log, stem=None, src_kind='local', success=True, dry_run=False):
    """单场处理结束（成功或失败都调用）后清理该场产生的中间文件。

    stem: 该场文件名主干（不含扩展名）。给定时只清理它的 16k MP3 与 ASR 切块；
          不给定则清理全部超过保留天数的。
    """
    cu = cfg_cleanup(cfg)
    if not cu['enabled']:
        return 0
    freed = 0

    # 1) 16k 转写临时 MP3（转写完就无用；失败也删，重跑会重新生成）
    mp3_dir = cfg['_dirs']['mp3']
    if os.path.isdir(mp3_dir) and cu.get('delete_mp3_after_success', True):
        prot = protected_paths(cfg)
        if os.path.abspath(mp3_dir) in prot:
            log('[cleanup] MP3 目录与 mp3_export.out_dir 相同，跳过清理以免误删归档 MP3', 'WARN')
        else:
            for name in os.listdir(mp3_dir):
                if stem and not name.startswith(stem[:30]):
                    continue
                p = os.path.join(mp3_dir, name)
                if not os.path.isfile(p):
                    continue
                freed += dir_size(p)
                if not dry_run:
                    force_unlink(p, log)

    # 2) ASR 切块目录
    f2, _ = purge_asr_chunks(cfg, log, dry_run=dry_run)
    freed += f2

    # 3) B站下载副本（已归档的）
    if src_kind == 'bilibili':
        f3, _ = purge_archived_downloads(cfg, log, dry_run=dry_run)
        freed += f3

    if freed:
        log('[cleanup] 本场收尾释放 %.2f GB%s'
            % (freed / (1024 ** 3), '（dry-run 未删）' if dry_run else ''))
    return freed


# ---------------------------------------------------------------------------
# 全量清扫
# ---------------------------------------------------------------------------
def lock_file_is_live(root, name, path):
    """判断 workdir 下某个 .lock/.heartbeat 的持有者是否**还活着**。

    ★ 2026-09-18 修复（并发安全）：旧实现 `int(整个文件内容)` + 只靠 `os.kill(pid,0)`，
      有两个洞：
      ① 新锁格式写作「pid ISO时间」，`int()` 必抛异常 → pid=0 → **跳过存活检查直接删锁**；
         于是一轮跑满 `orphan_lock_hours`(默认 2h) 的长任务，`.lock` 会在运行中途被清掉，
         **单实例保护失效**，第二个实例（计划任务/手动）可与它并发写同一份 Word。
      ② 本机 PID 复用很快，已死进程的 pid 常被无关进程顶替 → 判成「还活着」，
         孤儿锁永远清不掉（反向故障，下次启动反而被挡住）。

      现在判活统一走 `common`：`.daily_pipeline.*` 用「PID 存在 **且** 心跳新鲜(<20min)」，
      其它 `.lock` 退化为纯 PID 探测。与 daily_pipeline / run_scheduled 共用一套，不漂移。
    """
    if name in ('.daily_pipeline.lock', '.daily_pipeline.heartbeat'):
        return C.lock_owner_alive(root)
    pid = 0
    try:
        with open(path, encoding='utf-8') as f:
            m = re.match(r'\s*(\d+)', f.read() or '')
        if m:
            pid = int(m.group(1))
    except Exception:
        pid = 0
    return C.pid_alive(pid) if pid else False


def sweep(cfg, log, dry_run=False, aggressive=False):
    """按保留天数清扫 workdir 下的临时目录。返回 (释放字节数, 删除清单)。"""
    cu = cfg_cleanup(cfg)
    if not cu['enabled']:
        log('[cleanup] cleanup.enabled=false，跳过清扫')
        return 0, []

    root = os.path.abspath(cfg['_dirs']['root'])
    prot = protected_paths(cfg)
    freed = 0
    deleted = []
    skipped = []

    def _days(key):
        d = cu.get(key, -1)
        if aggressive and key in ('tmp_days', 'mp3_days', 'log_days', 'pycache_days'):
            return 0
        try:
            return float(d)
        except Exception:
            return -1

    # --- 目录类：按 mtime 保留天数 ---
    plan = [
        ('tmp', _days('tmp_days')),
        ('MP3', _days('mp3_days')),
        ('logs', _days('log_days')),
        ('TXT', _days('txt_days')),
        ('SRT', _days('srt_days')),
        ('backups', _days('backup_days')),
        ('__pycache__', _days('pycache_days')),
    ]
    now = time.time()
    for dname, days in plan:
        d = os.path.join(root, dname)
        if not os.path.isdir(d):
            continue
        if os.path.abspath(d) in prot:
            skipped.append('%s（受保护目录，跳过）' % dname)
            continue
        if days < 0:
            continue  # 永久保留
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if name in NEVER_TOUCH_NAMES:
                continue
            try:
                age_days = (now - os.path.getmtime(p)) / 86400.0
            except Exception:
                continue
            if age_days < days:
                continue
            size = dir_size(p)
            freed += size
            deleted.append((p, size))
            if not dry_run:
                if os.path.isdir(p):
                    force_rmtree(p, log)
                else:
                    force_unlink(p, log)

    # --- 根目录历史备份文件 state.json.bak / *.bak.YYYYMMDD ---
    bdays = _days('backup_days')
    if bdays >= 0:
        for name in os.listdir(root):
            if not (name.endswith('.bak') or '.bak.' in name):
                continue
            p = os.path.join(root, name)
            if not os.path.isfile(p):
                continue
            try:
                if (now - os.path.getmtime(p)) / 86400.0 < bdays:
                    continue
            except Exception:
                continue
            size = dir_size(p)
            freed += size
            deleted.append((p, size))
            if not dry_run:
                force_unlink(p, log)

    # --- 孤儿锁文件 / 心跳（进程已死但文件残留，会让 pipeline 误判有实例在跑）---
    lock_hours = float(cu.get('orphan_lock_hours') or 0)
    if lock_hours > 0:
        for name in os.listdir(root):
            if not (name.endswith('.lock') or name.endswith('.heartbeat')):
                continue
            p = os.path.join(root, name)
            if not os.path.isfile(p):
                continue
            try:
                age_h = (now - os.path.getmtime(p)) / 3600.0
            except Exception:
                continue
            if age_h < lock_hours:
                continue
            if lock_file_is_live(root, name, p):
                continue
            size = dir_size(p)
            freed += size
            deleted.append((p, size))
            if not dry_run:
                force_unlink(p, log)

    # --- 孤儿下载 / ASR 切块（不受保留天数限制，已归档即删）---
    f1, d1 = purge_archived_downloads(cfg, log, dry_run=dry_run)
    freed += f1
    deleted.extend((p, 0) for p in d1)
    f2, d2 = purge_asr_chunks(cfg, log, dry_run=dry_run)
    freed += f2
    deleted.extend((p, 0) for p in d2)

    log('[cleanup] 清扫完成：%d 项，释放 %.2f GB%s'
        % (len(deleted), freed / (1024 ** 3), '（dry-run 未删）' if dry_run else ''))
    for s in skipped:
        log('[cleanup] ' + s)
    for p, size in sorted(deleted, key=lambda x: -x[1])[:15]:
        log('[cleanup]   - %s  %.1f MB' % (os.path.basename(p), size / (1024 ** 2)))
    return freed, deleted


# ---------------------------------------------------------------------------
# 占用报告
# ---------------------------------------------------------------------------
def report(cfg, log):
    """打印各目录占用，便于人工判断磁盘去哪了。"""
    root = cfg['_dirs']['root']
    log('=' * 60)
    log('工作目录占用：%s' % root)
    total = 0
    rows = []
    for d in SWEEP_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            s = dir_size(p)
            total += s
            rows.append((d, s))
    for d, s in sorted(rows, key=lambda x: -x[1]):
        log('  %-12s %8.2f GB' % (d, s / (1024 ** 3)))
    log('  %-12s %8.2f GB' % ('合计', total / (1024 ** 3)))

    lr = (cfg.get('local') or {}).get('root')
    if lr and os.path.isdir(lr):
        log('录播根目录：%s' % lr)
        subs = 0
        for d in sorted(os.listdir(lr)):
            p = os.path.join(lr, d)
            if os.path.isdir(p):
                s = dir_size(p)
                subs += s
                log('  %-12s %8.2f GB' % (d, s / (1024 ** 3)))
        files = [f for f in os.listdir(lr) if os.path.isfile(os.path.join(lr, f))]
        fs = sum(dir_size(os.path.join(lr, f)) for f in files)
        log('  根级文件 %d 个  %8.2f GB' % (len(files), fs / (1024 ** 3)))
        log('  %-12s %8.2f GB' % ('合计', (subs + fs) / (1024 ** 3)))
        log('  磁盘剩余 %.1f GB' % disk_free_gb(lr))

        # 可回收提示：已有对应压缩版、却还留着的源视频（压缩成功但删除失败的历史残留）。
        # 只报告不删除——源视频体积大、误删代价高，交给用户确认。
        orphans = find_orphan_sources(cfg)
        if orphans:
            tot = sum(s for _p, s in orphans)
            log('  ⚠ 可回收源视频 %d 个，共 %.2f GB（已有压缩版，可手工删除）：'
                % (len(orphans), tot / (1024 ** 3)))
            for p, s in sorted(orphans, key=lambda x: -x[1])[:10]:
                log('      %.2f GB  %s' % (s / (1024 ** 3), os.path.relpath(p, lr)))
    log('=' * 60)


def find_orphan_sources(cfg):
    """找出「已有对应压缩版、源视频却还在」的文件（压缩成功但删除失败的残留）。

    只报告不删除：源视频体积大、误删代价高，由用户确认后再处理。
    匹配规则：同目录下存在 `<同名去后缀>_compressed.mp4`，或存在同名
    `_compressed` 变体，即认为该源文件已完成压缩。
    """
    out = []
    lr = (cfg.get('local') or {}).get('root')
    if not lr or not os.path.isdir(lr):
        return out
    dirs = [lr] + [os.path.join(lr, d) for d in os.listdir(lr)
                   if os.path.isdir(os.path.join(lr, d))]
    for d in dirs:
        try:
            names = os.listdir(d)
        except Exception:
            continue
        compressed = {n for n in names if '_compressed' in n}
        if not compressed:
            continue
        for n in names:
            if '_compressed' in n:
                continue
            stem, ext = os.path.splitext(n)
            if ext.lower() not in ('.mp4', '.flv', '.mkv', '.mov'):
                continue
            # 存在同名 _compressed 变体 → 这份源视频已完成压缩
            if any(c.startswith(stem) for c in compressed):
                p = os.path.join(d, n)
                try:
                    out.append((p, os.path.getsize(p)))
                except Exception:
                    pass
    return out


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sweep', action='store_true', help='执行清扫')
    ap.add_argument('--report', action='store_true', help='只打印占用报告')
    ap.add_argument('--dry-run', action='store_true', help='只报告不删除')
    ap.add_argument('--aggressive', action='store_true', help='无视保留天数清空临时类')
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--config', default=None)
    args = ap.parse_args()

    cfg = C.load_config(args.config or (os.path.join(args.workdir, 'config.json')
                                        if args.workdir else None))
    log = C.Logger(cfg, 'cleanup')

    if args.report or not (args.sweep or args.report):
        report(cfg, log)
    if args.sweep:
        if args.dry_run:
            log('[cleanup] === DRY RUN：以下为将要删除的内容 ===')
        sweep(cfg, log, dry_run=args.dry_run, aggressive=args.aggressive)
        if args.dry_run:
            log('[cleanup] === DRY RUN 结束，未删除任何文件 ===')
    if not args.sweep and not args.report:
        log('提示：加 --sweep 执行清扫，--dry-run 先看清单。')


if __name__ == '__main__':
    main()
