# -*- coding: utf-8 -*-
"""为某一场直播在其小节内补一份【短版总结】（长版=管线原生的逐段纪要，保留不动）。

起因：2026-10-27 场用户要求「电脑直播输出长、短两版总结」。管线原生只产长版
（逐段纪要），本脚本复用文档里已有的「内容概要 + 逐段纪要」作为输入，不重读
几小时转写、不重跑 ASR，只花一次模型调用生成短版。

输出结构（H2 小节内）：
    标题
    内容概要（管线原生）
    直播日期 / 来源（管线原生）
    【短版总结】            <- 本脚本插入
      <脉络段 3~4 句>
      · 要点 x6~8
    【长版总结 · 逐段纪要】  <- 本脚本插入的标签
      [HH:MM:SS] ...（管线原生）

特性：
  * 幂等：重复运行先删旧【短版总结】区块再写，不会堆叠
  * 输出净化：剥掉模型照抄的时间戳、丢弃金句行、要点统一「· 」前缀、去重、软截断
  * 术语：走 terminology.normalize（与管线写入口径一致）
  * 落盘：原子写；Word 占用时退化为原地覆写；写前备份 .bak_short_*
  * 给 TOC 域打 dirty=true，Word 打开自动刷新页码

用法：
  python short_summary.py --date 2026-10-27 [--hour 20] [--dry-run] [--workdir D:\\岁己直播自动总结]
"""
import argparse
import datetime
import os
import re
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from docx import Document  # noqa: E402

from common import load_config, Logger  # noqa: E402
import model_router as MR  # noqa: E402
import regen_gemini_v2 as RG  # noqa: E402
import summarize_gemini as S  # noqa: E402
import terminology as TERM  # noqa: E402

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

SYS_PROMPT = """你是资深直播内容编辑。用户会给你一场虚拟主播直播的「内容概要」与「逐段纪要」（带时间轴、条目很多）。
请**大幅度压缩重写**成一份短版总结（约为输入字数的 1/10），严格遵守：

【第一部分】一段连贯中文，3~4 句，概括整场直播的脉络（从开场到结束发生了什么大的转折）。
  - 必须是完整段落，不能分行、不能用列表、不能带任何时间戳。
  - 例：「岁己开场因软件卡顿反复排查，随后聊起换季穿搭与发质护理；中段演唱英文歌并讨论游戏成就，最后组织了一场有仪式感的观影会。」

【第二部分】按时间顺序 6~8 条要点，**每条必须以「· 」开头**（注意是中文间隔号加空格）。
  - **严禁**在要点里出现任何时间戳（不要 [00:05:27] 这种方括号时间）。
  - **严禁**照抄输入的句子，必须**自己概括**，每条不超过 40 字，只写「发生了什么」。
  - **严禁**摘录原话/金句/引号内容。
  - 正确示例：「· 开场隐形相机严重卡顿，排查后确认是软件问题」
  - 错误示例：「[00:05:27] 岁己开播后遭遇隐形相机严重卡顿，质疑高性能电脑配置…」（带时间戳、照抄原文）

【通篇要求】直接叙述，不要写「本次直播」「主播表示」「总结如下」这类套话；人名沿用原文（岁己、饼干岁等）。
只输出正文：不要标题、不要 markdown 符号（# * - >）、不要加粗、不要任何解释或说明性文字。"""


def is_h2(p):
    st = (p.style.name or "").lower().replace(" ", "")
    if st in ("heading2", "标题2"):
        return True
    pPr = p._p.pPr
    if pPr is not None:
        ol = pPr.find(W + "outlineLvl")
        if ol is not None and ol.get(W + "val") == "1":
            t = (p.text or "").strip()
            if not re.match(r"^\d{4}年\d{1,2}月", t):
                return True
    return False


def h2_index(doc):
    return [i for i, p in enumerate(doc.paragraphs) if is_h2(p)]


def section_bounds(doc, key):
    idx = h2_index(doc)
    for n, i in enumerate(idx):
        if key in (doc.paragraphs[i].text or ""):
            end = idx[n + 1] if n + 1 < len(idx) else len(doc.paragraphs)
            return i, end
    return None, None


