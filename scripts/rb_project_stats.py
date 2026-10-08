# -*- coding: utf-8 -*-
"""rb_project_stats.py —— 只读：按「项目根目录」统计回收站占用。

用途：purge_recyclebin 只按管线标记分「safe / 其他」，但「其他」里可能
混着好几个项目（岁己歌切 / _oss_prep / …）。要决定能不能清，得先看清
每个项目各占多少、各删了什么。本脚本只统计，不删。
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
            data = f.read(4096)
        if len(data) < 28:
            return None
        ver, size, ft, nlen = struct.unpack('<QQQI', data[:28])
        if ver not in (1, 2):
            return None
        raw = data[28:28 + nlen * 2]
        orig = raw.decode('utf-16-le', 'replace').rstrip('\x00') if ver == 2 else raw.decode('mbcs', 'replace')
        return orig, size
    except Exception:
        return None


def project_of(p):
    """取路径前两级当项目标识：D:\\岁己歌切\\... → 岁己歌切。"""
    parts = [x for x in p.replace('/', '\\').split('\\') if x]
    if len(parts) >= 2:
        return parts[1] if parts[0].endswith(':') else parts[0]
    return parts[0] if parts else '(空)'


def main():
    g = defaultdict(lambda: {'n': 0, 'b': 0, 'ext': defaultdict(int), 'sample': []})
    for ip in glob.glob(os.path.join(RB, 'S-*', '$I*')):
        got = parse_i(ip)
        if not got:
            continue
        orig, size = got
        pr = project_of(orig or '')
        e = g[pr]
        e['n'] += 1
        e['b'] += size
        ext = os.path.splitext((orig or '').lower())[1] or '(目录/无扩展)'
        e['ext'][ext] += size
        if len(e['sample']) < 3:
            e['sample'].append((size, orig))

    total_n = sum(v['n'] for v in g.values())
    total_b = sum(v['b'] for v in g.values())
    print('回收站条目 %d 个 / %.2f GB\n' % (total_n, total_b / 1024.0 ** 3))
    for pr in sorted(g, key=lambda k: -g[k]['b']):
        e = g[pr]
        print('[%s]  %d 个 / %.2f GB' % (pr, e['n'], e['b'] / 1024.0 ** 3))
        for ext, b in sorted(e['ext'].items(), key=lambda x: -x[1])[:5]:
            print('      %-16s %8.2f GB' % (ext, b / 1024.0 ** 3))
        for size, o in e['sample']:
            print('      e.g. %8.1f MB  %s' % (size / 1024.0 ** 2, (o or '')[:88]))
        print()


if __name__ == '__main__':
    sys.exit(main())