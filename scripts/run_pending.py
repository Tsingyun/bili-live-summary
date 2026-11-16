# -*- coding: utf-8 -*-
"""计划任务入口：读「待跑描述文件」并执行指定脚本，跑完标记为 done。

为什么需要它：
  工作流里的终端调用都跑在 Job Object 里（父进程退出即整树回收），
  从会话内起的任何后台进程都活不过那次调用 —— 回答被中断 / 会话关闭时
  长流程必被连带杀掉。唯一能真正脱离的入口是 Windows 计划任务：
  由 Task Scheduler 服务拉起，不在会话的 job 里。

用法（配合计划任务 BiliLiveSummary_Pending，无触发器、纯手动 Start）：
  1. 写 WORKDIR/logs/pending_run.json:
        {"argv": ["--date", "2026-11-15"], "note": "补跑"}
  2. PowerShell: Start-ScheduledTask -TaskName "BiliLiveSummary_Pending"
  3. 立刻返回，任务在系统侧继续跑，看 logs/pending_<时间>_*.log

描述文件字段:
  script : 可选，默认 daily_pipeline.py（相对于 scripts 目录）
  argv   : 必填，透传给脚本的参数数组
  env    : 可选，附加环境变量字典
  config : 可选，覆盖默认 config 路径
  note   : 可选，写进日志的说明
跑完会把 pending_run.json 重命名为 pending_run.done.json（含 rc / 起止时间），
避免同一请求被重复执行。
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load_cfg_path(explicit=None):
    if explicit:
        return explicit
    p = os.path.join(HERE, '_workdir.json')
    try:
        wd = json.load(open(p, encoding='utf-8')).get('workdir')
        if wd:
            c = os.path.join(wd, 'config.json')
            if os.path.isfile(c):
                return c
    except Exception:
        pass
    raise SystemExit('找不到 config.json，请用 --config 指定')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--request', default=None, help='描述文件路径，默认 <workdir>/logs/pending_run.json')
    a = ap.parse_args()

    cfg = _load_cfg_path(a.config)
    workdir = os.path.dirname(os.path.abspath(cfg))
    req_path = a.request or os.path.join(workdir, 'logs', 'pending_run.json')

    # pythonw.exe 下 sys.stdout/stderr 是 None，print 会 AttributeError：
    # 先兜底重定向到固定日志，保证脚本在无控制台环境里也能活。
    if sys.stdout is None or sys.stderr is None:
        try:
            _p = os.path.join(workdir, 'logs', 'pending_runner.log')
            os.makedirs(os.path.dirname(_p), exist_ok=True)
            _lf = open(_p, 'a', encoding='utf-8', buffering=1)
            if sys.stdout is None:
                sys.stdout = _lf
            if sys.stderr is None:
                sys.stderr = _lf
        except Exception:
            pass

    if not os.path.isfile(req_path):
        print('[pending] 没有待跑请求：%s' % req_path)
        return 0

    try:
        req = json.load(open(req_path, encoding='utf-8'))
    except Exception as e:
        print('[pending] 描述文件解析失败：%r' % (e,))
        return 2

    script = req.get('script') or 'daily_pipeline.py'
    argv = req.get('argv') or []
    if not isinstance(argv, list):
        print('[pending] argv 必须是数组')
        return 2
    env_extra = req.get('env') or {}
    cfg_override = req.get('config') or cfg
    note = req.get('note') or ''

    script_path = script if os.path.isabs(script) else os.path.join(HERE, script)
    if not os.path.isfile(script_path):
        print('[pending] 脚本不存在：%s' % script_path)
        return 2

    stamp = time.strftime('%Y%m%d_%H%M%S')
    base = os.path.splitext(os.path.basename(script))[0]
    log_path = os.path.join(workdir, 'logs', 'pending_%s_%s.log' % (stamp, base))

    cmd = [sys.executable, '-u', script_path]
    if req.get('with_config', True):
        cmd += ['--config', cfg_override]
    cmd += [str(x) for x in argv]
    print('[pending] %s' % (' '.join(cmd)))
    if note:
        print('[pending] 说明：%s' % note)
    print('[pending] 日志：%s' % log_path)

    env = dict(os.environ)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUNBUFFERED'] = '1'
    for k, v in env_extra.items():
        env[str(k)] = str(v)

    t0 = time.time()
    with open(log_path, 'ab', buffering=0) as logf:
        logf.write(('\n==== %s pending start: %s ====\n'
                    % (time.strftime('%Y-%m-%d %H:%M:%S'), ' '.join(cmd))).encode('utf-8'))
        p = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                             stdout=logf, stderr=subprocess.STDOUT, env=env)
        rc = p.wait()
    dur = time.time() - t0

    done = {
        'rc': rc,
        'started': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t0)),
        'finished': time.strftime('%Y-%m-%d %H:%M:%S'),
        'seconds': round(dur, 1),
        'cmd': cmd,
        'log': log_path,
        'request': req,
    }
    done_path = os.path.join(workdir, 'logs', 'pending_run.done.json')
    try:
        with open(done_path, 'w', encoding='utf-8') as f:
            json.dump(done, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    try:
        os.replace(req_path, req_path + '.consumed')
    except Exception:
        pass

    print('[pending] 结束 rc=%d 用时 %.1fs -> %s' % (rc, dur, log_path))
    return rc


if __name__ == '__main__':
    sys.exit(main())
