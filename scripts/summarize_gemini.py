#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 4：调用大模型分块总结 + 追加写入 Word 文档。

【硬性约束】转录文本必须按 30 分钟切块，逐块调用 API，绝对不允许整篇一次性发送。
文档按直播实际日期归档到「{streamer}直播总结_YYYY年M月.docx」，同月复用同一文档，
每场新直播插入分页符 + Heading2 标题区分，不新建文件。
每场总结完成后，标题正下方自动生成一段 ~200-300 字的内容概要（提示词见 scripts/prompt_overview.txt）。

用法:
  python summarize_gemini.py --txt "WORKDIR/TXT/20260801-xxx.txt" --date 2026-08-01
  python summarize_gemini.py --all
"""

import argparse
import datetime
import glob
import json
import os
import re
import sys
import time

import requests
from docx import Document
from docx.enum.text import WD_BREAK
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from common import load_config, Logger, hms, parse_hms, live_date

API_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'


def _api_base(sc):
    """API 基地址，优先用 config['summarize']['api_host']（可换成兼容端点）。"""
    h = sc.get('api_host')
    return h.rstrip('/') if h else API_BASE


def load_prompt(streamer=None, audience=None):
    """读取 scripts/prompt.txt 中的总结指令（支持 {streamer}/{audience} 占位符注入）。
    缺失时回退到内置模板。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompt.txt')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            txt = f.read().strip()
    except Exception:
        txt = ''
    if txt:
        if streamer:
            txt = txt.replace('{streamer}', streamer)
        if audience:
            txt = txt.replace('{audience}', audience)
        return txt
    return SYSTEM_PROMPT_FALLBACK.format(streamer=streamer or '主播', audience=audience or '观众')


SYSTEM_PROMPT_FALLBACK = """你是一名直播内容记录员。将直播转录文本总结为结构化摘要。
【格式】每条以 [HH:MM:SS] 开头。段落间空行分隔。不用 markdown。
【内容】以"{streamer}"为主语，覆盖完整话题弧。观众称呼统一为"{audience}"。
【金句】每条最多1条原文金句，用中文双引号“”包裹。"""

OVERVIEW_SYSTEM_PROMPT_FALLBACK = """你是一名直播内容编辑。下面是一场直播的分段总结（按时间顺序，每段覆盖约30分钟）。
请综合这些分段，写一段【内容概要】：用自然流畅的中文 prose（不是列表、不用项目符号），约 200–300 字，
以"{streamer}"为主语，概括这场直播整体在做什么、聊了哪些话题、氛围如何、有哪些关键节点或名场面。
不要逐条复述时间轴，重在让读者一眼抓住"这场直播是什么"的整体印象。观众统称"{audience}"。
纯文本输出，不要使用 ** 或 * 等 markdown 标记。"""

OVERVIEW_USER_TEMPLATE = """以下是本场直播的分段总结，请据此撰写【内容概要】：

{content}"""


def load_overview_prompt(streamer=None, audience=None):
    """读取 scripts/prompt_overview.txt 中的概要指令（支持 {streamer}/{audience} 占位符），
    缺失时回退到内置模板。"""
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'prompt_overview.txt')
    try:
        with open(p, 'r', encoding='utf-8') as f:
            txt = f.read().strip()
    except Exception:
        txt = ''
    if txt:
        if streamer:
            txt = txt.replace('{streamer}', streamer)
        if audience:
            txt = txt.replace('{audience}', audience)
        return txt
    return OVERVIEW_SYSTEM_PROMPT_FALLBACK.format(streamer=streamer or '主播', audience=audience or '观众')

USER_TEMPLATE = """请对以下直播转录内容进行总结与重组（这是本场直播 {chunk_label} 时段，第 {idx}/{total} 个片段）。

严格遵循系统指令中的分段、时间标注、压缩与输出格式要求。
输入文本每一行都以 [HH:MM:SS] 开头，表示该句在本场直播中的绝对时间，请据此标注时间，且时间必须落在本片段区间 {chunk_label} 内。
请输出纯文本，不要使用 ** 或 * 等 markdown 标记；粗体与斜体效果由后续排版自动处理。

{content}"""


