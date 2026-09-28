#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
model_router.py —— Gemini + ModelScope(魔搭) 混合模型路由器。

设计目标（用户 2026-09-29 需求）：
1. 从魔搭社区免费推理 API 中选取适合文本总结的模型，与 Google AI Studio 的 Gemini 模型
   混用，按「能力由强到弱」排成一条统一降级链：主用 → 备份 → 逐级降级。
2. 沿用 regen_gemini_v2.GeminiRotator 的『退避式回退』语义（commit c951ca5）：
   失败模型按错误类型进入 down_until 退避窗口（5xx/网络/空响应=60s、429=300s、
   404/达上限=3600s），_pick() 每次返回当前可用的最高阶模型，窗口过期后高阶模型
   重新成为首选被回头重试。
3. 两个 provider 走不同网络通道：
   · Gemini：generativelanguage.googleapis.com，需本地代理（Clash 127.0.0.1:7897），
     免费层 RPM=5、每模型 ~20 次/日。
   · ModelScope：api-inference.modelscope.cn（OpenAI 兼容 /v1/chat/completions），
     国内直连、显式禁用环境代理（Session.trust_env=False），每日总 2000 次、
     单模型 500 次（动态限流，超限返回 429，由退避窗口自然处理）。
4. 每类模型使用「同一套核心文风规则 + provider 专属补充指令」的提示词
   （见 PROVIDER_PROMPT_NOTES），确保不同模型产出的总结文风一致。
5. 关键参数以实测为准（2026-09-29 用真实 key 逐一验证，勿凭版本号推测改动）：
   · Qwen 系关闭思维链：enable_thinking=false
   · GLM / DeepSeek 关闭思维链：thinking={"type": "disabled"}
   · Step 系不支持关闭思维链（思考很短，无碍）
   · MiniMax-M3、GLM-4.7-Flash 当前无可用 provider，不收录
