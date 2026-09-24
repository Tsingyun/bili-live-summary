#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档健康检查与修复：格式错误检测/纠正 + 异常信息清理 + 热词替换校验。

背景（2026-09-24，9/23 那场暴露）Word 成品出现三类问题：
  ① 金句本该是「灰色斜体」，却有 24 处变成粗体、且下划线裸露——模型（gemini-2.5-flash）
     给金句套了 markdown 斜体 `“…”`，写入侧旧清理只剥 ** 与 __，单下划线漏网，
     于是 is_quote_line 判否 → 当正文加粗。
  ② 出现模型回显的提示词（「输入文本每一行都以 [HH:MM:SS] 开头…」）与元话语开场白
     （「好的，这是对输入文本进行分段重组与精炼总结后的内容：」），被当正文写入。
  ③ 金句里仍有未被热词表纠正的错字（部分是表里缺词条，部分是模型改写引入）。

写入侧已在 summarize_gemini.append_section 做了**源头拦截**（剥 markdown + 丢弃
提示词回显/元话语）；本脚本是**产物侧的自检与补救**：既能查、也能原地修，
并在每日管线收尾时自动执行一次，保证「已经写进文档的」问题也被持续纠正。

用法：
  python docx_healthcheck.py --workdir "D:/岁己直播自动总结"             # 只体检，不改
  python docx_healthcheck.py --workdir "D:/岁己直播自动总结" --fix       # 体检并原地修复
  python docx_healthcheck.py --doc "C:/.../岁己直播总结_2026年9月.docx" --fix
  python docx_healthcheck.py --workdir ... --fix --json report.json

退出码：0 = 无问题（或 --fix 后已解决）；1 = 仍有未解决问题。
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import common as C
import hotwords as HW
import summarize_gemini as S

from docx import Document
from docx.shared import Pt, RGBColor

# 与 append_section 保持一致的排版常量
QUOTE_SIZE = 10.0
QUOTE_COLOR = '666666'
BODY_SIZE = 11.0

_TS_PREFIX = re.compile(r'^\[\d{1,2}:\d{2}:\d{2}\]\s*')
_DATE_HEAD = re.compile(r'\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日')
_META_PREFIX = re.compile(r'^(直播日期|来源|总结于)\b')
# 备份/历史快照文件不参与体检（它们是归档，不该被改写）
_BACKUP_HINT = re.compile(r'(\.bak|_pre\d|blockfix|_clean|~\$|_old|_backup)', re.I)


def is_backup_file(path):
    return bool(_BACKUP_HINT.search(os.path.basename(path)))


def _has_residue(text, orig):
    """判断 text 里是否残留热词原词 orig。

    ★ 关键：英文/数字原词必须按**词边界**匹配，否则 `PC` 会命中 `NPC`（实测误报 21 处）。
      中文原词无词边界概念，用普通子串匹配。
    """
    if orig.isascii():
        return re.search(r'(?<![A-Za-z0-9])%s(?![A-Za-z0-9])' % re.escape(orig), text) is not None
    return orig in text


class _Log:
    """轻量日志：兼容 Logger 的 __call__ / error / warn 接口。"""

    def __init__(self, quiet=False, tag='healthcheck'):
        self.quiet = quiet
        self.tag = tag

    def _emit(self, msg):
        if not self.quiet:
            try:
                sys.stdout.buffer.write((str(msg) + '\n').encode('utf-8', 'replace'))
                sys.stdout.buffer.flush()
            except Exception:
                pass

    def __call__(self, msg, level='INFO'):
        self._emit(f'[{level}] {msg}')

    def info(self, msg):
        self._emit(f'[INFO] {msg}')

    def warn(self, msg):
        self._emit(f'[WARN] {msg}')

    def error(self, msg):
        self._emit(f'[ERROR] {msg}')


# ---------------- 底层小工具 ----------------
def _run_color(run):
    """返回 run 的字体颜色（十六进制大写）或 None（自动）。"""
    try:
        c = run.font.color
        if c is None or c.rgb is None:
            return None
        return str(c.rgb).upper()
    except Exception:
        return None


