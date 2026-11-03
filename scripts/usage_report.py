#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""usage_report.py —— 每次运行结束的「模型用量汇报」落盘。

背景（2026-11-04）
------------------
计划任务已改为静默启动（窗口样式 0，控制台不可见），而用量汇报原本只 `log()` 到
`logs/YYYY-MM-DD.log`。人不去翻日志，就等于「没有汇报」——尤其凌晨 4 点无人值守时。

本模块把汇报写成独立的 Markdown 文件，一眼能看：

    logs/usage/usage_YYYY-MM-DD_HHMMSS.md   每次运行一份（留档，不覆盖）
    logs/usage/latest.md                    最近一次（覆盖写，方便直接打开）

汇报是**纯附加输出**：内部所有异常都被吞掉，绝不影响交付物与退出码。
"""
import datetime
import io
import os

import regen_gemini_v2 as R

# ASR 走本地模型，不消耗任何 API 额度——固定写进汇报，免得每次都要解释
ASR_NOTE = 'ASR：本地 Fun-ASR-Nano-2512（0 API 消耗）'


def _fmt_int(n):
    try:
        return format(int(n or 0), ',')
    except Exception:
        return '0'


def _fmt_dur(sec):
    try:
        sec = int(sec)
    except Exception:
        return '—'
    if sec < 60:
        return '%d 秒' % sec
    m, s = divmod(sec, 60)
    if m < 60:
        return '%d 分 %d 秒' % (m, s)
    h, m = divmod(m, 60)
    return '%d 小时 %d 分' % (h, m)


def _provider_of(model):
    """与 model_router.is_modelscope 同口径：魔搭 Model-Id 一律带组织前缀。"""
    return 'ModelScope（魔搭）' if '/' in (model or '') else 'Gemini'


def _ready_rows(usage=None):
    u = R.USAGE if usage is None else usage
    rows = [(m, v) for m, v in (u or {}).items()
            if (v.get('calls') or v.get('total'))]
    rows.sort(key=lambda kv: (-kv[1].get('calls', 0), -kv[1].get('total', 0)))
    return rows


def render(meta=None, usage=None):
    """生成 Markdown 汇报文本。meta 为 dict，缺字段一律按「—」渲染，不抛异常。"""
    meta = dict(meta or {})
    now = datetime.datetime.now()
    rows = _ready_rows(usage)

    L = []
    L.append('# 模型用量汇报 · %s' % now.strftime('%Y-%m-%d %H:%M'))
    L.append('')

    # ---- 运行概况 ----
    trig = meta.get('trigger') or '手动'
    status = meta.get('status') or '正常完成'
    L.append('| 项 | 内容 |')
    L.append('|---|---|')
    L.append('| 触发方式 | %s |' % trig)
    if meta.get('started'):
        L.append('| 开始时间 | %s |' % meta['started'])
    if meta.get('ended'):
        L.append('| 结束时间 | %s |' % meta['ended'])
    if meta.get('elapsed') is not None:
        L.append('| 本轮耗时 | %s |' % _fmt_dur(meta['elapsed']))
    L.append('| 结束状态 | %s |' % status)

    proc = meta.get('processed') or []
    L.append('| 本轮处理场次 | %s |' % ('、'.join(proc) if proc else '无'))
    skip = meta.get('skipped') or []
    if skip:
        L.append('| 跳过场次 | %s |' % '；'.join(
            ('%s（%s）' % (it[0], it[1])) if len(it) > 1 and it[1] else str(it[0])
            for it in skip[:6]))
    L.append('')

    # ---- 模型用量 ----
    L.append('## 模型调用')
    L.append('')
    if not rows:
        L.append('本次未调用任何模型（无待处理场次）。')
    else:
        L.append('| 模型 | 提供方 | 调用次数 | 输入 token | 输出 token | 合计 token |')
        L.append('|---|---|---:|---:|---:|---:|')
        for m, v in rows:
            L.append('| %s | %s | %d | %s | %s | %s |'
                     % (m, _provider_of(m), v.get('calls', 0),
                        _fmt_int(v.get('in')), _fmt_int(v.get('out')),
                        _fmt_int(v.get('total'))))
        tc = sum(v.get('calls', 0) for _, v in rows)
        ti = sum(v.get('in', 0) for _, v in rows)
        to = sum(v.get('out', 0) for _, v in rows)
        tt = sum(v.get('total', 0) for _, v in rows)
        L.append('| **合计** | — | **%d** | **%s** | **%s** | **%s** |'
                 % (tc, _fmt_int(ti), _fmt_int(to), _fmt_int(tt)))
        L.append('')
        if tt == 0:
            L.append('> token 无法统计：provider 响应未返回 usage / usageMetadata 字段。')
        # 按 provider 汇总（判断「今天走的是哪条链」）
        agg = {}
        for m, v in rows:
            p = _provider_of(m)
            a = agg.setdefault(p, [0, 0])
            a[0] += v.get('calls', 0)
            a[1] += v.get('total', 0)
        L.append('')
        L.append('**分 provider**：' + '；'.join(
            '%s %d 次 / %s token' % (p, a[0], _fmt_int(a[1]))
            for p, a in sorted(agg.items())))
    L.append('')
    L.append('> %s' % ASR_NOTE)

    # ---- 降级提示：主用模型没被用上而别的模型有调用，说明发生了降级 ----
    if rows:
        primary = R_MODELS_PRIMARY
        names = [m for m, _ in rows]
        if primary not in names:
            L.append('>')
            L.append('> ⚠️ 主用 `%s` 本次 0 次成功 → 全程走了降级链（首位可用者：`%s`）。'
                     % (primary, names[0]))
    L.append('')
    return '\n'.join(L) + '\n'


# 主用模型（与 model_router.MODELS_MIXED[0] 一致；延迟读取避免循环导入）
try:
    import model_router as _MR
    R_MODELS_PRIMARY = _MR.MODELS_MIXED[0]
except Exception:                                    # pragma: no cover
    R_MODELS_PRIMARY = 'gemini-3.8-flash'


def save(cfg, log=None, meta=None, usage=None):
    """写汇报文件，返回主文件路径（失败返回 None，绝不抛）。

    - `logs/usage/usage_<时间戳>.md`：每次运行一份，留档
    - `logs/usage/latest.md`：覆盖写，随时能看最近一次
    """
    try:
        logs = (cfg.get('_dirs') or {}).get('logs')
        if not logs:
            return None
        out_dir = os.path.join(logs, 'usage')
        os.makedirs(out_dir, exist_ok=True)
        text = render(meta=meta, usage=usage)
        stamp = datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')
        path = os.path.join(out_dir, 'usage_%s.md' % stamp)
        for p in (path, os.path.join(out_dir, 'latest.md')):
            with io.open(p, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
        if log:
            try:
                log('模型用量汇报已写入：%s' % path)
            except Exception:
                pass
        return path
    except Exception:
        if log:
            try:
                log('模型用量汇报落盘失败（不影响交付物）', 'WARN')
            except Exception:
                pass
        return None


if __name__ == '__main__':
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import common
    _cfg = common.load_config()
    print(render(meta={'trigger': '命令行直跑', 'status': '仅渲染预览'}))
    print('落盘：', save(_cfg, log=print, meta={'trigger': '命令行直跑'}))
