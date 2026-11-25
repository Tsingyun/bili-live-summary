# -*- coding: utf-8 -*-
"""逐段转写原文 DOCX 导出（2026-11-25 用户规则）。

每场录播单独生成一份「原文汇总」文档，样式对标 C:\\Users\\Tsing\\DeskBox\\文档\\总结 现有样例：
  · 段0：样式「大标题」，文本 = <录播 basename>_原文
  · 段1：居中，生成时间「YYYY年M月D日 HH:MM」
  · 之后：(时间戳段 / 正文段 / 空行段) x N
  · 全部正文 run 用「等线(中文正文)」10pt（w:sz=20）

★ 用户规则（2026-11-25）：正文「一小段一小段」，每段聚焦一个要点、简短清晰，
  **不合并成长段**。因此这里严格按 TXT 的每一句转写独立成段（不做合并）。

幂等：输出文件已存在时默认跳过；--force 才覆盖。
用法：
  python transcript_docx.py --txt <转写稿.txt> --out <输出目录> [--name <stem>] [--force]
  python transcript_docx.py --selftest
"""
import argparse
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

FONT = "等线(中文正文)"
TS_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\]\s*(.*)$")
# 兼容无方括号、点分隔等历史写法
TS_RE2 = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})\s+(.*)$")


def fmt_ts(h, m, s):
    """00:00:39 -> 00:39；01:23:45 -> 01:23:45（与样例一致）"""
    h = h.zfill(2)
    if h == "00":
        return "%s:%s" % (m, s)
    return "%s:%s:%s" % (h, m, s)


def parse_transcript(txt_path):
    """返回 [(时间戳字符串, 正文), ...]，每句一段，不合并。"""
    rows = []
    with open(txt_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = TS_RE.match(line)
            if m:
                h, mi, s, text = m.group(1), m.group(2), m.group(3), m.group(4).strip()
            else:
                m2 = TS_RE2.match(line)
                if not m2:
                    continue
                h, mi, s, text = m2.group(1), m2.group(2), m2.group(3), m2.group(4).strip()
            if text:
                rows.append((fmt_ts(h, mi, s), text))
    return rows


def build_docx(txt_path, out_path, title=None, force=False, now=None):
    """生成 docx，返回 (是否生成, 段落数)。已存在且非 force 时跳过。"""
    if os.path.exists(out_path) and not force:
        return False, 0

    rows = parse_transcript(txt_path)
    if not rows:
        raise ValueError("转写稿无有效句子：%s" % txt_path)

    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Pt

    if title is None:
        title = os.path.splitext(os.path.basename(txt_path))[0].split(" 202")[0] + "_原文"

    doc = docx.Document()
    # 定义「大标题」样式（样例 styleId=ac 的等价实现）
    from docx.enum.style import WD_STYLE_TYPE
    try:
        hstyle = doc.styles["大标题"]
    except KeyError:
        hstyle = doc.styles.add_style("大标题", WD_STYLE_TYPE.PARAGRAPH)
    hstyle.font.size = Pt(16)
    hstyle.font.bold = True

    def style_run(run):
        run.font.name = FONT
        run.font.size = Pt(10)
        run.bold = False
        run.italic = False
        rpr = run._element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
            rfonts.set(qn(attr), FONT)

    # 1) 标题
    h = doc.add_paragraph(style="大标题")
    h.add_run(title)

    # 2) 生成时间（居中）
    now = now or datetime.now()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    style_run(p.add_run("%d年%d月%d日 %02d:%02d" %
                        (now.year, now.month, now.day, now.hour, now.minute)))

    # 3) (时间戳 / 正文 / 空行) x N —— 严格逐句，不合并
    for ts, body in rows:
        p = doc.add_paragraph()
        style_run(p.add_run(ts))
        p = doc.add_paragraph()
        style_run(p.add_run(body))
        p = doc.add_paragraph()
        style_run(p.add_run(""))

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    doc.save(tmp)
    shutil.move(tmp, out_path)
    return True, len(rows)


def selftest():
    import tempfile
    ok = 0
    fail = 0

    def check(name, cond):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print("FAIL:", name)

    check("fmt_ts 00", fmt_ts("00", "00", "39") == "00:39")
    check("fmt_ts 1h", fmt_ts("01", "23", "45") == "01:23:45")
    check("fmt_ts 6h", fmt_ts("06", "11", "15") == "06:11:15")
    check("fmt_ts 10h", fmt_ts("10", "02", "03") == "10:02:03")

    with tempfile.TemporaryDirectory() as d:
        txt = os.path.join(d, "demo.txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write("# 转写日志\n# 模型: X\n[00:00:39] 第一句。\n[00:00:59] 第二句。\n[01:23:45] 第三句。\n")
        rows = parse_transcript(txt)
        check("parse 3 rows", len(rows) == 3)
        check("parse skip header", rows[0] == ("00:39", "第一句。"))
        check("parse 1h", rows[2][0] == "01:23:45")

        out = os.path.join(d, "sub", "demo.docx")
        made, n = build_docx(txt, out, title="demo_原文", now=datetime(2026, 11, 25, 14, 0))
        check("build made", made is True)
        check("build n=3", n == 3)
        check("file exists", os.path.exists(out))
        made2, _ = build_docx(txt, out, title="demo_原文")
        check("idempotent skip", made2 is False)
        made3, _ = build_docx(txt, out, title="demo_原文", force=True,
                               now=datetime(2026, 11, 25, 14, 0))
        check("force rebuild", made3 is True)

        import docx
        doc = docx.Document(out)
        ps = [p.text for p in doc.paragraphs]
        check("paras=2+3*3", len(ps) == 11)
        check("title", ps[0] == "demo_原文")
        check("time", ps[1] == "2026年11月25日 14:00")
        check("ts", ps[2] == "00:39")
        check("body short", ps[3] == "第一句。")
        check("blank", ps[4] == "")
        check("style title", doc.paragraphs[0].style.name == "大标题")
        r = doc.paragraphs[3].runs[0]
        check("font size 10pt", r.font.size.pt == 10.0)

        from docx.oxml.ns import qn
        rfonts = r._element.rPr.rFonts
        check("eastAsia font", rfonts.get(qn("w:eastAsia")) == FONT)
        check("align center", doc.paragraphs[1].alignment is not None)

    print("selftest: %d passed, %d failed" % (ok, fail))
    return 0 if fail == 0 else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--txt", help="FunASR 转写稿 TXT 路径")
    ap.add_argument("--out", help="输出目录")
    ap.add_argument("--name", help="输出文件名 stem（不含 .docx）；默认取 TXT basename")
    ap.add_argument("--title", help="文档标题；默认 <name>_原文")
    ap.add_argument("--force", action="store_true", help="覆盖已有文件")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.txt or not a.out:
        ap.error("需要 --txt 与 --out（或用 --selftest）")
    name = a.name or os.path.splitext(os.path.basename(a.txt))[0]
    out_path = os.path.join(a.out, name + ".docx")
    made, n = build_docx(a.txt, out_path, title=a.title, force=a.force)
    print("created=%s segments=%d path=%s" % (made, n, out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
