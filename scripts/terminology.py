# -*- coding: utf-8 -*-
"""专有名词书写规范（全局约束条款）——确定性规范化，不依赖模型自觉。

★ 设计原则：
  1. 规范化是**确定性后处理**，不是提示词请求。提示词只能降低违规率，
     只有代码才能保证 100% 一致（实测 10/1 那场就出了《复联三》《雷神三》）。
  2. **金句同样适用**（2026-10-02 修订）：金句虽是逐字原文，但其中的
     人名、地名、机构名、产品名、专业术语必须与正文 / 标题 / 摘要写法完全一致，
     否则同一场直播里会出现「iphone」和「iPhone」两种写法。
     金句模式只做「专有名词层面」的替换（大小写、官方拼写、序号、型号、单位），
     **绝不触碰语气、口语词、断句与标点**；且 QUOTE_EXEMPT 里的译名类词不改
     （如 weibo→微博 会把说话人的原话书面化，金句内保留）。
  3. **不确定即不改**：表里没有的词一律原样保留，绝不臆造或模糊替换。
  4. 真正豁免的只有：录播原标题 / 文件名 / 来源路径、成语惯用语、代码命令行。

CLI：
  python terminology.py --scan <docx> [--fix]     扫描（可选就地修复）文档
  python terminology.py --check "<一句话>"          单行自检
  python terminology.py --check "<一句>" --quote    按金句模式自检
"""

import argparse
import os
import re

