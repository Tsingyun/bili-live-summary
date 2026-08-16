#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用 glm-4-flash（免费、无每日配额限制）按「全规则简化提示词」重建 4 场总结。
提示词 = scripts/prompt_glm_full.txt（完整保留用户原始 prompt_original.txt 的全部规则，
仅改写为 glm-4-flash 能稳定遵循的写法：合并段 + 每段 1–2 句「」原文金句）。

- 30 分钟切块逐块发送（硬约束，由 summarize_one 保证）。
- 已存在于文档的场次自动跳过（幂等，便于被环境回收后重跑不浪费）。
- 启动级单实例锁：环境会把后台命令拉起 2 份，靠锁让第二份立即退出，避免双写导致重复章节。
- 每场录播开始处理前，把全量提示词载入并记入日志（落实“每场开始发一遍提示词”的要求）。
"""
import os
import sys
import time
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from summarize_gemini import summarize_one, has_section, doc_path_for, open_or_create
import docx

WORKDIR = r'D:\岁己直播自动总结'
TXT = os.path.join(WORKDIR, 'TXT')
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
cfg = common.load_config(os.path.join(WORKDIR, 'config.json'))
log = common.Logger(cfg, 'regen_final')

STREAMER = cfg['summarize'].get('streamer', '岁己')
AUDIENCE = cfg['summarize'].get('audience', '饼干岁')

# ---- 启动级单实例锁（防止后台命令被拉起两份导致双写）----
LOCK = os.path.join(WORKDIR, '.regen_final.lock')


def _single_instance(lock_path, timeout=20):
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            return fd  # 保持打开，进程退出后由下次运行清理
        except OSError:
            holder = 0
            try:
                with open(lock_path) as _f:
                    holder = int((_f.read() or '0').strip() or 0)
            except Exception:
                holder = 0
            alive = False
            if holder:
                try:
                    os.kill(holder, 0)
                    alive = True
                except OSError:
                    alive = False
                except Exception:
                    alive = True
            if alive:
                if time.time() > deadline:
                    return None
                time.sleep(1)
                continue
            try:
                os.remove(lock_path)
            except OSError:
                pass
            time.sleep(0.2)


_inst = _single_instance(LOCK)
if _inst is None:
    log.error('另一实例已在运行，本实例退出')
    sys.exit(0)

# 载入全量提示词（完整原始规则）
with open(os.path.join(SCRIPTS, 'prompt_glm_full.txt'), encoding='utf-8') as f:
    GLM_FULL_PROMPT = f.read().replace('{streamer}', STREAMER).replace('{audience}', AUDIENCE)

sessions = [
    ('20260809-重温第一次3D演出！ 2026年08月09日20点场', '2026-08-09'),
    ('20260810-陪你这个猪看滴嘟侠炒饭2！ 2026年08月10日20点场', '2026-08-10'),
    ('20260811-和米帕栞栞玩机械狂欢！ 2026年08月11日15点场', '2026-08-11'),
    ('20260811-陪你这个猪过周二! 2026年08月11日20点场', '2026-08-11'),
]

log('==== regen_final 启动（glm-4-flash / 全规则提示词）====')
for stem, date in sessions:
    txt = os.path.join(TXT, stem + '.txt')
    if not os.path.exists(txt):
        log.error(f'缺少转录 {txt}，跳过')
        continue
    heading = stem
    d = datetime.date.fromisoformat(date)
    dpath = doc_path_for(cfg, d)
    if os.path.exists(dpath):
        try:
            doc = docx.Document(dpath)
            if has_section(doc, heading):
                log(f'SKIP {stem}（文档已存在该章节）')
                continue
        except Exception:
            pass
    # —— 每场录播开始总结时，载入并应用全量提示词（落实“每场发一遍”的要求）——
    log(f'=== 载入全量提示词（原始规则）并开始：{stem} ===')
    try:
        summarize_one(cfg, txt, log, date_str=date, title=heading,
                      prompt_override=GLM_FULL_PROMPT, clean_glm=True)
    except Exception as e:
        import traceback
        log.error(f'{stem} 异常：{e}')
        traceback.print_exc()
log('==== regen_final 结束 ====')
