#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 glm-4-flash（免费、无每日配额限制）按简化组合提示词重建 4 场总结。

glm-4-flash 对「一次性超长复杂提示词」遵循度差，但对「单一聚焦任务」遵循良好。
本驱动改用简化组合提示词：每个 30 分钟分块一次调用，要求模型输出
「[HH:MM:SS] 总结段落（100–180字，以{streamer}开头）\n「原文金句」」，
从而稳定得到「合并段落 + 每段 1–2 句原文金句」的合规格式。

- 30 分钟切块逐块发送（硬约束，由 summarize_one 保证）。
- 已存在于文档的场次自动跳过（幂等，便于被环境回收后重跑不浪费）。
- 内容概要仍走 glm-4-flash（prose，无需复杂格式）。
"""
import sys, os, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from summarize_gemini import summarize_one, has_section, doc_path_for
import docx

WORKDIR = r'D:\岁己直播自动总结'
TXT = os.path.join(WORKDIR, 'TXT')
cfg = common.load_config(os.path.join(WORKDIR, 'config.json'))
log = common.Logger(cfg, 'regen_glm_v2')

STREAMER = cfg['summarize'].get('streamer', '岁己')
AUDIENCE = cfg['summarize'].get('audience', '饼干岁')

SIMPLE_PROMPT = (
    "你是一名直播内容编辑。下面是一场直播某个 30 分钟片段的逐句转录，每行以 [HH:MM:SS] 开头表示时间。"
    "请将其重组为 3–4 段连贯总结，并【每段总结之后紧跟 1–2 句原文金句】。严格按以下格式输出：\n"
    "【格式】逐段输出，每段两行：\n"
    "第一行：[HH:MM:SS] 一段 100–180 字的连贯总结，以「{streamer}」为主语开头；\n"
    "第二行起：1–2 句该段对应的【原文原句】金句，用中文双引号「」包裹（每句一行）。\n"
    "要求：\n"
    "1. 总结段必须合并时间相近、内容相关的片段，连贯成段，禁止逐句罗列；\n"
    "2. 金句必须是转录里真实存在的原句，不得改写或编造，长度 15–80 字；"
    "优先挑有代表性、有情绪或观点的原句，避免半截话与纯口误；\n"
    "3. 观众统一称为「{audience}」；\n"
    "4. 不要列表、不要 markdown、不要标题、不要额外解释。\n"
    "只输出上述格式的段落与金句。"
).replace('{streamer}', STREAMER).replace('{audience}', AUDIENCE)

sessions = [
    ('20260809-重温第一次3D演出！ 2026年08月09日20点场', '2026-08-09'),
    ('20260810-陪你这个猪看滴嘟侠炒饭2！ 2026年08月10日20点场', '2026-08-10'),
    ('20260811-和米帕栞栞玩机械狂欢！ 2026年08月11日15点场', '2026-08-11'),
    ('20260811-陪你这个猪过周二! 2026年08月11日20点场', '2026-08-11'),
]

log('==== regen_glm_v2 启动 ====')
for stem, date in sessions:
    txt = os.path.join(TXT, stem + '.txt')
    if not os.path.exists(txt):
        log.error(f'缺少转录 {txt}，跳过')
        continue
    heading = stem
    d = doc_path_for(cfg, datetime.date.fromisoformat(date))
    if os.path.exists(d):
        try:
            doc = docx.Document(d)
            if has_section(doc, heading):
                log(f'SKIP {stem}（文档已存在该章节）')
                continue
        except Exception:
            pass
    log(f'=== glm-4-flash 重建：{stem} ===')
    try:
        summarize_one(cfg, txt, log, date_str=date, title=heading,
                      prompt_override=SIMPLE_PROMPT, clean_glm=True)
    except Exception as e:
        import traceback
        log.error(f'{stem} 异常：{e}')
        traceback.print_exc()
log('==== regen_glm_v2 结束 ====')