# ---------------- 分块 ----------------
def load_and_chunk(txt_path, chunk_sec):
    """解析 [HH:MM:SS] 行，按绝对时间切成 chunk_sec 一块。"""
    timed = []
    with open(txt_path, 'r', encoding='utf-8') as f:
        for line in f:
            m = re.match(r'^\[(\d{1,2}:\d{2}:\d{2})\]\s*(.*)', line.strip())
            if m and m.group(2).strip():
                timed.append((parse_hms(m.group(1)), m.group(2).strip()))
    if not timed:
        return []
    chunks, buf = [], []
    start = 0
    for sec, text in timed:
        while sec >= start + chunk_sec:
            if buf:
                chunks.append((start, buf))
            buf = []
            start += chunk_sec
        buf.append(f'[{hms(sec)}] {text}')
    if buf:
        chunks.append((start, buf))
    return [(s, '\n'.join(b)) for s, b in chunks if b]


# ---------------- 模型调用 ----------------
def _resolve_proxies(sc):
    """优先用 config['summarize']['proxy']（如 http://127.0.0.1:7897），
    其次读环境变量 HTTPS_PROXY/HTTP_PROXY。返回 requests 的 proxies 字典或 None。"""
    p = sc.get('proxy')
    if p:
        return {'http': p, 'https': p}
    env = os.environ.get('HTTPS_PROXY') or os.environ.get('HTTP_PROXY')
    if env:
        return {'http': env, 'https': env}
    return None


def _post_with_fallback(url, headers, payload, timeout, proxies, log):
    """发起请求；若配置了代理但代理连接失败（如 Clash 已关），
    自动回退为直连再试一次——沙箱直连可达 Gemini 时可直接生效。"""
    try:
        return requests.post(url, headers=headers, json=payload, timeout=timeout, proxies=proxies)
    except requests.exceptions.ConnectionError as e:
        if proxies:
            log.error(f'  代理连接失败，回退直连：{e}')
            return requests.post(url, headers=headers, json=payload, timeout=timeout, proxies=None)
        raise


def _backoff_seconds(r, attempt, log):
    """限流/重试等待时长：优先尊重服务端 Retry-After / retryDelay，否则指数退避。"""
    if r is not None:
        ra = r.headers.get('Retry-After')
        if ra:
            try:
                return int(ra)
            except (ValueError, TypeError):
                pass
        try:
            body = r.json()
            for det in (body.get('error', {}).get('details') or []):
                rd = det.get('retryDelay')
                if rd:
                    import re as _re
                    m = _re.match(r'(\d+)s', str(rd))
                    if m:
                        return int(m.group(1))
        except Exception:
            pass
    return min(8 * (2 ** (attempt - 1)), 120)


def call_gemini(cfg, system_prompt, user_content, log):
    sc = cfg['summarize']
    model = sc['model']
    url = f'{_api_base(sc)}/{model}:generateContent'
    gen = {
        'temperature': sc.get('temperature', 0.3),
        'maxOutputTokens': sc.get('max_output_tokens', 8192),
    }
    lvl = sc.get('thinking_level')
    if lvl:
        gen['thinkingConfig'] = {'thinkingLevel': lvl}
    payload = {
        'systemInstruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'role': 'user', 'parts': [{'text': user_content}]}],
        'generationConfig': gen,
    }
    headers = {'x-goog-api-key': sc['api_key'], 'Content-Type': 'application/json'}

    proxies = _resolve_proxies(sc)
    if proxies:
        log(f'  使用代理：{proxies.get("https")}')

    retries = sc.get('max_retries', 8)
    r = None
    for attempt in range(1, retries + 1):
        try:
            r = _post_with_fallback(url, headers, payload, 300, proxies, log)
            if r.status_code == 200:
                d = r.json()
                cand = (d.get('candidates') or [{}])[0]
                parts = (cand.get('content') or {}).get('parts') or []
                text = ''.join(p.get('text', '') for p in parts).strip()
                usage = d.get('usageMetadata', {})
                if text:
                    return text, usage
                log.error(f'  空响应 finishReason={cand.get("finishReason")}')
            elif r.status_code == 429:
                # 免费层每日配额耗尽：当天重试无意义，立即返回失败交由管线次日续跑
                log.error('  HTTP 429 每日配额耗尽，本块今日跳过（次日续跑）')
                return '', {}
            elif r.status_code in (500, 503, 504):
                log.error(f'  HTTP {r.status_code}，退避重试（{attempt}/{retries}）')
            else:
                log.error(f'  HTTP {r.status_code}: {r.text[:300]}')
                return '', {}
        except Exception as e:
            log.error(f'  请求异常（{attempt}/{retries}）：{e}')
        if attempt < retries:
            wait = _backoff_seconds(r, attempt, log)
            log(f'  等待 {wait}s 后重试（{attempt}/{retries}）')
            time.sleep(wait)
    return '', {}


