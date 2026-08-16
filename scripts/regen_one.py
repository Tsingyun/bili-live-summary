#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单场重建：对单个 stem 重新总结并写入 Word（依赖 summarize_gemini 的写锁与按标题去重）。
用法：python regen_one.py "<stem>"
stem 例如：20260809-重温第一次3D演出！ 2026年08月09日20点场
"""
import os, re, sys, traceback
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, summarize_gemini as S

cfg = common.load_config()
log = S.Logger(cfg, 'regen_one')

stem = sys.argv[1] if len(sys.argv) > 1 else None
if not stem:
    log.error('用法: python regen_one.py "<stem>"')
    sys.exit(2)

tp = os.path.join(cfg['_dirs']['txt'], stem + '.txt')
if not os.path.exists(tp):
    log.error(f'缺失 TXT：{tp}')
    sys.exit(1)

m = re.match(r'^(\d{4})(\d{2})(\d{2})-', stem)
ds = f'{m.group(1)}-{m.group(2)}-{m.group(3)}' if m else None
log(f'==== 单场重建 {stem} (date={ds}) ====')
try:
    r = S.summarize_one(cfg, tp, log, date_str=ds, title=stem)
    if r:
        log(f'完成 {stem}')
        log('ONE DONE')
        sys.exit(0)
    else:
        log.error(f'失败（返回空）{stem}')
        log('ONE FAILED')
        sys.exit(1)
except Exception as e:
    log.error(f'异常 {stem}: {e}')
    traceback.print_exc()
    log('ONE EXCEPTION')
    sys.exit(1)
