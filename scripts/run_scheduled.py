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


def _guard_done_today(path):
    """今天是否已有一轮「计划任务触发、正常完成」的运行。"""
    try:
        with open(path, encoding='utf-8') as f:
            d = json.load(f) or {}
        return (d.get('date') == datetime.date.today().isoformat()
                and d.get('ok') is True)
    except Exception:
        return False


def _resolve_target_date(extra):
    """目标直播日期：优先 extra 里的 --date，否则默认昨天（跨午夜归开播日）。"""
    for i, a in enumerate(extra):
        if a == '--date' and i + 1 < len(extra):
            return extra[i + 1]
    return (datetime.date.today() - datetime.timedelta(days=1)).isoformat()


def _make_plog(lf):
    """构造同时写日志文件 + stdout 的 logger（供决策器/护栏复用）。"""
    def plog(m, lv='INFO'):
        line = '[%s] %s' % (lv, m)
        try:
            lf.write(line + '\n')
            lf.flush()
        except Exception:
            pass
        print(line)
    return plog


def _load_cfg(workdir):
    import common
    cfg_path = os.path.join(workdir, 'config.json')
    cfg = common.load_config(cfg_path)
    cfg['_workdir'] = workdir
    cfg['_config_path'] = cfg_path
    return cfg


def _source_only(workdir, extra):
    """只做实时取源决策并打印，供自测验证（不下载、不跑管线、不写结果）。"""
    cfg = _load_cfg(workdir)
    ymd = _resolve_target_date(extra)
    import source_decider as SD
    d = SD.decide(cfg, ymd)
    print('[source_only] 目标日期 %s' % ymd)
    print('[source_only] 决策 %s  (%s)' % (d['action'], d['reason']))
    print('[source_only] 依据 %s' % d['detail'])
    print(json.dumps(d, ensure_ascii=False, indent=2))


def _preflight_source(cfg, workdir, ymd, log):
    """实时取源决策 + 必要时下完整版落位。返回 decision dict（action 可能是 FAILED）。"""
    import source_decider as SD
    decision = SD.decide(cfg, ymd, log=log)
    log('[取源] %s → %s（%s）' % (ymd, decision['action'], decision['detail']))
    if decision['action'] != 'DOWNLOAD':
        return decision

    fnames = (decision.get('evidence') or {}).get('local_files') or []
    root = (cfg.get('local') or {}).get('root')
    paths = [os.path.join(root, f) for f in fnames] if root else []
    paths = [p for p in paths if os.path.exists(p)]

    if paths:
        # 本地残缺/低画质 → 隔离（绝不删）→ 下完整版 → 失败自动回滚
        import completeness_guard as CG
        lv = datetime.date.fromisoformat(ymd)
        rep = CG.repair_incomplete_session(cfg, log, lv, paths, workdir=workdir)
        decision['repair'] = rep
        if not rep.get('ok'):
            decision['action'] = 'FAILED'
            decision['detail'] = rep.get('reason') or '隔离重拉失败'
    else:
        # 本地无源 → 直接下完整版落位
        import rec_fetch_date as RF
        gap = int((cfg.get('darkpy') or {}).get('max_gap_sec', 1800))
        try:
            rc = RF.fetch_date(cfg, log, ymd, gap, 0, no_pipeline=True)
            decision['fetch_rc'] = rc
            if rc != 0:
                decision['action'] = 'FAILED'
                decision['detail'] = '录播站下载落位失败 rc=%s' % rc
        except Exception as e:
            decision['action'] = 'FAILED'
            decision['detail'] = '录播站下载异常：%s' % e
    return decision