def find_key(doc, day, hour=None):
    """按「YYYY年M月D日」定位小节标题；hour 用于同日多场时精确匹配。"""
    pat = "%d年%d月%d日" % (day.year, day.month, day.day)
    cands = []
    for p in doc.paragraphs:
        if not is_h2(p):
            continue
        t = (p.text or "").strip()
        if pat in t:
            cands.append(t)
    if not cands:
        return None
    if hour is not None:
        hp = "%d年%d月%d日%d点场" % (day.year, day.month, day.day, hour)
        for t in cands:
            if hp in t:
                return hp
        return None
    return cands[0]


def collect_input(doc, i0, i1):
    """取小节内的「概要段 + 逐段纪要」，跳过元信息 / 已有短版区块 / 金句。"""
    overview, bullets, src_idx = "", [], None
    for i in range(i0 + 1, i1):
        t = (doc.paragraphs[i].text or "").strip()
        if not t:
            continue
        if t.startswith("来源"):
            src_idx = i
            continue
        if t.startswith("直播日期") or t.startswith("类型"):
            continue
        if t.startswith("【短版总结】") or t.startswith("【长版总结"):
            continue
        if re.match(r"^\[\d{2}:\d{2}:\d{2}\]", t):
            bullets.append(t)
        elif not overview and not t.startswith("“"):
            overview = t
    return overview, bullets, src_idx


def insert_after(cursor, doc, text, font, bold=False):
    p = doc.add_paragraph()
    if cursor.style is not None:
        p.style = cursor.style
    r = p.add_run(text)
    S.set_font(r, font)
    if bold:
        r.bold = True
    cursor._p.addnext(p._p)
    return p


def insert_before(anchor, doc, text, font, bold=False):
    p = doc.add_paragraph()
    if anchor.style is not None:
        p.style = anchor.style
    r = p.add_run(text)
    S.set_font(r, font)
    if bold:
        r.bold = True
    anchor._p.addprevious(p._p)
    return p


def drop_block(doc, i0, i1):
    """删掉已有的【短版总结】…【长版总结】区块（含标签），返回删除段数。"""
    ps = doc.paragraphs
    labs = [(i, (ps[i].text or "").strip()) for i in range(i0 + 1, i1)]
    short_i = next((i for i, t in labs if t.startswith("【短版总结】")), None)
    long_i = next((i for i, t in labs if t.startswith("【长版总结")), None)
    if short_i is None:
        return 0
    stop = long_i if long_i is not None else short_i + 1
    victims = [ps[i] for i in range(short_i, stop)]
    for p in victims:
        p._element.getparent().remove(p._element)
    return len(victims)


def set_toc_dirty(doc):
    """给 TOC 域打 dirty=true（instrText 在 run 内，需上溯到段落）。"""
    n = 0
    for it in doc.element.body.iter(W + "instrText"):
        if "TOC" not in (it.text or ""):
            continue
        node = it
        while node is not None and node.tag != W + "p":
            node = node.getparent()
        if node is None:
            continue
        for fc in node.iter(W + "fldChar"):
            if fc.get(W + "fldCharType") == "begin":
                fc.set(W + "dirty", "true")
                n += 1
    return n


