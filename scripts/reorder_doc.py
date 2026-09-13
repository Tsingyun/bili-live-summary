#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
reorder_doc.py —— 按直播日期重排《岁己直播总结_YYYY年M月.docx》各章节（Heading2）。

设计要点：
- 文档每场直播是一个「章节」：以 Heading2 标题开头，直到下一个 Heading2 之前。
- 每场（除第一篇外）前面有一个仅含分页符的空段落（wd_break PAGE），它随该章节一起移动，
  因此重排后分页符位置自动正确，无需额外处理。
- 标题识别使用 python-docx 的 `paragraph.style.name`（如 'Heading 2'），**不依赖底层 w:pStyle
  的 w:val**（某些 Word 模板里该值是 styleId 数字如 '4'，直接读会漏判）。
- 用 lxml 直接搬运 <w:p> 元素节点（保持粗体/斜体/字号等全部格式），不做任何文本重排。
- 文档结尾的 body 级 <w:sectPr> 单独剥离，重排后永远放回最后，避免 sectPr 落到正文中间。

用法:
  python reorder_doc.py [docx路径]   （缺省用 8 月文档）

示例:
  python reorder_doc.py "C:/Users/Tsing/DeskBox/文档/岁己直播总结_2026年9月.docx"

⚠️ 陷阱（2026-09-14 修复）：本模块会被 daily_pipeline 以 `import` 方式调用，
    此时 sys.argv[1] 是 daily_pipeline 自己的命令行参数（如 `--date`），**不是文档路径**。
    因此模块级绝不能出现 `DOC = sys.argv[1]` —— 否则保存时会把文档写到名为
    `--date` 的垃圾文件里，真正的月度文档却没被重排。
    正确做法：默认值只作常量，命令行参数只在 __main__ 分支里解析。
"""
import re
import sys
from docx import Document
from docx.oxml.ns import qn

# 仅作为「命令行未给路径时」的默认文档；不可写成 sys.argv[1]（见上方陷阱说明）
DEFAULT_DOC = r'C:/Users/Tsing/DeskBox/文档/岁己直播总结_2026年8月.docx'


def heading_text(el):
    return ''.join(t.text or '' for t in el.iter(qn('w:t')))


def parse_key(txt):
    # 形如 ... 2026年08月13日20点场  /  2026-08-13
    m = re.search(r'(\d{4})[年\-](\d{1,2})[月\-](\d{1,2})[日\s]*(\d{1,2})?', txt)
    if not m:
        return (9999, 99, 99, 99)
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    h = int(m.group(4)) if m.group(4) else 0
    return (y, mo, d, h)


def reorder(doc_path=None):
    """把文档里的章节按直播日期升序重排。

    doc_path：要重排的 docx；不传则用默认文档（DEFAULT_DOC）。
    调用方（daily_pipeline）务必传入本场实际写入的月度文档，否则会误排默认文档。
    保存路径必须与读取路径一致（曾因写死模块级 DOC 而把文档存到别处）。
    """
    target = doc_path or DEFAULT_DOC
    doc = Document(target)
    body = doc.element.body
    children = list(body)

    # 剥离 body 级 sectPr（必须是最后一个子节点才是 body 级）
    sectPr = None
    if children and children[-1].tag == qn('w:sectPr'):
        sectPr = children[-1]
        children = children[:-1]

    # 用 python-docx 识别 Heading 段落对应的 lxml 元素
    # （w:pStyle 的 w:val 在某些模板里是 styleId 数字如 '4'，不可靠，故用 style.name 判定）
    heading_els = set()
    for p in doc.paragraphs:
        if (p.style.name or '').strip().startswith('Heading'):
            heading_els.add(p._p)

    def is_heading(el):
        return el in heading_els

    heading_idx = [i for i, el in enumerate(children) if is_heading(el)]
    if len(heading_idx) < 2:
        print(f'章节不足 2 个（当前 {len(heading_idx)}），无需重排')
        return

    preamble = children[:heading_idx[0]]
    sections = []
    for hi in range(len(heading_idx)):
        s = heading_idx[hi]
        e = heading_idx[hi + 1] if hi + 1 < len(heading_idx) else len(children)
        elems = children[s:e]
        key = parse_key(heading_text(elems[0]))
        sections.append((key, elems))

    sections.sort(key=lambda x: x[0])

    for el in list(body):
        body.remove(el)
    for el in preamble:
        body.append(el)
    for _, elems in sections:
        for el in elems:
            body.append(el)
    if sectPr is not None:
        body.append(sectPr)

    doc.save(target)   # 必须存回读的那个文件，不能写死默认路径
    print('重排完成（已保存到 %s），最终章节顺序：' % target)
    for key, elems in sections:
        print('   ', key, heading_text(elems[0])[:54])


if __name__ == '__main__':
    # 只在命令行直接运行时才解析 argv —— import 时 argv 属于调用方
    reorder(sys.argv[1] if len(sys.argv) > 1 else None)
