#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""install_scheduled_task.py —— 注册/查看/删除「录播总结耐久执行」计划任务。

背景
----
长任务挂在 agent 会话的进程树下面时，agent 回合结束会导致宿主运行时被环境回收，
整棵进程树被拆掉，管线半途被杀（2026-09-17 / 09-18 生产事故）。
本脚本把 run_scheduled.py 注册成 **Windows 计划任务**——由 Task Scheduler 服务
拉起，不在 agent 进程树里，agent 回合结束与否都不影响它跑完。

默认策略（安全网，不是替代品）
------------------------------
每天 04:20 起，每 60 分钟触发一次、共 3 小时（04:20 / 05:20 / 06:20）：
  · 若此刻已有**存活**实例持有单实例锁 → 直接退出（不重复跑）；
  · 若上一轮被回收、锁是孤儿 → 接管并**从断点续做**（管线全流程幂等）。
这样即使自动化那一次被中途杀掉，当天也能自动补完，不会"整天零产物"。

用法
----
    python install_scheduled_task.py --install          # 注册/更新（默认 04:20 起 3 次）
    python install_scheduled_task.py --query            # 查看任务状态
    python install_scheduled_task.py --run              # 立即触发一次
    python install_scheduled_task.py --remove           # 删除任务
    python install_scheduled_task.py --install --start 03:30 --repeat-minutes 60 --duration 2:00
"""
import argparse
import os
import subprocess
import sys

TASK_NAME = 'BiliLiveSummary_Daily'
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RUNNER = os.path.join(SCRIPT_DIR, 'run_scheduled.py')


def _schtasks(args, quiet=False):
    """执行 schtasks 并返回 (rc, 输出)。输出按 mbcs/utf-8 宽容解码。"""
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


def resolve_workdir(explicit=None):
    import json
    if explicit and os.path.isdir(explicit):
        return explicit
    env = os.environ.get('BILI_WORKDIR')
    if env and os.path.isdir(env):
        return env
    ptr = os.path.join(SCRIPT_DIR, '_workdir.json')
    if os.path.exists(ptr):
        try:
            with open(ptr, encoding='utf-8') as f:
                wd = (json.load(f) or {}).get('workdir')
            if wd and os.path.isdir(wd):
                return wd
        except Exception:
            pass
    return ''


def build_tr(python, workdir, extra=''):
    """拼出计划任务的命令行（schtasks /tr 的引号规则：整体用双引号包裹）。"""
    parts = ['"%s"' % python, '-u', '"%s"' % RUNNER]
    if workdir:
        parts += ['--workdir', '"%s"' % workdir]
    if extra:
        parts.append(extra)
    return ' '.join(parts)


def install(args):
    python = args.python or sys.executable
    workdir = resolve_workdir(args.workdir)
    if not os.path.exists(RUNNER):
        print('找不到 run_scheduled.py：%s' % RUNNER)
        return 1
    tr = build_tr(python, workdir, args.extra)
    cmd = ['/create', '/tn', TASK_NAME, '/tr', tr,
           '/sc', 'DAILY', '/st', args.start, '/f']
    if args.repeat_minutes and args.duration:
        cmd += ['/ri', str(args.repeat_minutes), '/du', args.duration]
    print('注册计划任务：%s' % TASK_NAME)
    print('  命令：%s' % tr)
    print('  计划：每天 %s 起%s'
          % (args.start,
             ('，每 %d 分钟一次、持续 %s（共 %d 次）'
              % (args.repeat_minutes, args.duration,
                 _count(args.repeat_minutes, args.duration))) if args.repeat_minutes else ''))
    rc, out = _schtasks(cmd)
    if rc == 0:
        print('\n✅ 已注册。提示：任务为「只使用交互方式」（需用户已登录），'
              '这与自动化同处一个登录会话，符合预期。')
    else:
        print('\n❌ 注册失败，请检查输出。')
    return rc


def _count(repeat_minutes, duration):
    try:
        h, m = (duration.split(':') + ['0'])[:2]
        total = int(h) * 60 + int(m)
        return max(1, total // max(1, int(repeat_minutes)))
    except Exception:
        return 1


def main():
    ap = argparse.ArgumentParser(description='录播总结耐久执行计划任务管理')
    ap.add_argument('--install', action='store_true', help='注册/更新任务（默认动作）')
    ap.add_argument('--query', action='store_true', help='查看任务状态')
    ap.add_argument('--run', action='store_true', help='立即触发一次')
    ap.add_argument('--remove', action='store_true', help='删除任务')
    ap.add_argument('--workdir', default=None, help='录播工作目录（默认按 _workdir.json）')
    ap.add_argument('--python', default=None, help='运行用的解释器（默认当前解释器）')
    ap.add_argument('--start', default='04:20', help='每天首次触发时间 HH:MM（默认 04:20）')
    ap.add_argument('--repeat-minutes', type=int, default=60,
                    help='重复间隔分钟（默认 60；0 表示每天只跑一次）')
    ap.add_argument('--duration', default='03:00',
                    help='重复持续时长 HH:MM（默认 03:00 → 共 3 次）')
    ap.add_argument('--extra', default='', help='追加传给 daily_pipeline 的参数')
    args = ap.parse_args()

    if args.remove:
        return _schtasks(['/delete', '/tn', TASK_NAME, '/f'])[0]
    if args.query:
        return _schtasks(['/query', '/tn', TASK_NAME, '/fo', 'LIST', '/v'])[0]
    if args.run:
        return _schtasks(['/run', '/tn', TASK_NAME])[0]
    # 默认 install
    return install(args)


if __name__ == '__main__':
    sys.exit(main())
