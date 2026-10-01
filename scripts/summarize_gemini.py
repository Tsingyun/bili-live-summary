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
from docx.oxml import OxmlElement
from docx.shared import Pt, RGBColor

try:
    import terminology as TERM
except Exception:                                     # pragma: no cover
    TERM = None      # 规范模块缺失时静默降级，绝不影响正文写入

from common import (load_config, Logger, hms, parse_hms, live_date,
                    force_unlink, force_rmtree)

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
    自动回退为直连再试一次——沙箱直连可达 Gemini 时可直接生效。

    timeout 支持 (连接, 读取) 二元组：连接阶段快速失败，避免代理宕机时白等。
    """
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
            r = _post_with_fallback(url, headers, payload, (20, 300), proxies, log)
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
            r = _post_with_fallback(url, headers, payload, (20, 300), proxies, log)
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


def save_doc_atomic(doc, path, log=None):
    """原子保存 docx：写 <path>.part → 校验 → os.replace。

    ★ 2026-09-18 加固：docx 是本管线最贵重的产物（累积整月章节，且逐月追加）。
    python-docx 的 doc.save() 是**直接覆盖**目标文件：若此刻进程被环境回收
    （SIGKILL / 进程树拆除）或目标正被 Word / 腾讯文档占用，就会留下一个
    **损坏的 docx**（zip 结构不完整），整月内容都可能读不出来。
    改为：先写临时文件 → 用 zipfile 校验完整性 → 原子替换；替换失败时原文件不动。
    """
    import zipfile
    tmp = path + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    doc.save(tmp)
    # docx 本质是 zip 包：能被打开、无损坏项、含 word/document.xml 即视为完整
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
        if bad is not None or 'word/document.xml' not in z.namelist():
            try:
                force_unlink(tmp)
            except Exception:
                pass
            raise RuntimeError('docx 校验失败（损坏项=%s）' % bad)
    last = None
    for i in range(10):
        try:
            os.replace(tmp, path)
            return path
        except OSError as e:
            last = e
            # ★ 2026-09-19：WinError 5(拒绝访问) / 32(共享冲突) 表示目标被别的进程
            #   **持有且不允许删除共享**——典型就是 Word / 腾讯文档预览打开着这个 docx。
            #   这种占用是持续性的，干等 28s 也没用，早点跳到原地覆写分支。
            if getattr(e, 'winerror', None) in (5, 32):
                break
            time.sleep(0.5 * (i + 1))

    # ---- 退化路径：原地覆写 ----
    # 背景：本机预览面板会长期打开当月 docx，导致 os.replace 永远被拒；
    # 此时如果直接判失败，整个月的章节都写不进去（9/18 场次就是这样整场失败的）。
    # 代价：这一步失去「原子」保证——写入过程中被杀会损坏 docx。但：
    #   · 进这一步之前 .bak 已是上一版完整文档（保存流程会刷新它）；
    #   · open_or_create 读到损坏的主文档时会自动回退 .bak，不会静默丢内容；
    #   · 先写正文再 ftruncate，避免"先截断成 0 字节"这种最坏时序。
    try:
        with open(tmp, 'rb') as f:
            data = f.read()
        # ★ Windows 上 os.open 默认是**文本模式**，会把 \n 翻成 \r\n，写 docx(zip) 必损坏；
        #   必须显式 O_BINARY。另外 os.write 可能只写一部分，要循环写满。
        flags = os.O_WRONLY | os.O_CREAT
        if hasattr(os, 'O_BINARY'):
            flags |= os.O_BINARY
        fd = os.open(path, flags)
        try:
            mv = memoryview(data)
            off = 0
            while off < len(data):
                off += os.write(fd, mv[off:])
            os.ftruncate(fd, len(data))
            os.fsync(fd)
        finally:
            os.close(fd)
    except Exception as e:
        raise RuntimeError('docx 保存失败 %s：原子替换被拒(%s)，原地覆写也失败(%s)'
                           % (path, last, e))
    try:
        force_unlink(tmp)
    except Exception:
        pass
    if log is not None:
        try:
            log('[docx] 目标被占用（%s），已改为原地覆写：%s'
                % (last, os.path.basename(path)), 'WARN')
            log('[docx] ⚠️ 若该文档正开着编辑器，请关闭后重新打开以看到最新内容；'
                '在编辑器里手动保存可能覆盖本次写入', 'WARN')
        except Exception:
            pass
    return path


def open_or_create(path, font):
    if os.path.exists(path):
        try:
            return Document(path), False
        except Exception as e:
            # 主文档损坏（历史上被强杀/占用写坏过）→ 回退 .bak，再不行才新建，
            # 绝不静默地把新章节写进一个读不出来的文件。
            bak = path + '.bak'
            if os.path.exists(bak):
                try:
                    d = Document(bak)
                    import shutil as _sh
                    _sh.copy2(bak, path)
                    return d, False
                except Exception:
                    pass
            raise RuntimeError('打开 Word 失败（文件可能已损坏，已尝试 .bak）：%s' % e)
    doc = Document()
    st = doc.styles['Normal']
    st.font.size = Pt(11)
    try:
        st.font.name = font
        st.element.rPr.rFonts.set(qn('w:eastAsia'), font)
    except Exception:
        pass
    return doc, True


# ---------------- 标题样式 / 大纲级别 / 自动目录 ----------------
# Word 里「快速定位与跳转」由三件套构成，缺一不可：
#   1) 标题样式：内置样式「标题 1 / 标题 2 / 标题 3」（英文 Heading 1/2/3）；
#   2) 大纲级别：Outline Level 1~9 级，正文段落为「正文文本」级；
#   3) 导航窗格（视图 → 显示 → 导航窗格）与自动目录（引用 → 目录，本质是 TOC 域）
#      都是按大纲级别收集条目的；目录可右键「更新域」或用 F9 刷新，永不手写。
# 结论：每场总结只要用内置标题样式 + 正确的 outlineLvl，导航窗格与目录就同时可用。
#
# 为什么要显式写段落级 outlineLvl：本管线产出的 docx 会被第三方编辑器
# （腾讯文档 / WPS）改写，内置样式可能被换成自动 styleId（实测过 styleId=000069、
# w:name 仍是 heading 2 的情形），只靠样式不再保险。段落级 outlineLvl 是兜底。

_HEADING_NAME_RE = re.compile(r'^head(ing)?\s*(\d+)$', re.I)


def heading_level(p):
    """返回段落的标题层级（1~9）；不是标题则返回 None。

    优先读段落自身的 w:outlineLvl（0-based），其次按样式名解析。
    """
    pPr = p._p.pPr
    if pPr is not None:
        ol = pPr.find(qn('w:outlineLvl'))
        if ol is not None:
            try:
                v = int(ol.get(qn('w:val')))
                if 0 <= v <= 8:
                    return v + 1
            except Exception:
                pass
    name = (p.style.name or '').strip()
    m = _HEADING_NAME_RE.match(name)
    if m:
        try:
            lv = int(m.group(2))
        except Exception:
            return None
        if 1 <= lv <= 9:
            return lv
    return None


def set_outline_level(p, level):
    """显式写段落级大纲级别（level=1 → w:val=0）。

    不依赖样式也能被导航窗格 / TOC 识别，是样式被第三方编辑器改写后的保底手段。
    """
    try:
        lv = max(1, min(9, int(level)))
    except Exception:
        return
    pPr = p._p.get_or_add_pPr()
    for old in pPr.findall(qn('w:outlineLvl')):
        pPr.remove(old)
    ol = OxmlElement('w:outlineLvl')
    ol.set(qn('w:val'), str(lv - 1))
    pPr.append(ol)


def ensure_heading_style(doc, level, font, size_pt):
    """确保 Heading N 样式存在且用正文字体（中文必须写 eastAsia）。

    样式缺失时返回 None，调用方退回「普通段落 + 显式大纲级别」，功能不降级。
    """
    try:
        st = doc.styles['Heading %d' % level]
    except Exception:
        return None
    try:
        st.font.name = font
        rpr = st.element.get_or_add_rPr()
        rfonts = rpr.get_or_add_rFonts()
        rfonts.set(qn('w:ascii'), font)
        rfonts.set(qn('w:hAnsi'), font)
        rfonts.set(qn('w:eastAsia'), font)
        st.font.size = Pt(size_pt)
    except Exception:
        pass
    return st


def normalize_outline_levels(doc, font=None):
    """给文档里所有标题段落补齐/修正大纲级别（幂等）。返回实际修改的段落数。

    同时把标题 run 的字体刷成正文指定字体（中文字体需写 eastAsia）。
    """
    n = 0
    for p in doc.paragraphs:
        lv = heading_level(p)
        if not lv:
            continue
        pPr = p._p.pPr
        ol = pPr.find(qn('w:outlineLvl')) if pPr is not None else None
        if ol is None or ol.get(qn('w:val')) != str(lv - 1):
            set_outline_level(p, lv)
            n += 1
        if font:
            for r in p.runs:
                set_font(r, font)
    return n


def has_toc(doc):
    """文档里是否已存在 TOC 域（避免重复插入目录）。"""
    for it in doc.element.body.iter(qn('w:instrText')):
        if 'TOC' in (it.text or '').upper():
            return True
    return False


def add_toc_paragraph(doc, levels='1-3', font=None):
    """插入一个「可自动更新」的目录域 TOC \\o "1-3" \\h \\z \\u。

    \\h=目录条目带超链接（可点击跳转）、\\z \\u=Web 版式视图下隐藏、\\u=使用段落大纲级别。
    dirty=true 让 Word 打开时自动提示/执行更新，不需要手工重建。
    """
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(6)

    r1 = p.add_run()
    fb = OxmlElement('w:fldChar')
    fb.set(qn('w:fldCharType'), 'begin')
    fb.set(qn('w:dirty'), 'true')
    r1._r.append(fb)

    r2 = p.add_run()
    ins = OxmlElement('w:instrText')
    ins.set(qn('xml:space'), 'preserve')
    ins.text = ' TOC \\o "%s" \\h \\z \\u ' % (levels or '1-3')
    r2._r.append(ins)

    r3 = p.add_run()
    fs = OxmlElement('w:fldChar')
    fs.set(qn('w:fldCharType'), 'separate')
    r3._r.append(fs)

    # 域结果占位：Word 未刷新时显示这段提示（不含引号，避免被金句规则误判）
    r4 = p.add_run()
    if font:
        set_font(r4, font)
    r4.font.size = Pt(10)
    r4.font.color.rgb = RGBColor(0x88, 0x88, 0x88)
    r4.text = '（目录字段尚未刷新：在 Word 里右键目录 → 更新域，或 Ctrl+A 后按 F9）'

    r5 = p.add_run()
    fe = OxmlElement('w:fldChar')
    fe.set(qn('w:fldCharType'), 'end')
    r5._r.append(fe)
    return p


def month_title_from(heading):
    """从场次标题里解析出年月，生成月份大标题；解析不出返回 None。"""
    m = re.search(r'(\d{4})\s*[年\-/.]\s*(\d{1,2})', heading or '')
    if not m:
        return None
    return '%s年%d月 直播总结' % (m.group(1), int(m.group(2)))


def ensure_front_matter(doc, heading, font, levels='1-3', doc_title=None):
    """幂等地在文档开头补齐「月份大标题 + 目录标题 + 可更新目录域 + 分页符」。

    结构（大纲级别）：
        标题 1  → 2026年9月 直播总结        （大纲级别 1）
        普通段  → 目 录                    （正文级，不进目录）
        TOC 域  → 自动目录                  （可右键更新域 / F9 刷新）
        分页符
        标题 2  → 各场次标题                （大纲级别 2，导航窗格可跳转）

    已有 TOC 域时不重复插入；只做大纲级别修补。返回 True 表示本次有改动。
    """
    changed = normalize_outline_levels(doc, font) > 0
    if has_toc(doc):
        return changed
    title = doc_title or month_title_from(heading)
    if not title:
        return changed

    h1 = doc.add_paragraph()
    st1 = ensure_heading_style(doc, 1, font, 18)
    if st1 is not None:
        try:
            h1.style = st1
        except Exception:
            pass
    r = h1.add_run(title)
    set_font(r, font)
    r.font.size = Pt(18)
    r.bold = True
    set_outline_level(h1, 1)

    cap = doc.add_paragraph()
    cap.paragraph_format.space_before = Pt(10)
    cap.paragraph_format.space_after = Pt(4)
    rc = cap.add_run('目 录')
    set_font(rc, font)
    rc.font.size = Pt(14)
    rc.bold = True

    toc = add_toc_paragraph(doc, levels, font)

    br = doc.add_paragraph()
    br.add_run().add_break(WD_BREAK.PAGE)

    body = doc.element.body
    for i, el in enumerate([h1._p, cap._p, toc._p, br._p]):
        body.insert(i, el)
    return True


def month_doc_title(d):
    """月度文档的开头大标题：『2026年10月 直播总结』。"""
    return '%d年%d月 直播总结' % (d.year, d.month)


def ensure_month_doc(cfg, log=None, today=None, font=None):
    """确保「当月」的月度文档已存在，且带月份大标题 + 自动目录。

    ★ 归档语义（2026-10-02 起）：往月文档一律只读、原样归档；
      新总结只写「直播日期所属月份」的文档，绝不跨月混写。
      每月 1 日第一次跑管线时自动新建当月文档（之后幂等，重复跑只补目录不重写）。

    返回 (path, created)；created=True 表示本次是新建。
    """
    today = today or datetime.date.today()
    font = font or (cfg.get('output') or {}).get('font', '思源宋体CN Light')
    path = doc_path_for(cfg, datetime.date(today.year, today.month, 1))
    exists = os.path.exists(path)
    doc, _created = open_or_create(path, font)
    changed = ensure_front_matter(doc, None, font, doc_title=month_doc_title(today))
    if changed or not exists:
        save_doc_atomic(doc, path, log=log)
    if log is not None:
        try:
            log('[月度文档] %s（%s）' % (os.path.basename(path),
                                    '本月新建' if not exists else '已存在/已补目录'))
        except Exception:
            pass
    return path, (not exists)


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
    # 抢占阶段的绝对上限。旧实现在软超时后只靠 os.remove 抢占，若锁文件被其它程序
    # （例如腾讯文档编辑器正在预览该 docx）占用而删不掉，会以 0.2s 无限重试、
    # 进程静默挂死且无任何日志。故加硬上限，超时即放弃并让调用方明确报错。
    hard_deadline = time.time() + timeout + 30
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
            if time.time() > hard_deadline:
                # 抢占阶段也超时：锁文件被外部程序占用且删不掉，放弃并上报，
                # 绝不回到「0.2s 无限重试」的老路。
                return False
            try:
                force_unlink(lock_path)
            except OSError:
                pass
            time.sleep(0.2)


def _release_doc_lock(lock_path):
    try:
        if os.path.exists(lock_path):
            with open(lock_path) as _f:
                _cur = _f.read().strip()
            if _cur == str(os.getpid()):
                force_unlink(lock_path)
    except Exception:
        pass


# ---------------- 行级清洗与「异常内容」过滤 ----------------
# 2026-09-24 修复两类实测问题（9/23 那场暴露）：
#  ① 金句被误判成粗体：不同模型遵循度不同——gemini-2.5-flash 会给金句套 markdown
#     斜体 `“…”`，旧清理只剥 ** 与 __，**单下划线漏网** → is_quote_line 看到首字符
#     是 '_' 判否 → 走正文分支被加粗（本应灰色斜体），下划线还直接显示在 Word 里。
#  ② 提示词/元话语写进正文：模型把 user 消息里的说明句当「金句」回显
#     （如「输入文本每一行都以 [HH:MM:SS] 开头…」），或输出「好的，这是对输入文本
#     进行分段重组与精炼总结后的内容：」这类开场白，被当成正文段落加粗写入。

_MD_LIST_PREFIX = re.compile(r'^[\-\u2022\u30fb\u25cf]+\s*')
_MD_HEAD_PREFIX = re.compile(r'^#{1,6}\s*')
_MD_WRAP = (
    re.compile(r'^\*{1,3}(.+?)\*{1,3}$', re.S),
    re.compile(r'^_{1,3}(.+?)_{1,3}$', re.S),
    re.compile(r'^`{1,3}(.+?)`{1,3}$', re.S),
)
_TS_PREFIX = re.compile(r'^\[\d{1,2}:\d{2}:\d{2}\]\s*')
_QUOTE_OPEN = ('\u201c', '"', '\u300c', '\u300e')     # “ " 「 『
_QUOTE_CLOSE = ('\u201d', '"', '\u300d', '\u300f')    # ” " 」 』
# ★ 2026-09-29：模型会把排版指令「说」出来（gemini-3-flash-preview 在 9/28 场实测输出
#   「粗体岁己在02:01:00…」「斜体“…”」共 40 段），导致正文带字面格式词、金句判否被加粗。
_MD_FMT_WORD = re.compile(r'^(?:粗体|斜体|加粗|黑体)\s*[:：,，、]?\s*')


def strip_md_markers(line):
    """剥掉模型自发加的标记（列表/标题/强调/行内代码/**字面格式词**），返回纯文本行。

    单下划线**只剥成对包裹**的形式（`_斜体_`），不做全局删除——中文正文里 `_`
    几乎不出现，成对才处理可避免误伤英文歌词、账号名等。
    ★ 2026-09-29 新增：剥掉行首字面格式词（粗体/斜体/加粗/黑体）。本域内总结段必须
    以主语开头、金句必须以“开头，行首出现这些词只可能是模型把排版指令说了出来。
    本函数同时被 append_section（源头拦截）与 docx_healthcheck（产物修复）共用，
    故必须在 is_quote_line 判定之前完成——否则「斜体“…”」会被误判成正文加粗。
    """
    s = (line or '').strip()
    if not s:
        return ''
    s = _MD_LIST_PREFIX.sub('', s)
    s = _MD_HEAD_PREFIX.sub('', s)
    for _ in range(4):                 # 容忍 *** ** * 或 ___ __ _ 多层嵌套
        before = s
        for pat in _MD_WRAP:
            s = pat.sub(r'\1', s).strip()
        s = _MD_FMT_WORD.sub('', s).strip()
        if s == before:
            break
    s = s.replace('**', '').replace('__', '')
    return s.strip()


def is_quote_line(line):
    """是否为「金句」行（引号包裹的原文摘录）。

    先剥 markdown 标记（关键：`_“…”_` 要能被认出来），再容忍 [HH:MM:SS] 前缀。
    """
    s = strip_md_markers(line)
    if len(s) > 4 and s[0] in _QUOTE_OPEN and s[-1] in _QUOTE_CLOSE:
        return True
    t = _TS_PREFIX.sub('', s)
    if t != s and len(t) > 4 and t[0] in _QUOTE_OPEN and t[-1] in _QUOTE_CLOSE:
        return True
    return False


_FP_CACHE = {}


def _prompt_fingerprints(cfg=None):
    """收集提示词里长度足够的句子片段，作为「模型回显提示词」的指纹。

    长度阈值取 12：既是提示词里的实义片段，又几乎不可能与主播口语原句重合。
    """
    if 'fp' in _FP_CACHE:
        return _FP_CACHE['fp']
    parts = [globals().get(n) or '' for n in
             ('USER_TEMPLATE', 'SYSTEM_PROMPT_FALLBACK', 'OVERVIEW_SYSTEM_PROMPT_FALLBACK',
              'OVERVIEW_USER_TEMPLATE', 'TOPIC_SEG_SYSTEM')]
    # 实际生效的提示词文件（工作目录优先）
    paths = []
    if cfg and cfg.get('_workdir'):
        paths += [os.path.join(cfg['_workdir'], n) for n in
                  ('prompt_original.txt', 'prompt.txt', 'prompt_overview.txt')]
    _sd = os.path.dirname(os.path.abspath(__file__))
    paths += [os.path.join(_sd, n) for n in ('prompt.txt', 'prompt_overview.txt')]
    for p in paths:
        try:
            with open(p, 'r', encoding='utf-8') as f:
                parts.append(f.read())
        except Exception:
            pass
    # regen_gemini_v2 的 build_user 模板（延迟 import，避免循环依赖）
    try:
        import regen_gemini_v2 as _R
        parts.append(_R.build_user('x', 1, 1, ''))
    except Exception:
        pass
    fps = set()
    for c in parts:
        if not c:
            continue
        c = re.sub(r'\{[a-zA-Z_]+\}', ' ', c)          # 去掉 {chunk_label} 之类占位符
        for sent in re.split(r'[。；;\n，,：:]', c):
            s = sent.strip().strip('。，,、:：')
            if len(s) >= 12:
                fps.add(s)
    _FP_CACHE['fp'] = fps
    return fps


def prompt_leak_hits(line):
    """返回该行命中的提示词指纹；非空表示这是模型回显的提示词/系统指令。"""
    s = strip_md_markers(line)
    if not s:
        return []
    return [fp for fp in _prompt_fingerprints() if fp in s]


# 元话语（模型开场白/收尾语）的特征模板
_META_PATTERNS = (
    re.compile(r'对(输入|上述|以下|原文)?(文本|内容).{0,20}(分段重组|重组|精炼|整理|改写|压缩)'),
    re.compile(r'^(好(的|嘞)?|收到|明白|没问题|当然|可以)[，,。!！]?\s*'
               r'(这是|以下是|下面是|这里是|我将|我来|我|已|根据)?.{0,40}'
               r'(内容|总结|结果|输出|如下)[:：]?$'),
    re.compile(r'^(以下|下面|以上|如上|这是|这里是)是?.{0,30}(内容|总结|结果|输出|如下)[:：]?$'),
    re.compile(r'^(我)?(已|按照|根据)(您的?|你的)?(要求|指令|提示|系统指令).{0,40}$'),
    re.compile(r'^(希望|祝)(能|这(份|些)?)?.{0,20}(对你有帮助|帮到你|满意)'),
    re.compile(r'^(如果|若)(你|您)(还)?(有|需要).{0,30}(请|可以|随时)(告诉|说|提)'),
    re.compile(r'^(注意|备注|说明)[:：].{0,20}(格式|输出|规则)'),
)
_META_TAIL_HINT = re.compile(r'(内容|总结|结果|如下|输出|重组|整理|精炼)[:：]\s*$')


def is_meta_line(line):
    """是否为模型自说自话的元话语行（开场白/收尾语/格式说明），不应进正文。

    严格限制在「短行」上判断，避免误删正常总结与金句。
    """
    s = strip_md_markers(line)
    if not s or len(s) > 80:
        return False
    if _TS_PREFIX.match(s):          # 带时间戳的是正文
        return False
    for pat in _META_PATTERNS:
        if pat.search(s):
            return True
    if len(s) <= 45 and _META_TAIL_HINT.search(s):
        return True
    return False


def clean_block(text):
    """分组提示词产出的清洗：剥掉模型自发加的标号、『金句：』标题行、列表符号、
    markdown 强调标记，把 “” 归一为「」，保留 [HH:MM:SS] 起始时间行与「」金句行。"""
    out = []
    for line in text.split('\n'):
        line = strip_md_markers(line)
        if not line:
            continue
        if line.startswith('【') or line.startswith('格式') or line.startswith('输出'):
            continue
        # 剥标号（可能带或不带前置时间戳）
        line = re.sub(r'^(金句|总结段|一段|段落\d+|段\d+|quote)[:：]\s*', '', line, flags=re.I)
        line = re.sub(r'(\[\d{1,2}:\d{2}:\d{2}\]\s*)(金句|总结段|一段|段落\d+|段\d+)[:：]\s*', r'\1', line, flags=re.I)
        # “原句” -> 「原句」 （仅当整行是引号包裹的引用时归一，避免误伤总结正文）
        if line and line[0] == '\u201c' and line[-1] == '\u201d':
            line = '\u300c' + line[1:-1] + '\u300d'
        out.append(line)
    return '\n'.join(out)


def add_horizontal_rule(doc, color='BFBFBF', sz=6):
    """在文档中插入一条横向分隔线，用于话题块之间的视觉分隔。"""
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    pPr = p._p.get_or_add_pPr()
    pBdr = OxmlElement('w:pBdr')
    bottom = OxmlElement('w:bottom')
    bottom.set(qn('w:val'), 'single')
    bottom.set(qn('w:sz'), str(sz))
    bottom.set(qn('w:space'), '1')
    bottom.set(qn('w:color'), color)
    pBdr.append(bottom)
    pPr.append(pBdr)
    return p


TOPIC_SEG_SYSTEM = (
    '你是一名直播内容编辑助手。下面是一段直播总结文本（按时间顺序，含 [HH:MM:SS] 时间标注）。\n'
    '请识别其中的「话题边界」——即当主播从一个明显不同的话题切换到另一个话题时'
    '（例如：从吃饭聊到打游戏、从游戏聊到看电影、从闲聊切换到读信/互动环节等）。\n'
    '严格遵守以下规则：\n'
    '1. 在每一处话题边界处，单独插入一行，该行内容仅为一个标记「§SEP§」（前后不要有任何其它字符）。\n'
    '2. 绝对不要改写、删减、合并、重新组织或总结原文；你的唯一任务是插入分隔标记。\n'
    '3. 同一个话题内部的自然小分段（如同一话题下的不同小点）不要插入分隔。\n'
    '4. 若全文只围绕一个话题，则不要插入任何标记。\n'
    '5. 输出纯文本，逐行保留原文所有行（含 [HH:MM:SS] 行与「」金句行），不要使用 markdown。'
)


def segment_topics_with(combined_text, call_fn, log):
    """调用模型在话题边界插入 §SEP§ 标记，返回按标记切分后的话题块列表。
    任何异常、无返回、或模型改动/丢失了原文行，都安全退回为单一块（不丢内容、不加横线）。"""
    if not combined_text.strip():
        return [combined_text]
    try:
        text = call_fn(TOPIC_SEG_SYSTEM, combined_text)
    except Exception as e:
        log.error('  话题分隔模型调用异常，退回单块：%s' % e)
        return [combined_text]
    if not text:
        log.error('  话题分隔模型无返回，退回单块（不影响内容）')
        return [combined_text]
    # 安全网：模型不得改动原文行；任意非标记行不在原文集合中则退回
    orig_lines = set(l.strip() for l in combined_text.split('\n') if l.strip())
    model_lines = set(l.strip() for l in text.split('\n')
                      if l.strip() and l.strip() != '§SEP§')
    if not model_lines.issubset(orig_lines):
        log.error('  话题分隔模型改动了原文行，退回单块（不影响内容）')
        return [combined_text]
    segs = [s.strip('\n') for s in text.split('§SEP§')]
    segs = [s for s in segs if s.strip()]
    if len(segs) < 2:
        return [combined_text]
    return segs


def append_section(doc, heading, meta_lines, segments, font, first_in_doc, overview='',
                   topic_sep=True, log=None):
    """把一场直播的总结追加进文档。segments 为话题块列表（每个元素是含多行的字符串）；
    开启 topic_sep 时，在相邻话题块之间插入一条横向分隔线。
    金句（引号行）按提示词要求：每段总结可附带 1–2 句，超出该段 2 句上限的后续金句行丢弃。

    行级清理在**唯一的写入点**统一完成（两条总结路径 summarize_one / regen_gemini_v2
    都走这里），顺序固定：剥 markdown 标记 → 丢弃提示词回显/元话语 → 判金句 → 落格式。
    返回被丢弃的行列表（供健康检查与测试断言）。"""
    para_quote = 0  # 每段金句计数，遇到新的总结段重置
    dropped = []
    term_fixes = []   # 专有名词规范化改动（仅统计，不影响正文写入）
    # 文档开头必须有「月份大标题(标题1) + 自动目录(TOC 域)」，幂等插入。
    # 目录只是导航设施，任何异常都不能影响正文写入，故整段吞掉异常。
    try:
        ensure_front_matter(doc, heading, font)
    except Exception:
        pass
    if not first_in_doc:
        doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    ensure_heading_style(doc, 2, font, 14)
    h = doc.add_heading(heading, level=2)
    set_outline_level(h, 2)      # 显式大纲级别 2：导航窗格 / 目录的识别依据
    for r in h.runs:
        set_font(r, font)
        r.font.size = Pt(14)
    if not h.runs:
        set_font(h, font)

    # 内容概要：紧贴标题下方，正常字重 prose，与下方加粗要点形成层级
    if overview and overview.strip():
        _ov = overview.strip()
        if TERM is not None:
            _ov, _ovc = TERM.normalize(_ov)
            if _ovc and log:
                log.info('  专有名词规范化（概要）：%s'
                         % '；'.join('%s→%s' % (a, b) for a, b in _ovc[:5]))
        _op = doc.add_paragraph()
        _op.paragraph_format.space_after = Pt(6)
        _op.paragraph_format.line_spacing = 1.4
        _or = _op.add_run(_ov)
        set_font(_or, font)
        _or.font.size = Pt(11)

    for ml in meta_lines:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(ml)
        set_font(r, font)
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    for si, seg in enumerate(segments):
        if si > 0 and topic_sep:
            add_horizontal_rule(doc)
            para_quote = 0  # 新话题块 → 金句计数重置
        for raw in seg.strip().split('\n'):
            # ★ 统一剥 markdown 标记（关键：`_“…”_` 必须还原成 `“…”`，
            #   否则下面的 is_quote_line 会判否 → 金句被当正文加粗）
            line = strip_md_markers(raw)
            if not line:
                continue
            # 异常内容过滤：模型回显的提示词 / 元话语开场白，绝不写进文档
            if prompt_leak_hits(line) or is_meta_line(line):
                dropped.append(line)
                continue
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
                # ★ 专有名词书写规范（全局约束）：只作用于「总结行」。
                #   金句行必须逐字摘自原文，即使写法违规也不改写（is_quote 分支不动）。
                if TERM is not None:
                    line, _tc = TERM.normalize(line, is_quote=False)
                    term_fixes.extend(_tc)
                p = doc.add_paragraph()
                p.paragraph_format.space_after = Pt(4)
                p.paragraph_format.line_spacing = 1.35
                r = p.add_run(line)
                set_font(r, font)
                r.bold = True
                r.font.size = Pt(11)

    if dropped and log:
        log.error('  已拦截 %d 行非正文内容（提示词回显/元话语），未写入文档：%s'
                  % (len(dropped), ' | '.join(d[:40] for d in dropped[:3])))
    if term_fixes and log:
        seen = {}
        for a, b in term_fixes:
            seen[(a, b)] = seen.get((a, b), 0) + 1
        log.info('  专有名词书写规范：修正 %d 处（%s）'
                 % (len(term_fixes),
                    '、'.join('%s→%s×%d' % (a, b, c)
                              for (a, b), c in list(seen.items())[:6])))
    return dropped


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

        # 话题分隔：合并全部块 → 调模型在话题边界插入横线
        sep_cfg = cfg.get('topic_separator', {}) or {}
        if sep_cfg.get('enabled', True):
            combined = '\n'.join(blocks)
            segs = segment_topics_with(combined, lambda s, u: call_model(cfg, s, u, log)[0], log)
            log(f'  话题分隔完成：{len(segs)} 个话题块')
        else:
            segs = blocks
        first = created and len([p for p in doc.paragraphs if p.text.strip()]) == 0
        append_section(doc, heading, meta, segs, font, first, overview,
                       topic_sep=sep_cfg.get('enabled', True), log=log)
        # 保存前留一份 .bak（原子替换之外的第二道保险，读取端损坏时可回退）
        try:
            if os.path.exists(docx_path):
                import shutil as _sh
                _sh.copy2(docx_path, docx_path + '.bak')
        except Exception:
            pass
        save_doc_atomic(doc, docx_path, log)
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
