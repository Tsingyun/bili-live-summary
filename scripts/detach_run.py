# -*- coding: utf-8 -*-
"""脱离当前会话启动长任务：回答被中断 / 会话被关也不会连带杀掉进程。

用法:
    python detach_run.py --log <日志路径> [--cwd <目录>] -- <命令> [参数...]

示例:
    python detach_run.py --log D:/x/logs/manual.log -- \
        python -u daily_pipeline.py --config D:/x/config.json --date 2026-11-15

打印 DETACHED_PID=<pid> 后立即退出（exit 0），真正的任务在后台继续跑。
原理：subprocess.Popen 加 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP，
      stdin 接 DEVNULL，stdout/stderr 直接写日志文件句柄；
      子进程不隶属当前控制台与进程组，父进程（会话里的 shell）退出不影响它。
"""
import argparse
import os
import subprocess
import sys
import time

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', required=True, help='stdout/stderr 落盘路径')
    ap.add_argument('--cwd', default=None, help='子进程工作目录')
    ap.add_argument('cmd', nargs=argparse.REMAINDER,
                    help='-- 之后的完整命令')
    a = ap.parse_args()

    cmd = list(a.cmd)
    if cmd and cmd[0] == '--':
        cmd = cmd[1:]
    if not cmd:
        print('缺少命令：detach_run.py --log xx -- <cmd...>', file=sys.stderr)
        return 2

    log_path = os.path.abspath(a.log)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    # 追加模式：断点续跑时保留上一次的日志
    logf = open(log_path, 'ab', buffering=0)
    logf.write(('\n==== %s detach start: %s ====\n'
                % (time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(cmd))).encode('utf-8'))
    logf.flush()

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'

    p = subprocess.Popen(
        cmd,
        cwd=a.cwd or os.getcwd(),
        stdin=subprocess.DEVNULL,
        stdout=logf,
        stderr=subprocess.STDOUT,
        creationflags=flags,
        close_fds=True,
        env=env,
    )

    # 记 pid，方便后续查活/收尸
    pid_path = log_path + '.pid'
    try:
        with open(pid_path, 'w', encoding='utf-8') as f:
            f.write('%d\n%s\n' % (p.pid, time.strftime('%Y-%m-%d %H:%M:%S')))
    except Exception:
        pass

    print('DETACHED_PID=%d' % p.pid)
    print('LOG=%s' % log_path)
    print('PIDFILE=%s' % pid_path)
    return 0


if __name__ == '__main__':
    sys.exit(main())
