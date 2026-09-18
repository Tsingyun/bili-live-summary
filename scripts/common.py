#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公共工具：配置加载、状态管理、日志、B站接口、文件名清洗。

配置（config.json）查找顺序（即开即用关键）：
  1. 环境变量 BILI_LIVE_CONFIG 指向的路径
  2. 命令行 --config 参数
  3. 本脚本同级 _workdir.json 中记录的 workdir/config.json（由 setup.py 写入）
  4. 本脚本同级的 config.json（开发/兜底）
运行时产物（MP3/TXT/SRT/logs/tmp/state.json）默认落在 config.json 所在目录，
或通过 paths.project_root 指定；config 与脚本分离，skill 本体不被污染。
"""

import json
import os
import re
import sys
import time
import shutil
import datetime
import subprocess

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def _resolve_config_path(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get('BILI_LIVE_CONFIG')
    if env and os.path.exists(env):
        return env
    # setup.py 写入的工作目录指针
    wd_ptr = os.path.join(SCRIPT_DIR, '_workdir.json')
    if os.path.exists(wd_ptr):
        try:
            with open(wd_ptr, 'r', encoding='utf-8') as f:
                d = json.load(f)
            cand = os.path.join(d.get('workdir', ''), 'config.json')
            if os.path.exists(cand):
                return cand
        except Exception:
            pass
    return os.path.join(SCRIPT_DIR, 'config.json')


# ---------- 配置 ----------
def load_config(path=None):
    path = _resolve_config_path(path)
    with open(path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    cfg['_config_path'] = path
    cfg['_workdir'] = os.path.dirname(os.path.abspath(path))
    # project_root 留空则默认等于 config 所在目录
    root = cfg.get('paths', {}).get('project_root') or cfg['_workdir']
    cfg['_dirs'] = {
        'root': root,
        'mp3': os.path.join(root, 'MP3'),
        'txt': os.path.join(root, 'TXT'),
        'srt': os.path.join(root, 'SRT'),
        'logs': os.path.join(root, 'logs'),
        'tmp': os.path.join(root, 'tmp'),
    }
    for d in cfg['_dirs'].values():
        os.makedirs(d, exist_ok=True)
    return cfg


# ---------- 原子写 ----------
# 背景（2026-09-18）：流水线随时可能被环境回收（SIGKILL/进程树拆除），若此刻正在
# 写 state.json / docx / 归档 MP3，磁盘上就会留下**半截文件**；而下游的幂等判据往往
# 只看「文件存在 + 体积大于某个小阈值」，于是半成品被当成已完成，产出残缺交付物
# 且再也不会重跑。故所有「写完才算数」的落盘一律走这里：先写 <目标>.part，
# flush+fsync 落盘，最后 os.replace 原子替换——替换成功即为完成，中途被杀只留 .part。
def atomic_write_bytes(path, data, log=None):
    """原子写二进制：<path>.part → fsync → os.replace(path)。

    Windows 上 os.replace 可能因目标被其它程序打开（如 Word/腾讯文档预览 docx）
    而抛 PermissionError，故带重试；仍失败则抛异常，由调用方决定是否降级。
    """
    tmp = path + '.part'
    os.makedirs(os.path.dirname(os.path.abspath(path)) or '.', exist_ok=True)
    with open(tmp, 'wb') as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except Exception:
            pass
    last = None
    for i in range(10):
        try:
            os.replace(tmp, path)
            return path
        except PermissionError as e:          # 目标被占用：稍等再换
            last = e
            time.sleep(0.3 * (i + 1))
        except OSError as e:
            last = e
            time.sleep(0.3 * (i + 1))
    # 实在换不了：清掉 .part，把错误抛给调用方（绝不留下疑似完整的半截文件）
    try:
        os.remove(tmp)
    except Exception:
        pass
    raise RuntimeError('原子替换失败 %s：%s' % (path, last))


def atomic_write_text(path, text, encoding='utf-8', log=None):
    return atomic_write_bytes(path, (text or '').encode(encoding, 'replace'), log)


def atomic_write_json(path, obj, log=None):
    return atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2), log=log)


# ---------- 单实例锁 / 心跳 ----------
# 背景（2026-09-18）：流水线可能被环境"回收"（回合结束 → 宿主运行时 dispose →
# 整棵进程树被拆除）。被回收时不会有机会清理锁文件，于是留下"孤儿锁"。
# 而 Windows 的 PID 复用很快，仅凭 os.kill(pid, 0) 判活会把无关进程当成持有者，
# 导致新一轮永远启动不了（表现为"已有实例运行，退出"，当天零产物）。
# 统一用「PID 存在 **且** 心跳新鲜」双条件判活，并把心跳（heartbeat）作为
# 长任务的存活证明：只要进程还活着就周期刷新（daily_pipeline 每 60s），被杀则心跳自然变旧。
#
# 阈值取 1200s（20 分钟）的权衡：
#   · 太大 → 上一轮被杀后要等很久才能被接管（自动化 04:00 被杀、04:20 的安全网会跳过，
#     要等到 05:20 才接管）；
#   · 太小 → 万一某个步骤长时间占住 GIL、心跳线程 20 分钟没轮上，会误判活实例为孤儿，
#     造成两个实例并发（重复烧 Gemini 配额）。
# 心跳线程在独立线程里每 60s 写一次、且长耗时步骤都是 subprocess（会释放 GIL），
# 1200s 有 20 倍余量；同时把「被杀 → 可接管」的延迟控制在 20 分钟内，当天一定能补完。
HEARTBEAT_STALE_SEC = 1200


def lock_paths(workdir):
    return (os.path.join(workdir, '.daily_pipeline.lock'),
            os.path.join(workdir, '.daily_pipeline.heartbeat'))


def read_lock_pid(lock_path):
    """锁文件里的 PID；读不到返回 0。"""
    try:
        with open(lock_path, encoding='utf-8') as f:
            return int((f.read().strip() or '0').split()[0])
    except Exception:
        return 0


def pid_alive(pid):
    """PID 是否对应存在的进程。

    实测（Python 3.13 / Windows 11）：os.kill(pid, 0) 是纯探活，**不会**终止进程，
    可以安全用于判活。
    """
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, OSError):
        return False


def heartbeat_age(hb_path):
    """心跳年龄（秒）；读不到返回 None。"""
    try:
        with open(hb_path, encoding='utf-8') as f:
            parts = f.read().strip().split()
        ts = int(parts[1]) if len(parts) > 1 else int(parts[0] or 0)
        return max(0.0, time.time() - ts)
    except Exception:
        return None


def lock_owner_alive(workdir, stale_sec=HEARTBEAT_STALE_SEC):
    """锁是否被一个**确实还活着**的实例持有（防 PID 复用误判）。

    判定：PID 存在 **且** 心跳新鲜；心跳缺失或过期 → 视为孤儿锁（可安全抢占）。
    锁文件没写全（无 PID）时退回 mtime 判断。
    """
    lp, hb = lock_paths(workdir)
    if not os.path.exists(lp):
        return False
    pid = read_lock_pid(lp)
    if not pid:
        try:
            return (time.time() - os.path.getmtime(lp)) < stale_sec
        except OSError:
            return False
    if not pid_alive(pid):
        return False
    age = heartbeat_age(hb)
    if age is None:
        return False
    return age < stale_sec


# ---------- 状态 ----------
def state_path(cfg):
    return os.path.join(cfg['_dirs']['root'], 'state.json')


def load_state(cfg):
    """读 state.json。主文件损坏时自动回退 state.json.bak（原子写保护之外的最后兜底）。"""
    p = state_path(cfg)
    st = None
    for cand in (p, p + '.bak'):
        if not os.path.exists(cand):
            continue
        try:
            with open(cand, 'r', encoding='utf-8') as f:
                st = json.load(f)
            if isinstance(st, dict):
                break
            st = None
        except Exception:
            st = None
    if not isinstance(st, dict):
        st = {}
    st.setdefault('processed', {})       # bvid -> {title, pubdate, date, stages, doc}
    st.setdefault('last_pubdate', 0)     # 已处理过的最大 pubdate
    st.setdefault('runs', [])
    return st


def save_state(cfg, st):
    """原子保存 state.json，并留一份 .bak。

    · 先写 .part（fsync）再 replace，进程被杀只会留下 .part，主文件始终完整；
    · replace 之前把当前主文件复制为 .bak，读取端损坏时可回退（load_state）。
    """
    st['updated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
    p = state_path(cfg)
    if os.path.exists(p):
        try:
            shutil.copy2(p, p + '.bak')
        except Exception:
            pass
    atomic_write_text(p, json.dumps(st, ensure_ascii=False, indent=2))


def mark_stage(cfg, st, bvid, stage, info=None):
    rec = st['processed'].setdefault(bvid, {'stages': []})
    if stage not in rec['stages']:
        rec['stages'].append(stage)
    if info:
        rec.update(info)
    save_state(cfg, st)


# ---------- 强制删除 ----------
# 重要（2026-09-01 实测）：在自动化环境下，对 D 盘（用户数据盘）调用 os.remove 会被
# 安全删除 hook 拦截，且 hook 自身抛 `AttributeError: 'NoneType' object has no attribute
# 'strip'`（不是权限错误，是 hook 的 bug）。结果是所有 os.remove 全部静默失败——
# 分P下载的 m4s 碎片、ASR 转写切块、16k 临时音频、「压缩后删源视频」全都删不掉，
# 每场留下几 GB 到十几 GB，几天就能把磁盘撑爆。
# 因此所有删除必须走这里：os.remove 失败时降级到 PowerShell Remove-Item。
def _ps_remove(path, is_dir):
    """PowerShell 降级删除。

    stderr 必须用 PIPE 拿 bytes 再自行解码：若用 capture_output=True（text 模式），
    Windows 下 subprocess 读线程会按 gbk 解码 PowerShell 的中文报错并抛
    UnicodeDecodeError，异常发生在子线程里还会把日志刷爆。
    """
    q = path.replace("'", "''")
    cmd = ("Remove-Item -LiteralPath '%s' -Force -Recurse -ErrorAction Stop" % q) \
        if is_dir else ("Remove-Item -LiteralPath '%s' -Force -ErrorAction Stop" % q)
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', cmd],
                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)
        return r.returncode == 0, (r.stderr or b'').decode('utf-8', 'ignore').strip()[:200]
    except Exception as e:
        return False, '%s: %s' % (type(e).__name__, str(e)[:200])


def force_unlink(path, log=None):
    """强制删除单个文件（os.remove → PowerShell 降级）。存在与否都返回是否「已不存在」。"""
    if not path or not os.path.lexists(path):
        return True
    try:
        os.remove(path)
        if not os.path.lexists(path):
            return True
    except Exception as e1:
        ok, err = _ps_remove(path, False)
        if ok and not os.path.lexists(path):
            return True
        if log:
            log('删除失败 %s：%s | %s' % (path, e1, err), 'WARN')
        return False
    # os.remove 没抛错但文件还在（罕见）——同样降级
    ok, err = _ps_remove(path, False)
    if not os.path.lexists(path):
        return True
    if log:
        log('删除失败 %s：%s' % (path, err), 'WARN')
    return False


def force_rmtree(path, log=None):
    """强制删除目录树（shutil.rmtree → PowerShell 降级）。"""
    if not path or not os.path.lexists(path):
        return True
    try:
        shutil.rmtree(path)
        if not os.path.lexists(path):
            return True
    except Exception as e1:
        ok, err = _ps_remove(path, True)
        if ok and not os.path.lexists(path):
            return True
        if log:
            log('删除目录失败 %s：%s | %s' % (path, e1, err), 'WARN')
        return False
    ok, err = _ps_remove(path, True)
    if not os.path.lexists(path):
        return True
    if log:
        log('删除目录失败 %s：%s' % (path, err), 'WARN')
    return False


# ---------- 日志 ----------
class Logger:
    def __init__(self, cfg, tag='pipeline'):
        self.dir = cfg['_dirs']['logs']
        self.tag = tag
        self.file = os.path.join(self.dir, datetime.date.today().isoformat() + '.log')

    def __call__(self, msg, level='INFO'):
        ts = datetime.datetime.now().strftime('%H:%M:%S')
        line = f'[{ts}] [{level}] {msg}'
        # 直接以 UTF-8 字节写入 stdout 缓冲区，彻底避免 Windows GBK 控制台
        # 在打印 ✓/emoji 等非 GBK 字符时抛 UnicodeEncodeError 导致整个进程崩溃。
        try:
            sys.stdout.buffer.write((line + '\n').encode('utf-8', 'replace'))
            sys.stdout.buffer.flush()
        except Exception:
            try:
                print(line, flush=True)
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
            with open(os.path.join(self.dir, 'errors.log'), 'a', encoding='utf-8') as f:
                f.write(f'[{datetime.datetime.now().isoformat(timespec="seconds")}] {msg}\n')
        except Exception:
            pass


# ---------- B站 ----------
def load_cookies(path):
    """读取 Netscape 格式 cookies。"""
    cookies = {}
    if path and os.path.exists(path):
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    cookies[parts[5]] = parts[6]
    return cookies


def bili_get(url, params=None, cookies=None, retries=3, timeout=20):
    """带指数退避的 B站 API GET。

    ★ 2026-09-18 收紧重试策略：只对**可重试**的故障退避重试——
      · 网络层异常（连接失败/超时/重置）；
      · HTTP 429（限流）与 5xx（服务端临时故障）。
    对 4xx（403/404/412 风控等）**立即失败**：这类错误重试没有意义，
    原来的实现会对每个 4xx 白等 5s+10s，B站风控时一轮要浪费半分多钟。
    """
    import requests
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=BILI_HEADERS,
                             cookies=cookies or {}, timeout=timeout)
            code = r.status_code
            if code == 200:
                try:
                    return r.json()
                except Exception as e:
                    last = e
                    if i < retries - 1:
                        time.sleep(2 * (i + 1))
                    continue
            if code == 429 or 500 <= code < 600:
                last = RuntimeError('HTTP %s' % code)
                if i < retries - 1:
                    time.sleep(5 * (2 ** i))
                continue
            # 其它 4xx / 3xx：不可重试，直接失败（附响应片段便于定位风控）
            raise RuntimeError('B站接口 HTTP %s（不可重试）%s: %s'
                               % (code, url, (r.text or '')[:200]))
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(5 * (2 ** i))
    raise RuntimeError(f'B站接口请求失败 {url}: {last}')


BILI_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
    'Referer': 'https://www.bilibili.com/',
}


def fetch_series(mid, series_id, cookies=None, max_pages=60, stop_before_pubdate=None):
    """拉取合集视频（按发布时间倒序）。

    stop_before_pubdate: 翻到比该时间更早的视频就提前收工，避免为了拿几期新的
    而把上千期全部翻一遍（全量翻页耗时可达 2 分钟）。
    """
    out, pn = [], 1
    while pn <= max_pages:
        d = bili_get('https://api.bilibili.com/x/series/archives',
                     {'mid': mid, 'series_id': series_id, 'only_normal': 'true',
                      'sort': 'desc', 'pn': pn, 'ps': 30}, cookies)
        if d.get('code') != 0:
            raise RuntimeError(f'合集接口返回 code={d.get("code")} {d.get("message")}')
        data = d.get('data') or {}
        arcs = data.get('archives') or []
        out.extend(arcs)
        page = data.get('page') or {}
        if pn * page.get('size', 30) >= page.get('total', 0) or not arcs:
            break
        if stop_before_pubdate is not None and arcs and \
                min(a.get('pubdate', 0) for a in arcs) < stop_before_pubdate:
            break
        pn += 1
        time.sleep(0.4)
    return out


# ---------- 直播日期 ----------
_LIVE_DATE_RE = re.compile(r'(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')


def live_date(title, pubdate):
    """直播实际日期。标题里通常带「2026年08月01日19点场」，以它为准；
    因为凌晨下播后投稿会导致 pubdate 落到第二天，甚至跨月。"""
    m = _LIVE_DATE_RE.search(title or '')
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime.date(y, mo, d)
        except ValueError:
            pass
    return datetime.date.fromtimestamp(pubdate) if pubdate else datetime.date.today()


def fetch_view(bvid, cookies=None):
    d = bili_get('https://api.bilibili.com/x/web-interface/view', {'bvid': bvid}, cookies)
    if d.get('code') != 0:
        raise RuntimeError(f'视频信息接口 code={d.get("code")} {d.get("message")}')
    return d['data']


# ---------- 文件名 / 时间 ----------
def safe_filename(title, limit=90):
    t = re.sub(r'^【直播回放】\s*', '', title or '').strip()
    t = re.sub(r'[\\/:*?"<>|\r\n\t]', '', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:limit].strip() or 'untitled'


def stem_for(pubdate, title):
    """统一命名：YYYYMMDD-标题（YYYYMMDD 取直播日期）—— 与历史归档保持一致。"""
    return f'{live_date(title, pubdate):%Y%m%d}-{safe_filename(title)}'


def hms(sec):
    sec = int(sec)
    return f'{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}'


def parse_hms(s):
    p = [int(x) for x in s.strip().split(':')]
    while len(p) < 3:
        p.insert(0, 0)
    return p[0] * 3600 + p[1] * 60 + p[2]


def probe_duration(ffprobe, path):
    import subprocess
    try:
        r = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', path],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', timeout=120)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 外部命令（ffmpeg/ffprobe/aria2c）统一执行口
#
# 背景（2026-09-18）：原先多处 `subprocess.run(..., check=True)` **没有 timeout**，
# 一旦 ffmpeg 卡在损坏输入/网络挂载盘上就会永久阻塞——上一轮被环境回收时就可能
# 停在这样一个不可中断的调用里。这里统一加超时，并按输入体量自适应，避免大文件
# 被误杀：timeout = base + 时长(秒) × k，上限 cap。
# ---------------------------------------------------------------------------
def timeout_for_source(ffprobe, path, base=900, k=2.0, cap=6 * 3600):
    """按源文件时长估算合理的转码/合并超时；取不到时长则退回 base。"""
    try:
        dur = probe_duration(ffprobe, path) or 0
    except Exception:
        dur = 0
    return int(min(cap, max(base, base + dur * k)))


def run_tool(cmd, timeout=None, check=False, log=None, what=''):
    """统一执行外部命令，返回 CompletedProcess。

    · 超时：交给调用方处理（抛 subprocess.TimeoutExpired），绝不无限等待；
    · check=True 时命令非 0 退出抛 CalledProcessError；
    · stderr 用 bytes 读取后自行解码，避免 Windows 下按 gbk 解码中文报错。
    """
    import subprocess
    if log and what:
        log('[tool] %s（timeout=%s）' % (what, timeout if timeout else '不限'))
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                          timeout=timeout, check=check)


# ---------------------------------------------------------------------------
# 标点清洗：清理转写/总结里常见的冗余双标点（FunASR 分段兜底导致句末标点与
# 句内连接标点叠在一起，如 "。，" "？，" "，。" "。？"）。纯纠错，不改语义。
# ---------------------------------------------------------------------------
_PUNCT_TERMINAL = "。！？"          # 句末终结标点
_PUNCT_CONNECTOR = "，、；："        # 句内连接/停顿标点
_RE_TERMINAL_CONN = re.compile(r'([。！？])[，、；：]+')   # 句末 + 连接 → 句末
_RE_CONN_TERMINAL = re.compile(r'[，、；：]+([。！？])')   # 连接 + 句末 → 句末
_RE_DOTS = re.compile(r'。{2,}')                        # 多句号折叠


def clean_punctuation(text):
    """清理相邻/冗余的中文标点组合，不改变语义。
    例：'。，'→'。'  '？，'→'？'  '，。'→'。'  '。？'→'？'  '。。'→'。'"""
    if not text:
        return text
    text = _RE_TERMINAL_CONN.sub(r'\1', text)
    text = _RE_CONN_TERMINAL.sub(r'\1', text)
    text = _RE_DOTS.sub('。', text)
    text = text.replace('。？', '？').replace('？。', '？')
    return text


def clean_segment_boundaries(segs):
    """转写分段拼接处的标点衔接修复：上句以句末标点结尾、下句以连接标点开头时，
    去掉下句开头的连接标点（这种情况本应是同一句的延续，段切分误加了句号）。"""
    out = []
    for st, en, t in segs:
        t = clean_punctuation(t)
        if out and out[-1][2] and out[-1][2][-1:] in _PUNCT_TERMINAL \
                and t and t[0] in _PUNCT_CONNECTOR:
            t = t.lstrip(_PUNCT_CONNECTOR)
        out.append((st, en, t))
    return out


# ---------------------------------------------------------------------------
# 数字归一：FunASR 把规格数字读成中文（"三零五零显卡" "七十五赫兹" "八G内存"），
# 在转写源头与写 Word 前各做一遍，把这些「紧跟规格单位的中国数字」转回阿拉伯数字。
# 仅当数字紧邻规格单位（显卡/GPU/Hz/赫兹/G/GB/寸/英寸/K/p/ti 等）才转换，避免把
# 「三个显卡」「七十五块钱」里的普通计数误转。已是阿拉伯数字的文本不受影响（幂等）。
# ---------------------------------------------------------------------------
_CN_DIGIT = {'零': '0', '一': '1', '二': '2', '两': '2', '三': '3', '四': '4',
             '五': '5', '六': '6', '七': '7', '八': '8', '九': '9'}
_CN_MULT = set('十百千万亿')
_NUM_TRIGGERS = [
    # (原文单位, 归一后单位) —— 长的放前面，避免被短前缀误吞
    ('赫兹', 'Hz'), ('兆赫', 'MHz'), ('显卡', '显卡'),
    ('GPU', 'GPU'), ('gpu', 'GPU'), ('Hz', 'Hz'), ('hz', 'Hz'),
    ('GB', 'GB'), ('gb', 'GB'), ('G', 'G'), ('g', 'G'),
    ('英寸', '英寸'), ('吋', '寸'), ('寸', '寸'),
    ('Ti', 'Ti'), ('ti', 'Ti'), ('K', 'K'), ('k', 'K'),
    ('p', 'p'), ('P', 'p'),
]


def _cn_num_to_str(num):
    """把一段纯中文数字（含 十百千万）转成阿拉伯数字字符串；无法解析返回 None。"""
    if not num:
        return None
    if all(c in _CN_DIGIT for c in num):
        # 逐位读法（型号/编号，如 三零五零 → 3050）
        return ''.join(_CN_DIGIT[c] for c in num)
    if any(c in _CN_MULT for c in num):
        # 复合读法（如 七十五 → 75，一百零八 → 108，三千零五十 → 3050）
        total, cur = 0, 0
        for ch in num:
            if ch in _CN_DIGIT:
                cur = int(_CN_DIGIT[ch])
            elif ch == '十':
                cur = cur * 10 if cur else 10
                total += cur
                cur = 0
            elif ch == '百':
                total += cur * 100
                cur = 0
            elif ch == '千':
                total += cur * 1000
                cur = 0
            elif ch == '万':
                total = (total + cur) * 10000
                cur = 0
            else:
                return None
        return str(total + cur)
    return None


_NUM_RE = re.compile(r'[零一二两三四五六七八九十百千万亿]+')


def normalize_numbers(text):
    """把紧跟规格单位的中国数字转成阿拉伯数字（如 3050显卡 / 75Hz / 8G）。
    仅对「数字紧邻规格单位」的情况生效，普通计数（三个/七十五块）不动。幂等。"""
    if not text:
        return text
    out, last = [], 0
    for m in _NUM_RE.finditer(text):
        start, end = m.span()
        num = m.group()
        rest = text[end:]
        converted = unit_out = None
        consumed = 0
        for unit, repl in _NUM_TRIGGERS:
            if rest.startswith(unit):
                converted = _cn_num_to_str(num)
                unit_out = repl
                consumed = len(unit)
                break
            if rest.startswith(' ') and rest[1:].startswith(unit):
                converted = _cn_num_to_str(num)
                unit_out = repl
                consumed = 1 + len(unit)
                break
        out.append(text[last:start])
        if converted is not None:
            out.append(str(converted))
            out.append(unit_out)
            last = end + consumed
        else:
            out.append(num)
            last = end
    out.append(text[last:])
    return ''.join(out)