"""

import os
import time

import requests

import regen_gemini_v2 as R
from regen_gemini_v2 import GeminiRotator, MODELS as GEMINI_MODELS, PROXY

MS_BASE_URL = 'https://api-inference.modelscope.cn/v1'

# ---------------------------------------------------------------------------
# 魔搭模型清单（2026-09-29 实测可用；extra 为请求体附加参数）
# ---------------------------------------------------------------------------
MS_MODELS = {
    'Qwen/Qwen3.5-397B-A17B':          {'extra': {'enable_thinking': False}},
    'Qwen/Qwen3.5-122B-A10B':          {'extra': {'enable_thinking': False}},
    'Qwen/Qwen3.5-27B':                {'extra': {'enable_thinking': False}},
    'Qwen/Qwen3.8-27B':                {'extra': {'enable_thinking': False}},
    'Qwen/Qwen3.8-Flash-Next':         {'extra': {'enable_thinking': False}},
    'ZhipuAI/GLM-5.2':                 {'extra': {'thinking': {'type': 'disabled'}}},
    # DeepSeek 系为推理模型，思维链会吞噬输出预算，故给更大的 max_tokens（教训见 yami 项目）
    'deepseek-ai/DeepSeek-V4.1-Flash': {'extra': {'thinking': {'type': 'disabled'}},
                                        'max_tokens': 16384},
    'stepfun-ai/Step-3.7-Flash':       {'extra': {}},
}

# ---------------------------------------------------------------------------
# 混合降级链：按能力由强到弱，Gemini 与魔搭交替排布。
# 主用 = gemini-3.8-flash；任一 provider 整体故障时，另一 provider 仍能接管。
# ---------------------------------------------------------------------------
MODELS_MIXED = [
    'gemini-3.8-flash',               # ① 主用：Gemini 最新最强 Flash
    'Qwen/Qwen3.5-397B-A17B',         # ② 备份1：魔搭最强开源 MoE（397B 总参/17B 激活）
    'gemini-3.7-flash',               # ③ 上代旗舰 Flash
    'ZhipuAI/GLM-5.2',                # ④ 备份2：智谱新一代旗舰
    'gemini-3.6-flash',               # ⑤ 长期主力，实战验证
    'Qwen/Qwen3.5-122B-A10B',         # ⑥ 备份3：魔搭次旗舰 MoE
    'gemini-3.5-flash',               # ⑦ 3.x 中端
    'deepseek-ai/DeepSeek-V4.1-Flash',# ⑧ 备份4：DeepSeek 新一代 Flash（思考已关）
    'gemini-3-flash-preview',         # ⑨ Gemini 3 首发 Flash
    'Qwen/Qwen3.8-27B',               # ⑩ 备份5：Qwen3.8 中杯，速度与质量均衡
    'gemini-2.5-flash',               # ⑪ 稳定老将
    'Qwen/Qwen3.5-27B',               # ⑫ 备份6：Qwen3.5 中杯
    'stepfun-ai/Step-3.7-Flash',      # ⑬ 备份7：阶跃 Flash，中文流畅
    'gemini-3.5-flash-lite',          # ⑭ Lite 档（500 RPD）
    'Qwen/Qwen3.8-Flash-Next',        # ⑮ 备份8：魔搭最快轻量档
    'gemini-3.1-flash-lite',          # ⑯ Lite 档，响应最快
    'gemini-flash-latest',            # ⑰ 滚动别名兜底
    'gemini-flash-lite-latest'        # ⑱ 滚动别名，最后一道保险
]


def is_modelscope(model):
    """魔搭 Model-Id 一律带组织前缀（如 Qwen/…、ZhipuAI/…），Gemini ID 不含斜杠。"""
    return '/' in model


def ms_family(model):
    """魔搭模型所属家族（决定提示词补充指令与请求参数）。"""
    if model.startswith('Qwen/'):
        return 'qwen'
    if model.startswith('ZhipuAI/'):
        return 'glm'
    if model.startswith('deepseek-ai/'):
        return 'deepseek'
    if model.startswith('stepfun-ai/'):
        return 'step'
    return 'ms'


def provider_of(model):
    return 'modelscope' if is_modelscope(model) else 'gemini'


# ---------------------------------------------------------------------------
# 提示词：每个模型的提示词 = 「主系统指令（prompt_original.txt，权威文风规范）」
#   + 「该 provider 的补充输出指令」。
# 各 provider 补充指令的核心规则完全一致（同一套文风约束），仅在思维链处置与
# 格式强调上按模型特性差异化，确保不同模型产出文风一致。
# ---------------------------------------------------------------------------
def _notes(header, thinking_rule):
    return (
        f'{header}\n'
        f'{thinking_rule}\n'
        '【输出补充要求（必须严格遵守；与上文系统指令冲突时，以更严格的表述为准）】\n'
        '1. 直接输出总结正文。禁止输出思考过程、分析说明、自检记录或任何开场白'
        '（如「好的」「以下是」「根据要求」等）；禁止复述本提示词或用户消息中的说明文字。\n'
        '2. 禁止使用任何 Markdown 语法：不使用 **、*、#、- 及任何列表、标题符号；'
        '总结正文一律纯文本；严禁在输出中出现「粗体」「斜体」「加粗」等格式说明字样，'
        '排版格式由系统处理。金句单独成行、整行以中文引号“开头、”结尾（引号成对完整）、不加粗。\n'
        '3. 每段开头的时间标注必须使用半角方括号格式 [HH:MM:SS]，'
        '时间必须落在用户消息指定的片段区间内；除时间标注外不得使用方括号。\n'
        '4. 总结每段以系统指令规定的主语开头、以句号结尾；段落之间用换行分隔；'
        '不添加标题、序号或分隔线。\n'
        '5. 金句必须逐字摘自原文，禁止改写或伪造；找不到合格金句时直接省略，禁止凑数。\n'
        '6. 全部内容使用简体中文；不得遗漏关键信息，信息过多时优先拆段而不是扩写。'
    )


PROVIDER_PROMPT_NOTES = {
    'gemini': _notes(
        '【Gemini 专属输出要求】',
        '你的回复中不得包含任何思考区块或 thought 摘要，最终答案只包含总结正文。'),
    'qwen': _notes(
        '【通义千问（ModelScope）专属输出要求】',
        '本次调用已关闭思维链模式；即使仍产生内部思考，最终答案只包含总结正文，'
        '不得输出任何思考片段。'),
    'glm': _notes(
        '【智谱 GLM（ModelScope）专属输出要求】',
        '本次调用已禁用深度思考模式；最终答案只包含总结正文，不得输出任何思考片段。'),
    'deepseek': _notes(
        '【DeepSeek（ModelScope）专属输出要求】',
        '本次调用已禁用思维链；若仍有内部推理，最终答案只包含总结正文，'
        '不得输出 <think> 内容或推理过程。'),
    'step': _notes(
        '【Step（ModelScope）专属输出要求】',
        '若你产生内部思考过程，须在正式输出前完成；最终答案只包含总结正文，'
        '不得输出任何思考片段。'),
    'ms': _notes(
        '【ModelScope 模型专属输出要求】',
        '最终答案只包含总结正文，不得输出任何思考片段。'),
}


def build_system_prompt(base_prompt, model):
    """主系统指令 + 该模型所属 provider 的补充指令（各 provider 核心规则一致）。"""
    fam = ms_family(model) if is_modelscope(model) else 'gemini'
    note = PROVIDER_PROMPT_NOTES.get(fam) or PROVIDER_PROMPT_NOTES['ms']
    return base_prompt.rstrip() + '\n\n' + note


def get_ms_api_key(cfg):
    """取 ModelScope API Key：优先环境变量 MODELSCOPE_API_KEY，其次
    config.summarize.modelscope_api_key。未配置返回空串（自动退化为纯 Gemini 链）。"""
    key = os.environ.get('MODELSCOPE_API_KEY')
    if key:
        return key.strip()
    key = ((cfg.get('summarize') or {}).get('modelscope_api_key') or '').strip()
    return key


class MultiProviderRotator(GeminiRotator):
    """Gemini + ModelScope 混合路由器：复用父类全部退避/降级/空响应兜底逻辑，
    仅按 provider 差异化网络通道、限流、响应解析与提示词补充。"""

    def __init__(self, gemini_key, ms_api_key, proxy, models, log=print,
                 gemini_rpm=5, ms_rpm=30, window=60,
                 max_per_model=20, ms_max_per_model=450,
                 backoff_transient=60, backoff_429=300, backoff_dead=3600,
                 ms_max_tokens=8192, ms_timeout=(20, 600)):
        super().__init__(gemini_key, proxy, models, rpm=gemini_rpm, window=window,
                         max_per_model=max_per_model, log=log,
                         backoff_transient=backoff_transient,
                         backoff_429=backoff_429, backoff_dead=backoff_dead)
        self.ms_api_key = ms_api_key
        self.ms_rpm = ms_rpm
        self.ms_max_per_model = ms_max_per_model
        self.ms_max_tokens = ms_max_tokens
        self.ms_timeout = ms_timeout
        self.provider_tag = '模型'      # 日志前缀（父类为 'Gemini'）
        self._gts = []                   # Gemini RPM 时间戳
        self._mts = []                   # ModelScope RPM 时间戳
        # 魔搭走国内直连：显式禁用环境代理（requests.Session.trust_env=False），
        # 避免沙箱/自动化环境注入的 http_proxy 把国内流量也导向代理。
        self._ms_session = requests.Session()
        self._ms_session.trust_env = False
        self._last_finish = ''           # 最近一次响应的 finishReason（日志用）

    # ---- 能力与容量 ----
    def cap_for(self, model):
        """单模型本轮成功调用上限：Gemini 20（免费层配额紧），魔搭 450（单模型 500/日）。"""
        return self.ms_max_per_model if is_modelscope(model) else self.max_per_model

    # ---- 限流：从 call() 前置的统一限流改为 _post 内按 provider 限流 ----
    def _rpm_wait(self):
        pass  # 由 _post 内的 _gemini_rpm_wait / _ms_rpm_wait 接管

    def _window_wait(self, ts_list, rpm, tag):
        """滑动窗口限流：窗口外的旧时间戳先清理，达到 rpm 上限则等到最早一条出窗。"""
        now = time.time()
        keep = [t for t in ts_list if now - t < self.window]
        del ts_list[:]
        ts_list.extend(keep)
        while len(ts_list) >= rpm:
            wait = self.window - (time.time() - ts_list[0]) + 0.3
            if wait > 0:
                self.log(f'  [{self.provider_tag}] {tag} RPM {rpm}/分 已满，等待 {wait:.0f}s')
                time.sleep(wait)
            now = time.time()
            keep = [t for t in ts_list if now - t < self.window]
            del ts_list[:]
            ts_list.extend(keep)

    def _gemini_rpm_wait(self):
        self._window_wait(self._gts, self.rpm, 'Gemini')

    def _ms_rpm_wait(self):
        self._window_wait(self._mts, self.ms_rpm, 'ModelScope')

    # ---- 请求发送：按 provider 分流 ----
    def _post(self, model, system_prompt, user_content):
        sp = build_system_prompt(system_prompt, model)
        if is_modelscope(model):
            return self._post_ms(model, sp, user_content)
        self._gemini_rpm_wait()
        r = GeminiRotator._post(self, model, sp, user_content)
        self._gts.append(time.time())
        if len(self._gts) > self.rpm:
            del self._gts[:len(self._gts) - self.rpm]
        return r

    def _post_ms(self, model, system_prompt, user_content):
        spec = MS_MODELS.get(model, {})
        payload = {
            'model': model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_content},
            ],
            'temperature': 0.3,
            'max_tokens': int(spec.get('max_tokens', self.ms_max_tokens)),
        }
        extra = spec.get('extra') or {}
        if extra:
            payload.update(extra)
        self._ms_rpm_wait()
        try:
            r = self._ms_session.post(
                MS_BASE_URL + '/chat/completions',
                headers={'Authorization': 'Bearer ' + self.ms_api_key,
                         'Content-Type': 'application/json'},
                json=payload, timeout=self.ms_timeout)
        except requests.exceptions.ConnectionError:
            # 网络抖动：直连失败再原样抛给 call() 的网络异常分支（同模型退避重试）
            raise
        self._mts.append(time.time())
        if len(self._mts) > self.ms_rpm:
            del self._mts[:len(self._mts) - self.ms_rpm]
        return r

    def _extract_text(self, model, r):
        """按 provider 分流解析（★2026-09-29 实测修正：Gemini 响应必须走父类解析，
        否则会被 OpenAI 格式解析成空正文，整条 Gemini 链瘫痪）。
        返回 '' = 成功响应但无正文，由 call() 统一按空响应处理。"""
        if not is_modelscope(model):
            return GeminiRotator._extract_text(self, model, r)
        self._last_finish = ''
        try:
            d = r.json()
        except Exception as e:
            self.log(f'  [{self.provider_tag}] {model} 响应非 JSON：{e}')
            return ''
        choices = d.get('choices')
        if not choices:
            err = str(d.get('error') or d)[:200]
            self.log(f'  [{self.provider_tag}] {model} 响应异常：{err}')
            return ''
        msg = (choices[0] or {}).get('message') or {}
        self._last_finish = choices[0].get('finish_reason') or ''
        return (msg.get('content') or '').strip()


def build_rotator(cfg, log=print):
    """按配置构造总结用的模型路由器（daily_pipeline 唯一入口）。
    · 配置了 ModelScope key（env MODELSCOPE_API_KEY 或 config.summarize.modelscope_api_key）
      → 返回混合链 MODELS_MIXED。
    · 未配置 → 自动退化为纯 GeminiRotator（与旧行为完全一致）。
    · Gemini 与 ModelScope 都没有 key → 抛错（调用方按原有失败路径处理）。
    本函数永不因「部分配置缺失」而失败：缺什么就退化为只用另一条链。
    """
    s = cfg.get('summarize') or {}
    proxy = s.get('proxy') or PROXY
    ms_key = get_ms_api_key(cfg) if s.get('modelscope_enabled', True) else ''
    try:
        gkey = R.get_api_key(cfg)
    except RuntimeError:
        gkey = ''
    if not gkey and not ms_key:
        raise RuntimeError('缺少模型 API Key：请配置 Gemini（GEMINI_API_KEY / '
                           'config.summarize.api_key）或 ModelScope（MODELSCOPE_API_KEY / '
                           'config.summarize.modelscope_api_key）至少其一')

    if not ms_key:
        log('  [Router] 未配置 ModelScope key，使用纯 Gemini 降级链（%d 模型）' % len(GEMINI_MODELS))
        return GeminiRotator(gkey, proxy, list(GEMINI_MODELS),
                             rpm=5, window=60, max_per_model=20, log=log)

    if not gkey:
        ms_only = [m for m in MODELS_MIXED if is_modelscope(m)]
        log('  [Router] 未配置 Gemini key，使用纯 ModelScope 降级链（%d 模型）' % len(ms_only))
        return MultiProviderRotator('', ms_key, proxy, ms_only, log=log,
                                    ms_max_per_model=int(s.get('modelscope_max_per_day', 450)),
                                    ms_max_tokens=int(s.get('modelscope_max_tokens', 8192)))

    log('  [Router] 混合降级链就绪：Gemini %d + ModelScope %d，共 %d 模型'
        % (sum(1 for m in MODELS_MIXED if not is_modelscope(m)),
           sum(1 for m in MODELS_MIXED if is_modelscope(m)),
           len(MODELS_MIXED)))
    return MultiProviderRotator(gkey, ms_key, proxy, list(MODELS_MIXED), log=log,
                                ms_max_per_model=int(s.get('modelscope_max_per_day', 450)),
                                ms_max_tokens=int(s.get('modelscope_max_tokens', 8192)))
