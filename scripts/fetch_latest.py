#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 1：拉取 B站合集，判定「最新录播」，产出待处理清单。

「最新录播」的定义完全由 config.json > latest_mode.mode 决定：
  newer_than_last     比已处理过的最新一期更新的（首次运行只取最新 first_run_n 期）
  latest_n            只看合集最新 latest_n 期里未处理的
  since_date          发布日期 >= since_date 且未处理的
  latest_by_live_date 按「直播实际日期」滚动：只处理 live_date 大于「已处理最晚直播日期」的未处理录播【默认】
  backfill_all        全合集从旧到新补齐，受 max_per_run 限制

用法:
  python fetch_latest.py                 # 按配置判定，打印清单并写 tmp/pending.json
  python fetch_latest.py --dry-run       # 只看不写
  python fetch_latest.py --bvid BVxxx    # 手动指定某一期（忽略判定逻辑）
"""

import argparse
import datetime
import json
import os
import sys

from common import (load_config, load_state, load_cookies, fetch_series,
                    Logger, stem_for, live_date)


def scan_horizon(cfg, st):
    """按判定模式算出「最早需要翻到哪一期」，让合集翻页能提前收工。

    返回一个 pubdate 阈值：翻页时一旦出现比它更早的视频就停止。
    返回 None 表示必须全量翻（上千期约需 2 分钟）。
    """
    import time as _t
    lm = cfg['latest_mode']
    mode = lm.get('mode', 'newer_than_last')
    if mode == 'newer_than_last':
        last = st.get('last_pubdate', 0)
        # 首次运行只需要最新一页；有基线时翻到基线为止
        return _t.time() if not st.get('processed') or not last else last
    if mode == 'latest_by_live_date':
        last = st.get('last_pubdate', 0)
        since = lm.get('since_live_date')
        if since and not st.get('processed'):
            # 首次且指定起点：只翻最新一页即可判定。
            # since 通常是「未来/近期的起点」，目标一期上传后必然出现在最新页；
            # 若要补录很久以前的录播，请改用 backfill_all 或先全量跑一次。
            return _t.time()
        # 否则按基线 pubdate 翻页，命中约最近 30 期即可覆盖候选
        return _t.time() if not last else last
    if mode == 'latest_n':
        return _t.time()  # 第一页就够
    if mode == 'since_date':
        return datetime.datetime.strptime(lm['since_date'], '%Y-%m-%d').timestamp()
    return None  # backfill_all 需要全量


def pick_videos(cfg, st, archives, log):
    lm = cfg['latest_mode']
    mode = lm.get('mode', 'newer_than_last')
    processed = st.get('processed', {})
    last_pubdate = st.get('last_pubdate', 0)

    # 基础过滤：时长下限 + 标题关键词
    min_sec = int(lm.get('skip_shorter_than_sec') or 0)
    kw = (lm.get('title_must_contain') or '').strip()
    pool = []
    for a in archives:
        if min_sec and int(a.get('duration') or 0) < min_sec:
            continue
        if kw and kw not in (a.get('title') or ''):
            continue
        pool.append(a)

    # archives 已按 pubdate 倒序
    pool.sort(key=lambda x: x.get('pubdate', 0), reverse=True)
    done = lambda a: a['bvid'] in processed and 'summarize' in processed[a['bvid']].get('stages', [])

    # 预解析每期的直播实际日期（取自标题），用于 latest_by_live_date 模式
    for a in pool:
        a['_live_date'] = live_date(a.get('title', ''), a.get('pubdate', 0))

    if mode == 'newer_than_last':
        if not processed:
            n = int(lm.get('first_run_n', 1))
            picked = pool[:n]
            log(f'首次运行：以最新 {n} 期作为基线 → {len(picked)} 期')
        else:
            picked = [a for a in pool if a.get('pubdate', 0) > last_pubdate and not done(a)]
            log(f'已处理最新时间点 {datetime.datetime.fromtimestamp(last_pubdate):%Y-%m-%d %H:%M}'
                f' 之后的新录播 → {len(picked)} 期')
    elif mode == 'latest_n':
        n = int(lm.get('latest_n', 1))
        picked = [a for a in pool[:n] if not done(a)]
        log(f'最新 {n} 期中未处理 → {len(picked)} 期')
    elif mode == 'since_date':
        ts = datetime.datetime.strptime(lm['since_date'], '%Y-%m-%d').timestamp()
        picked = [a for a in pool if a.get('pubdate', 0) >= ts and not done(a)]
        log(f'{lm["since_date"]} 之后未处理 → {len(picked)} 期')
    elif mode == 'latest_by_live_date':
        # 基线 = max(配置里的 since_live_date 起点, 已处理过的最晚直播日期)
        # 这样既能硬性跳过「起点之前」的历史（如用户明确只要 8/9 起的录播），
        # 又能在处理完一期后自动滚动到下一期。
        baseline_ld = None
        s = lm.get('since_live_date')
        if s:
            try:
                baseline_ld = datetime.date.fromisoformat(s)
            except Exception:
                baseline_ld = None
        for rec in processed.values():
            ld = rec.get('date')
            if ld:
                try:
                    d = datetime.date.fromisoformat(ld)
                    if baseline_ld is None or d > baseline_ld:
                        baseline_ld = d
                except Exception:
                    pass
        if baseline_ld is None:
            n = int(lm.get('first_run_n', 1))
            cand = [a for a in pool if a.get('_live_date')]
            cand.sort(key=lambda x: x['_live_date'], reverse=True)
            picked = cand[:n]
            log(f'首次运行（按直播日期）：取直播日期最新 {n} 期作为基线 → {len(picked)} 期')
        else:
            # 用 >= 让 since_live_date 当天本身（如 8/9）也能被选中
            picked = [a for a in pool
                      if a.get('_live_date') and a['_live_date'] >= baseline_ld and not done(a)]
            log(f'直播日期 >= {baseline_ld} 的未处理录播 → {len(picked)} 期')
    elif mode == 'backfill_all':
        picked = [a for a in reversed(pool) if not done(a)]
        log(f'全合集补齐，剩余未处理 → {len(picked)} 期')
    else:
        raise ValueError(f'未知 latest_mode.mode: {mode}')

    # 从旧到新处理，保证 Word 文档内顺序自然（优先按直播日期）
    picked.sort(key=lambda x: (x.get('_live_date') or datetime.date.fromtimestamp(x.get('pubdate', 0)),
                               x.get('pubdate', 0)))

    cap = int(lm.get('max_per_run') or 0)
    if cap and len(picked) > cap:
        log(f'本次上限 max_per_run={cap}，截取最旧的 {cap} 期，其余下次继续')
        picked = picked[:cap]
    return picked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--bvid', default=None, help='手动指定 BV 号，忽略判定逻辑')
    ap.add_argument('--out', default=None, help='待处理清单输出路径')
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = Logger(cfg, 'fetch')
    st = load_state(cfg)
    cookies = load_cookies(cfg['paths'].get('cookies'))

    s = cfg['series']
    log(f'拉取合集 mid={s["mid"]} series_id={s["series_id"]}')
    horizon = None if args.bvid else scan_horizon(cfg, st)
    archives = fetch_series(s['mid'], s['series_id'], cookies, stop_before_pubdate=horizon)
    log(f'本次扫描 {len(archives)} 期，最新：{archives[0]["title"][:50] if archives else "无"}')

    if args.bvid:
        picked = [a for a in archives if a['bvid'] == args.bvid]
        if not picked:
            log.error(f'合集中找不到 {args.bvid}')
            sys.exit(2)
        log(f'手动指定 → {picked[0]["title"][:50]}')
    else:
        picked = pick_videos(cfg, st, archives, log)

    pending = [{
        'bvid': a['bvid'],
        'title': a['title'],
        'pubdate': a.get('pubdate', 0),
        # date = 直播实际日期（取自标题），决定归档到哪个月的 Word
        'date': live_date(a['title'], a.get('pubdate', 0)).isoformat(),
        'upload_date': datetime.datetime.fromtimestamp(a.get('pubdate', 0)).strftime('%Y-%m-%d'),
        'duration': a.get('duration', 0),
        'stem': stem_for(a.get('pubdate', 0), a['title']),
    } for a in picked]

    print('-' * 70)
    if not pending:
        print('本次没有需要处理的新录播。')
    for i, v in enumerate(pending, 1):
        d = v['duration']
        print(f'{i}. {v["bvid"]}  {v["date"]}  {d // 3600}h{d % 3600 // 60:02d}m  {v["title"][:46]}')
    print('-' * 70)

    if not args.dry_run:
        out = args.out or os.path.join(cfg['_dirs']['tmp'], 'pending.json')
        with open(out, 'w', encoding='utf-8') as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
        log(f'待处理清单已写入 {out}（{len(pending)} 期）')


if __name__ == '__main__':
    main()