def _set_para_text(p, new_text):
    """把段落文本整体替换为 new_text（保留首个 run 的样式，清空其余 run）。"""
    runs = p.runs
    if not runs:
        return
    runs[0].text = new_text
    for r in runs[1:]:
        r.text = ''


def _delete_para(p):
    el = p._element
    parent = el.getparent()
    if parent is not None:
        parent.remove(el)


def _style_as_quote(run, font):
    S.set_font(run, font)
    run.bold = False
    run.italic = True
    run.font.size = Pt(QUOTE_SIZE)
    run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)


def _style_as_body(run, font):
    S.set_font(run, font)
    run.bold = True
    run.italic = False
    run.font.size = Pt(BODY_SIZE)
    try:
        run.font.color.rgb = None      # 恢复自动色
    except Exception:
        pass


# ---------------- 主检查 ----------------
def check_doc(path, hotword_rules=None, hotword_protect=None, fix=False,
              font='思源宋体CN Light', log=None):
    """检查（并在 fix=True 时修复）单个 docx。返回报告 dict。"""
    log = log or _Log()
    doc = Document(path)
    ps = doc.paragraphs

    issues = {'quote_format': [], 'md_residue': [], 'abnormal': [],
              'hotword_residue': [], 'structure': []}
    to_delete = []
    fixed = 0
    sections = []          # (idx, 日期标题文本)

    for i, p in enumerate(ps):
        t = p.text.strip()
        if not t:
            continue
        if not p.runs:
            continue

        # --- 章节定位（供结构统计）---
        if (_DATE_HEAD.search(t) and len(t) < 120
                and not _TS_PREFIX.match(t) and not _META_PREFIX.match(t)):
            sections.append((i, t))

        # --- ① 异常内容：提示词回显 / 元话语 ---
        hits = S.prompt_leak_hits(t)
        if hits or S.is_meta_line(t):
            issues['abnormal'].append({
                'idx': i, 'kind': 'prompt-leak' if hits else 'meta-talk',
                'sev': 'high', 'text': t[:100],
                'detail': ('命中提示词指纹：%s' % hits[0][:40]) if hits else '模型元话语开场白'})
            to_delete.append(p)
            continue

        clean = S.strip_md_markers(t)

        # --- ② markdown 标记残留 ---
        if clean != t:
            issues['md_residue'].append({
                'idx': i, 'sev': 'mid', 'text': t[:100], 'clean': clean[:100]})
            if fix:
                _set_para_text(p, clean)
                t = clean
                fixed += 1

        r0 = p.runs[0] if p.runs else None
        if r0 is None:
            continue

        # --- ③ 金句格式：应为灰色斜体 10pt、非粗体 ---
        if S.is_quote_line(t):
            bad = []
            if r0.bold:                       bad.append('被加粗')
            if not r0.italic:                 bad.append('缺斜体')
            col = _run_color(r0)
            if col not in (QUOTE_COLOR, None):
                bad.append('颜色 %s' % col)
            try:
                sz = r0.font.size.pt if r0.font.size else None
            except Exception:
                sz = None
            if sz is not None and abs(sz - QUOTE_SIZE) > 0.01:
                bad.append('字号 %s' % sz)
            if bad:
                issues['quote_format'].append({
                    'idx': i, 'sev': 'high', 'text': t[:100], 'detail': '、'.join(bad)})
                if fix:
                    for r in p.runs:
                        _style_as_quote(r, font)
                    fixed += 1
        # --- 正文段落：应为粗体 11pt、非斜体 ---
        elif _TS_PREFIX.match(t):
            bad = []
            if not r0.bold:   bad.append('未加粗')
            if r0.italic:     bad.append('误加斜体')
            if bad:
                issues['quote_format'].append({
                    'idx': i, 'sev': 'mid', 'text': t[:100],
                    'detail': '正文段落' + '、'.join(bad)})
                if fix:
                    for r in p.runs:
                        _style_as_body(r, font)
                    fixed += 1

        # --- ④ 热词校验：文档里是否残留热词表的「原词」---
        if hotword_rules:
            for a, b in hotword_rules:
                if not a or a == b or len(a) < 2:
                    continue
                if _has_residue(t, a):
                    issues['hotword_residue'].append({
                        'idx': i, 'sev': 'mid', 'orig': a, 'repl': b, 'text': t[:100]})

    # --- ⑤ 结构检查：每场标题下应有 meta 段 ---
    for k, (idx, title) in enumerate(sections):
        end = sections[k + 1][0] if k + 1 < len(sections) else len(ps)
        body = [ps[j].text.strip() for j in range(idx + 1, end)]
        body = [x for x in body if x]
        n_meta = sum(1 for x in body if _META_PREFIX.match(x))
        n_quote = sum(1 for x in body if S.is_quote_line(x))
        if n_meta < 2:
            issues['structure'].append({
                'section': title[:60], 'sev': 'low',
                'detail': '元信息行可能不全（常规为「直播日期」「来源」两行），实际 %d 行' % n_meta})
        if n_quote == 0:
            issues['structure'].append({
                'section': title[:60], 'sev': 'low',
                'detail': '本章节没有任何金句（可能模型未输出，或写入时被误过滤）'})

    # --- 统一删段（索引会变，放最后）---
    if fix and to_delete:
        for p in to_delete:
            _delete_para(p)
        fixed += len(to_delete)

    # --- 保存 ---
    saved = False
    if fix and fixed:
        try:
            if os.path.exists(path):
                import shutil
                shutil.copy2(path, path + '.bak')
        except Exception:
            pass
        S.save_doc_atomic(doc, path, log)
        saved = True

    total = sum(len(v) for v in issues.values())
    # 严重度 high/mid 才算「必须处理」（low 是结构提示，不阻塞）
    blocking = sum(1 for v in issues.values() for it in v
                   if it.get('sev') in ('high', 'mid'))
    report = {
        'doc': path,
        'paragraphs': len(ps),
        'sections': [t for _, t in sections],
        'n_sections': len(sections),
        'counts': {k: len(v) for k, v in issues.items()},
        'total': total,
        'blocking': blocking,
        'fixed': fixed if fix else 0,
        'saved': saved,
        'issues': issues,
        'checked_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
    }
    return report


