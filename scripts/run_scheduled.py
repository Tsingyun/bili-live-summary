#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_scheduled.py —— 抗"会话回收"的耐久执行入口（推荐由 Windows 计划任务调用）。

为什么需要它
------------
2026-09-17 / 09-18 连续两天，自动化在 04:02 拉起 daily_pipeline 后，agent 回合于
04:04 正常结束，运行它的**宿主运行时实例随即被环境回收（lifecycle:dispose）**，
整棵进程树被一并拆掉——管线当时刚下到 0.8 GB/13 GB，于是当天没有任何产物。
这不是脚本 bug，而是"进程生命周期"问题：**只要长任务挂在 agent 会话的进程树下面，
它就有可能在回合结束时被一起清掉**。

把同一个命令交给 **Windows 计划任务** 执行即可根治：任务由 Task Scheduler 服务
拉起，不在 agent 的进程树里，agent 回合结束/重开都不影响它。

本脚本的职责
------------
1. 单实例守卫：若已有**存活**的实例持有 .daily_pipeline.lock（心跳新鲜），直接退出 0；
2. 以子进程方式跑 daily_pipeline.py，输出同时落到 logs/run_scheduled_YYYY-MM-DD.log；
3. 失败自动重试（默认 2 次，退避 120s）——管线是幂等的，重跑即从断点续做；
4. 把本次结果写成 logs/scheduled_last_run.json（退出码、耗时、失败摘要），
   供自动化/人后续汇报，不依赖 agent 会话存活。

用法
----
    python run_scheduled.py                     # 正常执行（计划任务用这个）
    python run_scheduled.py --max-minutes 240   # 单次最长执行时间
    python run_scheduled.py --attempts 3        # 最多重试次数
    python run_scheduled.py --status            # 只看上次结果，不执行
    python run_scheduled.py -- --date 2026-09-18  # 透传参数给 daily_pipeline
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

PIPELINE = os.path.join(SCRIPT_DIR, 'daily_pipeline.py')


def resolve_workdir(explicit=None):
    """按 --workdir / BILI_WORKDIR / _workdir.json / BILI_LIVE_CONFIG 定位工作目录。"""
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
    cfg_env = os.environ.get('BILI_LIVE_CONFIG')
    if cfg_env and os.path.isfile(cfg_env):
        return os.path.dirname(os.path.abspath(cfg_env))
    raise RuntimeError('无法确定工作目录：请传 --workdir 或设 BILI_WORKDIR')


def lock_owner_alive(workdir):
    """是否有存活的实例持锁。

    直接复用 common.lock_owner_alive（「PID 存在 + 心跳新鲜」双条件），
    避免两处实现漂移——判活规则一旦不一致，会出现"新实例以为没人跑、
    旧实例其实还活着"的并发写。
    """
    try:
        import common
        return bool(common.lock_owner_alive(workdir))
    except Exception:
        return False


def tail(path, n=40):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return ''.join(f.readlines()[-n:])
    except Exception:
        return ''


def main():
    ap = argparse.ArgumentParser(description='耐久执行入口（供 Windows 计划任务调用）')
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--attempts', type=int, default=2, help='最多尝试次数（幂等，重跑即续做）')
    ap.add_argument('--max-minutes', type=int, default=240, help='单次最长执行时间（分钟）')
    ap.add_argument('--status', action='store_true', help='只打印上次运行结果')
    ap.add_argument('extra', nargs='*', help="透传给 daily_pipeline 的参数（放在 -- 之后）")
    args = ap.parse_args()

    workdir = resolve_workdir(args.workdir)
    logs = os.path.join(workdir, 'logs')
    os.makedirs(logs, exist_ok=True)
    outcome_path = os.path.join(logs, 'scheduled_last_run.json')

    if args.status:
        if os.path.exists(outcome_path):
            with open(outcome_path, encoding='utf-8') as f:
                print(f.read())
        else:
            print('（还没有计划任务运行记录）')
        return 0

    if lock_owner_alive(workdir):
        msg = ('[run_scheduled] 已有存活实例在跑（心跳有效），本次跳过'
               '——若这是同一天的计划任务重复触发，属正常行为')
        print(msg)
        _write_outcome(outcome_path, {'skipped': True, 'reason': 'already_running',
                                      'at': _now()})
        return 0

    log_path = os.path.join(logs, 'run_scheduled_%s.log'
                            % datetime.date.today().isoformat())
    plan = [sys.executable, '-u', PIPELINE, '--workdir', workdir]
    if args.extra:
        plan += [a for a in args.extra if a != '--']

    attempts, rc, t_start = max(1, args.attempts), 1, time.time()
    for attempt in range(1, attempts + 1):
        header = ('\n' + '=' * 70 + '\n[run_scheduled] 第 %d/%d 次尝试  %s\n'
                  % (attempt, attempts, _now()) + '=' * 70 + '\n')
        print(header, end='')
        try:
            with open(log_path, 'a', encoding='utf-8', errors='replace') as lf:
                lf.write(header)
                lf.flush()
                p = subprocess.run(plan, stdout=lf, stderr=subprocess.STDOUT,
                                   timeout=max(60, args.max_minutes) * 60)
                rc = p.returncode
        except subprocess.TimeoutExpired:
            print('[run_scheduled] 单次执行超时（%d 分钟），已终止' % args.max_minutes)
            rc = 124
        except Exception as e:
            print('[run_scheduled] 执行异常：%s' % e)
            rc = 125
        if rc == 0:
            break
        if attempt < attempts:
            print('[run_scheduled] 退出码 %s，120s 后重试（管线幂等，将从断点续做）' % rc)
            time.sleep(120)

    result = {
        'at': _now(),
        'attempts': attempts,
        'exit_code': rc,
        'ok': rc == 0,
        'elapsed_sec': int(time.time() - t_start),
        'log': log_path,
        'errors_tail': tail(os.path.join(logs, 'errors.log'), 20) if rc != 0 else '',
    }
    _write_outcome(outcome_path, result)
    print('[run_scheduled] 结束：exit=%s ok=%s 用时 %ds'
          % (rc, rc == 0, result['elapsed_sec']))
    return 0 if rc == 0 else rc


def _now():
    return datetime.datetime.now().isoformat(timespec='seconds')


def _write_outcome(path, obj):
    try:
        tmp = path + '.part'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        pass


if __name__ == '__main__':
    sys.exit(main())