# ---------------------------------------------------------------------------
# 一、大小写与拼写：品牌 / 产品 / 技术名词的官方写法
#   key 为小写归一形式，value 为官方写法。匹配时大小写不敏感、按 ASCII 词边界。
# ---------------------------------------------------------------------------
CASE_FIX = {
    # Apple
    'iphone': 'iPhone', 'ipad': 'iPad', 'ipod': 'iPod', 'imac': 'iMac',
    'macbook': 'MacBook', 'macos': 'macOS', 'ios': 'iOS', 'ipados': 'iPadOS',
    'watchos': 'watchOS', 'tvos': 'tvOS', 'icloud': 'iCloud', 'airpods': 'AirPods',
    'airdrop': 'AirDrop', 'appstore': 'App Store', 'itunes': 'iTunes',
    'applemusic': 'Apple Music', 'appletv': 'Apple TV',
    # 平台 / 社区
    'github': 'GitHub', 'gitlab': 'GitLab', 'youtube': 'YouTube', 'twitch': 'Twitch',
    'discord': 'Discord', 'twitter': 'Twitter', 'instagram': 'Instagram',
    'tiktok': 'TikTok', 'weibo': '微博', 'bilibili': 'bilibili', 'steam': 'Steam',
    'epic': 'Epic', 'blizzard': 'Blizzard', 'nintendo': 'Nintendo',
    'playstation': 'PlayStation', 'xbox': 'Xbox', 'ps5': 'PS5', 'ps4': 'PS4',
    'psp': 'PSP', 'nswitch': 'Switch',
    # 语言 / 框架 / 工具
    'javascript': 'JavaScript', 'typescript': 'TypeScript', 'python': 'Python',
    'golang': 'Go', 'rust': 'Rust', 'nodejs': 'Node.js', 'vue': 'Vue',
    'react': 'React', 'nextjs': 'Next.js', 'tailwind': 'Tailwind',
    'c++': 'C++', 'c#': 'C#', 'objective-c': 'Objective-C', 'dotnet': '.NET',
    'windows': 'Windows', 'android': 'Android', 'linux': 'Linux', 'ubuntu': 'Ubuntu',
    'obs': 'OBS', 'blender': 'Blender', 'photoshop': 'Photoshop', 'unity': 'Unity',
    'unreal': 'Unreal', 'figma': 'Figma', 'notion': 'Notion',
    # 缩略语 / 协议 / 格式
    'ai': 'AI', 'api': 'API', 'sdk': 'SDK', 'url': 'URL', 'http': 'HTTP',
    'https': 'HTTPS', 'json': 'JSON', 'xml': 'XML', 'yaml': 'YAML', 'csv': 'CSV',
    'sql': 'SQL', 'html': 'HTML', 'css': 'CSS', 'dns': 'DNS', 'tcp': 'TCP',
    'udp': 'UDP', 'ssh': 'SSH', 'ssl': 'SSL', 'tls': 'TLS', 'ocr': 'OCR',
    'tts': 'TTS', 'asr': 'ASR', 'llm': 'LLM', 'rag': 'RAG',
    'ui': 'UI', 'ux': 'UX', 'pc': 'PC',
    # 硬件 / 图形 / 音视频
    'gpu': 'GPU', 'cpu': 'CPU', 'npu': 'NPU', 'ssd': 'SSD', 'hdd': 'HDD',
    'ram': 'RAM', 'rom': 'ROM', 'usb': 'USB', 'typec': 'USB-C', 'hdmi': 'HDMI',
    'dp': 'DP', 'rgb': 'RGB', 'hdr': 'HDR', 'sdr': 'SDR', 'dlss': 'DLSS',
    'fsr': 'FSR', 'nvidia': 'NVIDIA', 'geforce': 'GeForce',
    'rtx': 'RTX', 'gtx': 'GTX', 'amd': 'AMD', 'intel': 'Intel', 'radeon': 'Radeon',
    'ryzen': 'Ryzen', 'hevc': 'HEVC', 'h264': 'H.264', 'h265': 'H.265',
    '3d': '3D', '2d': '2D', 'vr': 'VR',
    'ar': 'AR', 'mr': 'MR', 'asmr': 'ASMR', 'vtuber': 'VTuber', 'live2d': 'Live2D',
    'fps': 'fps', 'npc': 'NPC', 'dlc': 'DLC', 'gta': 'GTA', 'ea': 'EA',
    'rpg': 'RPG', 'mmo': 'MMO',
    # 媒体格式（官方均为大写）
    'mp3': 'MP3', 'mp4': 'MP4', 'flv': 'FLV', 'mkv': 'MKV', 'mov': 'MOV',
    'aac': 'AAC', 'wav': 'WAV', 'flac': 'FLAC', 'png': 'PNG', 'jpg': 'JPG',
    'jpeg': 'JPEG', 'gif': 'GIF', 'webp': 'WebP', 'srt': 'SRT',
    # AI 模型 / 项目名
    'chatgpt': 'ChatGPT', 'gpt': 'GPT', 'gemini': 'Gemini', 'qwen': 'Qwen',
    'deepseek': 'DeepSeek', 'glm': 'GLM', 'llama': 'LLaMA', 'whisper': 'Whisper',
    'funasr': 'FunASR', 'modelscope': 'ModelScope', 'huggingface': 'Hugging Face',
    'copilot': 'Copilot', 'claude': 'Claude', 'midjourney': 'Midjourney',
    'modelscope': 'ModelScope',
    # 网络 / 连接
    'wifi': 'Wi-Fi', 'wi-fi': 'Wi-Fi', 'bluetooth': 'Bluetooth', 'ethernet': 'Ethernet',
    'nas': 'NAS', 'vpn': 'VPN', 'ip': 'IP',
    # 常用游戏 / 作品相关
    'minecraft': 'Minecraft', 'boss': 'Boss',
}

# 金句（逐字原文）里**不做**的替换：这类是「译名 / 改名」，改了就不是说话人的原话。
#   例：「我在 weibo 上发的」→ 若改成「我在微博上发的」，口语质感被书面化。
#   而 iphone→iPhone、复联三→复联3 只是写法规范化，不改变用词，金句内照改。
QUOTE_EXEMPT = {'weibo', 'golang'}

# 个别词需要额外的否定预查（避免误伤英文里同形的缩写点写法，如 Mr.）
_CASE_NEG_LOOKAHEAD = {'mr': r'(?!\.)'}