def resolve_rules(cfg, workdir=None):
    """按工作目录 config 解析出生效的热词规则与保护词。"""
    rules, protect = [], []
    try:
        md = (cfg.get('hotwords') or {}).get('path') or ''
        if md and not os.path.isabs(md):
            wd = cfg.get('_workdir') or workdir
            if wd:
                cand = os.path.join(wd, md)
                if os.path.exists(cand):
                    md = cand
        if not md or not os.path.exists(md):
            md = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hotwords.md')
        rules, protect = HW.load_rules(md)
    except Exception:
        pass
    return rules, protect


def print_report(rep, verbose=False):
    c = rep['counts']
    print('')
    print('=== 文档健康检查：%s ===' % os.path.basename(rep['doc']))
    print('段落 %d | 章节 %d | 检查时间 %s'
          % (rep['paragraphs'], rep['n_sections'], rep['checked_at']))
    print('  [A] 金句/正文格式异常      : %d 处' % c.get('quote_format', 0))
    print('  [B] markdown 标记残留      : %d 处' % c.get('md_residue', 0))
    print('  [C] 异常内容(提示词/元话语) : %d 处' % c.get('abnormal', 0))
    print('  [D] 热词未纠正残留          : %d 处' % c.get('hotword_residue', 0))
    print('  [E] 结构提示               : %d 条' % c.get('structure', 0))
    if rep['fixed']:
        print('  → 已修复 %d 处%s' % (rep['fixed'], '（已保存）' if rep['saved'] else ''))
    after = rep.get('blocking_after', rep['blocking'])
    if rep.get('fixed'):
        print('  → 复检后仍需处理（严重项）：%d 处' % after)
    else:
        print('  → 需处理（严重项）：%d 处' % after)
    if verbose:
        for key, label in (('abnormal', '异常内容'), ('quote_format', '格式异常'),
                           ('md_residue', 'markdown 残留'), ('structure', '结构')):
            for it in rep['issues'].get(key, [])[:15]:
                print('     · [%s] %s | %s' % (key, (it.get('section') or it.get('idx')),
                                               (it.get('detail') or '')[:60]))
        hw = rep['issues'].get('hotword_residue', [])
        if hw:
            agg = {}
            for it in hw:
                agg[(it['orig'], it['repl'])] = agg.get((it['orig'], it['repl']), 0) + 1
            print('     · 热词残留汇总（原词 → 应替换为，出现段落数）：')
            for (a, b), n in sorted(agg.items(), key=lambda x: -x[1])[:25]:
                print('         %-14s → %-14s  x%d' % (a, b, n))
    print('')


