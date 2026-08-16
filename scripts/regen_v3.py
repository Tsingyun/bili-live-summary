# -*- coding: utf-8 -*-
"""regen_v3：用 glm-4-flash 重建 4 场总结。
关键：glm-4-flash 对「每行带 [HH:MM:SS]」的密集转录会退化成逐行碎片/循环。
解法：先把每个 30 分钟分块按 ~4 分钟粗分组（组首仅一个起始时间、组内去掉逐句时间戳），
再让 glm 为每组产出一段 100–180 字合并总结 + 1–2 句「」金句。输入结构变了，glm 即稳定合规。
逐场可断点续跑（章节已存在则跳过），带启动级单实例锁与跨进程 doc 写锁。
"""
import sys, os, re, json, time, datetime, logging

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import summarize_gemini as S

CFG_PATH = r"D:\岁己直播自动总结\config.json"
WORK = r"D:\岁己直播自动总结"
TXT_DIR = os.path.join(WORK, "TXT")
LOCK = os.path.join(WORK, ".regen_v3.lock")
PROG_PATH = os.path.join(WORK, ".regen_v3_progress.json")


def load_progress():
    try:
        with open(PROG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_progress(data):
    tmp = PROG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, PROG_PATH)
WIN = 240  # 粗分组窗口（秒）≈ 4 分钟

SESSIONS = [
    ("20260809-重温第一次3D演出！ 2026年08月09日20点场", "2026-08-09", "重温第一次3D演出"),
    ("20260810-陪你这个猪看滴嘟侠炒饭2！ 2026年08月10日20点场", "2026-08-10", "陪你这个猪看滴嘟侠炒饭2"),
    ("20260811-和米帕栞栞玩机械狂欢！ 2026年08月11日15点场", "2026-08-11", "和米帕栞栞玩机械狂欢"),
    ("20260811-陪你这个猪过周二! 2026年08月11日20点场", "2026-08-11", "陪你这个猪过周二"),
]

SYS_PROMPT = (
    "你是直播内容编辑。下面给出【一组】直播片段，第一行是该组起始时间 [HH:MM:SS]，其后是对话内容（已去掉逐句时间戳）。\n"
    "请严格输出且仅输出以下两行：\n"
    "第1行（总结）：格式必须为“[HH:MM:SS] 后紧跟总结正文”（时间必须与总结同一行，禁止把时间单独写成一行，禁止用方括号包裹总结正文）。"
    "总结 100–180 字、以「岁己」为主语、以句号结尾、记录式摘要风格；"
    "禁止用“她”等指代不清的代词；“观众”统一称“饼干岁”；禁止逐句对应原文改写，必须抽象概括；单段最多 3–4 个信息点。\n"
    "第2行（金句，可选）：用「」包裹该组中最有代表性的 1–2 句原话（必须来自内容、不得编造、长度 15–80 字）；若确实无合适金句则不输出第2行。\n"
    "禁止输出“金句”等标题行、禁止列表符号、禁止额外解释。\n"
)

OV_SYS = (
    "你是直播内容编辑。下面是一场直播按时间顺序的全部要点总结。请写一段 200–300 字的内容概要，"
    "客观记录式，以岁己为主语，概括本场核心话题与走向；不要列表、不要金句、不要标题。\n"
)


