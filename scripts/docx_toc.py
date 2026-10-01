#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
docx_toc.py —— 给已生成的月度总结文档补上「标题样式 / 大纲级别 / 自动目录」。

背景
----
Word 里能让你「一点就跳过去」的东西由三件套组成：
  1. 标题样式：内置样式「标题 1 / 标题 2 / 标题 3」（英文 Heading 1/2/3）；
  2. 大纲级别：每段标题带 1~9 级 Outline Level，正文是「正文文本」级；
  3. 导航窗格（视图 → 显示 → 导航窗格）与自动目录（引用 → 目录，本质是 TOC 域）
     都是按大纲级别收集条目的，目录可右键「更新域」或 Ctrl+A 后 F9 刷新。

本脚本做的事（正文一字不改）：
  - 给所有场次标题补/修正大纲级别（幂等，重复跑无副作用）；
  - 在文档开头插入：标题 1（月份大标题）→「目 录」→ 可自动更新的 TOC 域 → 分页符；
  - 已有 TOC 域时不再重复插入，只修大纲级别。

用法
----
  python docx_toc.py "C:/Users/Tsing/DeskBox/文档/岁己直播总结_2026年9月.docx"
  python docx_toc.py 文档1.docx 文档2.docx --levels 1-2
  python docx_toc.py 文档.docx --title "2026年9月 直播总结"   # 自定义月份大标题
  python docx_toc.py 文档.docx --dry-run                      # 只体检不落盘

注意：文档若正被 Word / 腾讯文档打开，保存会走「原地覆写」兜底并被警告，
      此时请关闭编辑器重新打开，避免编辑器里的旧内容反向覆盖。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document          # noqa: E402
import summarize_gemini as SG      # noqa: E402

DEFAULT_FONT = '思源宋体CN Light'


def scan(doc):
    """统计文档当前的标题结构（不修改）。"""
    lv1 = lv2 = other = 0
    for p in doc.paragraphs:
        lv = SG.heading_level(p)
        if lv == 1:
            lv1 += 1
        elif lv == 2:
            lv2 += 1
        elif lv:
            other += 1
    return lv1, lv2, other, SG.has_toc(doc)


def process(path, font, levels, title, dry_run, backup=True):
    if not os.path.exists(path):
        print('  ✗ 文件不存在：%s' % path)
        return 1
    doc = Document(path)
    before = scan(doc)

    # 月份大标题优先用 --title；否则从文档里第一个「标题 2」场次标题解析年月；
    # 再不行才退回文件名（文件名可能被改过，最不可靠，故放最后）。
    first_heading = next((p.text for p in doc.paragraphs
                          if SG.heading_level(p) == 2), None)
    changed = SG.ensure_front_matter(doc, first_heading, font, levels=levels,
                                     doc_title=title)
    if title is None and not before[3] and not SG.has_toc(doc):
        guess = month_title_from_filename(path)
        if guess:
            changed = SG.ensure_front_matter(doc, first_heading, font, levels=levels,
                                             doc_title=guess) or changed
    after = scan(doc)

    print('  文件：%s' % os.path.basename(path))
    print('    处理前：标题1 ×%d / 标题2 ×%d / 其它标题 ×%d / 目录 %s'
          % (before[0], before[1], before[2], '有' if before[3] else '无'))
    print('    处理后：标题1 ×%d / 标题2 ×%d / 其它标题 ×%d / 目录 %s'
          % (after[0], after[1], after[2], '有' if after[3] else '无'))
    if dry_run:
        print('    [dry-run] 未写入磁盘')
        return 0
    if not changed and before == after:
        print('    无需改动')
        return 0

    if backup:
        bak = path + '.bak_toc'
        try:
            import shutil
            shutil.copy2(path, bak)
            print('    已备份：%s' % os.path.basename(bak))
        except Exception as e:
            print('    ⚠️ 备份失败（继续）：%s' % e)
    SG.save_doc_atomic(doc, path, log=lambda m, lvl='INFO': print('    %s' % m))
    print('    ✅ 已写入')
    return 0


def month_title_from_filename(path):
    """从文件名《…2026年9月.docx》解析月份大标题。"""
    import re
    m = re.search(r'(\d{4})\s*年\s*(\d{1,2})\s*月', os.path.basename(path))
    if m:
        return '%s年%d月 直播总结' % (m.group(1), int(m.group(2)))
    return None


def main():
    ap = argparse.ArgumentParser(description='给月度总结 docx 补标题样式/大纲级别/自动目录')
    ap.add_argument('docx', nargs='+', help='目标 docx（可多个）')
    ap.add_argument('--font', default=DEFAULT_FONT)
    ap.add_argument('--levels', default='1-3', help='TOC 收录的大纲级别范围，默认 1-3')
    ap.add_argument('--title', default=None, help='月份大标题文本；缺省从文件名解析')
    ap.add_argument('--dry-run', action='store_true', help='只体检不落盘')
    ap.add_argument('--no-backup', action='store_true')
    a = ap.parse_args()
    rc = 0
    for p in a.docx:
        rc |= process(p, a.font, a.levels, a.title, a.dry_run, not a.no_backup)
    return rc


if __name__ == '__main__':
    sys.exit(main())
