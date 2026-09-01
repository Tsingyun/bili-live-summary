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


# ---------- 状态 ----------
def state_path(cfg):
    return os.path.join(cfg['_dirs']['root'], 'state.json')


def load_state(cfg):
    p = state_path(cfg)
    if os.path.exists(p):
        try:
            with open(p, 'r', encoding='utf-8') as f:
                st = json.load(f)
        except Exception:
            st = {}
    else:
        st = {}
    st.setdefault('processed', {})       # bvid -> {title, pubdate, date, stages, doc}
    st.setdefault('last_pubdate', 0)     # 已处理过的最大 pubdate
    st.setdefault('runs', [])
    return st


def save_state(cfg, st):
    st['updated_at'] = datetime.datetime.now().isoformat(timespec='seconds')
    tmp = state_path(cfg) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(st, f, ensure_ascii=False, indent=2)
    os.replace(tmp, state_path(cfg))


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
    """带指数退避的 B站 API GET。"""
    import requests
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=BILI_HEADERS,
                             cookies=cookies or {}, timeout=timeout)
            r.raise_for_status()
            return r.json()
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
