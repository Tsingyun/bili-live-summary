#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regen_gemini_v2.py —— 用 Google Gemini（逐字原始 prompt）重建 4 场录播总结。

设计要点：
1. 每场用「逐字原始 prompt」（prompt_original.txt）作为 system 指令，逐 30 分钟块发送，
   落实用户「每场录播开始总结的时候发一遍原始 prompt」的要求。Gemini 能稳定遵循该复杂格式
   （glm-4-flash 不行，所以切回 Gemini）。
2. 模型轮换：优先 gemini-3.7-flash（2026-08-14 AI Studio 上线 GA，已设为首选），遇到 HTTP 429（每日配额耗尽）或 404（不可用）自动切到
   下一个模型；每个模型成功调用达 20 次也主动切换（"20次后切换"的软上限）。
   顺序（按能力）：3.7-flash → 3.6-flash → 3.5-flash → 3-flash → 2.5-flash → 3.5-flash-lite → …以此类推。
3. RPM 限流：Google 免费层 RPM=5，强制任意 60 秒内请求数 ≤ 5。
4. 抗环境回收：每块产出缓存到 sidecar JSON，重拉后从最后一个完成块续跑；每场整段写完后
   才 append 进文档（用 has_section 去重，已存在则跳过整场）。
5. 启动级单实例锁，避免后台任务被环境双拉起成两个互相抢锁的副本。

用法:
  python regen_gemini_v2.py            # 跑全部 4 场（断点续跑）
  python regen_gemini_v2.py --test-one # 只跑 8/9 第 1 块并打印原始输出，用于格式核验
