#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""purge_recyclebin.py —— 分类并（可选）永久清除 D 盘回收站里的条目。

为什么需要：Windows 的删除是软删，管线每跑一场就把「原始分段 / merged.mp4 /
转写分块 wav」删掉，全进 D:\\$RECYCLE.BIN，实测堆积到 **157 GB**，磁盘一直
不释放（还会把「水位线 40GB」触发得莫名频繁）。

★ 铁律：**用户手动删的文件 = 故意删，绝不动**。
  所以本脚本按「原路径」分类，只对明确属于「本管线自动删的中间产物」的类别
  下手，其余（H_其他，尤其涉及用户其它项目的目录）一律保留并单独列出。

用法：
  python purge_recyclebin.py                 # 只报告，不删
  python purge_recyclebin.py --purge safe    # 只永久删「管线中间产物」
  python purge_recyclebin.py --purge all     # 连 H_其他 也删（需用户明确要求）
  python purge_recyclebin.py --purge safe --ext .flv --ext .mp4   # 限定扩展名
"""
from __future__ import print_function

import argparse
import glob
import os
import re
import struct
import sys
import time
from collections import defaultdict

RB = r'D:\$RECYCLE.BIN'

# 本管线自己会产生的中间产物的目录特征（只有命中这些才归入 SAFE）
PIPE_MARKERS = (
    '岁己直播自动总结',          # 工作目录：tmp/ 转写分块、merged/、锁文件
    '录播\\25788785-岁己SUI',         # 录播根：管线删掉的分段与 merged
)


def parse_i(path):
    try:
        with open(path, 'rb') as f:
            data = f.read(4096)
        if len(data) < 28:
            return None
        ver, size, ft, nlen = struct.unpack('<QQQI', data[:28])
        if ver not in (1, 2):
            return None
        raw = data[28:28 + nlen * 2]      # nlen 是字符数，UTF-16 每字符 2 字节
        if ver == 2:
            orig = raw.decode('utf-16-le', 'replace').rstrip('\x00')
        else:
            end = raw.find(b'\x00')
            orig = raw[:end if end > 0 else len(raw)].decode('mbcs', 'replace')
        return orig, size
    except Exception:
        return None


def actual_size(ipath):
    """$I 里记的是「原大小」，对目录条目会偏大；这里返回 $R 的**实测占用**。

    ★ 2026-10-08 踩坑：只信 $I 会把回收站体积算成 171.74GB，
      而 $R 实测只有 11.05GB —— 差距来自 $I 记录的是删除瞬间的逻辑大小。
      拿这个错数字去做清理决策会误导人。目录要 walk 求和。
    """
    rpath = ipath.replace('$I', '$R', 1)
    if not os.path.exists(rpath):
        return 0
    if os.path.isdir(rpath):
        total = 0
        for r, _d, fs in os.walk(rpath):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(r, f))
                except OSError:
                    pass
        return total
    try:
        return os.path.getsize(rpath)
    except OSError:
        return 0


def bucket_of(orig, exts):
    """返回 (类别, 是否属于本管线中间产物)。"""
    if not orig:
        return 'X_无法解析', False
    p = orig.replace('/', '\\')
    low = p.lower()
    ext = os.path.splitext(low)[1]
    if exts and ext not in exts:
        return 'Z_扩展名不在白名单', False
    in_pipe = any(m in p for m in PIPE_MARKERS)
    if not in_pipe:
        return 'H_其他项目(保留)', False
    # ★ 目录型条目没有扩展名（ext=''），必须靠路径特征判类，否则会掉进兜底
    #   变成「保留」：实测 _upload_auto 的投稿副本（8GB）就是这样漏掉的。
    if ext in ('', '.partial_20261005') or ext.startswith('.partial_'):
        # drop_session --quarantine 改名隔离的旧产物，可清
        if ext.startswith('.partial_'):
            return 'A3_旧隔离件(drop_session)', True
        if '_upload_auto' in p:
            return 'A2_投稿临时副本(目录)', True
        if re.search(r'\\20\d\d-\d\d-\d\d$', p):
            return 'M_整日期目录(需人工确认)', False
        if '\\tmp\\asr_' in low or low.endswith('\\asr_2026'):
            return 'B2_转写临时目录', True
        return 'W_管线目录内其他(保留)', False
    if ext in ('.flv', '.mp4', '.mkv', '.mov'):
        # 投稿分P 临时副本 / 目录：auto_upload 切分或重试残留，可清
        if '_upload_auto' in p or '\\cuts\\' in low or '.render.' in low:
            return 'A2_投稿分P临时件', True
        # 整个「日期目录」被自动化删掉：里面是已上传场次的本地副本，
        # 不可逆，必须人工确认，不进 safe
        if re.search(r'\\20\d\d-\d\d-\d\d$', p):
            return 'M_整日期目录(需人工确认)', False
        return 'A_录播源分段(合并后删)', True
    if ext in ('.wav', '.mp3', '.m4a', '.aac'):
        return 'B_转写分块/音频中间件', True
    if ext in ('.txt', '.srt'):
        return 'C_转写文本(已被新版取代)', True
    if ext in ('.part', '.lock', '.heartbeat', '.stage', '.tmp', '.json~'):
        return 'E_临时锁/心跳', True
    if ext in ('.bak',) or '.bak_' in low or '.bak-' in low:
        return 'D_配置/文档备份', True
    # tmp\asr_* 转写分块目录（大小按 0 计，只是个空壳条目）
    if '\\tmp\\asr_' in low or low.endswith('\\asr_2026'):
        return 'B2_转写临时目录', True
    return 'W_管线目录内其他(保留)', False


def main():
    ap = argparse.ArgumentParser(description='D 盘回收站分类与清除')
    ap.add_argument('--purge', choices=['safe', 'all'], default=None)
    ap.add_argument('--ext', action='append', default=[],
                    help='只处理这些扩展名（可重复），如 --ext .flv')
    ap.add_argument('--yes', action='store_true', help='真的执行（否则只打印将删清单）')
    a = ap.parse_args()

    exts = set(e.lower() if e.startswith('.') else '.' + e.lower() for e in a.ext)
    groups = defaultdict(lambda: {'n': 0, 'bytes': 0, 'safe': False, 'items': []})
    total_n = total_b = 0
    for ipath in glob.glob(os.path.join(RB, 'S-*', '$I*')):
        got = parse_i(ipath)
        if not got:
            continue
        orig, size = got
        size = actual_size(ipath)          # 用 $R 实测，别用 $I 记录
        cat, safe = bucket_of(orig, exts)
        g = groups[cat]
        g['n'] += 1
        g['bytes'] += size
        g['safe'] = g['safe'] or safe
        if len(g['items']) < 8:
            g['items'].append((size, orig, ipath))
        total_n += 1
        total_b += size

    print('D:\\$RECYCLE.BIN   条目 %d 个 / %.2f GB\n' % (total_n, total_b / 1024.0 ** 3))
    for k in sorted(groups):
        g = groups[k]
        flag = '可清' if (g['safe'] and a.purge == 'safe') else '保留'
        print('[%s] %d 个 / %.2f GB   → %s' % (k, g['n'], g['bytes'] / 1024.0 ** 3, flag))
        for size, o, _ in g['items']:
            print('        %9.1f MB  %s' % (size / 1024.0 ** 2, (o or '')[:100]))
        if g['n'] > len(g['items']):
            print('        ...另有 %d 个' % (g['n'] - len(g['items'])))
        print()

    if not a.purge:
        print('未指定 --purge，仅报告。safe=只清本管线中间产物，all=连其他项目也清。')
        return 0
    if not a.yes:
        print('已加 --purge 但缺 --yes，演练模式，不执行。')
        return 0

    do_cats = [k for k, g in groups.items() if (a.purge == 'all' or g['safe'])]
    freed = n = 0
    for ipath in glob.glob(os.path.join(RB, 'S-*', '$I*')):
        got = parse_i(ipath)
        if not got:
            continue
        orig, size = got
        cat, safe = bucket_of(orig, exts)
        if cat not in do_cats:
            continue
        rpath = ipath.replace('$I', '$R', 1) if '$I' in os.path.basename(ipath) else None
        for p in (rpath, ipath):
            if not (p and os.path.exists(p)):
                continue
            try:
                n += 1
                if os.path.isdir(p):
                    # ★ $R 可能是**目录**（整日期目录、tmp/asr_* 分块目录）。
                    #   os.remove 对目录会失败，实测 79 个只删掉了 27GB/54GB ——
                    #   差额全在这些目录条目上，必须 shutil.rmtree 递归删。
                    import shutil
                    for root, _dirs, files in os.walk(p):
                        for f in files:
                            try:
                                freed += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    freed += os.path.getsize(p)
                    os.remove(p)      # os.remove 是永久删除，不进回收站
            except Exception as e:
                print('  删除失败 %s: %s' % (p, e))
    print('已永久删除 %d 个条目，释放 %.2f GB' % (n, freed / 1024.0 ** 3))
    return 0


if __name__ == '__main__':
    sys.exit(main())
