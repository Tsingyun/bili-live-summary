#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热词替换：解析热词表（markdown 表格）并对转写文本做纠正替换。

在「转写之后、送 AI 总结之前」对 TXT 与 SRT 各做一遍替换，保证送进模型
的文本已经是纠正过的干净文字（舰长/饼干岁/歌曲名等）。

用法（由 run_pipeline 自动调用，无需手动）：
  from hotwords import apply_to_file
  apply_to_file(txt_path, cfg, log)
"""

import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_rules(md_path):
    """解析热词表 markdown，返回 (rules, protect)。

    rules:  list[(原词, 替换为)]，用于文本替换
    protect: list[保护词]，这些短语不参与替换（避免被热词误伤）
    """
    if not md_path or not os.path.exists(md_path):
        return [], []
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()

    # 定位「保护规则（不受误伤）」章节起点，其后的表格行都是保护词
    protect_start = None
    for i, ln in enumerate(lines):
        if '保护规则' in ln and '不受误伤' in ln:
            protect_start = i
            break

    rules, protect = [], []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if not s.startswith('|'):
            continue
        parts = [p.strip() for p in s.strip('|').split('|')]
        if len(parts) < 2:
            continue
        a, b = parts[0], parts[1]
        if a in ('原词', '保护词'):        # 表头
            continue
        if not a or set(a) <= set('-: '):  # 分隔行 |---|---|
            continue
        if protect_start is not None and i > protect_start:
            protect.append(a)
        else:
            rules.append((a, b))

    # 去重（同原词保留首次出现），过滤空原词
    seen = {}
    for a, b in rules:
        if a and a not in seen:
            seen[a] = b
    rules = [(a, b) for a, b in seen.items() if a]
    return rules, protect


def apply_to_text(text, rules, protect):
    # 1) 保护占位：保护词先换成私用区占位符，替换完再还原，避免被热词误伤
    placeholders = {}
    for idx, w in enumerate(protect):
        if not w:
            continue
        token = '\uE000' + str(idx) + '\uE001'
        if token in text:
            continue
        text = text.replace(w, token)
        placeholders[token] = w
    # 2) 按原词长度降序替换，避免短词先替换破坏长词（如先替换"连麦"误伤"连卖"已无此序，但稳妥）
    for a, b in sorted(rules, key=lambda x: -len(x[0])):
        if a:
            text = text.replace(a, b)
    # 3) 还原保护词
    for token, w in placeholders.items():
        text = text.replace(token, w)
    return text


def apply_to_file(path, cfg, log=None):
    """对单个文件做热词替换（覆盖写回）。返回是否发生了改动。"""
    hw = cfg.get('hotwords') or {}
    if not hw.get('enabled', True):
        return False
    md = hw.get('path') or ''
    if md and not os.path.isabs(md) and cfg.get('_workdir'):
        # 相对路径以工作目录为基准解析，便于多主播各自放一份热词表
        cand = os.path.join(cfg['_workdir'], md)
        if os.path.exists(cand):
            md = cand
    if not md or not os.path.exists(md):
        md = os.path.join(SCRIPT_DIR, 'hotwords.md')
    rules, protect = load_rules(md)
    if not rules and not protect:
        return False
    if not path or not os.path.exists(path):
        return False
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    new = apply_to_text(text, rules, protect)
    if new != text:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new)
        if log:
            log(f'  热词替换：{os.path.basename(path)} 应用 {len(rules)} 条规则')
        return True
    return False


if __name__ == '__main__':
    # 简单自检：解析并报告规则数
    r, p = load_rules(os.path.join(SCRIPT_DIR, 'hotwords.md'))
    print(f'rules={len(r)} protect={len(p)}')
    sample = '饼干碎说上见连卖PC端点个赞晚上见'
    print('IN :', sample)
    print('OUT:', apply_to_text(sample, r, p))