def call_model(cfg, system_prompt, user_content, log):
    """按 config['summarize']['provider'] 分发到对应模型调用。
    支持 google(gemini) / zhipu(glm)。返回 (text, usage_dict)，
    usage_dict 统一为 {promptTokenCount, candidatesTokenCount} 便于上层复用。"""
    provider = (cfg.get('summarize', {}).get('provider', 'google') or 'google').lower()
    if provider == 'zhipu':
        return call_zhipu(cfg, system_prompt, user_content, log)
    return call_gemini(cfg, system_prompt, user_content, log)


def call_zhipu(cfg, system_prompt, user_content, log):
    """调用智谱 GLM（OpenAI 兼容 /chat/completions）。glm-4-flash 永久免费、128K 上下文。"""
    sc = cfg['summarize']
    model = sc['model']
    host = (sc.get('api_host') or 'https://open.bigmodel.cn/api/paas/v4').rstrip('/')
    url = host + '/chat/completions'
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_content},
        ],
        'temperature': float(sc.get('temperature', 0.3)),
        'max_tokens': int(sc.get('max_output_tokens', 8192)),
    }
    headers = {
        'Authorization': f'Bearer {sc["api_key"]}',
        'Content-Type': 'application/json',
    }

    proxies = _resolve_proxies(sc)
    if proxies:
        log(f'  使用代理：{proxies.get("https")}')

    retries = int(sc.get('max_retries', 10))
    r = None
    for attempt in range(1, retries + 1):
        try:
            r = _post_with_fallback(url, headers, payload, 300, proxies, log)
            if r.status_code == 200:
                d = r.json()
                msg = ((d.get('choices') or [{}])[0].get('message') or {})
                text = (msg.get('content') or '').strip()
                usage = d.get('usage', {}) or {}
                if text:
                    return text, {
                        'promptTokenCount': usage.get('prompt_tokens', 0),
                        'candidatesTokenCount': usage.get('completion_tokens', 0),
                    }
                log.error('  空响应（模型未返回内容）')
            elif r.status_code == 429:
                # 智谱免费模型按 RPM 限流，退避后重试即可，并非每日硬上限
                log.error('  HTTP 429 限流，退避后重试')
            elif r.status_code in (500, 502, 503, 504):
                log.error(f'  HTTP {r.status_code}，退避重试（{attempt}/{retries}）')
            else:
                log.error(f'  HTTP {r.status_code}: {r.text[:400]}')
                return '', {}
        except Exception as e:
            log.error(f'  请求异常（{attempt}/{retries}）：{e}')
        if attempt < retries:
            wait = _backoff_seconds(r, attempt, log)
            log(f'  等待 {wait}s 后重试（{attempt}/{retries}）')
            time.sleep(wait)
    return '', {}


# ---------------- Word ----------------
def set_font(obj, name):
    try:
        obj.font.name = name
        rpr = obj._element.get_or_add_rPr()
        rpr.rFonts.set(qn('w:eastAsia'), name)
        rpr.rFonts.set(qn('w:ascii'), name)
        rpr.rFonts.set(qn('w:hAnsi'), name)
    except Exception:
        pass


