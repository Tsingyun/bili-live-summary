#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VAD 段批量 ASR 离线回归（不加载 FunASR 模型、不耗 API 配额）。

覆盖：
  [1] Nano 按 batch_size_s 打批，调用次数 << 段数
  [2] 文本/时间轴按原 VAD 段顺序一一映射
  [3] asr_batch_size_s=0 → 退回旧逐段调用
  [4] 批量返回数量不符 → 自动回退逐段，不丢段
  [5] 批量抛异常 → 自动回退逐段，不丢段
  [6] 空文本段被跳过且不影响相邻段
  [7] SenseVoice 不走批量（保持旧路径）
  [8] 单批最多 64 段，防止显存峰值

跑法：python selftest_asr_batch.py（bili-archive venv）
"""
import os
import sys
from unittest import mock

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import transcribe as TR  # noqa: E402

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

    def warning(self, msg):
        self.lines.append('WARN ' + str(msg))

    warn = warning


class FakeVad:
    def __init__(self, n, step_ms=1000):
        self.n = n
        self.step_ms = step_ms

    def generate(self, input=None, **kw):
        return [{'value': [(i * self.step_ms, (i + 1) * self.step_ms)
                           for i in range(self.n)]}]


class FakeModel:
    def __init__(self, kind='nano', mode='ok', empty_every=0):
        self._asr_kind = kind
        self.mode = mode
        self.empty_every = empty_every
        self.calls = []
        self._seq = 0

    def generate(self, input=None, **kw):
        if isinstance(input, (list, tuple)):
            self.calls.append(('batch', len(input)))
            if self.mode == 'bad_count':
                return [{'text': 'x'}]
            if self.mode == 'raise':
                raise RuntimeError('batch boom')
            out = []
            for _ in input:
                txt = '' if (self.empty_every and self._seq % self.empty_every == 0) \
                    else '文本%d' % self._seq
                self._seq += 1
                out.append({'text': txt})
            return out
        self.calls.append(('one', 1))
        self._seq += 1
        return [{'text': '单段'}]


def run(n, model, batch_sec=3.0, kind='nano', step_ms=1000):
    cfg = {'transcribe': {'asr_batch_size_s': batch_sec, 'ncpu': 4}}
    log = Log()
    with mock.patch.object(TR, '_get_vad_model', lambda *a, **k: FakeVad(n, step_ms)), \
         mock.patch('soundfile.read',
                    lambda *a, **k: (np.zeros(200000, dtype='float32'), 16000)):
        return TR._vad_fallback(model, 'dummy.wav', cfg, 0.0, log), log


def main():
    # [1][2] 正常批量
    m = FakeModel()
    segs, _ = run(9, m, batch_sec=3.0)
    batches = [c for c in m.calls if c[0] == 'batch']
    check('1 9 段/3s 一批 → 3 次批量调用', len(batches) == 3, m.calls)
    check('1b 无逐段调用', all(c[0] == 'batch' for c in m.calls), m.calls)
    check('2 文本按段序映射', [s[2] for s in segs] == ['文本%d' % i for i in range(9)],
          [s[2] for s in segs])
    check('2b 时间轴按段序', [round(s[0]) for s in segs] == list(range(9)),
          [round(s[0]) for s in segs])

    # [3] 关闭批量
    m = FakeModel()
    segs, _ = run(4, m, batch_sec=0)
    check('3 batch=0 → 4 次逐段调用',
          len(m.calls) == 4 and all(c[0] == 'one' for c in m.calls), m.calls)
    check('3b 逐段文本仍完整', len(segs) == 4, segs)

    # [4] 返回数量不符
    m = FakeModel(mode='bad_count')
    segs, log = run(3, m, batch_sec=3.0)
    check('4 批量数量不符 → 逐段兜底 3 段不丢', len(segs) == 3, (len(segs), m.calls))
    check('4b 兜底有告警', any('回退逐段' in x for x in log.lines), log.lines[-3:])

    # [5] 批量异常
    m = FakeModel(mode='raise')
    segs, log = run(3, m, batch_sec=3.0)
    check('5 批量异常 → 逐段兜底 3 段不丢', len(segs) == 3, (len(segs), m.calls))
    check('5b 异常有告警', any('回退逐段' in x for x in log.lines), log.lines[-3:])

    # [6] 空文本跳过
    m = FakeModel(empty_every=3)
    segs, _ = run(9, m, batch_sec=3.0)
    check('6 空文本被跳过且相邻段不错位',
          [s[2] for s in segs] == ['文本1', '文本2', '文本4', '文本5', '文本7', '文本8'],
          [s[2] for s in segs])

    # [7] SenseVoice 保持逐段
    m = FakeModel(kind='sensevoice')
    segs, _ = run(4, m, batch_sec=3.0)
    check('7 SenseVoice 不走批量',
          len(m.calls) == 4 and all(c[0] == 'one' for c in m.calls), m.calls)

    # [8] 单批最多 64 段
    m = FakeModel()
    segs, _ = run(70, m, batch_sec=120.0, step_ms=100)
    batches = [c for c in m.calls if c[0] == 'batch']
    check('8 70 段 → 64+6 两批', [b[1] for b in batches] == [64, 6], batches)
    check('8b 70 段文本全产出', len(segs) == 70, len(segs))

    print('\n结果：%d 项通过，%d 项失败 %s' % (PASS, len(FAIL), FAIL if FAIL else ''))
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
