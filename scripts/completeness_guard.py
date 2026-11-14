# -*- coding: utf-8 -*-
"""completeness_guard.py —— 本地录播完整性护栏（用户规则 2026-10-07，2026-11-15 接入主管线）。

背景（两起真实事故）
--------------------
2026-10-06：rec.darkpy.cn 当天实际 6 段 / 19.17GB，归组后一场约 4h40m；
本地只有其中第 5 段（1.4GB / 02:20:49），管线把「单文件」当完整场次直接总结，
漏掉 2h40m+，还跨了两场不同标题的直播。

2026-11-13（本模块**失效**暴露）：本地只有 01:08:23，站点完整 03:00:49，
管线照旧直接总结残缺版。根因不是没有护栏——本模块与
config.local_completeness_guard 早在 10-07 就存在，但**只被已废弃的
local_priority.py 调用，daily_pipeline.py 从未 import**，配置形同虚设。
2026-11-15 起正式接入 daily_pipeline 步骤 1.5，并新增 repair_incomplete_session()：
判定疑似截断后自动隔离残缺源 + 从录播站拉完整版落位，绝不拿残片出总结。

设计原则
--------
  · 网络失败**绝不阻塞**管线：rec 站不可达时降级为「跳过校验、照常处理」，
    绝不让校验本身成为新的故障点（吸取 09-07 网络不通导致管线崩溃的教训）。
  · 只在「本地场次疑似截断」时动作；正常单场零打扰。
  · 比对按**场次时间窗匹配**，不用「当天全部场次时长之和」当分母——
    一晚上两场（电脑+电台）时后者会把正常的一场误判成只剩一半。
  · 隔离 = 移到工作目录 quarantine/<日期>/，**绝不删除**用户原文件（铁律）；
    补档失败自动回滚到原位。
  · 不在这里跑 daily_pipeline：外层已持有单实例锁，嵌套会自锁。
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys

__all__ = [
    'probe_duration_sec',
    'check_local_completeness',
    'repair_incomplete_session',
    'self_test',
]

# 覆盖率阈值：本地时长 / rec 站同场总时长 低于此值 → 疑似截断
DEFAULT_COVER_RATIO = 0.80
# 单场最短可信时长（秒）。低于 10 分钟的“场”多半是误判，不做截断判定。
MIN_SESSION_SEC = 600


# ---------------------------------------------------------------------------
# ffprobe 探测时长
# ---------------------------------------------------------------------------
def probe_duration_sec(path, timeout=60, ffprobe=None):
    """用 ffprobe 取视频时长（秒）。失败返回 None。

    ★ 2026-11-15 修：原实现遍历候选列表时，遇到第一个「不是文件」的候选
      （通常是 PATH 里的 'ffprobe'）就 break，cfg.paths.ffprobe 根本排不上，
      本机 ffprobe 又不在 PATH → 探测全部失败 → 静默退化成体积比对。
      现在优先用配置里的绝对路径。
    """
    if not path or not os.path.exists(path):
        return None
    cands = []
    if ffprobe:
        cands.append(ffprobe)
    cands += [os.environ.get('FFPROBE'), 'ffprobe',
              r'C:\ffmpeg\bin\ffprobe.exe', r'D:\ffmpeg\bin\ffprobe.exe']
    exe, fallback = None, None
    for cand in cands:
        if not cand:
            continue
        if os.path.isfile(cand):
            exe = cand
            break
        if fallback is None:
            fallback = cand
    exe = exe or fallback
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=timeout)
        val = (out.stdout or '').strip()
        return float(val) if val else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# rec 站侧：当天真实场次与总时长
# ---------------------------------------------------------------------------
def _rec_sessions_for_date(cfg, ymd, log=None):
    """返回 rec 站归属 ymd 的场次列表（已全局归组，跨午夜尾巴归开播日）。"""
    def _log(m, lv='INFO'):
        if log:
            log(m, lv)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import darkpy_rec as D
    except Exception as e:
        _log('[guard] 无法导入 darkpy_rec：%s（跳过校验）' % e, 'WARN')
        return None
    try:
        dd = D.cfg_darkpy(cfg)
        if not dd.get('enabled'):
            _log('[guard] rec 站未启用，跳过完整性校验', 'WARN')
            return None
        if not dd.get('room_dir'):
            _log('[guard] rec 站 room_dir 未配置，跳过完整性校验', 'WARN')
            return None
        files = D.fetch_index(cfg, _log)
        if not files:
            _log('[guard] rec 站索引为空/不可达，跳过完整性校验', 'WARN')
            return None
        sessions = D.sessions_for_date(files, ymd, dd.get('max_gap_sec', 900), _log)
        return sessions or []
    except Exception as e:
        _log('[guard] rec 站查询异常：%s（跳过校验，不阻塞管线）' % e, 'WARN')
        return None


def _session_span_sec(session):
    """用分段 start/end 估算该场总时长（秒）。取不到就返回 None。"""
    starts = [s.get('start') for s in session if s.get('start')]
    ends = [s.get('end') for s in session if s.get('end')]
    if not starts:
        return None
    t0 = min(starts)
    t1 = max(ends) if ends else max(starts)
    try:
        return max(0.0, (t1 - t0).total_seconds())
    except Exception:
        return None


def _match_sessions(sessions, local_span):
    """挑出与本地会话时间窗重叠的 rec 站场次。

    为什么必须按场次匹配：一晚上两场（1080p60 电脑场 + 竖屏电台场）时，
    当天总时长是两场之和；拿本地其中一场去除以总和会恒 < 0.8，
    把完好的场次误判成截断，进而误触发整场重新下载。
    取不到 local_span（旧调用方/自检 CLI）时退回当天全部场次，保持兼容。
    """
    if not local_span:
        return sessions
    t0, t1 = local_span
    if not (t0 and t1):
        return sessions
    tol = datetime.timedelta(minutes=10)
    hits = []
    for sess in sessions or []:
        starts = [s.get('start') for s in sess if s.get('start')]
        ends = [s.get('end') for s in sess if s.get('end')]
        if not starts:
            continue
        a, b = min(starts), (max(ends) if ends else max(starts))
        try:
            if a <= t1 + tol and b >= t0 - tol:
                hits.append(sess)
        except Exception:
            continue
    return hits or sessions


# ---------------------------------------------------------------------------
# 主校验
# ---------------------------------------------------------------------------
def check_local_completeness(cfg, live_date, local_files, log=None,
                             cover_ratio=DEFAULT_COVER_RATIO,
                             probe=True, local_span=None):
    """比对 rec 站与本地同一场直播的时长，判断本地是否疑似截断。

    参数
    ----
    cfg         : dict，已由 common.load_config 读入并注入 _dirs
    live_date   : datetime.date/datetime，本地会话归属的开播日
    local_files : list[str]，该本地会话包含的原始文件（1 个=单文件）
    cover_ratio : float，覆盖率阈值（默认 0.80）
    local_span  : (span_start, span_end) 可选，用于精确匹配 rec 站场次

    返回
    ----
    dict：checked / suspect / local_sec / rec_sec / rec_sess / rec_segs /
          rec_matched_sess / ratio / reason
    """
    def _log(m, lv='INFO'):
        if log:
            log(m, lv)

    res = {'checked': False, 'suspect': False, 'local_sec': None,
           'rec_sec': None, 'rec_sess': 0, 'rec_segs': 0,
           'rec_matched_sess': 0, 'ratio': None, 'reason': ''}

    ymd = live_date.strftime('%Y%m%d') if hasattr(live_date, 'strftime') \
        else str(live_date).replace('-', '')
    sessions = _rec_sessions_for_date(cfg, ymd, log=log)
    if not sessions:
        res['reason'] = 'rec站不可达/无数据，跳过校验（不阻塞）'
        return res
    matched = _match_sessions(sessions, local_span)
    res['checked'] = True
    res['rec_sess'] = len(sessions)
    res['rec_segs'] = sum(len(s) for s in sessions)
    res['rec_matched_sess'] = len(matched)

    ffprobe = (cfg.get('paths') or {}).get('ffprobe')

    # 本地时长
    local_sec = 0.0
    got_any = False
    if probe:
        for p in (local_files or []):
            d = probe_duration_sec(p, ffprobe=ffprobe)
            if d:
                local_sec += d
                got_any = True
    if not got_any:
        # 探测不可用 → 退化为文件体积比对（粗但够用：同源同码率，体积≈时长）
        try:
            local_bytes = sum(os.path.getsize(p) for p in (local_files or [])
                              if os.path.exists(p))
            rec_bytes = sum(float(s.get('size_gb', 0))
                            for sess in matched for s in sess) * (1024 ** 3)
            if local_bytes and rec_bytes:
                res['local_sec'] = None
                ratio = local_bytes / rec_bytes
                res['ratio'] = round(ratio, 3)
                if ratio < cover_ratio:
                    res['suspect'] = True
                    res['reason'] = ('本地体积仅为 rec 站的 %.0f%%（%dB / %dB），疑似截断'
                                     % (ratio * 100, local_bytes, int(rec_bytes)))
                    _log('[guard] ⚠️ %s %s' % (ymd, res['reason']), 'WARN')
                else:
                    res['reason'] = '本地体积覆盖 rec 站 %.0f%%，判定完整' % (ratio * 100)
                return res
        except Exception as e:
            _log('[guard] 体积比对异常：%s' % e, 'WARN')
        res['reason'] = '本地时长/体积均无法探测，跳过校验（不阻塞）'
        return res

    res['local_sec'] = round(local_sec, 1)

    # rec 站同场总时长（只取与本地时间窗重叠的场次）
    spans = [s for s in (_session_span_sec(x) for x in matched) if s]
    rec_total = sum(spans) if spans else None
    res['rec_sec'] = round(rec_total, 1) if rec_total else None

    if rec_total and rec_total >= MIN_SESSION_SEC:
        ratio = local_sec / rec_total
        res['ratio'] = round(ratio, 3)
        if ratio < cover_ratio:
            res['suspect'] = True
            res['reason'] = ('本地仅 %.0f 分钟 / rec 站同场 %.0f 分钟 = %.0f%%，'
                             '疑似截断（rec站当天 %d 场 / %d 段，本场匹配 %d 场）'
                             % (local_sec / 60, rec_total / 60, ratio * 100,
                                res['rec_sess'], res['rec_segs'],
                                res['rec_matched_sess']))
            _log('[guard] ⚠️ %s %s' % (ymd, res['reason']), 'WARN')
        else:
            res['reason'] = ('本地覆盖 rec 站同场 %.0f%%（%.0f/%.0f 分钟），判定完整'
                             % (ratio * 100, local_sec / 60, rec_total / 60))
    else:
        res['reason'] = 'rec站同场时长不可信/过短，跳过判定'

    return res


# ---------------------------------------------------------------------------
# 疑似截断后的自动修复
# ---------------------------------------------------------------------------
def _rollback(moved, log):
    """补档失败时把隔离出去的文件原样搬回（绝不弄丢用户原文件）。"""
    for src, dest in reversed(moved):
        try:
            if os.path.exists(dest) and not os.path.exists(src):
                os.replace(dest, src)
                log('[完整性护栏] 已回滚隔离文件：%s' % os.path.basename(src), 'WARN')
        except Exception as e:
            log('[完整性护栏] 回滚失败（文件仍在隔离目录 %s）：%s' % (dest, e), 'ERROR')


def repair_incomplete_session(cfg, log, live_date, local_files, workdir=None):
    """隔离残缺本地源（**不删除**）→ 从 rec 站拉该日完整版并落位到 local.root。

    只落位、不跑管线：外层 daily_pipeline 已持有单实例锁，嵌套调用会自锁；
    落位后的完整版由调用方重新 scan_root_sessions() 纳入本轮步骤 2。

    返回 {'ok', 'quarantined', 'quarantine_dir', 'reason', 'rolled_back'}
    """
    def _log(m, lv='INFO'):
        if log:
            log(m, lv)

    res = {'ok': False, 'quarantined': [], 'quarantine_dir': '',
           'reason': '', 'rolled_back': False}
    ymd = (live_date.strftime('%Y-%m-%d') if hasattr(live_date, 'strftime')
           else str(live_date))
    wd = (workdir or cfg.get('_workdir')
          or os.path.dirname(os.path.abspath(cfg.get('_config_path') or 'config.json')))
    qdir = os.path.join(wd, 'quarantine', ymd)
    res['quarantine_dir'] = qdir
    moved = []

    try:
        os.makedirs(qdir, exist_ok=True)
        for p in (local_files or []):
            if not p or not os.path.exists(p):
                continue
            dest = os.path.join(qdir, os.path.basename(p))
            n = 1
            while os.path.exists(dest):
                dest = os.path.join(qdir, '%s.%d' % (os.path.basename(p), n))
                n += 1
            os.replace(p, dest)
            moved.append((p, dest))
            res['quarantined'].append(dest)
            _log('[完整性护栏] 已隔离残缺源（保留不删）：%s → %s'
                 % (os.path.basename(p), dest), 'WARN')
    except Exception as e:
        res['reason'] = '隔离残缺源失败：%s' % e
        _log('[完整性护栏] %s' % res['reason'], 'ERROR')
        _rollback(moved, _log)
        res['rolled_back'] = True
        return res

    if not moved:
        res['reason'] = '没有可隔离的本地源（可能已被移走）'
        return res

    try:
        import rec_fetch_date as RF
        gap = int((cfg.get('darkpy') or {}).get('max_gap_sec', 1800))
        rc = RF.fetch_date(cfg, log, ymd, gap, 0, no_pipeline=True)
        if rc != 0:
            res['reason'] = '录播站补完整版失败（fetch_date 退出码 %s）' % rc
            _log('[完整性护栏] %s，残缺源已回滚' % res['reason'], 'ERROR')
            _rollback(moved, _log)
            res['rolled_back'] = True
            return res
        res['ok'] = True
        res['reason'] = '已从录播站落位完整版；残缺源隔离在 %s' % qdir
        _log('[完整性护栏] %s' % res['reason'])
    except Exception as e:
        res['reason'] = '录播站补完整版异常：%s' % e
        _log('[完整性护栏] %s，残缺源已回滚' % res['reason'], 'ERROR')
        _rollback(moved, _log)
        res['rolled_back'] = True
    return res


# ---------------------------------------------------------------------------
# 自检 CLI：python completeness_guard.py --config <path> --date 2026-10-06
# ---------------------------------------------------------------------------
def self_test():
    import argparse
    ap = argparse.ArgumentParser(
        description='本地录播完整性护栏自检（比对 rec 站时长）')
    ap.add_argument('--config', default=r'D:\岁己直播自动总结\config.json')
    ap.add_argument('--date', help='直播日 YYYY-MM-DD；缺省则用今天')
    ap.add_argument('--file', action='append', default=[],
                    help='本地文件（可多次）；缺省则扫录播根目录该日文件')
    ap.add_argument('--ratio', type=float, default=DEFAULT_COVER_RATIO)
    ap.add_argument('--no-probe', action='store_true',
                    help='跳过 ffprobe，退化为体积比对')
    a = ap.parse_args()

    def log(m, lv='INFO'):
        print('[%s] %s' % (lv, m))

    cfg = json.load(open(a.config, 'r', encoding='utf-8'))
    cfg['_workdir'] = os.path.dirname(os.path.abspath(a.config))
    cfg['_config_path'] = a.config

    if a.date:
        d = datetime.datetime.strptime(a.date, '%Y-%m-%d').date()
    else:
        d = datetime.date.today()

    files = list(a.file)
    if not files:
        root = (cfg.get('local') or {}).get('root') or r'D:\录播\25788785-岁己SUI'
        prefix = d.strftime('%Y%m%d')
        for dirpath, _, fnames in os.walk(root):
            for fn in fnames:
                if fn.lower().endswith(('.flv', '.mp4')) and prefix in fn:
                    files.append(os.path.join(dirpath, fn))
    print('本地候选文件（%s）: %d 个' % (d, len(files)))
    for f in files:
        print('   ', f)

    r = check_local_completeness(cfg, d, files, log=log,
                                 cover_ratio=a.ratio, probe=not a.no_probe)
    print()
    print('=== 校验结果 ===')
    for k in ('checked', 'suspect', 'local_sec', 'rec_sec', 'rec_sess',
              'rec_segs', 'rec_matched_sess', 'ratio', 'reason'):
        print('  %-18s = %s' % (k, r[k]))
    return 0 if (r['checked'] and not r['suspect']) else 1


if __name__ == '__main__':
    sys.exit(self_test())