def doc_path_for(cfg, d: datetime.date):
    out = cfg['output']
    sc = cfg.get('summarize', {})
    pat = out['doc_name_pattern']
    pat = pat.replace('{streamer}', sc.get('streamer', '')).replace('{audience}', sc.get('audience', ''))
    try:
        name = pat.format(year=d.year, month=d.month, month02=f'{d.month:02d}')
    except Exception:
        name = pat
    return os.path.join(out['doc_dir'], name)


def open_or_create(path, font):
    if os.path.exists(path):
        return Document(path), False
    doc = Document()
    st = doc.styles['Normal']
    st.font.size = Pt(11)
    try:
        st.font.name = font
        st.element.rPr.rFonts.set(qn('w:eastAsia'), font)
    except Exception:
        pass
    return doc, True


def has_section(doc, heading_text):
    """按标题文本精确匹配（不依赖样式），防止旧版本用普通段落写的标题被判为“不存在”而重复追加。"""
    ht = heading_text.strip()
    return any(p.text.strip() == ht for p in doc.paragraphs)


def remove_section(doc, heading_text):
    """删除文档中某个标题章节（含标题本身到下一个同名/同结构标题前的所有段落）。
    按标题文本精确匹配，兼容 Heading 与普通段落两种写法。"""
    ht = heading_text.strip()
    ps = doc.paragraphs
    idx = None
    for i, p in enumerate(ps):
        if p.text.strip() == ht:
            idx = i
            break
    if idx is None:
        return False
    nxt = len(ps)
    for j in range(idx + 1, len(ps)):
        if ps[j].text.strip() == ht:   # 下一个同名标题 → 章节结束
            nxt = j
            break
    for k in range(nxt - 1, idx - 1, -1):
        el = ps[k]._element
        el.getparent().remove(el)
    return True


def _acquire_doc_lock(lock_path, timeout=180):
    """跨进程写锁：用 O_EXCL 原子创建锁文件，避免多个进程同时写同一个 docx。
    若锁由已死亡进程持有（被强杀留下的孤儿），视为失效并抢占，避免后续运行空等 180s。"""
    deadline = time.time() + timeout
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return True
        except OSError:
            # 锁已存在：检查持有者是否还活着
            holder = 0
            try:
                with open(lock_path, 'r') as _f:
                    holder = int((_f.read() or '0').strip() or 0)
            except Exception:
                holder = 0
            alive = False
            if holder:
                try:
                    os.kill(holder, 0)   # Windows / POSIX 均可用于探测进程是否存在
                    alive = True
                except OSError:
                    alive = False
                except Exception:
                    alive = True
            if alive:
                # Windows PID 复用防护：os.kill 只验证 PID 是否存在，环境回收后 PID 会被
                # 其它进程顶替而误判「仍存活」。故额外用锁文件 mtime 兜底：若锁超过 600s
                # 未更新，无论 PID 是否“活着”都判定为孤儿并抢占。
                stale = False
                try:
                    if (time.time() - os.path.getmtime(lock_path)) > 600:
                        stale = True
                except OSError:
                    stale = False
                if not stale and time.time() <= deadline:
                    time.sleep(2)
                    continue
                # 过期或超时 -> 当作孤儿抢占
            # 持有者已死 / 过期 -> 孤儿锁，抢占后重试
            try:
                os.remove(lock_path)
            except OSError:
                pass
            time.sleep(0.2)


def _release_doc_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            with open(lock_path) as _f:
                _cur = _f.read().strip()
            if _cur == str(os.getpid()):
                os.remove(lock_path)
    except Exception:
        pass


def is_quote_line(line):
    s = line.strip()
    return (len(s) > 4
            and s[0] in ('“', '"', '「')
            and s[-1] in ('”', '"', '」'))