def polish(text):
    """把模型输出净化成：1 段脉络 + 若干「· 」要点。"""
    prose, points = None, []
    for raw in text.splitlines():
        ln = raw.strip()
        if not ln:
            continue
        ln = re.sub(r"^[#>\s]*", "", ln)
        ln = re.sub(r"\*\*(.+?)\*\*", r"\1", ln)
        ln = re.sub(r"^[-—]{3,}$", "", ln)
        if not ln:
            continue
        ln = re.sub(r"^[\[【]\d{1,2}:\d{2}(?::\d{2})?[\]】]\s*", "", ln)
        if re.match(r"^[“\"].+[”\"]$", ln):
            continue
        ln, _ = TERM.normalize(ln, is_quote=False)
        is_point = ln[:1] in ("·", "・", "•", "-", "—", "–")
        body = ln.lstrip("·・•—-– ").strip()
        if not body:
            continue
        if is_point:
            points.append(body)
        elif prose is None:
            prose = body
        else:
            points.append(body)

    def _trim(s):
        s = s.rstrip("。；;，,、 ")
        if len(s) <= 42:
            return s
        cut = s[:42]
        for ch in ("，", "、", "；", "。", " "):
            k = cut.rfind(ch)
            if k >= 25:
                return cut[:k]
        return cut

    seen, pts = set(), []
    for b in points:
        b = _trim(b)
        key = re.sub(r"\W", "", b)[:14]
        if not b or key in seen:
            continue
        seen.add(key)
        pts.append(b)
    pts = pts[:8]
    return ([prose] if prose else []) + ["· " + b for b in pts]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="直播日期 YYYY-MM-DD")
    ap.add_argument("--hour", type=int, default=None, help="同日多场时指定开播小时（如 20）")
    ap.add_argument("--workdir", default=None, help="工作目录（含 config.json）")
    ap.add_argument("--dry-run", action="store_true", help="只生成不写文档")
    args = ap.parse_args()

    day = datetime.date.fromisoformat(args.date)
    cfg_path = os.path.join(args.workdir, "config.json") if args.workdir else \
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    cfg = load_config(cfg_path)
    cfg["_config_path"] = cfg_path
    log = Logger(cfg, tag="short")
    font = (cfg.get("output") or {}).get("font", "思源宋体CN Light")
    doc_path = S.doc_path_for(cfg, day)
    log("目标文档：%s" % doc_path)
    if not os.path.exists(doc_path):
        log("文档不存在", "ERROR")
        return 2

    doc = Document(doc_path)
    key = find_key(doc, day, args.hour)
    if not key:
        log("找不到 %s 的小节（管线是否已写入？）" % args.date, "ERROR")
        return 2
    i0, i1 = section_bounds(doc, key)
    overview, bullets, src_idx = collect_input(doc, i0, i1)
    log("小节：%s；概要 %d 字；逐段纪要 %d 条" % (key, len(overview), len(bullets)))
    if not bullets:
        log("没有取到逐段纪要，终止", "ERROR")
        return 2
    if src_idx is None:
        log("没有找到「来源」行，短版将插在概要之后", "WARN")

    user = "内容概要：\n%s\n\n逐段纪要（共 %d 条）：\n%s" % (
        overview, len(bullets), "\n".join(bullets))
    log("输入 %d 字，调用模型生成短版…" % len(user))
    t0 = time.time()
    rot = MR.build_rotator(cfg, log)
    text = (rot.call(SYS_PROMPT, user, tag="[短版总结]") or "").strip()
    log("生成完成：%d 字（%.0fs）" % (len(text), time.time() - t0))
    if not text:
        log("模型返回空，终止", "ERROR")
        return 3

    lines = polish(text)
    if not lines:
        log("清理后无有效内容，终止", "ERROR")
        return 3

    print("")
    print("---------- 短版总结（生成结果） ----------")
    for ln in lines:
        print(ln)
    print("-----------------------------------------")

    if args.dry_run:
        log("--dry-run：不写文档")
        print(RG.format_usage_report())
        return 0

    n_drop = drop_block(doc, i0, i1)
    if n_drop:
        log("已清除旧短版区块 %d 段（幂等重写）" % n_drop)
        overview, bullets, src_idx = collect_input(doc, i0, i1)

    anchor = doc.paragraphs[src_idx] if src_idx is not None else doc.paragraphs[i0]
    cursor = insert_after(anchor, doc, "【短版总结】", font, bold=True)
    for ln in lines:
        cursor = insert_after(cursor, doc, ln, font)

    first_bullet = None
    i0b, i1b = section_bounds(doc, key)
    for i in range(i0b + 1, i1b):
        if re.match(r"^\[\d{2}:\d{2}:\d{2}\]", (doc.paragraphs[i].text or "").strip()):
            first_bullet = doc.paragraphs[i]
            break
    if first_bullet is not None:
        insert_before(first_bullet, doc, "【长版总结 · 逐段纪要】", font, bold=True)
        log("已插入【长版总结 · 逐段纪要】标签")
    else:
        log("未找到首条纪要，跳过长版标签", "WARN")

    log("TOC 刷新标记：%d 处" % set_toc_dirty(doc))

    bak = doc_path + ".bak_short_%s" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy2(doc_path, bak)
    tmp = doc_path + ".tmp_short"
    doc.save(tmp)
    try:
        os.replace(tmp, doc_path)
    except PermissionError:
        log("文档被 Word 占用，改为原地覆写", "WARN")
        shutil.copyfile(tmp, doc_path)
        try:
            os.remove(tmp)
        except Exception:
            pass
    log("已写入：%s（备份 %s）" % (doc_path, os.path.basename(bak)))

    print(RG.format_usage_report())
    return 0


if __name__ == "__main__":
    sys.exit(main())