# 需要「整体匹配」的写法（含非字母字符，无法用词边界简单处理）
# ★ 一律用 `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])` 而不是 `\b`：
#   Python 的 `\b` 是 Unicode 感知的，中文紧邻时（如「购买5070Ti」）前后都是「词字符」，
#   边界不成立 → 整条规则静默失效。
PHRASE_FIX = [
    (r'(?<![A-Za-z0-9])b(?=站)', 'B'),          # b站 → B站
    (r'(?i)(?<![A-Za-z0-9])wi\s?fi(?![A-Za-z0-9])', 'Wi-Fi'),
    (r'(?i)(?<![A-Za-z0-9])play\s?station\s?5(?![A-Za-z0-9])', 'PS5'),
    (r'(?i)(?<![A-Za-z0-9])nintendo\s?switch(?![A-Za-z0-9])', 'Switch'),
    # 显卡型号：Ti 首字母大写且与型号间有空格；已有 RTX 前缀时不重复叠加
    (r'(?i)(?<!RTX )(?<![A-Za-z0-9])(4060|4070|4080|4090|5060|5070|5080|5090)'
     r'\s?ti(?![A-Za-z0-9])', r'RTX \1 Ti'),
    (r'(?i)(?<![A-Za-z0-9])geforce\s?rtx\s?(\d{4})(?![A-Za-z0-9])', r'RTX \1'),
]

# ---------------------------------------------------------------------------
# 二、计量单位与技术符号（SI / IEEE 通行）
# ---------------------------------------------------------------------------
UNIT_FIX = [
    # 存储容量：B 大写（字节）
    (r'(?<![A-Za-z])(\d+(?:\.\d+)?)\s?(kb|mb|gb|tb|pb)(?![A-Za-z])',
     lambda m: m.group(1) + {'kb': 'KB', 'mb': 'MB', 'gb': 'GB',
                             'tb': 'TB', 'pb': 'PB'}[m.group(2)]),
    # 速率：b 小写（比特）
    (r'(?<![A-Za-z])(\d+(?:\.\d+)?)\s?(kbps|mbps|gbps)(?![A-Za-z])',
     lambda m: m.group(1) + {'kbps': 'Kbps', 'mbps': 'Mbps',
                             'gbps': 'Gbps'}[m.group(2)]),
    # 频率：Hz（H 大写、z 小写）
    (r'(?<![A-Za-z])(\d+(?:\.\d+)?)\s?(hz|khz|mhz|ghz)(?![A-Za-z])',
     lambda m: m.group(1) + {'hz': 'Hz', 'khz': 'kHz', 'mhz': 'MHz',
                             'ghz': 'GHz'}[m.group(2)]),
    # 分贝 dB（d 小写、B 大写）
    (r'(?<![A-Za-z])(\d+(?:\.\d+)?)\s?db(?![A-Za-z])', lambda m: m.group(1) + 'dB'),
    # 分辨率（仅 2K/4K/8K；紧跟中文量词时是「千」的含义，如「2k字」，不转）
    (r'(?<![A-Za-z0-9])([248])\s?[kK](?![A-Za-z0-9字个人员块条年月日])',
     lambda m: m.group(1) + 'K'),
    (r'(?<![A-Za-z])FPS(?![A-Za-z])', 'fps'),
    # 版本号：统一小写 v + 点分
    (r'(?<![A-Za-z0-9])V\s?(\d+(?:\.\d+)+)(?![A-Za-z0-9])', lambda m: 'v' + m.group(1)),
]

# ---------------------------------------------------------------------------
# 三、作品序号：中文数字 → 阿拉伯数字
#   只处理两类，其余中文数字一律不动（成语、惯用、第X次、第一人称等）。
# ---------------------------------------------------------------------------
_CN_DIGITS = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}


def _cn2num(s):
    """中文数字 → 阿拉伯数字，支持 1–99（含「十」「十五」）。失败返回 None。"""
    if not s:
        return None
    if all(c in _CN_DIGITS for c in s):
        if len(s) == 1:
            return _CN_DIGITS[s]
        # 多字纯数字串（如「一二」）视为序号拼接，不转换，避免误伤
        return None
    if '十' in s:
        a, _, b = s.partition('十')
        hi = _CN_DIGITS.get(a, 1) if a else 1
        lo = _CN_DIGITS.get(b, 0) if b else 0
        if (a == '' or a in _CN_DIGITS) and (b == '' or b in _CN_DIGITS):
            return hi * 10 + lo
    return None


# 书名号内、中文数字**位于结尾**、且前缀不在保护名单 → 转阿拉伯数字
# 保护名单：作品名本身固有中文数字（非序号），改了就变成另一部作品
PROTECT_TITLES = {
    '三体', '三国志', '三国的星空', '七大罪', '第五人格', '第一滴血', '七龙珠',
    '三十六计', '三字经', '百家姓', '千字文', '七剑', '十三机兵防卫圈',
    '二十世纪少年', '三月的狮子', '四月是你的谎言', '一周的朋友', '六花的勇者',
    '九品芝麻官', '十万个冷笑话', '七武士', '十二国记', '三傻大闹宝莱坞',
    '一出好戏', '三悦有了新工作', '二哥来了怎么办', '五月之恋',
}

