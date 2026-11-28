#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""source_decider.py —— 取源决策器（固化用户规则 2026-11-24）

用户规则（原话）：「如果本地没有或者本地缺少，或者是本地只有低画质版本，
马上去录播站下载完整版本，然后进行合并总结。」

三条 DOWNLOAD 判据（任一命中即下 rec 站完整版）：
  1. no_local_source —— 本地该日期无任何源 FLV/MKV/MOV（MP4 均为交付件，一律排除）
  2. local_too_short —— 本地合并时长 < rec 站同场跨度 - max_diff_min（默认 5 分钟）
  3. local_low_quality —— 本地平均码率 < rec 站同场平均码率 * quality_ratio（默认 0.6）

否则 LOCAL：用本地 FLV 按时间升序合并总结（缺开播前几分钟可接受）。

用法：
  python source_decider.py --config <cfg> --date YYYY-MM-DD          # 输出人类可读判定
  python source_decider.py --config <cfg> --date YYYY-MM-DD --json    # 输出 JSON（供编排）
  python source_decider.py --self-test                               # 三分支假数据自测

设计原则：rec 站不可达/未配置 → 只要有本地源就判 LOCAL，不阻塞；
完全无本地源且 rec 站也查不到 → 判 UNKNOWN（交上层报告，不静默空跑）。
"""
import os
import sys
import re
import json
import glob
import argparse
import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# ★ 2026-11-29：MP4 = 无损合并/转封装的交付件（文件名不再带 _merged），
#    绝不能当源片段；源只认 FLV/MKV/MOV/TS/M4V。
SOURCE_EXT = ('.flv', '.mkv', '.mov', '.ts', '.m4v')
DEFAULT_MAX_DIFF_MIN = 5.0
DEFAULT_QUALITY_RATIO = 0.6


def _lognoop(m, lv='INFO'):
    pass


def _is_source_file(path):
    """源片段：媒体扩展名，且去掉所有尾缀后不以 _merged/_compressed 结尾。"""
    low = path.lower()
    if not low.endswith(SOURCE_EXT):
        return False
    core = low
    while core.endswith(SOURCE_EXT):
        core = core[:core.rfind('.')]
    if core.endswith('_merged') or core.endswith('_compressed'):
        return False
    return True


def _date_tokens(ymd):
    """目标日期的本地文件名日期 token：当日 + 次日（跨午夜尾巴归开播日）。"""
    d = datetime.datetime.strptime(ymd, '%Y-%m-%d').date()
    nxt = d + datetime.timedelta(days=1)
    return d.strftime('%Y%m%d'), nxt.strftime('%Y%m%d')


def scan_local(cfg, ymd, log=_lognoop):
    """实时扫描本地录播根目录，返回该日期的源片段（按文件名升序）。"""
    root = (cfg.get('local') or {}).get('root')
    if not root or not os.path.isdir(root):
        return []
    today, nxt = _date_tokens(ymd)
    out = []
    for p in glob.glob(os.path.join(root, '*')):
        if not os.path.isfile(p) or not _is_source_file(p):
            continue
        base = os.path.basename(p)
        m = re.search(r'(20\d{6})[-_](\d{6})', base)
        if not m:
            continue
        tok = m.group(1)
        hh = int(m.group(2)[:2])
        # 当日全天 + 次日 12:00 前（跨午夜尾巴归开播日）
        if tok == today or (tok == nxt and hh < 12):
            out.append(p)
    out.sort(key=lambda x: os.path.basename(x))
    return out


def probe_local(cfg, files, log=_lognoop):
    """ffprobe 本地片段：合计时长/体积/最大分辨率。"""
    ffprobe = (cfg.get('paths') or {}).get('ffprobe') or r'D:\ffmpeg\bin\ffprobe.exe'
    import subprocess
    total_dur = 0.0
    total_bytes = 0
    max_pixels = 0
    resolution = None
    for p in files:
        try:
            total_bytes += os.path.getsize(p)
        except OSError:
            pass
        try:
            r = subprocess.run(
                [ffprobe, '-v', 'error', '-select_streams', 'v:0',
                 '-show_entries', 'stream=width,height',
                 '-show_entries', 'format=duration',
                 '-of', 'json', p],
                capture_output=True, text=True, timeout=90)
            d = json.loads(r.stdout or '{}')
            dur = float((d.get('format') or {}).get('duration') or 0)
            total_dur += dur
            for st in d.get('streams') or []:
                w, h = int(st.get('width') or 0), int(st.get('height') or 0)
                if w * h > max_pixels:
                    max_pixels = w * h
                    resolution = '%dx%d' % (w, h)
        except Exception as e:
            log('  [decider] ffprobe 跳过 %s：%s' % (os.path.basename(p), e), 'WARN')
    return {'duration': total_dur, 'bytes': total_bytes,
            'resolution': resolution, 'n': len(files)}


def _size_to_bytes(f):
    """把 rec 站文件条目体积换算成字节，兼容 size_gb 数值与 '639.2MB' 带单位字符串。"""
    sg = f.get('size_gb')
    if sg not in (None, ''):
        try:
            return float(sg) * 1024 ** 3
        except (TypeError, ValueError):
            pass
    s = str(f.get('size') or '').strip().upper().replace(',', '')
    m = re.match(r'^([\d.]+)\s*(GB|MB|KB|B)?$', s)
    if m:
        num = float(m.group(1))
        unit = m.group(2) or 'MB'
        return num * {'GB': 1024 ** 3, 'MB': 1024 ** 2,
                      'KB': 1024, 'B': 1}[unit]
    return 0.0


def rec_span_and_bytes(cfg, ymd, log=_lognoop):
    """查 rec 站该日期场次：合计跨度（秒）+ 合计体积（字节）。不可达返回 None。"""
    try:
        import completeness_guard as CG
        import darkpy_rec as D
        sessions = CG._rec_sessions_for_date(cfg, ymd, log)
        if sessions is None:
            return None
        files = D.fetch_index(cfg, log)
        if not files:
            return None
        # 归属 ymd 的所有 rec 文件（sessions 已按场次归组，跨午夜归开播日）
        used = set()
        span = 0.0
        nbytes = 0
        for sess in sessions:
            sp = CG._session_span_sec(sess)
            if sp:
                span += sp
            for f in sess:
                key = f.get('name')
                if key and key not in used:
                    used.add(key)
                    nbytes += _size_to_bytes(f)
        return {'span': span, 'bytes': nbytes, 'n_sessions': len(sessions)}
    except Exception as e:
        log('  [decider] rec 站查询异常：%s' % e, 'WARN')
        return None


def decide(cfg, ymd, log=_lognoop, max_diff_min=DEFAULT_MAX_DIFF_MIN,
           quality_ratio=DEFAULT_QUALITY_RATIO):
    """核心判定。返回 dict：action ∈ {LOCAL, DOWNLOAD, UNKNOWN}。"""
    local_files = scan_local(cfg, ymd, log)
    evidence = {'date': ymd, 'local_files': [os.path.basename(p) for p in local_files]}

    if not local_files:
        rec = rec_span_and_bytes(cfg, ymd, log)
        evidence['rec'] = rec
        if rec and rec.get('span', 0) > 0:
            return _res('DOWNLOAD', 'no_local_source',
                        '本地该日期无源片段，rec 站有完整场次（%d 场）' % rec['n_sessions'],
                        evidence)
        return _res('UNKNOWN', 'no_source_anywhere',
                    '本地无源且 rec 站未查到场次（可能停播或站点不可达）', evidence)

    loc = probe_local(cfg, local_files, log)
    evidence['local'] = loc
    rec = rec_span_and_bytes(cfg, ymd, log)
    evidence['rec'] = rec

    if not rec or not rec.get('span'):
        # rec 不可达：不阻塞，用本地（但仍记录无法做画质/缺时长比对）
        return _res('LOCAL', 'rec_unavailable_use_local',
                    'rec 站不可达/无场次，本地有源（%.1f 分钟）→ 用本地'
                    % (loc['duration'] / 60), evidence)

    diff_min = (rec['span'] - loc['duration']) / 60.0
    evidence['diff_min'] = round(diff_min, 2)
    if diff_min > max_diff_min:
        return _res('DOWNLOAD', 'local_too_short',
                    '本地 %.1f 分钟 vs rec 站 %.1f 分钟，缺 %.1f 分钟 > %.0f 分钟阈值'
                    % (loc['duration'] / 60, rec['span'] / 60, diff_min, max_diff_min),
                    evidence)

    # 码率比对（同场次同跨度，体积比 ≈ 码率比），判低画质
    if loc['duration'] > 0 and rec['span'] > 0 and loc['bytes'] > 0 and rec['bytes'] > 0:
        loc_br = loc['bytes'] * 8 / loc['duration']
        rec_br = rec['bytes'] * 8 / rec['span']
        ratio = loc_br / rec_br if rec_br else 1.0
        evidence['bitrate_ratio'] = round(ratio, 3)
        evidence['local_kbps'] = int(loc_br / 1000)
        evidence['rec_kbps'] = int(rec_br / 1000)
        if ratio < quality_ratio:
            return _res('DOWNLOAD', 'local_low_quality',
                        '本地平均码率 %.0f kbps < rec 站 %.0f kbps × %.1f（比值 %.2f）→ 本地疑似低画质'
                        % (loc_br / 1000, rec_br / 1000, quality_ratio, ratio), evidence)

    return _res('LOCAL', 'local_ok',
                '本地 %.1f 分钟（%s），与 rec 站 %.1f 分钟差 %.1f 分钟 ≤ %.0f 分钟，画质达标 → 用本地'
                % (loc['duration'] / 60, loc['resolution'] or '?', rec['span'] / 60,
                   diff_min, max_diff_min), evidence)


def _res(action, reason, detail, evidence):
    return {'action': action, 'reason': reason, 'detail': detail, 'evidence': evidence}


# --------------------------------------------------------------------------- 自测
def self_test():
    ok = 0
    fail = []

    def chk(name, cond):
        nonlocal ok
        if cond:
            ok += 1
        else:
            fail.append(name)

    # 分支1：本地无源 + rec 有 → DOWNLOAD
    r = _res('DOWNLOAD', 'no_local_source', 'x', {})
    chk('无本地源判 DOWNLOAD', r['action'] == 'DOWNLOAD')

    # 分支2：缺时长 > 5min → DOWNLOAD（直接算 diff）
    diff_min = (9000 - 8500) / 60.0
    chk('缺 8.3min>5min 判 DOWNLOAD', diff_min > DEFAULT_MAX_DIFF_MIN)

    # 分支2b：缺 1.89min ≤ 5min → 不判短
    diff_min2 = (17513 - 17399) / 60.0
    chk('缺 1.89min≤5min 不判短', diff_min2 <= DEFAULT_MAX_DIFF_MIN)

    # 分支3：码率比 < 0.6 → DOWNLOAD
    loc_br = 500 * 1000
    rec_br = 1000 * 1000
    chk('码率比 0.5<0.6 判低画质', (loc_br / rec_br) < DEFAULT_QUALITY_RATIO)
    chk('码率比 0.9 不判低画质', (900 * 1000 / rec_br) >= DEFAULT_QUALITY_RATIO)

    # 源片段过滤
    chk('排除 _merged', not _is_source_file('x_merged.mp4'))
    chk('排除 _compressed', not _is_source_file('x_compressed.mp4'))
    chk('保留普通 flv', _is_source_file('录制-25788785-20261119-201653-985-标题.flv'))
    chk('排除普通 mp4 交付件', not _is_source_file('录制-25788785-20261128-202613-963-标题.mp4'))

    # 跨午夜 token
    a, b = _date_tokens('2026-11-19')
    chk('日期 token 当日', a == '20261119')
    chk('日期 token 次日', b == '20261120')

    print('[source_decider] 自测 %d 项，失败 %d 项' % (ok, len(fail)))
    for f in fail:
        print('  FAIL:', f)
    return 0 if not fail else 1


def main():
    ap = argparse.ArgumentParser(description='取源决策器（本地/录播站）')
    ap.add_argument('--config')
    ap.add_argument('--date')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--max-diff-min', type=float, default=DEFAULT_MAX_DIFF_MIN)
    ap.add_argument('--quality-ratio', type=float, default=DEFAULT_QUALITY_RATIO)
    ap.add_argument('--self-test', action='store_true')
    args = ap.parse_args()

    if args.self_test:
        return self_test()

    from common import load_config
    cfg = load_config(args.config) if args.config else {}
    ymd = args.date
    if not ymd:
        ymd = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    r = decide(cfg, ymd, max_diff_min=args.max_diff_min,
               quality_ratio=args.quality_ratio)
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print('日期: %s' % r['evidence']['date'])
        print('决策: %s  (%s)' % (r['action'], r['reason']))
        print('依据: %s' % r['detail'])
        print('本地片段数: %d' % len(r['evidence'].get('local_files', [])))
    # action 码：0=LOCAL, 2=DOWNLOAD, 3=UNKNOWN（方便编排脚本判断）
    return {'LOCAL': 0, 'DOWNLOAD': 2, 'UNKNOWN': 3}[r['action']]


if __name__ == '__main__':
    sys.exit(main())
