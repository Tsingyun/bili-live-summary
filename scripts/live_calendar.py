#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""实际直播日期日历：回答「岁己SUI 哪几天真的开过播」。

设计要点（2026-09-05）
--------------------
用户指定以 https://danmakus.com/channel/1954091502 为准。但该站是纯前端 SPA
（HTML 只有 3.4KB 的骨架，数据由 /assets/*.js 里的私有接口异步拉取），
静态分析未能稳定拿到可直接调用的公开直播列表接口。

因此做成**可插拔 + 不阻塞**的形式：
  1. danmakus：若 config.live_calendar.danmakus_api 配了可用接口（返回 JSON），
     优先用它；拿不到就跳过（只告警，不中断）。
  2. rec.darkpy.cn：该站有录播 ⇒ 那天必然开过播。已实测可用，是最强佐证。
  3. 本地录播文件：同样的强佐证。
  4. B站合集：从标题解析直播日期。

多源取**并集**——只要任一来源证明某天开过播，就认为那天有直播。
这样即使 danmakus 接口变动，缺失检查功能依然可用（只是报告里会写明实际来源）。
"""
import datetime
import os
import re

DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
NAME_DATE_RE = re.compile(r'(?:录制-)?(?P<uid>\d+)-(?P<date>\d{8})-(?P<time>\d{6})')
MEDIA_EXT = ('.mp4', '.flv', '.mkv', '.mov')


def cfg_calendar(cfg):
    c = cfg.get('live_calendar') or {}
    return {
        'sources': c.get('sources') or ['danmakus', 'darkpy', 'local', 'bilibili'],
        'danmakus_api': c.get('danmakus_api') or '',
        'uid': c.get('uid') or (cfg.get('series') or {}).get('mid') or '',
        'lookback_days': int(c.get('lookback_days') or 45),
        'timeout': int(c.get('timeout') or 20),
    }


def _norm(d):
    """统一成 YYYY-MM-DD 字符串。"""
    if isinstance(d, datetime.date):
        return d.isoformat()
    s = str(d).strip().replace('/', '-')
    m = re.match(r'^(\d{4})-?(\d{2})-?(\d{2})$', s)
    if m:
        return '%s-%s-%s' % m.groups()
    return None


# ---------------------------------------------------------------------------
# 各来源
# ---------------------------------------------------------------------------
def from_danmakus(cfg, log):
    """从 danmakus 取直播日期。需要 config.live_calendar.danmakus_api 配置。"""
    c = cfg_calendar(cfg)
    api = c['danmakus_api']
    if not api:
        log('[calendar] 未配置 live_calendar.danmakus_api，跳过 danmakus', 'INFO')
        return set()
    try:
        import requests
        url = api.replace('{uid}', str(c['uid']))
        r = requests.get(url, timeout=c['timeout'],
                         headers={'User-Agent': 'Mozilla/5.0'})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log('[calendar] danmakus 取直播日期失败：%s' % e, 'WARN')
        return set()

    dates = set()
    # 宽容解析：递归找所有看起来像日期的字段
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif isinstance(v, str) and re.search(r'20\d{2}[-/年]\d{1,2}', v):
                    n = _norm(_cn_date(v))
                    if n:
                        dates.add(n)
                elif isinstance(v, (int, float)) and 1_600_000_000 < v < 4_000_000_000:
                    dates.add(datetime.datetime.fromtimestamp(v).date().isoformat())
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(data)
    if dates:
        log('[calendar] danmakus 提供 %d 个直播日期' % len(dates))
    return dates


def _cn_date(s):
    m = re.search(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})', s or '')
    if m:
        return '%s-%02d-%02d' % (m.group(1), int(m.group(2)), int(m.group(3)))
    m = re.search(r'(20\d{2})(\d{2})(\d{2})', s or '')
    if m:
        return '%s-%s-%s' % m.groups()
    return None


def from_darkpy(cfg, log):
    """rec.darkpy.cn 上有录播 ⇒ 那天开过播。

    以「场次起始日」为准（全局归组后取每场第一段文件名日期）：跨午夜尾巴
    （如 8/27 00:01 的文件属于 8/26 的深夜场）不会误当成独立直播日。
    """
    try:
        import darkpy_rec as D
        if not D.cfg_darkpy(cfg).get('enabled'):
            return set()
        files = D.fetch_index(cfg, log)
        if not files:
            return set()
        dcfg = D.cfg_darkpy(cfg)
        sessions = D.group_all_sessions(files, dcfg['max_gap_sec'], log)
    except Exception as e:
        log('[calendar] rec.darkpy.cn 取日期失败：%s' % e, 'WARN')
        return set()
    out = set()
    for s in sessions:
        ymd = (s[0].get('ymd') or '') if s else ''
        if len(ymd) == 8:
            out.add('%s-%s-%s' % (ymd[:4], ymd[4:6], ymd[6:8]))
    if out:
        log('[calendar] rec.darkpy.cn 提供 %d 个直播日期（按场次起始日）' % len(out))
    return out


def from_local(cfg, log):
    """本地录播文件的日期（根目录 + 日期子目录）。"""
    root = (cfg.get('local') or {}).get('root') or ''
    if not root or not os.path.isdir(root):
        return set()
    out = set()
    scan = [root] + [os.path.join(root, d) for d in os.listdir(root)
                     if os.path.isdir(os.path.join(root, d)) and DATE_DIR_RE.match(d)]
    for sd in scan:
        try:
            names = os.listdir(sd)
        except Exception:
            continue
        for n in names:
            if not n.lower().endswith(MEDIA_EXT):
                continue
            m = NAME_DATE_RE.search(n)
            if m:
                out.add(_norm(m.group('date')))
    if out:
        log('[calendar] 本地录播提供 %d 个直播日期' % len(out))
    return out


_BILI_CACHE = {'t': 0.0, 'dates': None}


def from_bilibili(cfg, log):
    """B站合集的直播日期（从标题解析）。结果缓存 15 分钟（一次 stage 会查两遍）。"""
    import time
    if _BILI_CACHE['dates'] is not None and time.time() - _BILI_CACHE['t'] < 900:
        return _BILI_CACHE['dates']
    try:
        from common import fetch_series, live_date
        s = cfg['series']
        arcs = fetch_series(s['mid'], s['series_id'], None)
    except Exception as e:
        log('[calendar] B站合集取日期失败：%s' % e, 'WARN')
        return set()
    out = set()
    for a in arcs:
        try:
            out.add(live_date(a.get('title', ''), a.get('pubdate', 0)).isoformat())
        except Exception:
            pass
    if out:
        log('[calendar] B站合集提供 %d 个直播日期' % len(out))
        _BILI_CACHE['t'] = time.time()
        _BILI_CACHE['dates'] = out
    return out


_FUNCS = {
    'danmakus': from_danmakus,
    'darkpy': from_darkpy,
    'local': from_local,
    'bilibili': from_bilibili,
}


def actual_live_dates(cfg, log, start_date=None, end_date=None, sources=None):
    """返回 (sorted_list[YYYY-MM-DD], sources_used)。

    sources=None 用配置；也可显式传子集（如 ['darkpy','local'] 避免每次拉 B站全集）。
    """
    c = cfg_calendar(cfg)
    names = sources or c['sources']
    all_d, used = set(), []
    for name in names:
        fn = _FUNCS.get(name)
        if not fn:
            continue
        got = fn(cfg, log) or set()
        if got:
            used.append(name)
        all_d |= got

    if start_date:
        all_d = {d for d in all_d if d >= start_date}
    if end_date:
        all_d = {d for d in all_d if d <= end_date}
    return sorted(all_d), used
