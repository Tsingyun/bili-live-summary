#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""auto_upload_daily.py —— 每日录播自动上传（每天 06:00）

定位
----
把「录播根目录/YYYY-MM-DD/ 里的 *_compressed.mp4 压缩成品」按直播日期自动投稿到
B站小号（复用 bili_upload.py 的多P投稿链路，2026-09-25 中秋场 BV1DEat61EgQ 实测通过）。

由两个入口触发（互为安全网，靠单实例锁防并发）：
  · Windows 计划任务 BiliUpload_Daily（06:00 起 /ri 60 /du 04:00 → 06/07/08/09 点共 4 次，
    不在 agent 进程树里，宿主回收杀不掉）；
  · WorkBuddy 自动化（每天 06:00，锁感知：计划任务在跑则只汇报不重复执行）。

核心保证
--------
  · 按日期定位：live_date 解析自文件名（录制-UID-YYYYMMDD-HHMMSS-序号-标题），
    绝不用日历日期猜场次（跨 0 点尾段仍归开播日）。
  · 幂等防重传：独立 state（upload_auto_state.json，按文件 stem 记 BV）；
    另自动收编 state.json /uploads/<date> 里已有的 bvid（如手动传过的 925）。
  · 超 7.5GB 自动 ffmpeg 流复制分P（无损、不重编码、按关键帧切），分P 标题 P1/P2/...。
  · 每步校验：分P 后合计时长 ≥ 源 98% 且单分P 仍低于上限；上传后必须拿到 BV 才算成功。
  · 失败自动重试（默认 3 次/项），失败原因写日志与摘要。
  · 收尾输出当天执行摘要：成功项（文件→BV）/ 失败项及原因 / 跳过项及原因，退出码 0/1。

用法
----
  python auto_upload_daily.py                    # 正常执行（计划任务/自动化调用）
  python auto_upload_daily.py --dry-run          # 只发现+打印计划，不上传
  python auto_upload_daily.py --report           # 只汇报当前状态与最近上传记录
  python auto_upload_daily.py --date 2026-09-27  # 只处理指定 live_date
  python auto_upload_daily.py --since 2026-09-26 # 覆盖 config 的 upload.auto_since
