#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""热词替换：解析热词表（markdown 表格）并对转写文本做纠正替换。

在「转写之后、送 AI 总结之前」对 TXT 与 SRT 各做一遍替换，保证送进模型
的文本已经是纠正过的干净文字（舰长/饼干岁/歌曲名等）。

用法（由 run_pipeline 自动调用，无需手动）：
  from hotwords import apply_to_file
  apply_to_file(txt_path, cfg, log)
"""

import json
import os

try:                                   # 复用 common 的原子写：被杀也不会把转写稿写坏
    import common as _common
except Exception:                      # 被单独拷走/独立运行时的降级，不影响功能
    _common = None

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _atomic_write_text(path, text):
    """覆盖写文本：优先用 common.atomic_write_text（.part → fsync → replace），
    拿不到 common 时就地做一份等价实现。
    这样即使进程在写盘途中被打断，正式文件也不会变成半截。"""
    if _common is not None and hasattr(_common, 'atomic_write_text'):
        return _common.atomic_write_text(path, text)
    tmp = path + '.part'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise


def resolve_md_path(cfg, md=None):
    """解析生效的热词表路径：显式传入 > config.hotwords.path（相对工作目录）
    > config.hotwords.path（绝对） > skill 自带模板。"""
    md = md or (cfg or {}).get('hotwords', {}).get('path') or ''
    if md and not os.path.isabs(md) and (cfg or {}).get('_workdir'):
        cand = os.path.join(cfg['_workdir'], md)
        if os.path.exists(cand):
            return cand
    if md and os.path.isabs(md):
        return md
    return os.path.join(SCRIPT_DIR, 'hotwords.md')


def _parse_rows(md_path):
    """解析 markdown 表格，返回 (rows, protect_start)。
    rows: [(行号, 原词, 替换为)]，含重复，保留用于审计。"""
    if not md_path or not os.path.exists(md_path):
        return [], None
    with open(md_path, 'r', encoding='utf-8') as f:
        lines = f.read().splitlines()
    protect_start = None
    for i, ln in enumerate(lines):
        if '保护规则' in ln and '不受误伤' in ln:
            protect_start = i
            break
    rows = []
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
        rows.append((i + 1, a, b, protect_start is not None and i > protect_start))
    return rows, protect_start


def audit_md(md_path):
    """审计热词表：统计生效规则数，并找出「说了要替换但其实没生效」的词条。

    返回 dict：
      rows       表内数据行数（含重复）
      rules      去重后实际生效的规则数
      protect    保护词数
      noop       空转行：原词 == 替换为，replace 不产生任何变化
      conflicts  {原词: [多个不同目标]}，load_rules 只保留首次，其余被静默丢弃
      dangerous  [(原词, 目标, 命中原词, 命中目标)]：一条规则的**替换结果**里
                 还含有另一条非空转规则的原词，会被二次替换（可能改错）
    """
    rows, _ = _parse_rows(md_path)
    rules, protect = load_rules(md_path)

    data = [(ln, a, b) for ln, a, b, is_p in rows if not is_p]
    noop = [(ln, a) for ln, a, b in data if a == b]

    seen = {}
    for ln, a, b in data:
        seen.setdefault(a, set()).add(b)
    conflicts = {a: sorted(v) for a, v in seen.items() if len(v) > 1}

    pairs = [(a, b) for a, b in rules]
    dangerous = []
    for a, b in pairs:
        for c, d in pairs:
            if c and c != a and c != d and c in b:
                dangerous.append((a, b, c, d))

    return {
        'rules': len(rules),
        'protect': len(protect),
        'rows': len(rows),
        'noop': noop,
        'conflicts': conflicts,
        'dangerous': dangerous,
    }


def load_rules(md_path):
    """解析热词表 markdown，返回 (rules, protect)。

    rules:  list[(原词, 替换为)]，用于文本替换
    protect: list[保护词]，这些短语不参与替换（避免被热词误伤）

    解析走 _parse_rows（与 audit_md 同一份实现），保证「审计看到的」和
    「实际生效的」永远一致，不会各写一份而漂移。
    """
    seen = {}
    protect = []
    for _ln, a, b, is_protect in _parse_rows(md_path)[0]:
        if is_protect:
            protect.append(a)
        elif a and a not in seen:      # 同原词保留首次出现
            seen[a] = b
    return [(a, b) for a, b in seen.items() if a], protect


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
    md = resolve_md_path(cfg)
    rules, protect = load_rules(md)
    if not rules and not protect:
        return False
    if not path or not os.path.exists(path):
        return False
    with open(path, 'r', encoding='utf-8') as f:
        text = f.read()
    new = apply_to_text(text, rules, protect)
    if new != text:
        # 原子写：中途被打断只会留下 .part，转写稿本身不会变成半截
        _atomic_write_text(path, new)
        if log:
            log(f'  热词替换：{os.path.basename(path)} 应用 {len(rules)} 条规则、'
                f'保护 {len(protect)} 词（表：{os.path.basename(md)}）')
        return True
    return False


def _default_md():
    """不带参数时的默认表：优先工作目录里 config.json 指向的那份，其次 skill 模板。"""
    try:
        ptr = os.path.join(SCRIPT_DIR, '_workdir.json')
        if os.path.exists(ptr):
            wd = json.load(open(ptr, encoding='utf-8')).get('workdir')
            cfgp = os.path.join(wd, 'config.json')
            if wd and os.path.exists(cfgp):
                cfg = json.load(open(cfgp, encoding='utf-8'))
                cfg['_workdir'] = wd
                return resolve_md_path(cfg)
    except Exception:
        pass
    return os.path.join(SCRIPT_DIR, 'hotwords.md')


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='热词表解析 / 自检 / 审计')
    ap.add_argument('--md', help='热词表路径（默认：工作目录生效表，退化到 skill 模板）')
    ap.add_argument('--audit', action='store_true',
                    help='审计：列出空转/冲突/危险级联词条；有问题时退出码为 1')
    ap.add_argument('--text', help='演示：对这段文本跑一遍替换')
    ap.add_argument('--limit', type=int, default=20, help='审计明细最多打印条数')
    a = ap.parse_args()

    md = a.md or _default_md()
    rules, protect = load_rules(md)
    print(f'热词表 = {md}')
    print(f'rules={len(rules)} protect={len(protect)}')

    if a.audit:
        rep = audit_md(md)
        print(f"表内数据行={rep['rows']}")
        print(f"空转(原词==替换，replace 无效)={len(rep['noop'])}")
        for ln, w in rep['noop'][:a.limit]:
            print(f'    L{ln} {w}')
        print(f"冲突(同原词不同目标，后者被丢)={len(rep['conflicts'])}")
        for w, tgts in list(rep['conflicts'].items())[:a.limit]:
            print(f'    {w} -> {tgts}')
        print(f"危险级联(结果还会被别的规则改动)={len(rep['dangerous'])}")
        for x in rep['dangerous'][:a.limit]:
            print(f'    {x[0]}->{x[1]}  内含 {x[2]}->{x[3]}')
        bad = len(rep['conflicts']) + len(rep['dangerous'])
        print('审计结论:', '存在问题，需人工确认' if bad else '无冲突、无危险级联')
        raise SystemExit(1 if bad else 0)

    if a.text:
        print('IN :', a.text)
        print('OUT:', apply_to_text(a.text, rules, protect))
    else:
        sample = '饼干碎说上见连卖PC端点个赞晚上见'
        print('IN :', sample)
        print('OUT:', apply_to_text(sample, rules, protect))