_TITLE_SEQ_RE = re.compile(r'《([^《》]{1,20}?)([一二三四五六七八九十两]{1,3})》')
# 影视剧集序号：「第三部 / 第二季 / 第 12 集」
_EPISODE_RE = re.compile(r'第([一二三四五六七八九十两]{1,3})([部季集])')

# 书名号内不参与转换的保护片段（含这些字则不转）
_TITLE_KEEP_HINT = ('第一', '之一', '唯一', '统一', '专一')

# 口语里常**不带书名号**直接说系列作品（「复联三真好看」），书名号规则覆盖不到。
# 这类只在白名单内、且序号紧跟作品名时转换，避免误伤普通中文数字。
SERIES_TITLES = [
    '复仇者联盟', '复联', '雷神', '奇异博士', '蜘蛛侠', '钢铁侠', '美国队长',
    '银河护卫队', '黑寡妇', '惊奇队长', '蚁人', '尚气', '永恒族', '死侍',
    '金刚狼', 'X战警', '哈利波特', '指环王', '霍比特人', '侏罗纪世界', '侏罗纪公园',
    '变形金刚', '加勒比海盗', '速度与激情', '使命召唤', '生化危机', '古墓丽影',
    '最终幻想', '星球大战', '星际迷航', '名侦探柯南', '哆啦A梦', '蜡笔小新',
    '精灵宝可梦', '宝可梦', '数码宝贝', '龙珠', '海贼王', '火影忍者', '银魂',
    '进击的巨人', '鬼灭之刃', '咒术回战', '电锯人', '间谍过家家', '葬送的芙莉莲',
    '魔法少女小圆', '超时空要塞', '机动战士高达', '新世纪福音战士', '攻壳机动队',
    '异形', '铁血战士', '虎胆龙威', '终结者', '黑客帝国', '碟中谍', '谍影重重',
    '狂暴之路', '寂静岭', '死亡空间', '生化奇兵', '质量效应', '上古卷轴',
    '刺客信条', '孤岛惊魂', '半条命', '传送门', '守望先锋', '魔兽世界',
    '暗黑破坏神', '星际争霸', '魔兽争霸', '炉石传说', '英雄联盟', '王者荣耀',
    '明日方舟', '原神', '崩坏', '赛博朋克', '荒野大镖客', '侠盗猎车手',
    '极品飞车', '愤怒的小鸟', '植物大战僵尸', '我的世界', '泰拉瑞亚',
]

_SERIES_RE = None


def _build_series_re():
    global _SERIES_RE
    alts = sorted(SERIES_TITLES, key=len, reverse=True)
    # 否定预查（两层保险）：
    #   ① 量词 / 时间词 → 「原神三十次」「原神两个小时」「龙珠三部曲」都不是序号
    #   ② 「十」→ 单字数字后紧跟「十」时说明是「三十」这类数量词，不是序号；
    #      真正的「十四」会由 {1,3} 的贪婪匹配先整体命中，不受影响
    _SERIES_RE = re.compile(
        r'(%s)([一二三四五六七八九十两]{1,3})'
        r'(?![部集季次遍回个只条块年月日天小时分钟秒钟层关卡章节十分秒时])'
        % '|'.join(re.escape(a) for a in alts)
    )


def _fix_series_seq(text):
    """口语里不带书名号的系列作品序号：复联三 → 复联3。"""
    if _SERIES_RE is None:
        _build_series_re()
    out, changes = [], []

    def _rep(m):
        n = _cn2num(m.group(2))
        if n is None:
            return m.group(0)
        new = '%s%d' % (m.group(1), n)
        changes.append((m.group(0), new))
        return new

    pos = 0
    for m in _SERIES_RE.finditer(text):
        out.append(text[pos:m.start()])
        out.append(_rep(m))
        pos = m.end()
    out.append(text[pos:])
    return ''.join(out), changes


