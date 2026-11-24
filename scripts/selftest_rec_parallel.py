#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rec 分段并行下载离线回归（不联网、不耗配额）。

覆盖：
  [1] 并发 3 路：5 段各 sleep 0.3s → 总耗时 < 1.0s（旧串行 ≥1.5s）
  [2] 返回列表严格按 session 原序（concat 合并依赖该顺序）
  [3] 并发度 clamp 到段数，不会开多余线程
  [4] parallel_downloads=1 退回旧串行语义
  [5] 任一段下载返回 None → rc=3 且不吞错
  [6] 文件明显偏小（<90% 列表值）→ rc=3
  [7] 线程抛异常 → rc=3
  [8] 列表未给大小（exp=0）时放行走原有语义

跑法：python selftest_rec_parallel.py（bili-archive venv）
"""
import os
import sys
import tempfile
import threading
import time
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import rec_fetch_date as RF  # noqa: E402

PASS = 0
FAIL = []


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print('  [PASS] %s' % name)
    else:
        FAIL.append(name)
        print('  [FAIL] %s  %s' % (name, detail))


class Log:
    def __init__(self):
        self.lines = []

    def __call__(self, msg, level='INFO'):
        self.lines.append(str(msg))

    def error(self, msg):
        self.lines.append('ERROR ' + str(msg))

    def info(self, msg):
        self.lines.append(str(msg))

    def warning(self, msg):
        self.lines.append('WARN ' + str(msg))

    warn = warning

    def debug(self, msg):
        self.lines.append(str(msg))


def session(n, size_mb=10.0):
    return [{'name': 'seg%d.flv' % i, 'size': '%.1fMB' % size_mb} for i in range(n)]


def main():
    tmp = tempfile.mkdtemp(prefix='bls_rec_parallel_')
    log = Log()
    work = os.path.join(tmp, 'work')
    os.makedirs(work, exist_ok=True)

    # ---------- [1][2][3] 并发、顺序、clamp ----------
    lock = threading.Lock()
    state = {'cur': 0, 'peak': 0, 'calls': []}

    def fake_ok(cfg, _log, name, dest, expect_bytes=0):
        with lock:
            state['cur'] += 1
            state['peak'] = max(state['peak'], state['cur'])
            state['calls'].append(name)
        time.sleep(0.30)
        with open(dest, 'wb') as f:
            f.write(b'x' * (10 * 1024 * 1024))
        with lock:
            state['cur'] -= 1
        return dest

    with mock.patch.object(RF.D, 'download_one', fake_ok):
        t0 = time.time()
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 3}}, log, work, session(5))
        elapsed = time.time() - t0
    check('1 三路并发明显快于串行', rc == 0 and elapsed < 1.0,
          '%.2fs' % elapsed)
    check('2 返回顺序严格按 session 原序',
          [os.path.basename(p) for p in paths] == ['seg%d.flv' % i for i in range(5)],
          str([os.path.basename(p) for p in paths]))
    check('3 并发峰值=3（配置值）', state['peak'] == 3, state['peak'])
    check('3b 5 段全部实际调用', len(state['calls']) == 5, len(state['calls']))

    # ---------- [3c] clamp 到段数 ----------
    state.update(cur=0, peak=0, calls=[])
    with mock.patch.object(RF.D, 'download_one', fake_ok):
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 99}}, log, work, session(2))
    check('3c 并发度 clamp 到段数', rc == 0 and state['peak'] <= 2, state['peak'])

    # ---------- [4] 串行模式 ----------
    state.update(cur=0, peak=0, calls=[])
    with mock.patch.object(RF.D, 'download_one', fake_ok):
        t0 = time.time()
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 1}}, log, work, session(4))
        serial_elapsed = time.time() - t0
    check('4 parallel=1 退回串行', rc == 0 and state['peak'] == 1
          and serial_elapsed >= 1.1, '%.2fs peak=%d' % (serial_elapsed, state['peak']))

    # ---------- [5] 下载失败 ----------
    def fake_fail(cfg, _log, name, dest, expect_bytes=0):
        return None
    with mock.patch.object(RF.D, 'download_one', fake_fail):
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 3}}, log, work, session(3))
    check('5 任一段失败 → rc=3', rc == 3 and not paths, (rc, paths))

    # ---------- [6] 大小偏小 ----------
    def fake_short(cfg, _log, name, dest, expect_bytes=0):
        with open(dest, 'wb') as f:
            f.write(b'x' * (5 * 1024 * 1024))  # 列表 10MB，只给 5MB
        return dest
    with mock.patch.object(RF.D, 'download_one', fake_short):
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 3}}, log, work, session(3))
    check('6 文件偏小 → rc=3', rc == 3, (rc, paths))

    # ---------- [7] 线程异常 ----------
    def fake_raise(cfg, _log, name, dest, expect_bytes=0):
        raise RuntimeError('boom')
    with mock.patch.object(RF.D, 'download_one', fake_raise):
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 3}}, log, work, session(3))
    check('7 线程异常 → rc=3', rc == 3, (rc, paths))

    # ---------- [8] 列表未给大小 ----------
    def fake_zero(cfg, _log, name, dest, expect_bytes=0):
        with open(dest, 'wb') as f:
            f.write(b'x' * 4096)
        return dest
    sess0 = [{'name': 'nometa.flv', 'size': ''}]
    with mock.patch.object(RF.D, 'download_one', fake_zero):
        paths, rc = RF._download_session(
            {'darkpy': {'parallel_downloads': 3}}, log, work, sess0)
    check('8 列表无大小 → 放行（沿用旧语义）', rc == 0 and len(paths) == 1, (rc, paths))

    print('\n结果：%d 项通过，%d 项失败 %s' % (PASS, len(FAIL), FAIL if FAIL else ''))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