"""
import argparse
import datetime
import glob
import json
import math
import os
import re
import subprocess
import sys
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
import common  # noqa: E402

DEFAULT_CONFIG = r"D:\岁己直播自动总结\config.json"
LOCK_NAME = '.auto_upload.lock'
HB_NAME = '.auto_upload.heartbeat'
HEARTBEAT_STALE_SEC = 1200
GB = 1024 * 1024 * 1024
NAME_RE = re.compile(r'^录制-\d+-(?P<date>\d{8})-\d{6}-\d+-(?P<title>.+)$')
BV_RE = re.compile(r'BV[0-9A-Za-z]{10}')

# upload.auto_* 配置缺省值（config.json 同名键可覆盖）
DEF_AUTO = {
    'auto_retries': 3,
    'auto_retry_delay_sec': 120,
    'auto_max_single_gb': 7.5,
    'auto_part_target_gb': 6.5,
    'auto_min_age_min': 15,
}


# ---------- 日志（专用 upload_auto_YYYY-MM-DD.log + stdout 安全输出） ----------
class Ulog:
    def __init__(self, logs_dir):
        self.file = os.path.join(
            logs_dir, 'upload_auto_' + datetime.date.today().isoformat() + '.log')

    def __call__(self, msg, level='INFO'):
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        line = '[%s] [%s] %s' % (ts, level, msg)
        try:
            sys.stdout.buffer.write((line + '\n').encode('utf-8', 'replace'))
            sys.stdout.buffer.flush()
        except Exception:
            pass
        try:
            with open(self.file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def error(self, msg):
        self(msg, 'ERROR')
        try:
            with open(os.path.join(os.path.dirname(self.file), 'errors.log'),
                      'a', encoding='utf-8') as f:
                f.write('[%s] %s\n' % (datetime.datetime.now().isoformat(timespec='seconds'), msg))
        except Exception:
            pass


# ---------- 单实例锁（双条件判活：PID 存在 且 心跳新鲜；复用 common 的判活原语） ----------
def _lock_files(workdir):
    return (os.path.join(workdir, LOCK_NAME), os.path.join(workdir, HB_NAME))


def lock_held(workdir):
    lp, hb = _lock_files(workdir)
    if not os.path.exists(lp):
        return False
    pid = common.read_lock_pid(lp)
    if not pid:
        try:
            return (time.time() - os.path.getmtime(lp)) < HEARTBEAT_STALE_SEC
        except OSError:
            return False
    if not common.pid_alive(pid):
        return False
    age = common.heartbeat_age(hb)
    return age is not None and age < HEARTBEAT_STALE_SEC


def lock_acquire(workdir):
    lp, hb = _lock_files(workdir)
    with open(lp, 'w', encoding='utf-8') as f:
        f.write('%d %d\n' % (os.getpid(), int(time.time())))
    common.atomic_write_text(hb, '%d %d' % (os.getpid(), int(time.time())))


def lock_release(workdir):
    lp, hb = _lock_files(workdir)
    for p in (lp, hb):
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


class Heartbeat:
    """后台线程每 60s 续心跳——上传一个分P可达半小时+，必须在子进程运行期间保活。"""

    def __init__(self, workdir):
        self._hb = _lock_files(workdir)[1]
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self._stop.wait(60):
            try:
                common.atomic_write_text(
                    self._hb, '%d %d' % (os.getpid(), int(time.time())))
            except Exception:
                pass

    def __enter__(self):
        self._t.start()
        return self

    def __exit__(self, *a):
        self._stop.set()


# ---------- 状态 ----------
def _state_path(cfg):
    return os.path.join(cfg['_dirs']['root'], 'upload_auto_state.json')


def load_auto_state(cfg):
    try:
        with open(_state_path(cfg), encoding='utf-8') as f:
            return json.load(f) or {}
    except Exception:
        return {'uploaded': {}}


def save_auto_state(cfg, st, log):
    st['updated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
    try:
        common.atomic_write_json(_state_path(cfg), st, log)
    except Exception as e:
        log('写 upload_auto_state.json 失败（不影响已发布）：%s' % e, 'WARN')


# ---------- 发现 ----------
def discover_items(cfg, log):
    """扫描 根目录 + YYYY-MM-DD 子目录 的 *_compressed.mp4（压缩成品），解析 live_date/标题。"""
    root = (cfg.get('local') or {}).get('root') or ''
    if not root or not os.path.isdir(root):
        log('录播根目录不存在：%s' % root, 'ERROR')
        return []
    scan = [(root, '')]
    for name in sorted(os.listdir(root)):
        sub = os.path.join(root, name)
        if os.path.isdir(sub) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', name):
            scan.append((sub, name))
    items, seen = [], set()
    for d, sub_name in scan:
        for fp in sorted(glob.glob(os.path.join(d, '*.mp4'))):
            b = os.path.basename(fp)
            if b.startswith('~$') or '.part' in b or '.bak' in b:
                continue
            if not (b.endswith('_compressed.mp4') or b.endswith('_merged_compressed.mp4')):
                continue  # 只上传压缩成品；B站兜底下载的原样 MP4 本来就在B站，不重传
            stem_full = b[:-len('.mp4')]
            stem = re.sub(r'_(merged_)?compressed$', '', stem_full)
            if stem in seen:
                continue
            seen.add(stem)
            m = NAME_RE.match(stem)
            if m:
                try:
                    ld = datetime.datetime.strptime(m.group('date'), '%Y%m%d').date()
                except ValueError:
                    ld = datetime.date.fromtimestamp(os.path.getmtime(fp))
                title = m.group('title').strip()
            else:
                ld = datetime.date.fromtimestamp(os.path.getmtime(fp))
                title = stem
                log('文件名无法解析直播日期，回退 mtime：%s' % b, 'WARN')
            items.append({
                'path': fp, 'stem': stem, 'live_date': ld, 'title': title,
                'size': os.path.getsize(fp), 'mtime': os.path.getmtime(fp),
            })
    items.sort(key=lambda x: (x['live_date'], x['stem']))
    return items


def adopt_existing_uploads(cfg, items, st, log):
    """收编 state.json /uploads/<date> 里已有的 bvid（如手动传过的 925），
    仅当该 live_date 恰好只发现一个成品时才收编，避免多场同日误标。"""
    try:
        wd = json.load(open(common.state_path(cfg), encoding='utf-8'))
    except Exception:
        return
    ups = wd.get('uploads') or {}
    by_date = {}
    for it in items:
        by_date.setdefault(it['live_date'].isoformat(), []).append(it)
    for d, lst in by_date.items():
        bv = (ups.get(d) or {}).get('bvid')
        if bv and len(lst) == 1 and lst[0]['stem'] not in st.get('uploaded', {}):
            it = lst[0]
            st.setdefault('uploaded', {})[it['stem']] = {
                'bvid': bv, 'ts': int(time.time()), 'live_date': d,
                'title': full_title(it), 'adopted': True,
            }
            log('收编已有上传记录：%s → %s（state.json /uploads/%s）' % (it['stem'], bv, d))


def full_title(it):
    """标题规范（用户 2026-09-27 拍板）：日期 + 原标题 + 岁己SUI。"""
    return '%s %s 岁己SUI' % (it['live_date'].isoformat(), it['title'])


# ---------- 分P ----------
def split_parts(cfg, it, auto, log):
    """超过上限用 ffmpeg 流复制按关键帧切（无损、不重编码）。返回 (parts, tmpdir)。"""
    ffmpeg = (cfg.get('paths') or {}).get('ffmpeg') or 'ffmpeg'
    ffprobe = (cfg.get('paths') or {}).get('ffprobe') or 'ffprobe'
    size = it['size']
    n = max(2, math.ceil(size / (auto['auto_part_target_gb'] * GB)))
    dur = common.probe_duration(ffprobe, it['path'])
    if not dur or dur <= 0:
        raise RuntimeError('ffprobe 取时长失败，无法安全分P')
    times = ','.join('%.3f' % (dur * k / n) for k in range(1, n))
    tmp = os.path.join(cfg['_dirs']['root'], '_upload_auto',
                       re.sub(r'[^\w\-]', '_', it['stem'])[:80])
    os.makedirs(tmp, exist_ok=True)
    pat = os.path.join(tmp, 'part_%03d.mp4')
    log('[split] %.2fGB / 上限 %.1fGB → 切 %d 段（时长 %.0fs）'
        % (size / GB, auto['auto_max_single_gb'], n, dur))
    r = subprocess.run(
        [ffmpeg, '-y', '-i', it['path'], '-c', 'copy', '-map', '0',
         '-f', 'segment', '-segment_times', times, '-reset_timestamps', '1', pat],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError('ffmpeg 分段失败(rc=%d)：%s'
                           % (r.returncode, (r.stderr or b'')[-400:].decode('utf-8', 'replace')))
    parts = sorted(glob.glob(os.path.join(tmp, 'part_*.mp4')))
    if not parts:
        raise RuntimeError('ffmpeg 分段未产出任何文件')
    total, why = 0.0, ''
    for p in parts:
        sz = os.path.getsize(p)
        if sz < 1024 * 1024:
            why = '分段过小：%s（%d B）' % (os.path.basename(p), sz)
            break
        if sz > auto['auto_max_single_gb'] * GB * 0.98:
            why = '分段仍超限：%s（%.2fGB）' % (os.path.basename(p), sz / GB)
            break
        d = common.probe_duration(ffprobe, p) or 0.0
        total += d
    if not why and total < dur * 0.98:
        why = '分段时长合计 %.0fs < 源 %.0fs 的 98%%' % (total, dur)
    if why:
        raise RuntimeError('分P校验失败：%s' % why)
    log('[split] 校验通过：%d 段，合计 %.0fs（源 %.0fs）'
        % (len(parts), total, dur))
    return parts, tmp


def cleanup_tmp(tmp, log):
    if not tmp:
        return
    try:
        shutil_ignore_errors(tmp)
        log('[split] 已清理临时分P目录：%s' % tmp)
    except Exception as e:
        log('清理临时分P目录失败（不影响已发布）：%s' % e, 'WARN')


def shutil_ignore_errors(tmp):
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


# ---------- 上传 ----------
def build_child_env(cfg):
    env = dict(os.environ)
    proxy = ((cfg.get('upload') or {}).get('proxy')
             or (cfg.get('summarize') or {}).get('proxy')
             or 'http://127.0.0.1:7897')
    env['HTTP_PROXY'] = env['HTTPS_PROXY'] = proxy
    return env


def upload_once(cfg, cfg_path, it, parts, log):
    """跑一次 bili_upload.py 多P投稿，返回 (bvid 或 None, 输出尾部)。"""
    cmd = [sys.executable, os.path.join(SCRIPT_DIR, 'bili_upload.py'),
           '--src', *parts,
           '--date', it['live_date'].isoformat(),
           '--title', full_title(it),
           '--part-title', *['P%d' % (i + 1) for i in range(len(parts))],
           '--force', '--config', cfg_path]
    log('[upload] %s' % ' '.join(
        ['bili_upload.py', '--src %d文件' % len(parts), '--date', it['live_date'].isoformat(),
         '--title', full_title(it), '--force']))
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       env=build_child_env(cfg))
    out = (r.stdout or b'').decode('utf-8', 'replace')
    for line in out.strip().splitlines()[-8:]:
        log('  | %s' % line)
    m = None
    for m in BV_RE.finditer(out):
        pass
    bvid = m.group(0) if m else None
    # 双重校验：stdout 有 BV 且工作目录 state.json /uploads/<date> 已记录
    try:
        wd = json.load(open(common.state_path(cfg), encoding='utf-8'))
        wd_bv = (wd.get('uploads') or {}).get(it['live_date'].isoformat(), {}).get('bvid')
    except Exception:
        wd_bv = None
    if bvid and wd_bv and wd_bv != bvid:
        log('stdout BV(%s) 与 state BV(%s) 不一致，以 state 为准' % (bvid, wd_bv), 'WARN')
        bvid = wd_bv
    if not bvid and wd_bv:
        bvid = wd_bv
    tail = '\n'.join(out.strip().splitlines()[-12:])
    return bvid, (tail if r.returncode != 0 or not bvid else '')


def _is_hard_fail(err):
    """判断这次失败是否**不该重试**（重试只会更糟）。

    ★ 2026-10-06 实测：同一份 cookie 连续 3 次撞上
      `member.bilibili.com/preupload` 的 HTTP 403 之后，
      `space/myinfo` 就变成 `code=-101 账号未登录` —— **SESSDATA 被风控作废**。
      所以 4xx（尤其 403）与 cookie 失效类错误必须**立即停止**，
      换成新 cookie 再来，而不是用同一份硬扛 `auto_retries`。

    可重试的：网络抖动、5xx、429、超时、分片级失败（内部已有自己的重试）。
    """
    if not err:
        return False
    e = err.lower()
    # cookie 失效 / 账号未登录
    if 'code=-101' in e or '账号未登录' in e or 'cookie 无效' in e or 'cookie 校验失败' in e:
        return True
    # 明确的 4xx（不可重试）：403 风控 / 404 / 401 等
    for code in ('http 400', 'http 401', 'http 403', 'http 404', 'http 409'):
        if code in e:
            return True
    return False


def process_item(cfg, cfg_path, it, auto, log):
    """带重试地上传一个成品。返回 (bvid 或 None, 原因)。"""
    parts, tmp = [it['path']], None
    try:
        if it['size'] > auto['auto_max_single_gb'] * GB:
            parts, tmp = split_parts(cfg, it, auto, log)
        last_err = ''
        for attempt in range(1, auto['auto_retries'] + 1):
            log('[attempt %d/%d] %s' % (attempt, auto['auto_retries'], it['stem']))
            try:
                bvid, err = upload_once(cfg, cfg_path, it, parts, log)
            except Exception as e:
                bvid, err = None, '子进程异常：%s' % e
            if bvid:
                log('✅ 上传成功：%s → %s' % (it['stem'], bvid))
                return bvid, ''
            last_err = err or '未拿到 BV（输出无 BV 且 state 未记录）'
            log('❌ 第 %d 次上传失败：%s' % (attempt, last_err), 'WARN')
            # 403 / cookie 失效：再试只会把这份 cookie 也弄废，立刻停
            if _is_hard_fail(last_err):
                log('⛔ 硬失败（4xx 或 cookie 失效），不再重试 —— '
                    '继续用同一份 cookie 重试会触发 B站风控把它作废；'
                    '请换一份新 cookie 后再跑', 'WARN')
                return None, last_err
            if attempt < auto['auto_retries']:
                time.sleep(auto['auto_retry_delay_sec'])
        return None, last_err
    finally:
        if tmp:
            cleanup_tmp(tmp, log)


# ---------- 摘要 ----------
def print_summary(log, run_date, ok_list, fail_list, skip_list, extra=''):
    lines = ['=' * 60, '====== 每日自动上传摘要 %s ======' % run_date,
             '成功 %d 项' % len(ok_list)]
    for stem, bv in ok_list:
        lines.append('  ✅ %s → %s' % (stem, bv))
    lines.append('失败 %d 项' % len(fail_list))
    for stem, why in fail_list:
        lines.append('  ❌ %s\n      原因：%s' % (stem, why))
    lines.append('跳过 %d 项' % len(skip_list))
    for stem, why in skip_list:
        lines.append('  ⏭ %s（%s）' % (stem, why))
    if extra:
        lines.append(extra)
    lines.append('=' * 60)
    for ln in lines:
        log(ln)


# ---------- 汇报模式 ----------
def do_report(cfg, log, items, st):
    held = lock_held(cfg['_dirs']['root'])
    log('—— 自动上传状态汇报 ——')
    log('单实例锁：%s' % ('被存活实例持有（上传进行中）' if held else '空闲'))
    ups = st.get('uploaded') or {}
    log('累计已上传 %d 项（最近 10 条）:' % len(ups))
    for stem, rec in sorted(ups.items(), key=lambda kv: -(kv[1].get('ts') or 0))[:10]:
        log('  %s → %s%s' % (stem, rec.get('bvid'),
                             '（收编）' if rec.get('adopted') else ''))
    pend = [it for it in items if it['stem'] not in ups]
    log('当前待上传 %d 项:' % len(pend))
    for it in pend:
        log('  %s（%.2fGB，live_date=%s）' % (it['stem'], it['size'] / GB,
                                             it['live_date'].isoformat()))


# ---------- 主流程 ----------
def main():
    ap = argparse.ArgumentParser(description='每日录播自动上传（06:00）')
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    ap.add_argument('--dry-run', action='store_true', help='只发现+打印计划，不上传')
    ap.add_argument('--report', action='store_true', help='只汇报状态，不执行')
    ap.add_argument('--date', default='', help='只处理该 live_date（ISO）')
    ap.add_argument('--since', default='', help='只处理 live_date >= 该日期（覆盖 config）')
    args = ap.parse_args()

    cfg = common.load_config(args.config)
    log = Ulog(cfg['_dirs']['logs'])
    run_date = datetime.date.today().isoformat()

    uc = cfg.get('upload') or {}
    auto = {**DEF_AUTO, **{k: uc[k] for k in DEF_AUTO if uc.get(k) is not None}}
    since = args.since or uc.get('auto_since') or run_date

    items = discover_items(cfg, log)
    st = load_auto_state(cfg)
    adopt_existing_uploads(cfg, items, st, log)

    if args.report:
        do_report(cfg, log, items, st)
        return 0

    if not uc.get('auto_enabled', True):
        log('upload.auto_enabled=false，自动上传未启用，退出。')
        return 0

    # 过滤
    plan, skip = [], []
    now = time.time()
    for it in items:
        d_iso = it['live_date'].isoformat()
        if it['stem'] in (st.get('uploaded') or {}):
            skip.append((it['stem'], '已上传 %s' % st['uploaded'][it['stem']].get('bvid')))
        elif args.date and d_iso != args.date:
            skip.append((it['stem'], '不在 --date %s' % args.date))
        elif d_iso < since:
            skip.append((it['stem'], 'live_date 早于 auto_since=%s' % since))
        elif (now - it['mtime']) < auto['auto_min_age_min'] * 60:
            skip.append((it['stem'], '文件太新（<%d 分钟），疑似仍在写' % auto['auto_min_age_min']))
        else:
            plan.append(it)

    if args.dry_run:
        log('—— dry-run 计划（共 %d 项待上传，%d 项跳过）——' % (len(plan), len(skip)))
        for it in plan:
            log('  将上传：%s（%.2fGB，live_date=%s）→《%s》'
                % (it['stem'], it['size'] / GB, it['live_date'].isoformat(), full_title(it)))
        for stem, why in skip:
            log('  跳过：%s（%s）' % (stem, why))
        return 0

    if not plan:
        print_summary(log, run_date, [], [], skip, '无待上传项。')
        return 0

    workdir = cfg['_dirs']['root']
    if lock_held(workdir):
        log('已有存活的自动上传实例在跑（计划任务或上一次触发），本次退出不重复执行。'
            '可用 --report 查看进度。', 'WARN')
        do_report(cfg, log, items, st)
        return 0
    lock_acquire(workdir)
    try:
        with Heartbeat(workdir):
            ok_list, fail_list = [], []
            for it in plan:
                bvid, why = process_item(cfg, args.config, it, auto, log)
                if bvid:
                    st.setdefault('uploaded', {})[it['stem']] = {
                        'bvid': bvid, 'ts': int(time.time()),
                        'live_date': it['live_date'].isoformat(), 'title': full_title(it),
                    }
                    save_auto_state(cfg, st, log)
                    ok_list.append((it['stem'], bvid))
                else:
                    fail_list.append((it['stem'], why))
            print_summary(log, run_date, ok_list, fail_list, skip)
            return 0 if not fail_list else 1
    finally:
        lock_release(workdir)


if __name__ == '__main__':
    sys.exit(main())
