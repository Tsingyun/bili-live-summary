#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""inspect_recyclebin.py —— 只读盘点 D 盘回收站，按「原路径」归类。

为什么需要：本机 rm / 脚本删除都是软删（进 D:\\$RECYCLE.BIN），磁盘并不会
真正释放。管线每跑一场就把「合并前的原始分段 + merged.mp4」删掉，这些加起来
动辄几十 GB，全躺在回收站里。不区分「管线删的」和「用户自己删的」就清空，
会有误伤用户手动删除文件的风险（用户手动删的 = 故意删，不能动）。

$I 元数据格式（Vista+）：
    0-7   版本 (8)
    8-15  原文件大小 (8)
    16-23 删除时间 FILETIME (8)
    24-27 路径字符数 (4)
    28+   UTF-16LE 原路径
对应的实际内容在同名 $R 文件。
"""
from __future__ import print_function

import glob
import os
import struct
import sys
from collections import defaultdict

RB = r'D:\$RECYCLE.BIN'


def parse_i(path):
    try:
        with open(path, 'rb') as f:
            data = f.read(1024)
        if len(data) < 28:
            return None
        ver, size, ft, nlen = struct.unpack('<QQQI', data[:28])
        if ver not in (1, 2):
            return None
        # ★ nlen 是**字符数**，UTF-16 每字符 2 字节 → 必须 nlen*2。
        #   按 nlen 取会把路径截半（实测得到 'D:\_oss_prep\_r' 这种残缺串，
        #   导致所有条目都掉进「H_其他」分类，看着像没法处理）。
        raw = data[28:28 + nlen * 2]
        if ver == 2:
            orig = raw.decode('utf-16-le', 'replace').rstrip('\x00')
        else:
            end = raw.find(b'\x00')
            orig = raw[:end if end > 0 else len(raw)].decode('mbcs', 'replace')
        return orig, size
    except Exception:
        return None


def classify(orig):
    """把原路径归成若干类，便于决定谁该永久删除、谁必须保留。"""
    if not orig:
        return '未知'
    p = orig.replace('/', '\\')
    low = p.lower()
    if '录播' in p and (low.endswith('.flv') or low.endswith('.mp4')):
        return 'A_录播源文件(管线删)'
    if low.endswith(('.mp3', '.wav', '.m4a')):
        return 'B_音频产物'
    if low.endswith(('.docx.bak_', '.bak')) or '.bak_' in low or low.endswith('.bak'):
        return 'C_文档备份'
    if low.endswith(('.txt', '.srt')) and ('TXT' in p or 'SRT' in p):
        return 'D_转写文本'
    if low.endswith(('.part', '.heartbeat', '.stage', '.lock', '.tmp')):
        return 'E_临时锁文件'
    if '\\temp\\' in low or low.startswith('c:\\users\\tsing\\appdata\\local\\temp'):
        return 'F_Temp临时文件'
    if '__pycache__' in low or low.endswith('.pyc'):
        return 'G_缓存'
    return 'H_其他(需人工判断)'


def main():
    buckets = defaultdict(lambda: {'n': 0, 'bytes': 0, 'items': []})
    total_n = total_b = 0
    for ipath in glob.glob(os.path.join(RB, 'S-*', '$I*')):
        got = parse_i(ipath)
        if not got:
            continue
        orig, size = got
        k = classify(orig)
        buckets[k]['n'] += 1
        buckets[k]['bytes'] += size
        if len(buckets[k]['items']) < 6:
            buckets[k]['items'].append((size, orig))
        total_n += 1
        total_b += size

    print('回收站 %s' % RB)
    print('条目 %d 个，合计 %.2f GB\n' % (total_n, total_b / 1024.0 ** 3))
    for k in sorted(buckets):
        b = buckets[k]
        print('[%s] %d 个  %.2f GB' % (k, b['n'], b['bytes'] / 1024.0 ** 3))
        for size, o in b['items']:
            print('        %8.1f MB  %s' % (size / 1024.0 ** 2, (o or '')[:96]))
        if b['n'] > len(b['items']):
            print('        ... 另有 %d 个' % (b['n'] - len(b['items'])))
        print()


if __name__ == '__main__':
    sys.exit(main())