def main():
    ap = argparse.ArgumentParser(description='耐久执行入口（供 Windows 计划任务调用）')
    ap.add_argument('--workdir', default=None)
    ap.add_argument('--attempts', type=int, default=2, help='最多尝试次数（幂等，重跑即续做）')
    ap.add_argument('--max-minutes', type=int, default=240, help='单次最长执行时间（分钟）')
    ap.add_argument('--status', action='store_true', help='只打印上次运行结果')
    ap.add_argument('--source-only', action='store_true',
                    help='只做取源决策并打印（不下载/不跑管线/不写结果），用于自测验证')
    ap.add_argument('extra', nargs='*', help="透传给 daily_pipeline 的参数（放在 -- 之后）")
    args = ap.parse_args()

    workdir = resolve_workdir(args.workdir)
    logs = os.path.join(workdir, 'logs')
    os.makedirs(logs, exist_ok=True)
    outcome_path = os.path.join(logs, 'scheduled_last_run.json')
    guard_path = os.path.join(logs, 'daily_run_guard.json')

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

    # ---- 日级幂等守卫（2026-11-15）----
    # 04:20 与 06:20 是两个独立触发器；06:20 的本意只是「04:20 失败时兜底」。
    # 过去没有「今天已成功」的概念，04:20 正常跑完后 06:20 还会再完整跑一轮
    # （空转但照样清扫、写 state、烧一遍用量汇报）。现在 daily_pipeline 在正常
    # 完成时写 daily_run_guard.json，这里读到就跳过；失败/中断不写，兜底语义
    # 保留；-- --force 可强制重跑。
    extra = [a for a in args.extra if a != '--']
    force = '--force' in extra
    extra = [a for a in extra if a != '--force']

    if args.source_only:
        _source_only(workdir, extra)
        return 0

    if not force and _guard_done_today(guard_path):
        msg = ('[run_scheduled] 今天已成功跑过一轮（daily_run_guard），本次跳过'
               '——避免 04:20/06:20 双触发器重复运行；-- --force 可强制')
        print(msg)
        _write_outcome(outcome_path, {'skipped': True,
                                      'reason': 'already_done_today',
                                      'at': _now()})
        return 0

    log_path = os.path.join(logs, 'run_scheduled_%s.log'
                            % datetime.date.today().isoformat())
    plan = [sys.executable, '-u', PIPELINE, '--workdir', workdir]
    if extra:
        plan += extra

    # ---- 步骤 0：实时取源决策（用户规则 2026-11-24）----
    # 本地无源 / 缺>5 分钟 / 只有低画质 → 立即下完整版（隔离残缺源，绝不删）
    # 再合并总结。决策器每次实时扫描，绝不沿用旧观察。
    # 下载只做一次（幂等），放在重试循环外；失败不写日级守卫 → 06:20 兜底重跑。
    ymd = _resolve_target_date(extra)
    try:
        with open(log_path, 'a', encoding='utf-8', errors='replace') as lf:
            plog = _make_plog(lf)
            plog('[run_scheduled] 步骤0 取源决策，目标日期 %s' % ymd)
            cfg = _load_cfg(workdir)
            decision = _preflight_source(cfg, workdir, ymd, plog)
    except Exception as e:
        print('[run_scheduled] 取源决策异常：%s' % e)
        decision = {'action': 'FAILED', 'reason': 'preflight_exception',
                    'detail': str(e)}

    if decision.get('action') == 'UNKNOWN':
        # 本地无源且 rec 站无场次（可能停播）：不下载、不空跑管线
        print('[run_scheduled] %s' % decision.get('detail'))
        _write_outcome(outcome_path, {'skipped': True, 'reason': 'no_session',
                                      'date': ymd, 'decision': decision,
                                      'at': _now()})
        return 0

    if decision.get('action') == 'FAILED':
        # 补档失败：阻断本轮（不写日级守卫），让 06:20 兜底重试
        print('[run_scheduled] 取源补档失败，阻断本轮：%s' % decision.get('detail'))
        _write_outcome(outcome_path, {'ok': False, 'reason': 'source_refetch_failed',
                                      'date': ymd, 'decision': decision,
                                      'errors_tail': tail(os.path.join(logs, 'errors.log'), 20),
                                      'at': _now()})
        return 1

    attempts, rc, t_start = max(1, args.attempts), 1, time.time()
    for attempt in range(1, attempts + 1):
        header = ('\n' + '=' * 70 + '\n[run_scheduled] 第 %d/%d 次尝试  %s\n'
                  % (attempt, attempts, _now()) + '=' * 70 + '\n')
        print(header, end='')
        try:
            with open(log_path, 'a', encoding='utf-8', errors='replace') as lf:
                lf.write(header)
                lf.flush()
                # 标记触发来源：管线据此在用量汇报里写「计划任务」而不是「手动」
                _env = dict(os.environ)
                _env['BLS_TRIGGER'] = 'scheduled'
                p = subprocess.run(plan, stdout=lf, stderr=subprocess.STDOUT,
                                   timeout=max(60, args.max_minutes) * 60, env=_env)
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