def setup_log():
    os.makedirs(os.path.join(WORK, "logs"), exist_ok=True)
    log = logging.getLogger("regen_v3")
    log.setLevel(logging.INFO)
    if not log.handlers:
        h = logging.FileHandler(os.path.join(WORK, "logs", "2026-08-13.log"), encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(h)
        log.addHandler(logging.StreamHandler())
    return log


def fmt_ts(t):
    h = t // 3600; m = (t % 3600) // 60; s = t % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def group_chunk(lines, win=WIN):
    """lines: list of (t_seconds, text). 返回 list of (start_t, [texts])。"""
    groups = []
    cur_start = None; buf = []
    for t, txt in lines:
        if cur_start is None or (t - cur_start) >= win:
            if buf:
                groups.append((cur_start, buf))
            cur_start = t; buf = [txt]
        else:
            buf.append(txt)
    if buf:
        groups.append((cur_start, buf))
    return groups


def parse_ts_line(line):
    m = re.match(r"\[(\d{1,2}):(\d{2}):(\d{2})\]\s*(.*)", line.strip())
    if not m:
        return None, None
    h, mm, ss = (int(x) for x in m.groups()[:3])
    return h * 3600 + mm * 60 + ss, m.group(4)


def build_user(groups):
    parts = []
    for g_start, g_buf in groups:
        parts.append(f"[{fmt_ts(g_start)}]\n" + "\n".join(g_buf) + "\n")
    return "\n".join(parts)


def split_segments(out):
    """按各组“[HH:MM:SS] 总结行”把 glm 输出切成若干段（顺序与输入组一致）。
    不依赖空行（clean_block 会去掉空行），直接以时间前缀的总结行作为分组边界。"""
    cleaned = S.clean_block(out)
    segs = []
    cur = []
    for line in cleaned.split("\n"):
        s = line.strip()
        if not s:
            continue
        is_hdr = re.match(r"^\[\d{1,2}:\d{2}:\d{2}\]", s) and not S.is_quote_line(s)
        if is_hdr and cur:
            segs.append(cur)
            cur = []
        cur.append(s)
    if cur:
        segs.append(cur)
    return segs


def build_blocks(out, groups):
    """把 glm 输出按已知组顺序重组为 blocks，强制用各组真实起始时间做前缀，
    并清洗 glm 偶尔多包的方括号。返回 (blocks, n_seg, n_group)。"""
    segs = split_segments(out)
    n = min(len(segs), len(groups))
    blocks = []
    for i in range(n):
        start = groups[i][0]
        seg = segs[i]
        summary = None
        rest = []
        for ln in seg:
            if summary is None and re.match(r"^\[\d{1,2}:\d{2}:\d{2}\]", ln):
                content = re.sub(r"^\[\d{1,2}:\d{2}:\d{2}\]\s*", "", ln)
                if content.strip():
                    summary = content
                # 若时间行后内容为空，继续从后续行取总结
            elif summary is None:
                summary = ln
            else:
                rest.append(ln)
        if not summary:
            summary = ""
        summary = summary.strip()
        if summary.startswith("[") and summary.endswith("]") and not re.match(r"^\[\d{1,2}:\d{2}:\d{2}\]", summary):
            summary = summary[1:-1].strip()
        summary = f"[{fmt_ts(start)}] {summary.lstrip()}"
        blocks.append("\n".join([summary] + rest))
    return blocks, len(segs), len(groups)


def single_instance(lock_path, wait=20):
    """启动级单实例锁：抢不到在 wait 秒内退出，避免环境双拉起导致双写。"""
    deadline = time.time() + wait
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            return True
        except OSError:
            try:
                with open(lock_path) as f:
                    holder = int((f.read() or "0").strip() or 0)
            except Exception:
                holder = 0
            if holder and holder != os.getpid():
                try:
                    os.kill(holder, 0); alive = True
                except OSError:
                    alive = False
                except Exception:
                    alive = True
                if alive:
                    if time.time() > deadline:
                        return False
                    time.sleep(2); continue
            try:
                os.remove(lock_path)
            except OSError:
                pass


def main():
    if not single_instance(LOCK):
        print("另一实例已在运行，本实例退出")
        return
    log = setup_log()
    cfg = json.load(open(CFG_PATH, encoding="utf-8"))
    font = cfg["output"].get("font", "思源宋体CN Light")

    for fname, date_str, title in SESSIONS:
        txt_path = os.path.join(TXT_DIR, fname + ".txt")
        if not os.path.exists(txt_path):
            log.error(f"缺少转录：{txt_path}，跳过")
            continue
        d = datetime.date.fromisoformat(date_str)
        heading = f"{d.year}年{d.month:02d}月{d.day:02d}日 {title}"
        docx_path = S.doc_path_for(cfg, d)
        lockp = docx_path + ".writelock"

        # 断点续跑：章节已存在则跳过整场
        if os.path.exists(docx_path):
            try:
                from docx import Document as _D
                _doc = _D(docx_path)
                if S.has_section(_doc, heading):
                    log.info(f"== 已存在章节，跳过：{heading}")
                    continue
            except Exception:
                pass

        log.info(f"== 载入全量规则并开始：{heading} ==\n   源文件：{txt_path}")
        # 读转录、解析时间戳行
        raw = [l for l in open(txt_path, encoding="utf-8").read().split("\n") if l.strip()]
        parsed = []
        for l in raw:
            t, txt = parse_ts_line(l)
            if t is not None:
                parsed.append((t, txt))
        if not parsed:
            log.error(f"{fname} 无带时间轴内容，跳过")
            continue

        # 按 30 分钟分块
        chunk_sec = int(cfg["summarize"].get("chunk_minutes", 30)) * 60
        chunks = []
        cur_start = None; cbuf = []
        for t, txt in parsed:
            if cur_start is None or (t - cur_start) >= chunk_sec:
                if cbuf:
                    chunks.append((cur_start, cbuf))
                cur_start = t; cbuf = [(t, txt)]
            else:
                cbuf.append((t, txt))
        if cbuf:
            chunks.append((cur_start, cbuf))
        log.info(f"   分块数={len(chunks)}")

        # 断点续跑：从进度文件恢复已完成的分块
        progress = load_progress()
        sess = progress.get(fname, {"done": [], "blocks": []})
        done = set(sess.get("done", []))
        all_blocks = list(sess.get("blocks", []))
        log.info(f"   进度恢复：已完成块={sorted(done)} 已累积段={len(all_blocks)}")

        for ci, (cstart, clines) in enumerate(chunks, 1):
            if ci in done:
                log.info(f"   [块{ci}/{len(chunks)}] 已完成，跳过")
                continue
            groups = group_chunk(clines, WIN)
            chunk_blocks = []
            for gi, (g_start, g_buf) in enumerate(groups, 1):
                user = f"[{fmt_ts(g_start)}]\n" + "\n".join(g_buf) + "\n"
                text, _ = S.call_model(cfg, SYS_PROMPT, user, log)
                if not text:
                    log.error(f"   [块{ci}/{len(chunks)} 组{gi}/{len(groups)}] 空响应，跳过该组（留缺口）")
                    continue
                blks, n_seg, n_group = build_blocks(text, [(g_start, g_buf)])
                if n_seg != 1:
                    log.warning(f"   [块{ci} 组{gi}] 期望1段，产出{n_seg}段，取首段")
                chunk_blocks.extend(blks[:1])
                time.sleep(cfg["summarize"].get("inter_group_delay_sec", 3))
            if chunk_blocks:
                all_blocks.extend(chunk_blocks)
                done.add(ci)
                sess = {"done": sorted(done), "blocks": all_blocks}
                progress[fname] = sess
                save_progress(progress)  # 每块落盘，支持被回收后续跑
                log.info(f"   [块{ci}/{len(chunks)}] 完成并落盘（累积 {len(all_blocks)} 段）")
            else:
                log.error(f"   [块{ci}/{len(chunks)}] 无产出，本块留缺口，继续后续块")
            time.sleep(cfg["summarize"].get("inter_chunk_delay_sec", 3))

        if not all_blocks:
            log.error(f"   {heading} 全程无产出，跳过保存")
            continue

        # 概要
        summary_only = "\n".join(
            ln for b in all_blocks for ln in b.split("\n")
            if re.match(r"^\[\d{1,2}:\d{2}:\d{2}\]", ln.strip()) and not S.is_quote_line(ln.strip())
        )
        overview = ""
        if summary_only.strip():
            overview, _ = S.call_model(cfg, OV_SYS, summary_only, log)
            log.info(f"   概要字数={len(overview) if overview else 0}")

        # 写 doc
        if not S._acquire_doc_lock(lockp, timeout=180):
            log.error(f"   获取写锁超时，跳过保存：{heading}")
            continue
        try:
            doc, _ = S.open_or_create(docx_path, font)
            meta = [f"来源：{title}（{date_str}）"]
            S.append_section(doc, heading, meta, all_blocks, font,
                             first_in_doc=(not any(p.text.strip() for p in doc.paragraphs)),
                             overview=overview or "")
            doc.save(docx_path)
            # 整场完成，清理进度
            progress.pop(fname, None)
            save_progress(progress)
            log.info(f"   ✅ 已保存：{docx_path}（{len(all_blocks)} 段）")
        finally:
            S._release_doc_lock(lockp)

    try:
        os.remove(LOCK)
    except OSError:
        pass
    log.info("== regen_v3 全部完成 ==")


if __name__ == "__main__":
    main()