def clean_block(text):
    """分组提示词产出的清洗：剥掉模型自发加的标号、『金句：』标题行、列表符号，
    把 “” 归一为「」，保留 [HH:MM:SS] 起始时间行与「」金句行。"""
    out = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('【') or line.startswith('格式') or line.startswith('输出'):
            continue
        # 剥列表符号与标号（可能带或不带前置时间戳）
        line = re.sub(r'^[-\u2022\u30fb\u25cf]+\s*', '', line)
        line = re.sub(r'^(金句|总结段|一段|段落\d+|段\d+|quote)[:：]\s*', '', line, flags=re.I)
        line = re.sub(r'(\[\d{1,2}:\d{2}:\d{2}\]\s*)(金句|总结段|一段|段落\d+|段\d+)[:：]\s*', r'\1', line, flags=re.I)
        # “原句” -> 「原句」 （仅当整行是引号包裹的引用时归一，避免误伤总结正文）
        if line and line[0] == '\u201c' and line[-1] == '\u201d':
            line = '\u300c' + line[1:-1] + '\u300d'
        out.append(line)
    return '\n'.join(out)


def append_section(doc, heading, meta_lines, blocks, font, first_in_doc, overview=''):
    """把一场直播的总结追加进文档。overview 为标题正下方的内容概要（可选）。
    金句（引号行）按提示词要求：每段总结可附带 1–2 句，超出该段 2 句上限的后续金句行丢弃。"""
    para_quote = 0  # 每段金句计数，遇新的总结段重置
    if not first_in_doc:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    h = doc.add_heading(heading, level=2)
    for r in h.runs:
        set_font(r, font)
        r.font.size = Pt(14)
    if not h.runs:
        set_font(h, font)

    # 内容概要：紧贴标题下方，正常字重 prose，与下方加粗要点形成层级
    if overview and overview.strip():
        _op = doc.add_paragraph()
        _op.paragraph_format.space_after = Pt(6)
        _op.paragraph_format.line_spacing = 1.4
        _or = _op.add_run(overview.strip())
        set_font(_or, font)
        _or.font.size = Pt(11)

    for ml in meta_lines:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(ml)
        set_font(r, font)
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    for text in blocks:
        for line in text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            line = re.sub(r'^[\*#\-\u2022]+\s*', '', line)
            line = line.replace('**', '').replace('__', '').strip().strip('*').strip()
            if is_quote_line(line):
                if para_quote >= 2:
                    continue  # 该段金句已达 2 句上限，跳过多余金句行
                para_quote += 1
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.line_spacing = 1.35
                r = p.add_run(line)
                set_font(r, font)
                r.italic = True
                r.font.size = Pt(10)
                r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
            else:
                para_quote = 0  # 遇到新的总结段，重置该段金句计数
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.line_spacing = 1.35
                r = p.add_run(line)
                set_font(r, font)
                r.bold = True
                r.font.size = Pt(11)


