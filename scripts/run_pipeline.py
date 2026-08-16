#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
B站直播录播 —— 全自动总结主编排器（即开即用版）

阶段：抓取合集最新录播 → 下载并合并分P为单个MP3 → FunASR带时间轴转写
      → 大模型按30分钟切块分块总结 → 追加写入 Word → 清理MP3

特性：
  * 断点续跑：每场每阶段进度写入 state.json，中断后重跑自动跳过已完成部分
  * 单场失败不影响整批，错误记入 logs/errors.log
  * 网络抖动友好：启动前等待网络就绪，接口层自带指数退避重试
  * 配置自动定位：优先 BILI_LIVE_CONFIG 环境变量 / --config / _workdir.json 指针

用法:
  python run_pipeline.py                     # 按 config.json 判定并全自动处理
  python run_pipeline.py --bvid BV1iL3m6oEyU # 手动指定某一期
  python run_pipeline.py --dry-run           # 只看这次会处理哪些，不动手
  python run_pipeline.py --skip-cleanup      # 保留 MP3
"""

import argparse
import datetime
import json
import os
import socket
import sys
import time
import traceback

from common import (load_config, load_state, save_state, load_cookies, Logger,
                    fetch_view, stem_for, live_date, hms)
import fetch_latest
import download_merge
# transcribe（含 FunASR）延迟导入：仅当需要转写时才 import，
# 避免 Windows 上 FunASR 导入即触发 multiprocessing spawn 重跑本脚本。
asr = None
import summarize_gemini as summ
import hotwords


def _host_of(api_host):
    """从 api_host 中解析出可用于 DNS 检测的主机名。

    api_host 可能是裸主机名（如 generativelanguage.googleapis.com），
    也可能是完整 URL（如 https://generativelanguage.googleapis.com/v1beta/models）。
    socket.gethostbyname 只接受裸主机名，所以这里用 urlparse 把 URL 还原成主机名。
    """
    try:
        from urllib.parse import urlparse
        parsed = urlparse(api_host)
        if parsed.scheme:  # 带 scheme 说明是完整 URL，取 hostname
            return parsed.hostname or api_host
    except Exception:
        pass
    return api_host


def wait_network(log, cfg=None, timeout=900):
    """启动前等待网络就绪（凌晨跑批时代理/网络可能还没起来）。"""
    hosts = ['api.bilibili.com']  # B站合集/视频接口
    if cfg:
        for key in ('api_host',):
            h = cfg.get('summarize', {}).get(key)
            if h:
                hosts.append(_host_of(h))
            h2 = cfg.get('series', {}).get(key)
            if h2:
                hosts.append(_host_of(h2))
    if not hosts:
        hosts = ['api.bilibili.com', 'generativelanguage.googleapis.com']
    deadline = time.time() + timeout
    delay = 10
    while time.time() < deadline:
        ok = True
        for h in hosts:
            try:
                socket.gethostbyname(h)
            except Exception:
                ok = False
                break
        if ok:
            return True
        log(f'网络尚未就绪，{delay}s 后重试 ...', 'WARN')
        time.sleep(delay)
        delay = min(delay * 2, 120)
    log.error('等待网络超时')
    return False


def process_one(cfg, st, video, model_holder, log, skip_cleanup=False):
    global asr
    if asr is None:
        import transcribe as asr
    bvid = video['bvid']
    stem = video['stem']
    rec = st['processed'].setdefault(bvid, {'stages': []})
    rec.update({'title': video['title'], 'date': video['date'],
                'pubdate': video['pubdate'], 'stem': stem})

    log('=' * 68)
    log(f'处理 {bvid}  {video["date"]}  {video["title"][:46]}')
    log('=' * 68)

    # --- 下载 + 合并 ---
    mp3 = os.path.join(cfg['_dirs']['mp3'], stem + '.mp3')
    txt = os.path.join(cfg['_dirs']['txt'], stem + '.txt')
    if not os.path.exists(txt):
        if not (os.path.exists(mp3) and os.path.getsize(mp3) > 10240):
            log('[1/4] 下载并合并音频')
            mp3 = download_merge.download_one(cfg, video, log)
            if not mp3:
                raise RuntimeError('下载/合并失败')
        else:
            log('[1/4] MP3 已存在，跳过下载')
        if 'download' not in rec['stages']:
            rec['stages'].append('download')
        save_state(cfg, st)

        # --- 转写 ---
        log('[2/4] FunASR 转写')
        if model_holder.get('model') is None:
            model_holder['model'] = asr.load_model(cfg, log)
        txt = asr.transcribe_one(cfg, mp3, model_holder['model'], log)
        if not txt:
            raise RuntimeError('转写失败')
    else:
        log('[1-2/4] 转写结果已存在，跳过下载与转写')
    if 'transcribe' not in rec['stages']:
        rec['stages'].append('transcribe')
    save_state(cfg, st)

    # --- 热词替换（转写后、总结前，保证送进 AI 的文本已纠正）---
    if cfg.get('hotwords', {}).get('enabled', True):
        srt = os.path.join(cfg['_dirs']['srt'], stem + '.srt')
        hotwords.apply_to_file(txt, cfg, log)
        hotwords.apply_to_file(srt, cfg, log)

    # --- 总结 ---
    log('[3/4] 大模型分块总结并写入 Word')
    docx = summ.summarize_one(cfg, txt, log,
                              date_str=video['date'], title=stem, bvid=bvid,
                              url=f'https://www.bilibili.com/video/{bvid}')
    if not docx:
        raise RuntimeError('总结失败')
    rec['doc'] = docx
    if 'summarize' not in rec['stages']:
        rec['stages'].append('summarize')
    st['last_pubdate'] = max(st.get('last_pubdate', 0), video['pubdate'])
    save_state(cfg, st)

    # --- 清理 ---
    if cfg['cleanup'].get('delete_mp3_after_success') and not skip_cleanup:
        if os.path.exists(mp3):
            sz = os.path.getsize(mp3) / 1024 / 1024
            try:
                os.remove(mp3)
                log(f'[4/4] 已清理 MP3（释放 {sz:.0f}MB），保留 TXT 与 SRT')
            except Exception as e:
                log.error(f'清理 MP3 失败：{e}')
    else:
        log('[4/4] 按配置保留 MP3')

    if 'cleanup' not in rec['stages']:
        rec['stages'].append('cleanup')
    save_state(cfg, st)
    return docx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--bvid', default=None, help='手动指定某一期，忽略最新判定')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--skip-cleanup', action='store_true')
    ap.add_argument('--no-wait-network', action='store_true')
    args = ap.parse_args()

    cfg = load_config(args.config)

    # --- 单实例锁：避免多个 run_pipeline 并发抢 Gemini 配额导致 429 ---
    import atexit, errno
    LOCK = os.path.join(cfg['_dirs']['root'], 'run_pipeline.lock')
    _lock_ok = False
    try:
        if os.path.exists(LOCK):
            try:
                with open(LOCK) as _lf:
                    _old = int(_lf.read().strip() or 0)
                if _old and _old != os.getpid():
                    try:
                        os.kill(_old, 0)        # 进程仍存活 -> 真有另一实例
                        _lock_ok = False
                    except Exception:
                        os.remove(LOCK)          # 死锁残留，清理后继续
            except Exception:
                try:
                    os.remove(LOCK)
                except Exception:
                    pass
        if not os.path.exists(LOCK):
            try:
                _fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(_fd, str(os.getpid()).encode())
                os.close(_fd)
                _lock_ok = True
            except OSError as _e:
                if getattr(_e, 'errno', None) == errno.EEXIST:
                    _lock_ok = False   # 锁已被另一实例占用 -> 退出
                else:
                    _lock_ok = True    # 其他错误退化，避免误杀
    except Exception:
        _lock_ok = True   # 锁机制异常时退化为不限制，避免误杀正常流程
    if not _lock_ok:
        print('[单实例锁] 已有 run_pipeline 实例在运行，本次直接退出以避免并发抢配额。', flush=True)
        sys.exit(0)

    def _release_lock():
        try:
            if os.path.exists(LOCK):
                try:
                    with open(LOCK) as _f:
                        _cur = int(_f.read().strip() or 0)
                except Exception:
                    _cur = 0
                if _cur == os.getpid():
                    os.remove(LOCK)
        except Exception:
            pass
    atexit.register(_release_lock)

    log = Logger(cfg, 'pipeline')
    started = time.time()
    log('#' * 68)
    log(f'B站直播录播自动总结 启动  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}')

    if not args.no_wait_network and not wait_network(log, cfg):
        sys.exit(3)

    st = load_state(cfg)
    cookies = load_cookies(cfg['paths'].get('cookies'))

    # --- 选片 ---
    try:
        if args.bvid:
            info = fetch_view(args.bvid, cookies)
            d = live_date(info['title'], info.get('pubdate', 0))
            pending = [{'bvid': args.bvid, 'title': info['title'],
                        'pubdate': info.get('pubdate', 0), 'date': d.isoformat(),
                        'duration': info.get('duration', 0),
                        'stem': stem_for(info.get('pubdate', 0), info['title'])}]
        else:
            horizon = fetch_latest.scan_horizon(cfg, st)
            from common import fetch_series
            s = cfg['series']
            archives = fetch_series(s['mid'], s['series_id'], cookies,
                                    stop_before_pubdate=horizon)
            log(f'扫描到 {len(archives)} 期，最新：{archives[0]["title"][:46] if archives else "无"}')
            picked = fetch_latest.pick_videos(cfg, st, archives, log)
            pending = [{'bvid': a['bvid'], 'title': a['title'],
                        'pubdate': a.get('pubdate', 0),
                        'date': live_date(a['title'], a.get('pubdate', 0)).isoformat(),
                        'duration': a.get('duration', 0),
                        'stem': stem_for(a.get('pubdate', 0), a['title'])}
                       for a in picked]
    except Exception as e:
        log.error(f'获取待处理列表失败：{e}')
        sys.exit(4)

    if not pending:
        log('本次没有新录播，收工。')
        return

    log(f'本次待处理 {len(pending)} 期：')
    for v in pending:
        log(f'  · {v["bvid"]}  {v["date"]}  {hms(v["duration"])}  {v["title"][:42]}')

    if args.dry_run:
        log('dry-run，未执行实际处理。')
        return

    model_holder = {'model': None}
    ok, failed = 0, []
    for idx_v, v in enumerate(pending):
        try:
            process_one(cfg, st, v, model_holder, log, args.skip_cleanup)
            ok += 1
        except Exception as e:
            failed.append(v['bvid'])
            log.error(f'{v["bvid"]} 处理失败：{e}')
            log.error(traceback.format_exc(limit=3))
        # 期之间冷却：避免连跑触发 Gemini 限流（429）
        if idx_v < len(pending) - 1:
            cd = int(cfg.get('summarize', {}).get('inter_video_delay_sec', 45))
            log(f'期之间冷却 {cd}s，避免触发限流 ...')
            time.sleep(cd)

    st['runs'].append({'at': datetime.datetime.now().isoformat(timespec='seconds'),
                       'ok': ok, 'failed': failed,
                       'seconds': round(time.time() - started)})
    st['runs'] = st['runs'][-60:]
    save_state(cfg, st)

    log('#' * 68)
    log(f'本次完成 {ok}/{len(pending)} 期，耗时 {hms(time.time() - started)}')
    if failed:
        log.error(f'失败：{", ".join(failed)}（下次运行会自动重试）')
    sys.exit(0 if not failed else 1)


if __name__ == '__main__':
    main()