"""

import argparse
import datetime
import json
import os
import re
import sys
import time

import requests

# 复用技能库里的 Word 排版 / 文档函数（粗体总结、斜体金句、写锁等）
import summarize_gemini as S
from common import (load_config, Logger, clean_punctuation, normalize_numbers,
                    force_unlink, force_rmtree)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_workdir(explicit=None):
    """确定工作目录（按优先级）：
    1. explicit（调用方显式传入，如 --workdir）
    2. 环境变量 BILI_WORKDIR
    3. skill 同级 _workdir.json 中的 workdir
    4. 环境变量 BILI_LIVE_CONFIG 所在目录
    全部缺失则抛错。工作目录与主播一一对应，故 skill 可同时服务多位主播。"""
    if explicit:
        return explicit
    env = os.environ.get('BILI_WORKDIR')
    if env and os.path.isdir(env):
        return env
    ptr = os.path.join(SCRIPT_DIR, '_workdir.json')
    if os.path.exists(ptr):
        try:
            d = json.load(open(ptr, encoding='utf-8'))
            if d.get('workdir') and os.path.isdir(d['workdir']):
                return d['workdir']
        except Exception:
            pass
    cfg_env = os.environ.get('BILI_LIVE_CONFIG')
    if cfg_env and os.path.isfile(cfg_env):
        wd = os.path.dirname(os.path.abspath(cfg_env))
        if os.path.isdir(wd):
            return wd
    raise RuntimeError('无法确定工作目录：请传 --workdir、设 BILI_WORKDIR，'
                       '或在 skill 目录放 _workdir.json')


PROXY = 'http://127.0.0.1:7897'  # 默认本地 Clash；config.summarize.proxy 可覆盖


def get_api_key(cfg):
    """取 Gemini API Key（脱敏）：优先环境变量 GEMINI_API_KEY，其次 config.summarize.api_key。"""
    key = os.environ.get('GEMINI_API_KEY')
    if key:
        return key
    key = (cfg.get('summarize') or {}).get('api_key')
    if key:
        return key
    raise RuntimeError('缺少 Gemini API Key：请设置环境变量 GEMINI_API_KEY，'
                       '或在 config.json 的 summarize.api_key 填写')


def load_prompt(cfg):
    """读取总结用的 system prompt。优先 config.summarize.prompt_path，
    否则用工作目录下的 prompt_original.txt。支持 {streamer}/{audience} 占位符
    （来自 config.summarize），无占位符则原文不变，兼容岁己既有字面 prompt。"""
    p = (cfg.get('summarize') or {}).get('prompt_path') \
        or os.path.join(cfg['_workdir'], 'prompt_original.txt')
    with open(p, 'r', encoding='utf-8') as f:
        text = f.read().strip()
    text = text.replace('{streamer}', (cfg.get('summarize') or {}).get('streamer', '主播'))
    text = text.replace('{audience}', (cfg.get('summarize') or {}).get('audience', '观众'))
    return text


def build_overview_sys(cfg):
    """「内容概要」用的 system 指令，主播名/观众名从 config 读取（不再写死）。"""
    streamer = (cfg.get('summarize') or {}).get('streamer', '主播')
    audience = (cfg.get('summarize') or {}).get('audience', '观众')
    return (
        '你是一名直播内容编辑。下面是一场直播的分段总结（按时间顺序，每段覆盖约30分钟）。'
        '请综合这些分段，写一段【内容概要】：用自然流畅的中文 prose（不是列表、不用项目符号），'
        f'约 200–300 字，以“{streamer}”为主语，概括这场直播整体在做什么、聊了哪些话题、氛围如何、'
        '有哪些关键节点或名场面。不要逐条复述时间轴，重在让读者一眼抓住“这场直播是什么”的整体印象。'
        f'观众统称“{audience}”。纯文本输出，不要使用 ** 或 * 等 markdown 标记。'
    )


def touch_heartbeat(cfg):
    """刷新心跳时间戳，供看门狗/启动锁判断本实例是否仍存活（抗 PID 复用）。"""
    try:
        with open(os.path.join(cfg['_workdir'], '.regen_gemini_v2.heartbeat'),
                  'w', encoding='utf-8') as _f:
            _f.write(str(int(time.time())))
    except Exception:
        pass

# 模型轮换顺序（按能力，用户指定 + 依能力类推的补充 lite/pro 兜底）
# 2026-08-14：Gemini 3.7 Flash 于 AI Studio 上线 GA，提升为首选（能力最强、单价更低）
MODELS = [
    'gemini-3.7-flash',
    'gemini-3.6-flash',
    'gemini-3.5-flash',
    'gemini-3-flash',
    'gemini-2.5-flash',
    'gemini-3.5-flash-lite',
    'gemini-3.6-flash-lite',
    'gemini-3-flash-lite',
    'gemini-2.5-flash-lite',
    'gemini-2.5-pro',
    'gemini-3-pro',
    'gemini-3.5-pro',
]

# 只处理 2026-08-09 起的 4 场（8/1 那期不在范围内）
TARGETS = [
    ('20260809-重温第一次3D演出！ 2026年08月09日20点场.txt', '2026-08-09'),
    ('20260810-陪你这个猪看滴嘟侠炒饭2！ 2026年08月10日20点场.txt', '2026-08-10'),
    ('20260811-和米帕栞栞玩机械狂欢！ 2026年08月11日15点场.txt', '2026-08-11'),
    ('20260811-陪你这个猪过周二! 2026年08月11日20点场.txt', '2026-08-11'),
]


class GeminiRotator:
    """按模型列表轮换调用 Gemini；429/404 自动切下一个；RPM≤5/60s。"""

    def __init__(self, api_key, proxy, models, rpm=5, window=60, max_per_model=20, log=print):
        self.api_key = api_key
        self.proxies = {'http': proxy, 'https': proxy} if proxy else None
        self.models = models
        self.idx = 0
        self.rpm = rpm
        self.window = window
        self.max_per_model = max_per_model
        self._ts = []
        self._used = {}
        self.log = log

    def _rpm_wait(self):
        now = time.time()
        self._ts = [t for t in self._ts if now - t < self.window]
        while len(self._ts) >= self.rpm and (time.time() - self._ts[0]) < self.window:
            wait = self.window - (time.time() - self._ts[0]) + 0.3
            self.log(f'  [RPM] 已达 {self.rpm}/分钟上限，等待 {wait:.0f}s')
            time.sleep(wait)
            now = time.time()
            self._ts = [t for t in self._ts if now - t < self.window]

    def _post(self, model, system_prompt, user_content):
        url = (f'https://generativelanguage.googleapis.com/v1beta/models/{model}'
               f':generateContent?key={self.api_key}')
        payload = {
            'systemInstruction': {'parts': [{'text': system_prompt}]},
            'contents': [{'role': 'user', 'parts': [{'text': user_content}]}],
            'generationConfig': {'temperature': 0.3, 'maxOutputTokens': 8192},
        }
        headers = {'Content-Type': 'application/json'}
        try:
            return requests.post(url, headers=headers, json=payload, timeout=300, proxies=self.proxies)
        except requests.exceptions.ConnectionError:
            if self.proxies:
                self.log('  [Gemini] 代理连接失败，回退直连重试')
                return requests.post(url, headers=headers, json=payload, timeout=300, proxies=None)
            raise

    def _advance(self):
        self.idx = (self.idx + 1) % len(self.models)

    def call(self, system_prompt, user_content, tag=''):
        self._rpm_wait()
        tried = 0
        last_err = ''
        while tried < len(self.models):
            model = self.models[self.idx]
            self.log(f'  [Gemini] {tag} 模型={model}（已用 {self._used.get(model, 0)}/{self.max_per_model}）')
            r = self._post(model, system_prompt, user_content)
            now = time.time()
            self._ts.append(now)
            if len(self._ts) > self.rpm:
                self._ts = self._ts[-self.rpm:]
            if r.status_code == 200:
                d = r.json()
                cand = (d.get('candidates') or [{}])[0]
                parts = (cand.get('content') or {}).get('parts') or []
                text = ''.join(p.get('text', '') for p in parts).strip()
                if text:
                    self._used[model] = self._used.get(model, 0) + 1
                    if self._used[model] >= self.max_per_model:
                        self.log(f'  [Gemini] {model} 已达 {self.max_per_model} 次，切换下一模型')
                        self._advance()
                    return text
                last_err = f'{model} 空响应 finishReason={cand.get("finishReason")}'
                self.log(f'  [Gemini] {last_err}，重试该模型')
                time.sleep(3)
                continue
            elif r.status_code == 429:
                last_err = f'{model} HTTP 429 配额耗尽'
                self.log(f'  [Gemini] {last_err}，切换下一模型')
                self._advance(); tried += 1; continue
            elif r.status_code == 404:
                last_err = f'{model} HTTP 404 不可用'
                self.log(f'  [Gemini] {last_err}，切换下一模型')
                self._advance(); tried += 1; continue
            elif r.status_code in (500, 503, 504):
                last_err = f'{model} HTTP {r.status_code}'
                self.log(f'  [Gemini] {last_err}，切换下一模型')
                time.sleep(min(8 * (tried + 1), 60))
                self._advance(); tried += 1; continue
            else:
                last_err = f'{model} HTTP {r.status_code}: {r.text[:200]}'
                self.log(f'  [Gemini] {last_err}')
                self._advance(); tried += 1; continue
        self.log(f'  [Gemini] 所有模型均不可用：{last_err}')
        return ''


def load_progress(path):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_progress(data, path):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_user(chunk_label, idx, total, content):
    return (
        f'请对以下直播转录内容严格按「系统指令」进行总结与重组'
        f'（本场直播 {chunk_label} 时段，第 {idx}/{total} 个片段）。\n\n'
        f'输入文本每一行都以 [HH:MM:SS] 开头，表示该句在本场直播中的绝对时间；'
        f'请据此标注时间，且时间必须落在本片段区间 {chunk_label} 内。\n\n'
        f'请输出纯文本，不要使用 ** 或 * 等 markdown 标记；'
        f'粗体与斜体效果由后续排版自动处理。\n\n'
        f'{content}'
    )


# 内容概要指令已改为配置驱动：见 build_overview_sys(cfg)，主播名/观众名取自 config.summarize。
# 保留此常量仅为向后兼容极旧的调用，新代码一律用 build_overview_sys(cfg)。
OVERVIEW_SYS_DEPRECATED = (
    '你是一名直播内容编辑。下面是一场直播的分段总结（按时间顺序，每段覆盖约30分钟）。'
    '请综合这些分段，写一段【内容概要】：用自然流畅的中文 prose（不是列表、不用项目符号），'
    '约 200–300 字，概括这场直播整体在做什么、聊了哪些话题、氛围如何、'
    '有哪些关键节点或名场面。不要逐条复述时间轴，重在让读者一眼抓住“这场直播是什么”的整体印象。'
    '纯文本输出，不要使用 ** 或 * 等 markdown 标记。'
)


def process_session(rot, sys_prompt, cfg, fname, date_str, font, log, progress, src_path=None):
    txt_path = os.path.join(cfg['_dirs']['txt'], fname)
    progress_path = os.path.join(cfg['_workdir'], '.regen_gemini_v2_progress.json')
    title = fname.split('-', 1)[1].rsplit('.txt', 1)[0] if '-' in fname else fname
    heading = title
    d = datetime.date.fromisoformat(date_str)
    docx_path = S.doc_path_for(cfg, d)

    lock_path = docx_path + '.writelock'
    if not S._acquire_doc_lock(lock_path):
        log.error(f'获取写锁超时，跳过：{heading}')
        return False

    try:
        doc, created = S.open_or_create(docx_path, font)
        if S.has_section(doc, heading):
            log(f'文档已存在「{heading[:30]}」章节，跳过整场')
            progress.pop(date_str, None)
            save_progress(progress, progress_path)
            return True

        chunks = S.load_and_chunk(txt_path, int(cfg['summarize'].get('chunk_minutes', 30)) * 60)
        if not chunks:
            log.error(f'{fname} 无带时间轴内容，跳过')
            return False
        total = len(chunks)
        log(f'== 载入全量原始规则并开始：{heading}（{total} 块）==')

        sess = progress.setdefault(date_str, {'chunks': {}, 'overview': ''})
        all_blocks = []
        for i, (start, content) in enumerate(chunks, 1):
            touch_heartbeat(cfg)
            label = f'{S.hms(start)} - {S.hms(start + int(cfg["summarize"].get("chunk_minutes", 30)) * 60)}'
            if str(i) in sess['chunks']:
                all_blocks.append(sess['chunks'][str(i)])
                log(f'  [{i}/{total}] {label} [缓存命中]')
                continue
            user = build_user(label, i, total, content)
            t0 = time.time()
            text = rot.call(sys_prompt, user, tag=f'[{i}/{total}]')
            if not text:
                log.error(f'  [{i}/{total}] {label} [FAIL] 所有模型失败，本场中止（保留进度，稍后续跑）')
                save_progress(progress, progress_path)
                return False
            # 清洗可能由模型生成的冗余双标点（如 "。，"），双保险
            text = clean_punctuation(text)
            # 数字归一双保险：规格数字若是中文读法也转回阿拉伯（3050显卡/75Hz/8G）
            text = normalize_numbers(text)
            sess['chunks'][str(i)] = text
            save_progress(progress, progress_path)
            all_blocks.append(text)
            log(f'  [{i}/{total}] {label} [OK] {len(text)} 字 / {time.time() - t0:.0f}s')

        # 概要
        if not sess.get('overview'):
            summary_only = '\n'.join(
                ln for b in all_blocks for ln in b.split('\n')
                if re.match(r'^\s*\[\d{1,2}:\d{2}:\d{2}\]', ln) and not S.is_quote_line(ln.strip())
            )
            if summary_only.strip():
                ov = rot.call(build_overview_sys(cfg), summary_only, tag='[概要]')
                if ov:
                    sess['overview'] = normalize_numbers(clean_punctuation(ov))
                    save_progress(progress, progress_path)
                    log(f'  概要生成完成（{len(ov)} 字）')
                else:
                    log.error('  概要生成失败，跳过概要（不影响正文）')
        overview = sess.get('overview', '')

        # 话题分隔：合并全部块 → 调模型在话题边界插入横线
        sep_cfg = cfg.get('topic_separator', {}) or {}
        if sep_cfg.get('enabled', True):
            combined = '\n'.join(all_blocks)
            segs = S.segment_topics_with(combined, lambda s, u: rot.call(s, u, tag='[话题分隔]'), log)
            log(f'  话题分隔完成：{len(segs)} 个话题块')
        else:
            segs = all_blocks

        chunk_sec = int(cfg['summarize'].get('chunk_minutes', 30)) * 60
        dur = S.hms(chunks[-1][0] + chunk_sec)
        meta = [f'直播日期 {d:%Y-%m-%d}  |  时长 {dur}  |  {len(all_blocks)}/{total} 段  |  总结于 {datetime.datetime.now():%Y-%m-%d %H:%M}']
        if src_path:
            meta.append(f'来源 {src_path}')
        first = created and len([p for p in doc.paragraphs if p.text.strip()]) == 0
        S.append_section(doc, heading, meta, segs, font, first, overview,
                         topic_sep=sep_cfg.get('enabled', True))
        os.makedirs(os.path.dirname(docx_path), exist_ok=True)
        doc.save(docx_path)
        log(f'  ✅ 已保存：{docx_path}（{len(all_blocks)} 块）')
        # 整场完成，清理该场进度缓存
        progress.pop(date_str, None)
        save_progress(progress, progress_path)
        return True
    finally:
        S._release_doc_lock(lock_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--workdir', default=None, help='工作目录（含 config.json）；不传则按 BILI_WORKDIR / _workdir.json / BILI_LIVE_CONFIG 推断')
    ap.add_argument('--test-one', action='store_true', help='只跑 TXT 目录里第 1 个 .txt 的第 1 块并打印原始输出')
    args = ap.parse_args()

    workdir = resolve_workdir(args.workdir)
    DOC_LOCK = os.path.join(workdir, '.regen_gemini_v2.lock')
    HEARTBEAT = os.path.join(workdir, '.regen_gemini_v2.heartbeat')
    PROGRESS = os.path.join(workdir, '.regen_gemini_v2_progress.json')

    # 启动级单实例锁（抗环境回收 + 抗 Windows PID 复用）：
    # 用「心跳文件」判断持有者是否还活着，而非单纯 os.kill(pid,0)。
    # 环境回收进程后 PID 会被其它进程顶替，os.kill 会误判“仍存活”→ 永久死锁。
    # 心跳新鲜（<240s）才认为实例存活；否则视为孤儿锁，强制清除后本实例接管。
    def lock_holder_alive():
        if not os.path.exists(DOC_LOCK):
            return False
        try:
            age = time.time() - float(open(HEARTBEAT, encoding='utf-8').read().strip() or '0')
        except Exception:
            age = 99999
        return age < 240

    def grab_lock():
        try:
            lf = os.open(DOC_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(lf, str(os.getpid()).encode())
            return lf
        except OSError:
            return None

    lf = grab_lock()
    if lf is None:
        if lock_holder_alive():
            print('[regen_gemini_v2] 另一实例已在运行（心跳有效），本实例退出')
            sys.exit(0)
        # 孤儿锁：持有者心跳已失效，强制清除后重试
        try:
            force_unlink(DOC_LOCK)
        except OSError:
            pass
        try:
            force_unlink(HEARTBEAT)
        except OSError:
            pass
        lf = grab_lock()
        if lf is None:
            print('[regen_gemini_v2] 锁仍被占用，退出')
            sys.exit(0)
    touch_heartbeat({'_workdir': workdir})

    cfg = load_config(os.path.join(workdir, 'config.json'))
    log = Logger(cfg, 'regen_gemini_v2')
    font = cfg['output'].get('font', '思源宋体CN Light')
    sys_prompt = load_prompt(cfg)

    rot = GeminiRotator(get_api_key(cfg), cfg['summarize'].get('proxy') or PROXY,
                        MODELS, rpm=5, window=60, max_per_model=20, log=log)
    progress = load_progress(PROGRESS)

    # 扫描工作目录 TXT 下所有 .txt（已写入文档的会被 process_session 的 has_section 跳过）
    txt_dir = cfg['_dirs']['txt']
    targets = []
    if os.path.isdir(txt_dir):
        for fn in sorted(os.listdir(txt_dir)):
            if not fn.lower().endswith('.txt'):
                continue
            m = re.match(r'(\d{8})-', fn)
            ds = '%s-%s-%s' % (m.group(1)[:4], m.group(1)[4:6], m.group(1)[6:8]) if m else ''
            targets.append((fn, ds))

    if args.test_one:
        if not targets:
            print('[regen_gemini_v2] TXT 目录无 .txt 文件')
            sys.exit(1)
        txt_path = os.path.join(txt_dir, targets[0][0])
        chunks = S.load_and_chunk(txt_path, 30 * 60)
        c0 = chunks[0]
        label = f'{S.hms(c0[0])} - {S.hms(c0[0] + 30 * 60)}'
        user = build_user(label, 1, len(chunks), c0[1])
        out = rot.call(sys_prompt, user, tag='[TEST]')
        print('\n========== RAW OUTPUT (%s block1) ==========' % targets[0][0])
        print(out)
        print('========== END ==========')
        os.close(lf)
        sys.exit(0)

    ok = 0
    for fname, date_str in targets:
        touch_heartbeat({'_workdir': workdir})
        try:
            if process_session(rot, sys_prompt, cfg, fname, date_str, font, log, progress):
                ok += 1
        except Exception as e:
            import traceback
            log.error(f'{fname} 异常：{e}')
            traceback.print_exc()
    log(f'Gemini 重建完成 {ok}/{len(targets)}')
    try:
        os.close(lf)
        force_unlink(DOC_LOCK)
    except Exception:
        pass
    try:
        force_unlink(HEARTBEAT)
    except Exception:
        pass


if __name__ == '__main__':
    main()
