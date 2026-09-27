#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_upload_task.py —— 注册/查看/删除「每日录播自动上传」耐久计划任务。

背景：长任务挂在 agent 会话进程树下会被宿主回收拆掉（2026-09-17/18 事故；
2026-09-27 凌晨 925 上传再次被杀在 P3 60/268）。本脚本把 auto_upload_daily.py
注册成 **Windows 计划任务** BiliUpload_Daily——由 Task Scheduler 服务拉起，
不在 agent 进程树里，agent 回合结束与否都不影响它跑完。

默认策略（安全网，与 WorkBuddy 06:00 自动化互为备份）：
  每天 06:00 起，每 60 分钟触发一次、共 4 小时（06:00 / 07:00 / 08:00 / 09:00）：
    · 若此刻已有**存活**实例持有单实例锁（.auto_upload.lock）→ 直接退出；
    · 若上一轮被杀、锁是孤儿（心跳过期）→ 接管续跑（脚本全流程幂等防重传）。

用法
----
    python install_upload_task.py --install          # 注册/更新（默认 06:00 起 4 次）
    python install_upload_task.py --query            # 查看任务状态
    python install_upload_task.py --run              # 立即触发一次
    python install_upload_task.py --remove           # 删除任务
"""
import argparse
import os
import subprocess
import sys

TASK_NAME = 'BiliUpload_Daily'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(SCRIPT_DIR, 'auto_upload_daily.py')


def _schtasks(args, quiet=False):
    try:
        r = subprocess.run(['schtasks'] + args, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=120)
        out = (r.stdout or b'').decode('utf-8', 'ignore').strip()
        if not out:
            out = (r.stdout or b'').decode('mbcs', 'ignore').strip()
        if not quiet:
            print(out)
        return r.returncode, out
    except Exception as e:
        if not quiet:
            print('schtasks 执行失败：%s' % e)
        return -1, str(e)


def install(args):
    python = args.python or sys.executable
    if not os.path.exists(RUNNER):
        print('找不到 auto_upload_daily.py：%s' % RUNNER)
        return 1
    # /tr 全 ASCII：config 路径由脚本内 DEFAULT_CONFIG 兜底（含中文，不进命令行）
    tr = '"%s" -u "%s"' % (python, RUNNER)
    cmd = ['/create', '/tn', TASK_NAME, '/tr', tr,
           '/sc', 'DAILY', '/st', args.start, '/f']
    if args.repeat_minutes and args.duration:
        cmd += ['/ri', str(args.repeat_minutes), '/du', args.duration]
    print('注册计划任务：%s' % TASK_NAME)
    print('  命令：%s' % tr)
    print('  计划：每天 %s 起，每 %d 分钟一次、持续 %s'
          % (args.start, args.repeat_minutes, args.duration))
    rc, out = _schtasks(cmd)
    print('\n✅ 已注册。' if rc == 0 else '\n❌ 注册失败，请检查输出。')
    return rc


def main():
    ap = argparse.ArgumentParser(description='每日录播自动上传计划任务管理')
    ap.add_argument('--install', action='store_true', help='注册/更新任务（默认动作）')
    ap.add_argument('--query', action='store_true', help='查看任务状态')
    ap.add_argument('--run', action='store_true', help='立即触发一次')
    ap.add_argument('--remove', action='store_true', help='删除任务')
    ap.add_argument('--python', default=r'C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe',
                    help='运行用的解释器（默认 bili-archive venv）')
    ap.add_argument('--start', default='06:00', help='每天首次触发时间 HH:MM（默认 06:00）')
    ap.add_argument('--repeat-minutes', type=int, default=60, help='重复间隔分钟（默认 60）')
    ap.add_argument('--duration', default='04:00', help='重复持续时长 HH:MM（默认 04:00 → 共 4 次）')
    args = ap.parse_args()

    if args.remove:
        return _schtasks(['/delete', '/tn', TASK_NAME, '/f'])[0]
    if args.query:
        return _schtasks(['/query', '/tn', TASK_NAME, '/fo', 'LIST', '/v'])[0]
    if args.run:
        return _schtasks(['/run', '/tn', TASK_NAME])[0]
    return install(args)


if __name__ == '__main__':
    sys.exit(main())
