#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 Gemini 干净重建 4 场总结（8/9、8/10、8/11 两场）。

- 分块总结走 Gemini（gemini-3.6-flash / gemini-2.5-flash 各 20/天硬上限，
  按场次分配：3.6 承载 8/9+8/11-15点 共 16 块；2.5 承载 8/10+8/11-20点 共 19 块，均≤20）。
- 内容概要走 glm-4-flash（免费，不占 Gemini 每日配额）。
- 已存在于文档的场次自动跳过（幂等，便于被环境回收后重跑不浪费配额）。
- 30 分钟切块逐块发送为硬约束（由 summarize_gemini.summarize_one 保证）。
"""
import sys, os, copy, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from summarize_gemini import summarize_one, has_section, doc_path_for
import docx, requests

WORKDIR = r'D:\岁己直播自动总结'
TXT = os.path.join(WORKDIR, 'TXT')

# ── 密钥脱敏（2026-08-16）：从 config.json 读取，不硬编码明文，避免推到 GitHub 泄露 ──
_cfg_path = os.path.join(WORKDIR, 'config.json')
try:
    _cfg = common.load_config(_cfg_path)
    GEMINI_KEY = (_cfg.get('summarize') or {}).get('api_key') or os.environ.get('GEMINI_KEY')
except Exception:
    GEMINI_KEY = os.environ.get('GEMINI_KEY')
if not GEMINI_KEY:
    raise RuntimeError('缺少 Gemini API Key：请在 config.json 的 summarize.api_key 或环境变量 GEMINI_KEY 填写')

# 原始配置（含 zhipu 概要用的 key 与 provider）
zhipu_cfg = common.load_config(os.path.join(WORKDIR, 'config.json'))
log = common.Logger(zhipu_cfg, 'regen_gemini')

# Gemini 配置（基于原始配置深拷贝后覆盖 summarize 段）
gem_cfg = copy.deepcopy(zhipu_cfg)
gem_cfg['summarize'].update({
    'provider': 'google',
    'api_key': GEMINI_KEY,
    'api_host': '',
    'proxy': 'http://127.0.0.1:7897',
})

# 场次分配：3.6-flash 承载 8/9 + 8/11-15点(共16块)；2.5-flash 承载 8/10 + 8/11-20点(共19块)
sessions = [
    ('20260809-重温第一次3D演出！ 2026年08月09日20点场', '2026-08-09', 'gemini-3.6-flash'),
    ('20260811-和米帕栞栞玩机械狂欢！ 2026年08月11日15点场', '2026-08-11', 'gemini-3.6-flash'),
    ('20260810-陪你这个猪看滴嘟侠炒饭2！ 2026年08月10日20点场', '2026-08-10', 'gemini-2.5-flash'),
    ('20260811-陪你这个猪过周二! 2026年08月11日20点场', '2026-08-11', 'gemini-2.5-flash'),
]


def preflight():
    """做一次最小 Gemini 调用，确认 key 有效且今日配额未耗尽（429=耗尽则中止）。"""
    url = (f'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash'
           f':generateContent?key={GEMINI_KEY}')
    payload = {'contents': [{'role': 'user', 'parts': [{'text': 'ping'}]}],
               'generationConfig': {'maxOutputTokens': 1}}
    try:
        r = requests.post(url, json=payload, timeout=30,
                          proxies={'http': 'http://127.0.0.1:7897', 'https': 'http://127.0.0.1:7897'})
        log(f'preflight gemini-3.6-flash status={r.status_code}')
        if r.status_code == 429:
            log.error('Gemini 今日配额已耗尽（429），中止重建，待配额重置后续跑')
            sys.exit(2)
        if r.status_code != 200:
            log.error(f'preflight 异常 {r.status_code}: {r.text[:200]}（仍尝试继续）')
    except Exception as e:
        log.error(f'preflight 连接异常（将尝试直连）：{e}')


log('==== regen_gemini 启动 ====')
preflight()

for stem, date, model in sessions:
    txt = os.path.join(TXT, stem + '.txt')
    if not os.path.exists(txt):
        log.error(f'缺少转录 {txt}，跳过')
        continue
    heading = stem
    # 幂等：已存在于文档则跳过（不重写，避免浪费配额）
    d = doc_path_for(gem_cfg, datetime.date.fromisoformat(date))
    if os.path.exists(d):
        try:
            doc = docx.Document(d)
            if has_section(doc, heading):
                log(f'SKIP {stem}（文档已存在该章节）')
                continue
        except Exception:
            pass
    gem_cfg['summarize']['model'] = model
    log(f'=== Gemini {model} 重建：{stem} ===')
    try:
        summarize_one(gem_cfg, txt, log, date_str=date, title=heading, overview_cfg=zhipu_cfg)
    except Exception as e:
        import traceback
        log.error(f'{stem} 异常：{e}')
        traceback.print_exc()
log('==== regen_gemini 结束 ====')
