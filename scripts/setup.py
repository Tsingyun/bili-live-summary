#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bili-live-summary 首次配置向导：检测环境 → 生成工作目录 config.json → 写 _workdir.json 指针。

即开即用：本向导运行一次后，直接 `python daily_pipeline.py --workdir <工作目录>` 即可全自动处理，
无需每次指定路径。所有参数既可用命令行传入（便于智能体一键调用），也可交互式填写。
"""

import argparse
import json
import os
import shutil

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 用户机器上已知的默认路径：检测到即用，检测不到则提示手动填。
DEFAULTS = {
    'workdir': r'D:\直播自动总结',
    'python': r'C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe',
    'ffmpeg': r'D:\ffmpeg\bin\ffmpeg.exe',
    'ffprobe': r'D:\ffmpeg\bin\ffprobe.exe',
    'aria2c': r'C:\Users\Tsing\AppData\Local\Microsoft\WinGet\Packages\aria2.aria2_Microsoft.Winget.Source_8wekyb3d8bbwe\aria2-1.37.0-win-64bit-build1\aria2c.exe',
    'cookies': r'C:\Users\Tsing\.bilibili_cookies.txt',
    'doc_dir': r'C:\Users\Tsing\DeskBox\文档',
}


def ask(prompt, default=''):
    try:
        val = input(f'{prompt} [{default}]: ' if default else f'{prompt}: ').strip()
        return val or default
    except EOFError:
        return default


def find_exe(candidates):
    for c in candidates:
        if not c:
            continue
        if os.path.isabs(c) or '/' in c or '\\' in c:
            if os.path.exists(c):
                return c
        else:
            p = shutil.which(c)
            if p:
                return p
    return None


def parse_series_url(url):
    import re
    m = re.search(r'space\.bilibili\.com/(\d+)/lists/(\d+)', url or '')
    if m:
        return int(m.group(1)), int(m.group(2))
    m2 = re.search(r'space\.bilibili\.com/(\d+)', url or '')
    if m2:
        return int(m2.group(1)), None
    return None, None


def main():
    ap = argparse.ArgumentParser(description='bili-live-summary 配置向导')
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--series-url', default=None)
    ap.add_argument('--api-key', default=None)
    ap.add_argument('--api-provider', default='google')
    ap.add_argument('--model', default='gemini-3.7-flash')
    ap.add_argument('--streamer', default=None)
    ap.add_argument('--audience', default=None)
    ap.add_argument('--doc-dir', default=None)
    ap.add_argument('--python', default=None)
    ap.add_argument('--ffmpeg', default=None)
    ap.add_argument('--ffprobe', default=None)
    ap.add_argument('--aria2c', default=None)
    ap.add_argument('--cookies', default=None)
    ap.add_argument('--schedule-hours', default='2,10,18', help='每日调度小时，逗号分隔')
    ap.add_argument('--since-live-date', default=None, help='起点直播日期 YYYY-MM-DD，只处理此日期及之后的录播')
    ap.add_argument('--force', action='store_true', help='覆盖已有 config.json（以模板重新生成）')
    ap.add_argument('--non-interactive', action='store_true', help='不交互，缺失项用默认/空')
    args = ap.parse_args()

    ni = args.non_interactive

    def get(attr, default, prompt, secret=False):
        v = getattr(args, attr, None)
        if v:
            return v
        if ni:
            return default
        return ask(prompt, default)

    print('=== bili-live-summary 配置向导 ===')

    workdir = get('workdir', DEFAULTS['workdir'], '工作目录（下载/转写/文档/state 存放处）')
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    for sub in ('MP3', 'TXT', 'SRT', 'logs', 'tmp'):
        os.makedirs(os.path.join(workdir, sub), exist_ok=True)

    url = get('series_url', '', 'B站合集页 URL（space.bilibili.com/.../lists/...）')
    mid, sid = parse_series_url(url)

    streamer = get('streamer', '主播', '主播名（总结主语）')
    audience = get('audience', '观众', '观众名（文中"观众"统一替换为此）')
    api_key = get('api_key', '', 'AI API Key（必填）')
    while not ni and not api_key:
        api_key = ask('AI API Key（必填）', '')
    model = get('model', 'gemini-3.7-flash', '模型名')
    doc_dir = get('doc_dir', DEFAULTS['doc_dir'], 'Word 文档输出目录')

    # 依赖探测
    py = args.python or find_exe([DEFAULTS['python'], 'python', 'python3'])
    if not py and not ni:
        py = ask('Python 解释器路径（需含 torch/funasr/docx/requests）', DEFAULTS['python'])
    ffmpeg = args.ffmpeg or find_exe([DEFAULTS['ffmpeg'], 'ffmpeg'])
    ffprobe = args.ffprobe or find_exe([DEFAULTS['ffprobe'], 'ffprobe'])
    aria2c = args.aria2c or find_exe([DEFAULTS['aria2c'], 'aria2c'])
    cookies = args.cookies or DEFAULTS['cookies']

    cfg_path = os.path.join(workdir, 'config.json')
    migrate_from = os.path.join(workdir, 'scripts', 'config.json')

    if os.path.exists(cfg_path) and not args.force:
        print(f'* 已存在 {cfg_path}，保留现有配置，仅更新 _workdir.json 指针。')
        print('  （加 --force 可基于模板重新生成）')
    else:
        tmpl = None
        ex_path = os.path.join(SCRIPT_DIR, 'config.example.json')
        if os.path.exists(ex_path):
            with open(ex_path, 'r', encoding='utf-8') as f:
                tmpl = json.load(f)
        if not tmpl:
            tmpl = {}
        # 若旧版 scripts/config.json 存在，以其为基准（保留调试好的 paths）
        if os.path.exists(migrate_from):
            with open(migrate_from, 'r', encoding='utf-8') as f:
                old = json.load(f)
            base = tmpl.copy()
            for k, v in old.items():
                if k in ('_comment',) or k.startswith('_mode'):
                    continue
                base[k] = v
            tmpl = base
        # 填入用户输入
        tmpl.setdefault('series', {})
        if mid:
            tmpl['series']['mid'] = mid
        if sid:
            tmpl['series']['series_id'] = sid
        if url:
            tmpl['series']['url'] = url
        tmpl.setdefault('latest_mode', {})
        if args.since_live_date:
            tmpl['latest_mode']['since_live_date'] = args.since_live_date
            print(f'* 已设定起点直播日期：{args.since_live_date}')
        tmpl.setdefault('summarize', {})
        tmpl['summarize']['provider'] = args.api_provider
        tmpl['summarize']['model'] = model
        # 仅在用户提供了值时覆盖，避免非交互/默认空值清空已有配置
        if api_key:
            tmpl['summarize']['api_key'] = api_key
        if streamer:
            tmpl['summarize']['streamer'] = streamer
        if audience:
            tmpl['summarize']['audience'] = audience
        tmpl.setdefault('output', {})
        if doc_dir:
            tmpl['output']['doc_dir'] = doc_dir
        tmpl.setdefault('paths', {})
        tmpl['paths']['project_root'] = workdir
        if py:
            tmpl['paths']['python'] = py
        if ffmpeg:
            tmpl['paths']['ffmpeg'] = ffmpeg
        if ffprobe:
            tmpl['paths']['ffprobe'] = ffprobe
        if aria2c:
            tmpl['paths']['aria2c'] = aria2c
        if cookies:
            tmpl['paths']['cookies'] = cookies
        with open(cfg_path, 'w', encoding='utf-8') as f:
            json.dump(tmpl, f, ensure_ascii=False, indent=2)
        print(f'* 已生成配置：{cfg_path}')

    # 写指针文件（让 run_pipeline.py 自动定位工作目录）
    ptr = os.path.join(SCRIPT_DIR, '_workdir.json')
    with open(ptr, 'w', encoding='utf-8') as f:
        json.dump({'workdir': workdir}, f, ensure_ascii=False, indent=2)
    print(f'* 已写入指针：{ptr} → {workdir}')

    print('\n=== 配置完成 ===')
    print(f'工作目录：{workdir}')
    if not ffmpeg:
        print('[!] 未检测到 ffmpeg，请在 config.json 的 paths.ffmpeg 填写路径')
    if not aria2c:
        print('[!] 未检测到 aria2c，请在 config.json 的 paths.aria2c 填写路径')
    # 检查最终写盘的 config 是否真的缺少 api_key（迁移场景可能已保留旧值，不应误报）
    _missing_key = True
    try:
        with open(cfg_path, 'r', encoding='utf-8') as f:
            _c = json.load(f)
        _missing_key = not _c.get('summarize', {}).get('api_key')
    except Exception:
        _missing_key = not api_key
    if _missing_key:
        print('[!] 未填写 API Key，请在 config.json 的 summarize.api_key 填写')
    print('\n下一步：')
    print(f'  试运行：  "{py or "python"}" "{os.path.join(SCRIPT_DIR, "daily_pipeline.py")}" --dry-run')
    print(f'  正式运行："{py or "python"}" "{os.path.join(SCRIPT_DIR, "daily_pipeline.py")}"')
    print(f'  每日自动化：在 WorkBuddy 自动化面板建任务，命令为上面「正式运行」那行（加 --workdir 指向本工作目录）；')
    print(f'            调度 RRULE 建议：FREQ=DAILY;BYHOUR={args.schedule_hours};BYMINUTE=0;BYSECOND=0')


if __name__ == '__main__':
    main()