def main():
    ap = argparse.ArgumentParser(description='直播总结 Word 健康检查与修复')
    ap.add_argument('--workdir', default=None, help='工作目录（含 config.json）')
    ap.add_argument('--doc', default=None, help='只检查指定 docx')
    ap.add_argument('--fix', action='store_true', help='原地修复（会先留 .bak）')
    ap.add_argument('--json', default=None, help='把完整报告写到该 JSON 路径')
    ap.add_argument('--verbose', action='store_true', help='打印明细')
    ap.add_argument('--quiet', action='store_true', help='只在有问题时输出')
    args = ap.parse_args()

    log = _Log(quiet=args.quiet)
    cfg = {}
    if args.workdir:
        cfg_path = args.workdir
        if os.path.isdir(cfg_path):
            cfg_path = os.path.join(cfg_path, 'config.json')
        try:
            cfg = C.load_config(cfg_path)
        except Exception as e:
            log.warn('读取 config 失败（将跳过热词校验）：%s' % e)
            cfg = {}
    rules, protect = resolve_rules(cfg, args.workdir)
    font = (cfg.get('output') or {}).get('font', '思源宋体CN Light')

    # 目标文档列表
    docs = []
    if args.doc:
        docs = [args.doc]
    else:
        out_dir = (cfg.get('output') or {}).get('doc_dir')
        if not out_dir and args.workdir:
            out_dir = os.path.join(args.workdir, 'output')
        if out_dir and os.path.isdir(out_dir):
            docs = sorted(glob.glob(os.path.join(out_dir, '*直播总结_*.docx')))
            docs = [d for d in docs if not is_backup_file(d)]
    if not docs:
        log.warn('没有找到需要检查的文档')
        return 0

    reports, bad = [], 0
    for d in docs:
        try:
            rep = check_doc(d, rules, protect, fix=args.fix, font=font, log=log)
            if args.fix and rep['fixed']:
                # 复检：确认修复真的落地（也用于退出码判定）
                rep2 = check_doc(d, rules, protect, fix=False, font=font,
                                 log=_Log(quiet=True))
                rep['issues_after'] = rep2['issues']
                rep['counts_after'] = rep2['counts']
                rep['blocking_after'] = rep2['blocking']
            else:
                rep['blocking_after'] = rep['blocking']
        except Exception as e:
            log.error('检查失败 %s：%s' % (d, e))
            bad += 1
            continue
        reports.append(rep)
        if rep['blocking_after'] > 0:
            bad += 1
        if not args.quiet or rep['total']:
            print_report(rep, verbose=args.verbose)

    if args.json:
        try:
            with open(args.json, 'w', encoding='utf-8') as f:
                json.dump(reports, f, ensure_ascii=False, indent=2)
            log.info('报告已写入 %s' % args.json)
        except Exception as e:
            log.warn('写报告失败：%s' % e)

    if args.quiet:
        log.info('健康检查完成：%d 个文档，%d 个仍有问题'
                 % (len(reports), bad))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