# ---------------- 主流程 ----------------
def summarize_one(cfg, txt_path, log, date_str=None, title=None, bvid=None, url=None, overview_cfg=None, prompt_override=None, clean_glm=False):
    sc = cfg['summarize']
    stem = os.path.splitext(os.path.basename(txt_path))[0]
    chunk_sec = int(sc.get('chunk_minutes', 30)) * 60

    chunks = load_and_chunk(txt_path, chunk_sec)
    if not chunks:
        log.error(f'{stem} 没有带时间轴的内容，跳过')
        return None

    # 归档月份：优先用传入的直播日期，否则从文件名 YYYYMMDD- 解析
    if date_str:
        d = datetime.date.fromisoformat(date_str)
    else:
        m = re.match(r'^(\d{4})(\d{2})(\d{2})-', stem)
        d = (datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
             if m else live_date(stem, 0))

    docx_path = doc_path_for(cfg, d)
    font = cfg['output'].get('font', '思源宋体CN Light')
    heading = title or stem

    # 跨进程写锁：同一 docx 任何时刻只允许一个进程读写，杜绝并发跑批互相覆盖
    lock_path = docx_path + '.writelock'
    if not _acquire_doc_lock(lock_path):
        log.error('获取文档写锁超时，跳过本场以避免损坏文档')
        return None

    try:
        doc, created = open_or_create(docx_path, font)
        if has_section(doc, heading):
            log(f'文档已存在「{heading[:40]}」章节，删除旧内容后重写')
            remove_section(doc, heading)

        total = len(chunks)
        log(f'{stem}')
        log(f'  切成 {total} 块（每块 {sc.get("chunk_minutes", 30)} 分钟），逐块发送')

        blocks, tok_in, tok_out = [], 0, 0
        sys_p = prompt_override or load_prompt(sc.get('streamer'), sc.get('audience'))

        for i, (start, content) in enumerate(chunks, 1):
            label = f'{hms(start)} - {hms(start + chunk_sec)}'
            t0 = time.time()
            user_msg = USER_TEMPLATE.format(chunk_label=label, idx=i, total=total, content=content)
            text, usage = call_model(cfg, sys_p, user_msg, log)
            if text:
                if clean_glm:
                    text = clean_block(text)
                blocks.append(text)
                tok_in += usage.get('promptTokenCount', 0)
                tok_out += usage.get('candidatesTokenCount', 0)
                log(f'  [{i}/{total}] {label}  [OK] {len(text)} 字 / {time.time() - t0:.0f}s')
            else:
                log.error(f'  [{i}/{total}] {label}  [FAIL] 该块总结失败，继续后续块')
            if i < total:
                time.sleep(sc.get('inter_chunk_delay_sec', 6))

        if len(blocks) < total:
            log.error(f'{stem} 仅有 {len(blocks)}/{total} 段成功（其余可能因配额耗尽失败），'
                      f'不写入残缺章节，下次运行自动补全')
            return None

        # ---- 生成「内容概要」：标题下方一小段概述（~200-300 字）----
        overview = ''
        try:
            ov_sys = load_overview_prompt(sc.get('streamer'), sc.get('audience'))
            ov_user = OVERVIEW_USER_TEMPLATE.format(content='\n\n'.join(blocks))
            # 概要走 overview_cfg（默认=glm-4-flash 免费模型），避免占用 Gemini 每日配额；
            # 仅分块总结用 Gemini（对合并段+每段金句的复杂格式遵循度更高）。
            ov_text, _ = call_model(overview_cfg or cfg, ov_sys, ov_user, log)
            if ov_text:
                overview = ov_text
                log(f'  内容概要生成完成（{len(overview)} 字）')
            else:
                log.error('  内容概要生成失败，跳过概要（不影响正文）')
        except Exception as e:
            log.error(f'  内容概要生成异常：{e}')

        meta = [f'直播日期 {d:%Y-%m-%d}  |  时长 {hms(chunks[-1][0] + chunk_sec)}  |  '
                f'{len(blocks)}/{total} 段  |  总结于 {datetime.datetime.now():%Y-%m-%d %H:%M}']
        if bvid:
            meta.append(f'来源 {url or "https://www.bilibili.com/video/" + bvid}')

        first = created and len([p for p in doc.paragraphs if p.text.strip()]) == 0
        append_section(doc, heading, meta, blocks, font, first, overview)
        os.makedirs(os.path.dirname(docx_path), exist_ok=True)
        doc.save(docx_path)
        log(f'  已{"创建" if created else "追加至"}文档：{docx_path}')
        log(f'  token 用量：输入 {tok_in} / 输出 {tok_out}')
        return docx_path
    finally:
        _release_doc_lock(lock_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--txt', default=None)
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--date', default=None, help='直播日期 YYYY-MM-DD，决定归档月份')
    ap.add_argument('--title', default=None, help='文档中的章节标题')
    ap.add_argument('--bvid', default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = Logger(cfg, 'summarize')

    files = [args.txt] if args.txt else sorted(glob.glob(os.path.join(cfg['_dirs']['txt'], '*.txt')))
    if not files:
        log('没有待总结的 TXT')
        return

    ok = 0
    for f in files:
        try:
            if summarize_one(cfg, f, log, args.date, args.title, args.bvid):
                ok += 1
        except Exception as e:
            import traceback
            log.error(f'{os.path.basename(f)} 总结异常：{e}')
            traceback.print_exc()
    log(f'总结完成 {ok}/{len(files)}')
    sys.exit(0 if ok == len(files) else 1)


if __name__ == '__main__':
    main()