def _fix_title_seq(text):
    """《复联三》→《复联3》；《三体》等保护作品名不动。"""
    out, changes = [], []

    def _rep(m):
        name, num = m.group(1), m.group(2)
        if name in PROTECT_TITLES:
            return m.group(0)
        if any(h in name for h in _TITLE_KEEP_HINT):
            return m.group(0)
        if not name or not re.search(r'[A-Za-z\u4e00-\u9fff0-9]$', name):
            return m.group(0)
        n = _cn2num(num)
        if n is None:
            return m.group(0)
        new = '《%s%d》' % (name, n)
        changes.append((m.group(0), new))
        return new

    pos = 0
    for m in _TITLE_SEQ_RE.finditer(text):
        out.append(text[pos:m.start()])
        out.append(_rep(m))
        pos = m.end()
    out.append(text[pos:])
    return ''.join(out), changes


def _fix_episode_seq(text):
    """第三部 → 第3部；第二季 → 第2季；第 12 集不变。"""
    out, changes = [], []

    def _rep(m):
        n = _cn2num(m.group(1))
        if n is None:
            return m.group(0)
        new = '第%d%s' % (n, m.group(2))
        changes.append((m.group(0), new))
        return new

    pos = 0
    for m in _EPISODE_RE.finditer(text):
        out.append(text[pos:m.start()])
        out.append(_rep(m))
        pos = m.end()
    out.append(text[pos:])
    return ''.join(out), changes


# ---------------------------------------------------------------------------
# 四、主入口
# ---------------------------------------------------------------------------
def _compile_case_rules():
    """按 key 长度降序编译，保证 github 优先于 git 之类长词先命中。"""
    keys = sorted(CASE_FIX.keys(), key=len, reverse=True)
    parts = []
    for k in keys:
        esc = re.escape(k)
        neg = _CASE_NEG_LOOKAHEAD.get(k, '')
        parts.append(r'(?<![A-Za-z0-9])%s%s(?![A-Za-z0-9])' % (esc, neg))
    return re.compile('|'.join(parts), re.IGNORECASE)


_CASE_RE = _compile_case_rules()
# 官方写法本身无需再替换（如已是 iPhone），用「值 != 命中文本」跳过
_CANON = {v.lower(): v for v in CASE_FIX.values()}


def normalize(text, is_quote=False):
    """规范化一段文本。返回 (新文本, 改动列表[(旧, 新), ...])。

    is_quote=True 表示这是逐字摘录的**金句/原文**：
      · 依然应用全部专有名词规则（大小写、官方拼写、作品序号、型号、单位），
        以保证金句与正文 / 标题 / 摘要的术语、姓名写法完全一致；
      · 但跳过 QUOTE_EXEMPT 里的「译名类」替换，避免把说话人的原话书面化；
      · 任何规则都不会增删字词、改动标点或断句，因此语气与口语表达不受影响。
    """
    if not text:
        return text, []
    changes = []
    exempt = QUOTE_EXEMPT if is_quote else ()

    # 1) 短语级（含空格/连字符/型号）
    #    ★ 不能直接 re.sub(pat, rep, m.group(0))：含零宽断言（如 (?=站)）时
    #      片段里没有断言上下文会匹配不上；统一用 m.expand 处理反向引用。
    for pat, rep in PHRASE_FIX:
        def _r(m, rep=rep):
            old = m.group(0)
            if callable(rep):
                new = rep(m)
            elif '\\' in rep:
                new = m.expand(rep)
            else:
                new = rep
            if new != old:
                changes.append((old, new))
            return new
        text = re.sub(pat, _r, text)

    # 2) 词形：大小写 / 官方拼写
    def _case_rep(m):
        old = m.group(0)
        if old.lower() in exempt:
            return old
        want = CASE_FIX.get(old.lower())
        if want is None or want == old:
            return old
        changes.append((old, want))
        return want
    text = _CASE_RE.sub(_case_rep, text)

    # 3) 计量单位与技术符号
    for pat, rep in UNIT_FIX:
        def _u(m, rep=rep):
            old = m.group(0)
            new = rep(m) if callable(rep) else rep
            if new != old:
                changes.append((old, new))
            return new
        text = re.sub(pat, _u, text)

    # 4) 作品序号 / 剧集序号 / 口语里的系列作品序号
    text, c1 = _fix_title_seq(text)
    text, c2 = _fix_episode_seq(text)
    text, c3 = _fix_series_seq(text)
    changes.extend(c1)
    changes.extend(c2)
    changes.extend(c3)
    return text, changes


