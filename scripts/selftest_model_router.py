#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_model_router.py —— model_router 混合模型路由器离线回归（不耗任何 API 配额）。

覆盖场景：
  [A] 降级链顺序：高阶模型 429 → 立即落到下一可用模型
  [B] 退避回头：高阶模型 503 进入短退避，窗口过期后重新成为首选
  [C] 空响应收敛：连续空响应 3 次后该模型退避并换下家，绝不死循环
  [D] cap 差异化：Gemini 20 / ModelScope 450（cap_for），达上限本轮停用
  [E] 提示词：各 provider 补充指令注入，核心规则一致
  [F] 魔搭请求体：enable_thinking / thinking disabled / max_tokens 覆写正确
  [G] 响应解析：双格式分流（Gemini/OpenAI）、忽略 reasoning_content、坏 JSON 返回空
  [H] build_rotator：无 MS key 退化纯 Gemini；有 key 用混合链
  [I] 全链失败：尝试预算耗尽返回空串（不抛异常）
  [J] 用量账本：成功计数 + token 累计（双 provider）、失败不计、汇报格式与「无法统计」标注

跑法：python selftest_model_router.py（需要 requests + python-docx，用 bili-archive venv）
"""

import os
import sys
import time

import model_router as MR
from model_router import (MultiProviderRotator, build_rotator, build_system_prompt,
                          is_modelscope, MS_MODELS)
from regen_gemini_v2 import GeminiRotator
import regen_gemini_v2 as RG   # 模块级用量账本（USAGE / reset_usage / format_usage_report）

PASS, FAIL = 0, []


def check(name, cond, detail=''):
    global PASS
    if cond:
        PASS += 1
        print(f'  [PASS] {name}')
    else:
        FAIL.append(name)
        print(f'  [FAIL] {name}  {detail}')


class FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._p = payload
        self.text = str(payload)[:200]

    def json(self):
        if self._p is None:
            raise ValueError('no json')
        return self._p


def openai_resp(content, finish='stop'):
    return FakeResp(200, {'choices': [{'message': {'content': content,
                                                   'reasoning_content': 'SHOULD-IGNORE'},
                                       'finish_reason': finish}]})


def gemini_resp(content):
    return FakeResp(200, {'candidates': [{'content': {'parts': [{'text': content}]},
                                          'finishReason': 'STOP'}]})


def provider_resp(model, content):
    """按模型 provider 造格式正确的假响应（模拟真实 _post 的产物）。"""
    if is_modelscope(model):
        return openai_resp(content)
    return gemini_resp(content)


def make_rot(models, log=print, **kw):
    kw.setdefault('gemini_rpm', 1000)
    kw.setdefault('ms_rpm', 1000)
    return MultiProviderRotator('fake-gkey', 'fake-mskey', None, models, log=log, **kw)


def t_a_fallback_order():
    print('[A] 降级链顺序')
    rot = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.8-27B'],
                   backoff_429=60)  # 真实量级：429 后 60s 内不再首选

    def fake_post(model, sp, uc):
        if model == 'gemini-3.8-flash':
            return FakeResp(429, {'error': 'quota'})
        return provider_resp(model, 'OK-MS')
    rot._post = fake_post
    out = rot.call('sys', 'user', tag='[A]')
    check('A1 首选为列表首位', rot.models[0] == 'gemini-3.8-flash')
    check('A2 429 后落到魔搭备份', out == 'OK-MS', out)
    check('A3 429 模型进入退避窗口', 'gemini-3.8-flash' in rot.down_until)


def t_b_backoff_retry_higher():
    print('[B] 退避回头重试高阶')
    rot = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.8-27B'],
                   backoff_transient=60)  # 真实量级：503 后 60s 内低阶顶上
    state = {'first': True}

    def fake_post(model, sp, uc):
        if model == 'gemini-3.8-flash' and state['first']:
            state['first'] = False
            return FakeResp(503, {})
        return provider_resp(model, 'OK-' + model)
    rot._post = fake_post
    t0 = time.time()
    out1 = rot.call('sys', 'user', tag='[B1]', max_attempts=3)
    check('B1 503 后本轮用低阶完成', out1 == 'OK-Qwen/Qwen3.8-27B', out1)
    check('B1 无多余长等待', time.time() - t0 < 30, time.time() - t0)
    # 模拟 60s 后窗口过期
    rot.down_until['gemini-3.8-flash'] = time.time() - 1
    out2 = rot.call('sys', 'user', tag='[B2]')
    check('B2 窗口过期后高阶重新首选', out2 == 'OK-gemini-3.8-flash', out2)


def t_c_empty_converge():
    print('[C] 空响应收敛')
    rot = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.8-27B'],
                   backoff_transient=60)  # 真实量级：连续空响应 3 次后 60s 退避

    def fake_post(model, sp, uc):
        if model == 'gemini-3.8-flash':
            return provider_resp(model, '')  # 200 但空正文
        return provider_resp(model, 'OK-MS')
    rot._post = fake_post
    out = rot.call('sys', 'user', tag='[C]', max_attempts=6)
    check('C1 空响应 3 次后退避并换模型', out == 'OK-MS', out)
    check('C2 空响应模型进退避窗口', 'gemini-3.8-flash' in rot.down_until)


def t_d_caps():
    print('[D] cap 差异化')
    rot = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.8-27B'],
                   max_per_model=20, ms_max_per_model=450)
    check('D1 Gemini cap=20', rot.cap_for('gemini-3.8-flash') == 20)
    check('D2 魔搭 cap=450', rot.cap_for('Qwen/Qwen3.8-27B') == 450)
    rot2 = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.8-27B'],
                    max_per_model=1, backoff_dead=60)  # 达 cap 本轮停用（真实量级）

    def fake_post(model, sp, uc):
        return provider_resp(model, 'OK-' + model)
    rot2._post = fake_post
    rot2.call('s', 'u', tag='[D]', max_attempts=2)
    out = rot2.call('s', 'u', tag='[D]', max_attempts=2)
    check('D3 达 cap 后本轮停用换下家', out == 'OK-Qwen/Qwen3.8-27B', out)


def t_e_prompt():
    print('[E] 提示词注入（直接单测 build_system_prompt）')
    base = '任务说明：对输入文本进行分段重组与精炼总结。'
    fams = {
        'gemini-3.8-flash': 'gemini',
        'Qwen/Qwen3.8-27B': 'qwen',
        'Qwen/Qwen3.5-397B-A17B': 'qwen',
        'ZhipuAI/GLM-5.2': 'glm',
        'deepseek-ai/DeepSeek-V4.1-Flash': 'deepseek',
        'stepfun-ai/Step-3.7-Flash': 'step',
    }
    for model, fam in fams.items():
        sp = build_system_prompt(base, model)
        check(f'E1 {model} 含主指令', sp.startswith(base))
        check(f'E2 {model} 禁markdown', '禁止使用任何 Markdown' in sp)
        check(f'E3 {model} 禁思考泄漏', '只包含总结正文' in sp)
        check(f'E4 {model} 时间格式约束', '[HH:MM:SS]' in sp)
        check(f'E5 {model} 家族头标识', MR.PROVIDER_PROMPT_NOTES[fam].split('\n')[0] in sp)
        check(f'E6 {model} 不含请求体参数', 'enable_thinking' not in sp)
    check('E7 各家族提示词互不相同但核心一致',
          len(set(MR.PROVIDER_PROMPT_NOTES.values())) == len(MR.PROVIDER_PROMPT_NOTES)
          and all('禁止使用任何 Markdown' in v for v in MR.PROVIDER_PROMPT_NOTES.values()))


def t_f_ms_payload():
    print('[F] 魔搭请求体参数')
    captured = {}

    class FakeSession:
        trust_env = True

        def post(self, url, headers=None, json=None, timeout=None):
            captured.update(url=url, json=json, headers=headers)
            return openai_resp('OK')
    rot2 = MultiProviderRotator('g', 'm', None, ['Qwen/Qwen3.8-27B'])
    check('F1 真实会话 trust_env=False（国内直连）', rot2._ms_session.trust_env is False)
    rot2._ms_session = FakeSession()
    rot2.call('sys', 'user', tag='[F]', max_attempts=2)
    j = captured['json']
    check('F2 base_url 正确', captured['url'] == MR.MS_BASE_URL + '/chat/completions')
    check('F3 enable_thinking=False', j.get('enable_thinking') is False)
    check('F4 max_tokens 默认 8192', j.get('max_tokens') == 8192)
    check('F5 system+user 双消息', [m['role'] for m in j['messages']] == ['system', 'user'])
    check('F6 Bearer 鉴权', captured['headers']['Authorization'] == 'Bearer m')
    rot2.models = ['deepseek-ai/DeepSeek-V4.1-Flash']
    rot2.down_until.clear()
    rot2.call('sys', 'user', tag='[F2]', max_attempts=2)
    j2 = captured['json']
    check('F7 deepseek thinking disabled', j2.get('thinking') == {'type': 'disabled'})
    check('F8 deepseek max_tokens=16384', j2.get('max_tokens') == 16384)


def t_g_extract():
    print('[G] 响应解析（双格式分流）')
    rot = make_rot(['Qwen/Qwen3.8-27B'])
    check('G1 魔搭格式取 content', rot._extract_text('Qwen/Qwen3.8-27B', openai_resp('正文')) == '正文')
    check('G2 忽略思维链', 'SHOULD-IGNORE' not in rot._extract_text('Qwen/Qwen3.8-27B', openai_resp('正文')))
    check('G3 空 content 返回空', rot._extract_text('Qwen/Qwen3.8-27B', openai_resp('')) == '')
    check('G4 坏 JSON 返回空', rot._extract_text('Qwen/Qwen3.8-27B', FakeResp(200, None)) == '')
    check('G5 error 响应返回空',
          rot._extract_text('Qwen/Qwen3.8-27B', FakeResp(200, {'error': {'message': 'x'}})) == '')
    check('G6 ★Gemini 模型走 Gemini 格式解析（回归保护）',
          rot._extract_text('gemini-3.8-flash', gemini_resp('正文')) == '正文')
    check('G7 Gemini 模型不吃 OpenAI 格式',
          rot._extract_text('gemini-3.8-flash', openai_resp('正文')) == '')
    g = GeminiRotator('k', None, ['gemini-3.8-flash'])
    check('G8 父类 Gemini 格式解析保留', g._extract_text('m', gemini_resp('正文')) == '正文')


def t_h_build_rotator():
    print('[H] build_rotator 工厂')
    old = os.environ.pop('MODELSCOPE_API_KEY', None)
    oldg = os.environ.pop('GEMINI_API_KEY', None)
    try:
        cfg = {'summarize': {'api_key': 'gkey', 'proxy': ''}, '_workdir': '.'}
        rot = build_rotator(cfg, log=lambda *_: None)
        check('H1 无 MS key → 纯 GeminiRotator', type(rot) is GeminiRotator)
        cfg['summarize']['modelscope_api_key'] = 'mskey'
        rot2 = build_rotator(cfg, log=lambda *_: None)
        check('H2 有 MS key → MultiProviderRotator', isinstance(rot2, MultiProviderRotator))
        check('H3 混合链首模型=主用 gemini-3.8-flash', rot2.models[0] == 'gemini-3.8-flash')
        check('H4 第 2 位为魔搭备份', is_modelscope(rot2.models[1]))
        check('H5 魔搭 8 模型在链',
              sum(1 for m in rot2.models if is_modelscope(m)) == 8)
        check('H6 链长 18', len(rot2.models) == 18, len(rot2.models))
        cfg2 = {'summarize': {'modelscope_api_key': 'mskey'}, '_workdir': '.'}
        rot3 = build_rotator(cfg2, log=lambda *_: None)
        check('H7 无 Gemini key → 纯魔搭链', isinstance(rot3, MultiProviderRotator)
              and all(is_modelscope(m) for m in rot3.models))
    finally:
        if old is not None:
            os.environ['MODELSCOPE_API_KEY'] = old
        if oldg is not None:
            os.environ['GEMINI_API_KEY'] = oldg


def t_i_all_fail():
    print('[I] 全链失败')
    rot = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.8-27B'], backoff_transient=0.5)
    rot._post = lambda m, s, u: FakeResp(500, {})
    t0 = time.time()
    out = rot.call('sys', 'user', tag='[I]', max_attempts=3)
    check('I1 返回空串不抛异常', out == '')
    check('I2 快速收敛（<60s）', time.time() - t0 < 60, time.time() - t0)


def t_j_usage():
    """[J] 用量账本：成功计数 + token 累计（双 provider 格式）+ 收尾汇报输出。"""
    print('[J] 用量账本与运行汇报')
    RG.reset_usage()
    rot = make_rot(['gemini-3.8-flash', 'Qwen/Qwen3.5-397B-A17B'], backoff_transient=0.5)

    # J1 Gemini：usageMetadata 计入
    rot._post = lambda m, s, u: FakeResp(200, {
        'candidates': [{'content': {'parts': [{'text': 'OK-G'}]}, 'finishReason': 'STOP'}],
        'usageMetadata': {'promptTokenCount': 1000, 'candidatesTokenCount': 200,
                          'totalTokenCount': 1200}})
    check('J1 Gemini 调用返回正文', rot.call('sys', 'u', tag='[J1]') == 'OK-G')
    rec = rot.usage.get('gemini-3.8-flash') or {}
    check('J1b Gemini 成功计数 1', rec.get('calls') == 1, str(rec))
    check('J1c Gemini token 累计 in/out/total',
          rec.get('in') == 1000 and rec.get('out') == 200 and rec.get('total') == 1200, str(rec))

    # J2 魔搭：usage 计入，且与 Gemini 分开累计
    rot._post = lambda m, s, u: FakeResp(200, {
        'choices': [{'message': {'content': 'OK-M'}, 'finish_reason': 'stop'}],
        'usage': {'prompt_tokens': 4000, 'completion_tokens': 500, 'total_tokens': 4500}})
    rot.models = ['Qwen/Qwen3.5-397B-A17B']
    rot.down_until.clear()
    check('J2 魔搭调用返回正文', rot.call('sys', 'u', tag='[J2]') == 'OK-M')
    rec2 = rot.usage.get('Qwen/Qwen3.5-397B-A17B') or {}
    check('J2b 魔搭 token 累计',
          rec2.get('in') == 4000 and rec2.get('out') == 500 and rec2.get('total') == 4500, str(rec2))

    # J3 失败请求不计数、不计 token
    before = {k: dict(v) for k, v in rot.usage.items()}
    rot._post = lambda m, s, u: FakeResp(503, {})
    rot.call('sys', 'u', tag='[J3]', max_attempts=2)
    check('J3 失败请求不增加 calls',
          all(rot.usage.get(k, {}).get('calls') == v.get('calls') for k, v in before.items()),
          str(rot.usage))
    check('J3b 失败请求不增加 token',
          all(rot.usage.get(k, {}).get('total') == v.get('total') for k, v in before.items()),
          str(rot.usage))

    # J4 汇报：表头 + 合计
    rows = RG.format_usage_report()
    text = '\n'.join(rows)
    check('J4 汇报含表头与合计行', '本次运行模型用量' in text and '合计' in text, text[:200])
    tl = [r for r in rows if r.strip().startswith('合计')]
    check('J4b 合计 = 2 模型 / 2 次 / 5,700 token',
          bool(tl) and ('2 个模型' in tl[0]) and ('5,700' in tl[0]), tl[0] if tl else '')

    # J5 provider 未返回 usage → 次数照记、token 标注无法统计
    RG.reset_usage()
    rot2 = make_rot(['gemini-3.8-flash'], backoff_transient=0.5)
    rot2._post = lambda m, s, u: gemini_resp('OK')
    rot2.call('sys', 'u', tag='[J5]')
    rows2 = RG.format_usage_report()
    check('J5 token 缺失时标注无法统计', any('无法统计' in r for r in rows2), str(rows2))
    check('J5b 次数仍统计', any('1 次' in r for r in rows2), str(rows2))

    # J6 空账本
    RG.reset_usage()
    check('J6 空账本提示未调用',
          any('未发生模型调用' in r for r in RG.format_usage_report()))


def main():
    t_a_fallback_order()
    t_b_backoff_retry_higher()
    t_c_empty_converge()
    t_d_caps()
    t_e_prompt()
    t_f_ms_payload()
    t_g_extract()
    t_h_build_rotator()
    t_i_all_fail()
    t_j_usage()
    print(f'\n结果：{PASS} 项通过，{len(FAIL)} 项失败 {FAIL if FAIL else ""}')
    sys.exit(1 if FAIL else 0)


if __name__ == '__main__':
    main()
