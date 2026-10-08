# -*- coding: utf-8 -*-
"""completeness_guard.py —— 本地录播完整性护栏（用户规则 2026-10-07）。

背景（真实事故）：
    2026-10-06 直播，rec.darkpy.cn 上当天实际有 **6 个分段 / 19.17 GB**，
    归组后是**一场**连续直播（20:02:04 → 次日 00:42:16，约 4h40m）。
    但本地只有**1 个**文件 `20261006-222645-854-...flv`（1.4GB / 02:20:49），
    恰好是第 5 段。管线把「单文件」当成完整场次直接总结，
    结果只覆盖 22:42–23:42，**漏掉 20:02–22:42 共约 2h40m + 00:10 的续段**，
    而且漏掉的还是两场不同标题的直播（白天「过周二」+深夜「看复仇者联盟3」）。

    根因：管线只信「本地文件列表」，从不去rec 站核对当天真实总时长。
    本地文件**不保证是完整的一场**（可能只下到某一段、可能录制中断只剩尾段）。

对策（用户要求「以后要避免这种情况」）：
    在**处理本地会话之前**，去rec 站拉当天索引，做全局归组，算出：
      · rec 站当天归属场次数 / 总分段 / 总时长(或总字节）
      · 本地该场次覆盖的时长
    若本地覆盖率 < 阈值（默认 80%），判定为「疑似截断」，
    **拒绝按残缺内容出总结**，改为提示走rec 站补档合并全量分段。

设计原则（重要）：
    · **纯新增模块，默认不改现有管线行为**：不改 daily_pipeline.py 主流程，
      只提供可被调用的 `check_local_completeness()` 与自检 CLI。
    · 网络失败**绝不阻塞**管线：rec 站不可达时降级为「跳过校验、照常处理」，
      绝不让校验本身成为新的故障点（吸取 09-07 网络不通导致管线崩溃的教训）。
    · 只在「本地场次疑似截断」时给出 WARN/阻断信号，正常单场不打扰。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

__all__ = [
    'probe_duration_sec',
    'check_local_completeness',
    'self_test',
]

# 覆盖率阈值：本地时长 / rec 站该场总时长 低于此值 → 疑似截断
DEFAULT_COVER_RATIO = 0.80
# 单场最短可信时长（秒）。低于 10 分钟的“场”多半是误判，不做截断判定。
MIN_SESSION_SEC = 600


# ---------------------------------------------------------------------------
# ffprobe 探测时长
# ---------------------------------------------------------------------------
def probe_duration_sec(path, timeout=60):
    """用 ffprobe 取视频时长（秒）。失败返回 None。"""
    if not path or not os.path.exists(path):
        return None
    exe = None
    for cand in (os.environ.get('FFPROBE'),
                 'ffprobe',
                 r'C:\ffmpeg\bin\ffprobe.exe',
                 r'D:\ffmpeg\bin\ffprobe.exe'):
        if not cand:
            continue
        if os.path.isfile(cand):
            exe = cand
            break
        exe = cand  # 交给 PATH 解析
        break
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
    import datetime
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


# ---------------------------------------------------------------------------
# 主校验
# ---------------------------------------------------------------------------
def check_local_completeness(cfg, live_date, local_files, log=None,
                             cover_ratio=DEFAULT_COVER_RATIO,
                             probe=True):
    """比对 rec 站与本地同一场直播的时长，判断本地是否疑似截断。

    参数
    ----
    cfg         : dict，已由 common.load_config 读入并注入 _workdir
    live_date   : datetime.date/datetime，本地会话归属的开播日
    local_files : list[str]，该本地会话包含的原始文件（1 个=单文件）
    cover_ratio : float，覆盖率阈值（默认 0.80）

    返回
    ----
    dict：
      {
        'checked'   : bool   # 是否成功拿到 rec 站数据（False=校验跳过，永不阻塞）
        'suspect'   : bool   # 是否判定本地疑似截断
        'local_sec' : float|None,
        'rec_sec'   : float|None,
        'rec_sess'  : int,   # rec 站当天归属场次数
        'rec_segs'  : int,   # rec 站当天总分段
        'ratio'     : float|None,
        'reason'    : str,
      }
    """
    def _log(m, lv='INFO'):
        if log:
            log(m, lv)

    res = {'checked': False, 'suspect': False, 'local_sec': None,
           'rec_sec': None, 'rec_sess': 0, 'rec_segs': 0, 'ratio': None,
           'reason': ''}

    ymd = live_date.strftime('%Y%m%d') if hasattr(live_date, 'strftime') \
        else str(live_date).replace('-', '')
    sessions = _rec_sessions_for_date(cfg, ymd, log=log)
    if not sessions:
        res['reason'] = 'rec站不可达/无数据，跳过校验（不阻塞）'
        return res
    res['checked'] = True
    res['rec_sess'] = len(sessions)
    res['rec_segs'] = sum(len(s) for s in sessions)

    # 本地时长
    local_sec = 0.0
    got_any = False
    if probe:
        for p in (local_files or []):
            d = probe_duration_sec(p)
            if d:
                local_sec += d
                got_any = True
    if not got_any:
        # 探测不可用 → 退化为文件体积比对（粗但够用：同源同码率，体积≈时长）
        try:
            local_bytes = sum(os.path.getsize(p) for p in (local_files or [])
                              if os.path.exists(p))
            rec_bytes = sum(float(s.get('size_gb', 0)) for sess in sessions for s in sess) \
                * (1024 ** 3)
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

    # rec 站当天总时长（可能多场，取总和作为「当天真实直播体量」）
    spans = [s for s in (_session_span_sec(x) for x in sessions) if s]
    rec_total = sum(spans) if spans else None
    res['rec_sec'] = round(rec_total, 1) if rec_total else None

    if rec_total and rec_total >= MIN_SESSION_SEC:
        ratio = local_sec / rec_total
        res['ratio'] = round(ratio, 3)
        if ratio < cover_ratio:
            res['suspect'] = True
            res['reason'] = ('本地仅 %.0f 分钟 / rec 站当天 %.0f 分钟 = %.0f%%，'
                             '疑似截断（rec站当天 %d 场 / %d 段）'
                             % (local_sec / 60, rec_total / 60, ratio * 100,
                                res['rec_sess'], res['rec_segs']))
            _log('[guard] ⚠️ %s %s' % (ymd, res['reason']), 'WARN')
        else:
            res['reason'] = ('本地覆盖 rec 站 %.0f%%（%.0f/%.0f 分钟），判定完整'
                             % (ratio * 100, local_sec / 60, rec_total / 60))
    else:
        res['reason'] = 'rec站当天时长不可信/过短，跳过判定'

    return res


# ---------------------------------------------------------------------------
# 自检 CLI：python completeness_guard.py --config <path> --date 2026-10-06
# ---------------------------------------------------------------------------
def self_test():
    import argparse
    import datetime
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

    if a.date:
        d = datetime.datetime.strptime(a.date, '%Y-%m-%d').date()
    else:
        d = datetime.date.today()

    files = list(a.file)
    if not files:
        root = cfg.get('paths', {}).get('local_root') or r'D:\录播\25788785-岁己SUI'
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
    for k in ('checked', 'suspect', 'local_sec', 'rec_sec',
              'rec_sess', 'rec_segs', 'ratio', 'reason'):
        print('  %-10s = %s' % (k, r[k]))
    return 0 if (r['checked'] and not r['suspect']) else 1


if __name__ == '__main__':
    sys.exit(self_test())