def find_violations(text):
    """只扫描不改写：返回改动列表（供体检报告与审计使用）。"""
    _, ch = normalize(text or '', is_quote=False)
    return ch


# ---------------------------------------------------------------------------
# 五、文档级扫描 / 修复
# ---------------------------------------------------------------------------
# 形如文件路径 / 文件名的文本：原始数据，改了就对不上源文件 → 整体豁免
_PATH_RE = re.compile(r'[A-Za-z]:[\\/]|\.(mp4|flv|mp3|m4a|wav|docx?|srt|txt|json)\b', re.I)


def _kind_of(p, r):
    """判断一个 run 属于哪类文本，决定用哪种模式规范化。

    返回 'summary' | 'quote' | 'skip'。
      summary → 总结行（粗体），应用完整规范
      quote   → 金句行（斜体非粗体），应用完整规范但豁免译名类替换
      skip    → 标题（场次原标题）、来源路径、目录域等，一律不改
    """
    style = (p.style.name or '')
    if style.startswith('Heading') or style.startswith('标题'):
        return 'skip'
    if _PATH_RE.search(r.text or ''):
        return 'skip'
    bold = r.bold is True
    italic = r.italic is True
    if bold:
        return 'summary'
    if italic:
        return 'quote'
    return 'skip'      # 9pt 灰色来源行等 → 原始数据


def scan_docx(path, fix=False, log=None):
    """扫描 docx。三类文本分别处理：

      · 总结行（粗体）   → 完整规范
      · 金句行（斜体）   → 同样应用规范（金句术语须与正文一致），豁免译名类替换
      · 标题 / 来源路径  → 豁免（原始数据，改了就对不上源文件）

    返回 (修复条数, 明细列表[(旧, 新, 上下文, 类别), ...])。
    """
    import docx
    d = docx.Document(path)
    total, detail = 0, []
    for p in d.paragraphs:
        if not p.runs:
            continue
        runs = [r for r in p.runs if r.text]
        if not runs:
            continue
        for r in runs:
            kind = _kind_of(p, r)
            if kind == 'skip':
                continue
            new, ch = normalize(r.text, is_quote=(kind == 'quote'))
            if not ch:
                continue
            for old, nw in ch:
                detail.append((old, nw, r.text[:40], kind))
            total += len(ch)
            if fix:
                r.text = new
    if fix and total:
        try:
            d.save(path)
        except PermissionError:
            # 被编辑器占用 → 原地覆写（必须二进制）
            with open(path, 'wb') as f:
                d.save(f)
    return total, detail


def main():
    ap = argparse.ArgumentParser(description='专有名词书写规范 扫描/修复')
    ap.add_argument('--scan', help='docx 路径')
    ap.add_argument('--fix', action='store_true', help='就地修复')
    ap.add_argument('--check', help='单行自检')
    ap.add_argument('--quote', action='store_true',
                    help='配合 --check：按金句模式（保留口语用词）自检')
    a = ap.parse_args()

    if a.check:
        new, ch = normalize(a.check, is_quote=a.quote)
        print('原文：', a.check, '（%s）' % ('金句模式' if a.quote else '正文模式'))
        print('规范：', new)
        for old, nw in ch:
            print('   %s → %s' % (old, nw))
        if not ch:
            print('   （无需修改）')
        return 0

    if a.scan:
        n, detail = scan_docx(a.scan, fix=a.fix)
        print('文档：%s' % a.scan)
        print('违规 %d 处%s' % (n, '（已修复）' if a.fix and n else ''))
        kinds = {}
        for _, _, _, k in detail:
            kinds[k] = kinds.get(k, 0) + 1
        for k, c in sorted(kinds.items(), key=lambda x: -x[1]):
            print('   其中 %s %d 处' % ({'summary': '总结行', 'quote': '金句行'}.get(k, k), c))
        seen = {}
        for old, new, _ctx, _k in detail:
            seen.setdefault((old, new), 0)
            seen[(old, new)] += 1
        for (old, new), c in sorted(seen.items(), key=lambda x: -x[1])[:40]:
            print('   %-14s → %-14s ×%d' % (old, new, c))
        return 0

    ap.print_help()
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
