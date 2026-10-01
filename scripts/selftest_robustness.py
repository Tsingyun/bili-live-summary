#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest_robustness.py —— 流水线健壮性回归自测（离线，无需外网）。

覆盖的目标（都是 2026-09-17/18 事故复盘后新增/加固的点）
------------------------------------------------------
  A. 服务器忽略 Range 返回 200 时，断点续传**不得**产出"双倍大小"的损坏文件
  B. 响应被截断（Content-Length 与实际不符）时，**不得**把截断文件提升为正式文件
  C. 正常支持 Range(206) 时，断点续传必须逐字节还原原文件
  D. 原子写（文本 / state.json）不留半成品，且 state 有 .bak 兜底
  E. 单实例锁判活：心跳过期/进程已死 → 判为孤儿（可接管）；心跳新鲜 → 拒绝并发
  F. docx 原子保存 + 主文档损坏时回退 .bak
  G. 压缩产物完整性门禁：时长不足的 "_compressed.mp4" 不得被当成已完成
     （这道门禁同时保护"删源视频"——旧逻辑会把完好源删掉、留下残片）
  L. 小号上传模块纯逻辑：默认值/元数据渲染与截断/延时发布夹取/幂等/缺 cookie 优雅失败
     （2026-09-26 新增，8 项）

用法
----
    python selftest_robustness.py            # 全跑
    python selftest_robustness.py -v         # 打印每步细节
退出码 0 = 全部通过；非 0 = 有用例失败。
"""
import argparse
import http.server
import json
import os
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

PASS, FAIL = [], []


def unused_pid():
    """返回一个当前未被占用的 PID（Windows PID 为 4 的倍数）。

    ★ 为什么不用「刚退出的子进程 PID」：Windows 会快速复用 PID，
      那个号码可能已经被分配给另一个活进程，于是「已死进程」被判为存活
      → E4 / H3 偶发假失败（真因，2026-09-30 实测）。改用从未分配过的 PID 稳定复现。"""
    import common   # 本文件所有 common 引用都在函数内延迟导入（保持一致的导入习惯）
    pid = 40000
    while pid < 65000 and common.pid_alive(pid):
        pid += 4
    return pid


def check(name, cond, detail=''):
    if cond:
        PASS.append(name)
        print('  ✅ %s' % name)
    else:
        FAIL.append((name, detail))
        print('  ❌ %s  %s' % (name, detail))


# ---------------------------------------------------------------------------
# mock HTTP 服务
# ---------------------------------------------------------------------------
PAYLOAD = (b'0123456789abcdef' * 18750)[:300000]      # 300000 字节确定内容


class _Handler(http.server.BaseHTTPRequestHandler):
    mode = 'range_ok'          # range_ok | ignore_range | truncate
    hits = 0

    def log_message(self, *a):
        pass

    def do_GET(self):
        type(self).hits += 1
        rng = self.headers.get('Range')
        start = 0
        if rng and self.mode == 'range_ok':
            try:
                start = int(rng.split('=')[1].split('-')[0])
            except Exception:
                start = 0
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header('Content-Range', 'bytes %d-%d/%d'
                             % (start, len(PAYLOAD) - 1, len(PAYLOAD)))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.mode == 'truncate':
            # 谎报完整长度，实际只发一半就断开 → 模拟"连接被中途关闭"
            self.send_response(200)
            self.send_header('Content-Length', str(len(PAYLOAD)))
            self.end_headers()
            half = PAYLOAD[:len(PAYLOAD) // 2]
            try:
                self.wfile.write(half)
                self.wfile.flush()
            except Exception:
                pass
            self.close_connection = True
            return
        # ignore_range / 默认：无论有没有 Range 都回整份 200
        self.send_response(200)
        self.send_header('Content-Length', str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


def start_server(mode):
    _Handler.mode = mode
    _Handler.hits = 0
    httpd = socketserver.TCPServer(('127.0.0.1', 0), _Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd, port


class _Log:
    """极简 log，兼容 Logger(msg, level) 调用形态。"""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.lines = []

    def __call__(self, msg, level='INFO'):
        self.lines.append('[%s] %s' % (level, msg))
        if self.verbose:
            print('      | %s' % msg)

    def error(self, msg):
        self(msg, 'ERROR')


def _darkpy_cfg(port):
    return {'darkpy': {'enabled': True, 'host': '127.0.0.1', 'file_port': port,
                       'room_dir': 'room', 'retries': 3, 'timeout': 10}}


# ---------------------------------------------------------------------------
# A / B / C：下载
# ---------------------------------------------------------------------------
def test_download(tmp, v):
    import darkpy_rec as D

    print('\n[A] 服务器忽略 Range（返回 200 全量）→ 不得产出双倍损坏文件')
    httpd, port = start_server('ignore_range')
    try:
        dest = os.path.join(tmp, 'a.flv')
        with open(dest + '.part', 'wb') as f:      # 已有 100000 字节分片
            f.write(PAYLOAD[:100000])
        log = _Log(v)
        got = D.download_one(_darkpy_cfg(port), log, 'a.flv', dest)
        size = os.path.getsize(dest) if os.path.exists(dest) else -1
        check('A1 下载返回成功', bool(got), '返回 %r' % got)
        check('A2 文件大小 == 300000（未被追加成 400000）', size == len(PAYLOAD),
              '实际 %d' % size)
        if size == len(PAYLOAD):
            with open(dest, 'rb') as f:
                check('A3 内容逐字节正确', f.read() == PAYLOAD)
        check('A4 未残留 .part', not os.path.exists(dest + '.part'))
    finally:
        httpd.shutdown()

    print('\n[B] 响应被截断（Content-Length 与实际不符）→ 不得提升为正式文件')
    httpd, port = start_server('truncate')
    try:
        dest = os.path.join(tmp, 'b.flv')
        log = _Log(v)
        got = D.download_one(_darkpy_cfg(port), log, 'b.flv', dest)
        check('B1 判定失败（返回 None）', got is None, '返回 %r' % got)
        check('B2 未生成正式文件', not os.path.exists(dest))
    finally:
        httpd.shutdown()

    print('\n[C] 正常 206 断点续传 → 逐字节还原')
    httpd, port = start_server('range_ok')
    try:
        dest = os.path.join(tmp, 'c.flv')
        with open(dest + '.part', 'wb') as f:
            f.write(PAYLOAD[:120000])
        log = _Log(v)
        got = D.download_one(_darkpy_cfg(port), log, 'c.flv', dest)
        ok = bool(got) and os.path.getsize(dest) == len(PAYLOAD)
        if ok:
            with open(dest, 'rb') as f:
                ok = f.read() == PAYLOAD
        check('C1 续传结果逐字节正确', ok,
              'size=%s' % (os.path.getsize(dest) if os.path.exists(dest) else None))
        check('C2 未残留 .part', not os.path.exists(dest + '.part'))
    finally:
        httpd.shutdown()


# ---------------------------------------------------------------------------
# D：原子写 / state
# ---------------------------------------------------------------------------
def test_atomic(tmp, v):
    import common
    print('\n[D] 原子写与 state 兜底')
    p = os.path.join(tmp, 'note.txt')
    common.atomic_write_text(p, 'hello 原子写')
    with open(p, encoding='utf-8') as f:
        check('D1 内容正确', f.read() == 'hello 原子写')
    check('D2 不留 .part', not os.path.exists(p + '.part'))

    cfg = {'_dirs': {'root': tmp}}
    st = {'processed': {'X': 1}, 'done_dates': ['2026-09-18']}
    common.save_state(cfg, st)
    check('D3 state 已写', os.path.exists(os.path.join(tmp, 'state.json')))
    common.save_state(cfg, {'processed': {'Y': 2}, 'done_dates': []})
    check('D4 生成 .bak', os.path.exists(os.path.join(tmp, 'state.json.bak')))
    # 主文件损坏 → 必须回退 .bak 而不是静默返回空 state
    with open(os.path.join(tmp, 'state.json'), 'w', encoding='utf-8') as f:
        f.write('{ this is not json')
    st2 = common.load_state(cfg)
    check('D5 主文件损坏时回退 .bak', 'processed' in st2 and len(st2['processed']) > 0,
          '读回 %r' % (st2.get('processed'),))


# ---------------------------------------------------------------------------
# E：锁判活
# ---------------------------------------------------------------------------
def test_lock(tmp, v):
    import common
    print('\n[E] 单实例锁判活（防 PID 复用）')
    wd = os.path.join(tmp, 'lockdir')
    os.makedirs(wd, exist_ok=True)
    lp, hb = common.lock_paths(wd)

    # E1 心跳新鲜 + 本进程 PID → 视为活着
    with open(lp, 'w', encoding='utf-8') as f:
        f.write('%d' % os.getpid())
    with open(hb, 'w', encoding='utf-8') as f:
        f.write('%d %d' % (os.getpid(), int(time.time())))
    check('E1 心跳新鲜 → 判为存活（拒绝并发）', common.lock_owner_alive(wd) is True)

    # E2 心跳过期（模拟上一轮被回收，PID 被别的进程顶替）→ 必须判为孤儿
    with open(hb, 'w', encoding='utf-8') as f:
        f.write('%d %d' % (os.getpid(), int(time.time()) - 7200))
    check('E2 心跳过期 → 判为孤儿（可接管）', common.lock_owner_alive(wd) is False)

    # E3 心跳缺失 → 无法证明存活 → 孤儿
    os.remove(hb)
    check('E3 心跳缺失 → 判为孤儿', common.lock_owner_alive(wd) is False)

    # E4 PID 已死 → 孤儿
    # 注意：必须 del 掉 Popen 对象再探测。Windows 上只要**还持有该进程的句柄**
    # （Popen 未析构时就是这样），已退出进程的 PID 在 OpenProcess 看来依然有效，
    # os.kill(pid,0) 会误报"存活"。生产场景里上一轮的进程不是本进程的子进程、
    # 我们并不持有它的句柄，所以这里必须还原成同样的前提。
    dead_pid = unused_pid()   # 不用刚退出的子进程 PID（会被系统复用，见 unused_pid 说明）
    time.sleep(0.1)
    with open(lp, 'w', encoding='utf-8') as f:
        f.write('%d' % dead_pid)
    with open(hb, 'w', encoding='utf-8') as f:
        f.write('%d %d' % (dead_pid, int(time.time())))
    check('E4 进程已死 → 判为孤儿', common.lock_owner_alive(wd) is False)

    # E5 没有锁文件 → 不算存活
    os.remove(lp)
    check('E5 无锁文件 → 不判存活', common.lock_owner_alive(wd) is False)


# ---------------------------------------------------------------------------
# F：docx
# ---------------------------------------------------------------------------
def test_docx(tmp, v):
    print('\n[F] docx 原子保存与损坏回退')
    try:
        import summarize_gemini as S
        from docx import Document
    except Exception as e:
        check('F0 导入 docx 相关模块', False, str(e))
        return
    p = os.path.join(tmp, 'doc.docx')
    doc = Document()
    doc.add_paragraph('第一章')
    S.save_doc_atomic(doc, p)
    check('F1 保存后可正常打开', Document(p).paragraphs[0].text == '第一章')
    check('F2 不留 .part', not os.path.exists(p + '.part'))

    # 制造"主文档损坏 + .bak 完好"：open_or_create 必须回退 .bak
    good = os.path.join(tmp, 'good.docx')
    d2 = Document()
    d2.add_paragraph('来自备份的内容')
    d2.save(good)
    shutil.copy2(good, p + '.bak')
    with open(p, 'w', encoding='utf-8') as f:
        f.write('broken-not-a-zip')
    docx_obj, created = S.open_or_create(p, '宋体')
    txts = [x.text for x in docx_obj.paragraphs]
    check('F3 主文档损坏时回退 .bak', (not created) and '来自备份的内容' in txts,
          'created=%s txts=%r' % (created, txts))

    # F4-F6 目标被占用（Word / 腾讯文档预览打开着 docx）：
    #   os.replace 会一直抛 WinError 5/32，干等无效。必须退化到「原地覆写」，
    #   否则整个月的章节都写不进去——2026-09-19 的 9/18 场次就是这样整场失败的。
    p2 = os.path.join(tmp, 'busy.docx')
    d3 = Document()
    d3.add_paragraph('原有章节')
    S.save_doc_atomic(d3, p2)
    real_replace = os.replace

    def _deny(src, dst, *a, **k):
        if str(dst).endswith('.docx'):
            raise OSError(13, 'Access is denied', str(dst), 5)   # winerror=5
        return real_replace(src, dst, *a, **k)

    os.replace = _deny
    try:
        d4 = Document(p2)
        d4.add_paragraph('占用时追加的章节')
        err = ''
        S.save_doc_atomic(d4, p2)
    except Exception as e:
        err = str(e)
    finally:
        os.replace = real_replace
    check('F4 目标被占用时退化到原地覆写（不整场失败）', not err, err)
    try:
        got = [x.text for x in Document(p2).paragraphs]
    except Exception as e:
        got = ['<打不开: %s>' % e]
    check('F5 原地覆写后内容正确（旧+新都在）',
          '原有章节' in got and '占用时追加的章节' in got, 'got=%r' % got)
    check('F6 原地覆写不留 .part', not os.path.exists(p2 + '.part'))


# ---------------------------------------------------------------------------
# G：压缩完整性门禁
# ---------------------------------------------------------------------------
def test_compress(tmp, v):
    print('\n[G] 压缩产物完整性门禁（保护删源）')
    try:
        import compress_video as CV
    except Exception as e:
        check('G0 导入 compress_video', False, str(e))
        return
    ffmpeg = shutil.which('ffmpeg') or r'D:\ffmpeg\bin\ffmpeg.exe'
    ffprobe = shutil.which('ffprobe') or r'D:\ffmpeg\bin\ffprobe.exe'
    if not (os.path.exists(ffmpeg) and os.path.exists(ffprobe)):
        print('  ⏭  跳过：本机没有 ffmpeg/ffprobe')
        return
    cfg = {'paths': {'ffmpeg': ffmpeg, 'ffprobe': ffprobe}}
    src = os.path.join(tmp, 'src.mp4')
    short = os.path.join(tmp, 'short_compressed.mp4')

    def gen(out, sec):
        # 加噪点让画面不可压缩，把测试素材撑到 >1MB——_compressed_ok 的第一道门
        # 就是"体积 >1MB"，素材太小根本走不到时长判据（纯 testsrc 只有几百 KB）。
        return subprocess.run(
            [ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
             '-i', 'testsrc2=size=640x480:rate=25', '-t', str(sec),
             '-vf', 'noise=alls=90:allf=t',
             '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
             '-pix_fmt', 'yuv420p', out],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=180)

    gen(src, 4)
    gen(short, 1)
    if not (os.path.exists(src) and os.path.exists(short)):
        print('  ⏭  跳过：测试视频生成失败')
        return
    ok, why = CV._compressed_ok(cfg, src, short)
    check('G1 时长不足的产物被判不合格（旧逻辑会误判为已完成并删源）',
          ok is False, 'why=%s' % why)
    ok2, why2 = CV._compressed_ok(cfg, src, src)
    check('G2 时长一致的产物判合格', ok2 is True, 'why=%s' % why2)
    # 体积过小的产物必须不合格
    tiny = os.path.join(tmp, 'tiny.mp4')
    with open(tiny, 'wb') as f:
        f.write(b'0' * 1024)
    ok3, _ = CV._compressed_ok(cfg, src, tiny)
    check('G3 体积异常的产物判不合格', ok3 is False)


def test_cleanup_lock(tmp, v):
    """H：清理器不得误删「活实例」的锁（长跑时的并发保护）。

    修复前：锁文件内容是「pid + ISO时间」，旧实现 `int(整个文件)` 必抛异常 → pid=0
    → 跳过存活检查直接删锁。于是一轮跑满 orphan_lock_hours(2h) 的实例，锁会在
    中途被 clean 掉，第二个实例（例如 Windows 计划任务）即可与它并发写同一份 Word。
    """
    print('\n[H] 清理器不得误删活实例的锁（并发保护）')
    try:
        import common
        import cleanup as CU
    except Exception as e:
        check('H0 导入 cleanup', False, str(e))
        return
    wd = os.path.join(tmp, 'cleandir')
    os.makedirs(wd, exist_ok=True)
    lp, hb = common.lock_paths(wd)

    # H1 活实例 + 锁文件 mtime 伪造成 3 小时前（模拟长跑超过 orphan_lock_hours）
    #    → 必须仍判为存活（不得被清）
    with open(lp, 'w', encoding='utf-8') as f:
        f.write('%d %s' % (os.getpid(), '2026-09-18T20:00:00'))
    with open(hb, 'w', encoding='utf-8') as f:
        f.write('%d %d' % (os.getpid(), int(time.time())))
    old = time.time() - 3 * 3600
    os.utime(lp, (old, old))
    check('H1 长跑中(锁文件超期)但心跳新鲜 → 仍判存活，不得清',
          CU.lock_file_is_live(wd, '.daily_pipeline.lock', lp) is True)

    # H2 心跳过期（实例已被回收）→ 判为孤儿，可清
    with open(hb, 'w', encoding='utf-8') as f:
        f.write('%d %d' % (os.getpid(), int(time.time()) - 7200))
    check('H2 心跳过期 → 判为孤儿（可清）',
          CU.lock_file_is_live(wd, '.daily_pipeline.lock', lp) is False)

    # H3 回归：旧实现之所以漏判，是因为 int(「pid 时间戳」) 抛异常后默认 pid=0。
    #    这里确保「pid 后面带时间戳」不会被解析成 0 —— 换成死 pid + 新鲜心跳，
    #    必须判为孤儿（说明 pid 确实被正确解析出来了）。
    dead_pid = unused_pid()   # 不用刚退出的子进程 PID（会被系统复用，见 unused_pid 说明）
    time.sleep(0.1)
    with open(lp, 'w', encoding='utf-8') as f:
        f.write('%d %s' % (dead_pid, '2026-09-18T20:00:00'))
    with open(hb, 'w', encoding='utf-8') as f:
        f.write('%d %d' % (dead_pid, int(time.time())))
    check('H3 pid 带时间戳仍被正确解析（死进程 → 孤儿）',
          CU.lock_file_is_live(wd, '.daily_pipeline.lock', lp) is False)

    # H4 非 pipeline 的 .lock：纯 pid 探测，本进程活着 → 判存活
    other = os.path.join(wd, '.regen.lock')
    with open(other, 'w', encoding='utf-8') as f:
        f.write(str(os.getpid()))
    check('H4 其它 .lock 走 pid 探测（活进程 → 存活）',
          CU.lock_file_is_live(wd, '.regen.lock', other) is True)


class _SilentLog:
    """替身日志：吞掉输出，只在断言失败时才需要看细节。"""

    def __init__(self):
        self.lines = []

    def __call__(self, msg, level=None):
        self.lines.append('%s %s' % (level or 'INFO', msg))

    def error(self, msg):
        self.lines.append('ERROR %s' % msg)


def test_ffmpeg_part_output(tmp, v):
    """I：ffmpeg 原子写的临时名不能破坏容器格式推断（回归 4294967274）。

    2026-09-19 事故：`extract_16k_audio` / `mp3_export` 把临时输出写成
    `<x>.mp3.part`，ffmpeg 从扩展名猜不出容器 → 以 AVERROR(EINVAL) 退出
    （Windows 上 `exit status 4294967274`），9/18 场次在第一步就失败。
    修法是给 ffmpeg 显式加 `-f mp3`。这个用例直接调用**真实函数**跑一遍，
    谁把 `-f mp3` 删掉就会红。
    """
    ffmpeg = shutil.which('ffmpeg') or r'D:\ffmpeg\bin\ffmpeg.exe'
    ffprobe = shutil.which('ffprobe') or r'D:\ffmpeg\bin\ffprobe.exe'
    if not (os.path.exists(ffmpeg) and os.path.exists(ffprobe)):
        print('\n[I] 跳过：本机没有 ffmpeg/ffprobe')
        return
    print('\n[I] ffmpeg .part 临时名（容器格式推断 + 原子改名）')
    try:
        import common
        import daily_pipeline as DP
        import mp3_export as ME
    except Exception as e:
        check('I0 导入 daily_pipeline / mp3_export', False, str(e))
        return

    # 造一段有**音轨**的源（G 组那套 testsrc2 没有音频，走不到本路径）
    src = os.path.join(tmp, 'i_src.wav')
    r = subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=4',
                        '-ar', '44100', '-ac', '2', src],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=120)
    if not (os.path.exists(src) and os.path.getsize(src) > 1000):
        print('  ⏭  跳过：测试音频生成失败 %s' % ((r.stderr or b'')[-200:]))
        return
    log = _SilentLog()
    cfg = {'paths': {'ffmpeg': ffmpeg, 'ffprobe': ffprobe},
           '_dirs': {'root': tmp},
           'mp3_export': {'enabled': True, 'out_dir': os.path.join(tmp, 'mp3out'),
                          'bitrate': '192k', 'ar': 44100}}

    # I1 抽 16k 转写音频（就是 9/18 失败的那一步）
    dst = os.path.join(tmp, 'i_16k.mp3')
    try:
        DP.extract_16k_audio(cfg, log, src, dst)
        err = ''
    except Exception as e:
        err = str(e)
    check('I1 extract_16k_audio 成功（.part 临时名 + 显式 -f）',
          os.path.exists(dst) and os.path.getsize(dst) > 1000, err or '产物缺失')
    check('I2 不留 .part 残留', not os.path.exists(dst + '.part'))
    d = common.probe_duration(ffprobe, dst) or 0
    check('I3 抽出的音频时长与源一致（4s）', 3.0 <= d <= 5.0, 'got=%.2f' % d)

    # I4 归档中质量 MP3（另一处同类写法）
    out = ME.export_mp3(cfg, log, src)
    check('I4 export_mp3 成功', bool(out) and os.path.exists(out),
          'out=%s' % out)
    if out:
        check('I5 归档 MP3 不留 .part', not os.path.exists(out + '.part'))
        d2 = common.probe_duration(ffprobe, out) or 0
        check('I6 归档 MP3 时长与源一致（4s）', 3.0 <= d2 <= 5.0, 'got=%.2f' % d2)


def test_hotwords(tmp, v):
    """J：热词替换（转写后 → 送总结前）必须真的生效，且不误伤、不留半截。

    这是「标准流程」的守门测试：转写产物必须先经热词表纠正，再交给大模型总结。
    任何人改坏解析顺序、把保护词机制删掉、或换成非原子写，这里都会红。
    """
    print('\n[J] 热词替换（解析 / 保护 / 长词优先 / 审计 / 原子写）')
    try:
        import hotwords as HW
    except Exception as e:
        check('J0 导入 hotwords', False, str(e))
        return

    def write_md(name, lines):
        p = os.path.join(tmp, name)
        with open(p, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')
        return p

    # J1 基础解析：规则与保护词分别归位
    md = write_md('j_hw.md', [
        '# 测试热词表', '',
        '| 原词 | 替换为 |',
        '| --- | --- |',
        '| 饼干碎 | 饼干岁 |',
        '| 剑长 | 舰长 |',
        '| 连卖 | 连麦 |',
        '| 点个 | 点歌 |',
        '| 长词样本ABC | 替换后 |', '',
        '## 保护规则（不受误伤）', '',
        '| 保护词 | 原因 |',
        '| --- | --- |',
        '| 点个赞 | 理由 |',
        '| 点个关注 | 理由 |',
        '| 晚上见 | 理由 |',
    ])
    rules, protect = HW.load_rules(md)
    check('J1 规则/保护词分别解析正确',
          len(rules) == 5 and protect == ['点个赞', '点个关注', '晚上见'],
          'rules=%d protect=%r' % (len(rules), protect))

    # J2 保护词生效：点个赞/点个关注/晚上见 不被「点个→点歌」误伤；
    #    没被保护的「点个」仍应正常替换（保护是精确短语、不是整段屏蔽）
    out = HW.apply_to_text('点个赞然后晚上见再点个关注再点个菜', rules, protect)
    check('J2 保护词不被短词误伤',
          out == '点个赞然后晚上见再点个关注再点歌菜', 'got=%r' % out)
    check('J3 应替换的仍然替换',
          HW.apply_to_text('饼干碎和剑长连卖', rules, protect) == '饼干岁和舰长连麦')

    # J4 长词优先：ABC 整体替换，不被更短的 BC 抢先切碎
    md2 = write_md('j_hw2.md', [
        '| 原词 | 替换为 |', '| --- | --- |',
        '| ABC | Z |', '| BC | Y |',
    ])
    r2, _ = HW.load_rules(md2)
    check('J4 长词优先（不被短词抢先替换）',
          HW.apply_to_text('ABC', r2, []) == 'Z',
          'got=%r' % HW.apply_to_text('ABC', r2, []))

    # J5 占位符不泄漏：保护词还原后不应残留私用区字符
    out5 = HW.apply_to_text('点个赞点个赞晚上见', rules, protect)
    check('J5 保护占位符完整还原、无残留',
          '\uE000' not in out5 and '\uE001' not in out5 and out5 == '点个赞点个赞晚上见',
          'got=%r' % out5)

    # J6 审计能发现「冲突重复」（同原词不同目标，后者会被静默丢弃）
    md6 = write_md('j_hw6.md', [
        '| 原词 | 替换为 |', '| --- | --- |',
        '| 甲 | 乙 |', '| 甲 | 丙 |',
    ])
    rep6 = HW.audit_md(md6)
    check('J6 审计发现同原词不同目标的冲突',
          '甲' in rep6['conflicts'], 'conflicts=%r' % rep6['conflicts'])

    # J7 审计能发现「危险级联」（替换结果还会被另一条非空转规则改动）
    md7 = write_md('j_hw7.md', [
        '| 原词 | 替换为 |', '| --- | --- |',
        '| 坏词 | 好B |', '| 好B | 更好B |',
    ])
    rep7 = HW.audit_md(md7)
    check('J7 审计发现危险级联',
          any(x[0] == '坏词' and x[2] == '好B' for x in rep7['dangerous']),
          'dangerous=%r' % rep7['dangerous'])

    # J8 审计能发现「空转」（原词 == 替换为，replace 没有任何作用）
    md8 = write_md('j_hw8.md', [
        '| 原词 | 替换为 |', '| --- | --- |',
        '| 银魂 | 银魂 |',
    ])
    rep8 = HW.audit_md(md8)
    check('J8 审计发现空转词条', len(rep8['noop']) == 1, 'noop=%r' % rep8['noop'])

    # J9 相对路径解析：以 cfg._workdir 为基准找到工作目录里的表
    sub = os.path.join(tmp, 'j_wd')
    os.makedirs(sub, exist_ok=True)
    write_md('j_wd/hotwords.md', ['| 原词 | 替换为 |', '| --- | --- |', '| 碎姐 | 岁己 |'])
    cfg9 = {'_workdir': sub, 'hotwords': {'enabled': True, 'path': 'hotwords.md'}}
    check('J9 hotwords.path 相对工作目录解析',
          HW.resolve_md_path(cfg9) == os.path.join(sub, 'hotwords.md'))

    # J10 apply_to_file：真改文件、且不留 .part（原子写）
    txt = os.path.join(tmp, 'j_tr.txt')
    with open(txt, 'w', encoding='utf-8') as f:
        f.write('饼干碎说剑长')
    changed = HW.apply_to_file(txt, cfg9, None)      # cfg9 表里只有「碎姐」，不该改
    check('J10a 无命中时不改动文件', changed is False)
    cfg10 = {'_workdir': sub, 'hotwords': {'enabled': True, 'path': 'hotwords.md'}}
    with open(os.path.join(sub, 'hotwords.md'), 'w', encoding='utf-8') as f:
        f.write('| 原词 | 替换为 |\n| --- | --- |\n| 饼干碎 | 饼干岁 |\n| 剑长 | 舰长 |\n')
    changed = HW.apply_to_file(txt, cfg10, None)
    body = open(txt, encoding='utf-8').read()
    check('J10b 命中时正确改写文件', changed is True and body == '饼干岁说舰长',
          'changed=%s body=%r' % (changed, body))
    check('J10c 写回不留 .part 残留', not os.path.exists(txt + '.part'))

    # J11 enabled=false 时完全不动作
    txt2 = os.path.join(tmp, 'j_tr2.txt')
    with open(txt2, 'w', encoding='utf-8') as f:
        f.write('饼干碎')
    check('J11 enabled=false 时不替换',
          HW.apply_to_file(txt2, {'_workdir': sub,
                                  'hotwords': {'enabled': False, 'path': 'hotwords.md'}}) is False
          and open(txt2, encoding='utf-8').read() == '饼干碎')


def test_docx_healthcheck(tmp, v):
    """[K] 2026-09-24 新增：金句格式、异常内容拦截、热词词边界与文档体检修复。"""
    print('\n[K] 金句格式 / 异常内容过滤 / 热词校验 / 文档体检')
    try:
        import summarize_gemini as S
        import docx_healthcheck as HC
        from docx import Document
        from docx.shared import Pt, RGBColor
    except Exception as e:
        check('K0 导入相关模块', False, str(e))
        return
    quiet = HC._Log(quiet=True)

    # --- K1 markdown 标记剥除 ---
    cases = [
        ('_\u201c\u4f60\u597d\u5440\u3002\u201d_', '\u201c\u4f60\u597d\u5440\u3002\u201d'),
        ('**\u201c\u52a0\u7c97\u5305\u88f9\u3002\u201d**', '\u201c\u52a0\u7c97\u5305\u88f9\u3002\u201d'),
        ('***\u4e09\u661f***', '\u4e09\u661f'),
        ('__\u53cc\u4e0b\u5212\u7ebf__', '\u53cc\u4e0b\u5212\u7ebf'),
        ('- \u5217\u8868\u9879', '\u5217\u8868\u9879'),
        ('# \u6807\u9898\u9879', '\u6807\u9898\u9879'),
        ('`\u4ee3\u7801`', '\u4ee3\u7801'),
        # --- K1b 2026-09-29 新增：字面格式词（9/28 场 gemini-3-flash-preview 输出 40 段）---
        ('\u7c97\u4f53\u5c81\u5df1\u572802:01:00\u65f6\u786e\u8ba4\u3002', '\u5c81\u5df1\u572802:01:00\u65f6\u786e\u8ba4\u3002'),
        ('\u659c\u4f53\u201c\u6211\u4e0d\u77e5\u9053\u3002\u201d', '\u201c\u6211\u4e0d\u77e5\u9053\u3002\u201d'),
        ('**\u7c97\u4f53\u5d4c\u5957\u5e74\u4ee3\u3002**', '\u5d4c\u5957\u5e74\u4ee3\u3002'),
    ]
    bad = [(a, S.strip_md_markers(a)) for a, b in cases if S.strip_md_markers(a) != b]
    check('K1 markdown 标记统一剥除（含单下划线斜体）', not bad, '%r' % bad[:2])

    # --- K2 金句识别 ---
    check('K2 markdown 斜体金句被正确识别',
          S.is_quote_line('_\u201c\u5c0f\u7f16\u4e5f\u4e0d\u77e5\u9053\u3002\u201d_'))
    check('K2b 带时间戳的金句被识别',
          S.is_quote_line('[00:12:00] \u201c\u5c31\u662f\u8fd9\u6837\u3002\u201d'))
    check('K2c 普通正文不被误判为金句',
          not S.is_quote_line('[00:12:00] \u5c81\u5df1\u611f\u8c22\u997c\u5e72\u5c81\u9001\u51fa\u7684\u793c\u7269\u3002'))
    check('K2d 字面「斜体」前缀的金句被正确识别',
          S.is_quote_line('\u659c\u4f53\u201c\u6211\u4e0d\u77e5\u9053\u3002\u201d'))

    # --- K3 提示词回显识别 ---
    leak = '\u201c\u8f93\u5165\u6587\u672c\u6bcf\u4e00\u884c\u90fd\u4ee5 [HH:MM:SS] \u5f00\u5934\uff0c\u8868\u793a\u8be5\u53e5\u5728\u672c\u573a\u76f4\u64ad\u4e2d\u7684\u7edd\u5bf9\u65f6\u95f4\uff1b\u8bf7\u636e\u6b64\u6807\u6ce8\u65f6\u95f4\u3002\u201d'
    check('K3 模型回显的提示词被抓出', bool(S.prompt_leak_hits(leak)))
    check('K3b 金句含「你是」等普通词不被误杀',
          not S.prompt_leak_hits('\u201c\u4f60\u662f\u4e00\u4e2a\u5f88\u597d\u5f88\u597d\u7684\u997c\u3002\u201d'))

    # --- K4 元话语识别 ---
    check('K4 模型开场白被识别',
          S.is_meta_line('\u597d\u7684\uff0c\u8fd9\u662f\u5bf9\u8f93\u5165\u6587\u672c\u8fdb\u884c\u5206\u6bb5\u91cd\u7ec4\u4e0e\u7cbe\u7ec3\u603b\u7ed3\u540e\u7684\u5185\u5bb9\uff1a'))
    check('K4b 「以下是…内容：」被识别',
          S.is_meta_line('\u4ee5\u4e0b\u662f\u6839\u636e\u60a8\u7684\u8981\u6c42\u5bf9\u8f93\u5165\u6587\u672c\u8fdb\u884c\u5206\u6bb5\u91cd\u7ec4\u4e0e\u7cbe\u70bc\u603b\u7ed3\u7684\u5185\u5bb9\uff1a'))
    check('K4c 正常短句不被误判为元话语',
          not S.is_meta_line('\u5c81\u5df1\u611f\u8c22\u997c\u5e72\u5c81\u9001\u51fa\u7684\u793c\u7269\u3002'))

    # --- K5 append_section 端到端：金句落灰色斜体、异常行不落盘 ---
    d = Document()
    seg = ('[00:00:10] \u5c81\u5df1\u5f00\u573a\u3002\n'
           '\u201c\u6b63\u5e38\u91d1\u53e5\u3002\u201d\n'
           '_\u201cmarkdown \u91d1\u53e5\u3002\u201d_\n'
           '\u597d\u7684\uff0c\u8fd9\u662f\u5bf9\u8f93\u5165\u6587\u672c\u8fdb\u884c\u5206\u6bb5\u91cd\u7ec4\u4e0e\u7cbe\u7ec3\u603b\u7ed3\u540e\u7684\u5185\u5bb9\uff1a\n'
           '\u201c\u8f93\u5165\u6587\u672c\u6bcf\u4e00\u884c\u90fd\u4ee5 [HH:MM:SS] \u5f00\u5934\uff0c'
           '\u8868\u793a\u8be5\u53e5\u5728\u672c\u573a\u76f4\u64ad\u4e2d\u7684\u7edd\u5bf9\u65f6\u95f4\uff1b\u8bf7\u636e\u6b64\u6807\u6ce8\u65f6\u95f4\u3002\u201d')
    dropped = S.append_section(d, '\u6d4b\u8bd5\u573a\u6b21', [], [seg],
                               '\u601d\u6e90\u5b8b\u4f53CN Light', True, log=quiet)
    txts = [p.text for p in d.paragraphs]
    joined = '\n'.join(txts)
    check('K5 提示词回显未写入文档',
          '\u8f93\u5165\u6587\u672c\u6bcf\u4e00\u884c\u90fd\u4ee5' not in joined)
    check('K5b 元话语未写入文档',
          '\u597d\u7684\uff0c\u8fd9\u662f\u5bf9\u8f93\u5165\u6587\u672c' not in joined)
    check('K5c dropped 记录了被拦截的行', len(dropped) >= 2, 'dropped=%d' % len(dropped))
    quotes = [p for p in d.paragraphs if S.is_quote_line(p.text)]
    all_ok = bool(quotes) and all(
        q.runs and q.runs[0].italic and not q.runs[0].bold
        and str(q.runs[0].font.color.rgb).upper() == '666666' for q in quotes)
    check('K5d 金句全部落灰色斜体（不再是粗体）', all_ok,
          '金句数=%d' % len(quotes))
    md_left = [t for t in txts if '_' in t and S.is_quote_line(t)]
    check('K5e 金句不再残留 markdown 下划线', not md_left, '%r' % md_left[:2])

    # --- K6 热词词边界（NPC 不得命中 PC） ---
    check('K6 英文原词按词边界匹配（NPC 不命中 PC）',
          not HC._has_residue('\u6211\u9047\u89c1\u4e86NPC\u96f7\u5a1c', 'PC'))
    check('K6b 独立的英文原词仍能命中',
          HC._has_residue('\u6211\u5728PC\u7aef\u73a9', 'PC'))
    check('K6c 中文原词用子串匹配',
          HC._has_residue('\u997c\u5e72\u788e\u4f60\u597d', '\u997c\u5e72\u788e'))

    # --- K7 文档体检：能查出并修复"金句被加粗"与"异常内容" ---
    path = os.path.join(tmp, 'health.docx')
    hd = Document()
    hd.add_paragraph('\u6d4b\u8bd5\u573a\u6b21 2026\u5e7409\u670823\u65e5 20\u70b9\u573a')
    p = hd.add_paragraph()
    r = p.add_run('\u201c\u6211\u662f\u91d1\u53e5\u4f46\u88ab\u52a0\u7c97\u4e86\u3002\u201d')
    r.bold = True
    r.font.size = Pt(11)
    hd.add_paragraph('\u597d\u7684\uff0c\u8fd9\u662f\u5bf9\u8f93\u5165\u6587\u672c\u8fdb\u884c\u5206\u6bb5\u91cd\u7ec4\u4e0e\u7cbe\u7ec3\u603b\u7ed3\u540e\u7684\u5185\u5bb9\uff1a')
    hd.save(path)

    rep = HC.check_doc(path, [], [], fix=False, log=quiet)
    check('K7 体检能查出金句格式与异常内容',
          rep['counts']['quote_format'] >= 1 and rep['counts']['abnormal'] >= 1,
          'counts=%r' % rep['counts'])
    rep2 = HC.check_doc(path, [], [], fix=True, log=quiet)
    hd2 = Document(path)
    q2 = [x for x in hd2.paragraphs if S.is_quote_line(x.text)]
    fixed_ok = bool(q2) and q2[0].runs[0].italic and not q2[0].runs[0].bold
    check('K7b 修复后金句变灰色斜体', fixed_ok)
    left = '\n'.join(x.text for x in hd2.paragraphs)
    check('K7c 修复后异常内容已删除',
          '\u597d\u7684\uff0c\u8fd9\u662f\u5bf9\u8f93\u5165\u6587\u672c' not in left)
    check('K7d 修复过程留了 .bak 备份', os.path.exists(path + '.bak'))
    check('K7e 修复后不留 .part', not os.path.exists(path + '.part'))


def test_bili_upload(tmp, v):
    """[L] 2026-09-26 新增：小号上传模块纯逻辑（默认值/元数据/延时夹取/幂等/缺 cookie 优雅失败）。"""
    print('\n[L] 小号上传模块纯逻辑（无需联网/cookie）')

    class _Log:
        def __call__(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

    try:
        import bili_upload as BU
    except Exception as e:
        check('L0 导入 bili_upload', False, str(e))
        return
    cfg = {'summarize': {'streamer': '岁己'}, 'paths': {'cookies': 'C:/nope_bili.txt'}}
    uc = BU.load_upload_cfg(cfg)
    check('L1 默认 enabled=false / backend=api', (uc['enabled'] is False and uc['backend'] == 'api'))
    check('L1b streamer 取自 summarize', uc['streamer'] == '岁己')

    # 元数据：标题模板渲染 + 80 字截断 + 标签 ≤10
    meta = BU.build_metadata(cfg, uc, {'date_iso': '2026-09-23',
                                      'fname': '录制-25788785-20260923-220000-1-晚场_compressed.mp4'})
    check('L2 标题模板渲染', meta['title'] == '岁己 2026-09-23 直播回放', meta['title'])
    check('L2b copyright 默认自制', meta['copyright'] == 1)
    uc2 = dict(uc)
    uc2['title_template'] = '{title}' + 'x' * 100
    meta2 = BU.build_metadata({'summarize': {'streamer': '岁己'}}, uc2, {'fname': 'abc.mp4'})
    check('L2c 标题截断到 80 字', len(meta2['title']) <= 80, 'len=%d' % len(meta2['title']))
    uc3 = dict(uc)
    uc3['tags'] = ['t%d' % i for i in range(15)]
    meta3 = BU.build_metadata({'summarize': {'streamer': '岁己'}}, uc3, {'fname': 'abc.mp4'})
    check('L2d 标签被截到 ≤10', len(meta3['tags']) <= 10, 'n=%d' % len(meta3['tags']))

    # 延时发布夹取到合法区间（>4.1h 且 ≤15d）
    d1 = BU._norm_dtime(uc, 1_000_000_000)
    check('L3 dtime 默认 6h 落在合法区间（>4.1h）',
          1_000_000_000 + 4.1 * 3600 < d1 <= 1_000_000_000 + 15 * 86400, 'd1=%s' % d1)
    uc_neg = dict(uc)
    uc_neg['dtime_hours'] = 0
    check('L3b dtime=0 返回 None（不延时）', BU._norm_dtime(uc_neg, 1_000_000_000) is None)

    # 缺 cookie 优雅失败（不抛异常）
    ok, info = BU.check_cookie(cfg, uc, _Log())
    check('L4 缺 cookie 文件 → (False, error)', (ok is False and 'error' in info))
    uc_b = dict(uc)
    uc_b['backend'] = 'biliup'
    uc_b['biliup_cookie_json'] = 'C:/nope.json'
    ok2, info2 = BU.check_cookie(cfg, uc_b, _Log())
    check('L4b backend=biliup 缺 json → (False, error)', (ok2 is False and 'error' in info2))

    # 幂等：mark → is_uploaded
    st = {}
    BU.mark_uploaded(st, '2026-09-23', 'BVtest', 'api')
    rec = BU.is_uploaded(st, '2026-09-23')
    check('L5 mark/is_uploaded 幂等记录', rec and rec.get('bvid') == 'BVtest'
          and rec.get('backend') == 'api')
    check('L5b 未上传日期返回 None', BU.is_uploaded(st, '2026-09-24') is None)

    # upload_one：源文件不存在 → 返回 None 不抛异常
    r = BU.upload_one({'summarize': {'streamer': '岁己'}, 'paths': {'cookies': 'C:/nope.txt'}},
                      _Log(), uc, 'C:/nope_src.mp4',
                      {'date_iso': '2026-09-23', 'fname': 'x.mp4'}, {}, st=None, force=False)
    check('L6 upload_one 源不存在 → 返回 None 不抛异常', r is None)


def test_audio_enhance(tmp, v):
    """[M] audio_enhance：自适应前处理的判档、兜底与「默认不动手」约束。

    ★ 历史背景：本能力初版默认 auto（按电平自动处理）。2026-10-01 用真实素材
    做了 8 片段 × 3 条件受控对照，结果是**前处理净收益为负**：
        不处理 95.27% / 仅抬电平 92.43% / 抬+压缩 93.07%（相似度，越高越好）
    因此默认改为 probe（只诊断不改音频），并用回归测试把这个约束钉死，
    防止日后被无意改回 auto 而悄悄损失质量。
    """
    import shutil
    import subprocess
    import audio_enhance as AE

    class _Collect(object):
        def __init__(self):
            self.msgs = []

        def __call__(self, m, level='INFO'):
            self.msgs.append((level, m))

        def error(self, m):
            self.msgs.append(('ERROR', m))

    def prof(speech, floor=-72.0, dyn=None):
        return {'speech_db': speech, 'floor_db': floor,
                'dyn_db': (speech - floor) if dyn is None else dyn,
                'peak_db': -10.0, 'mean_db': speech - 4, 'scan_sec': 0.1}

    auto = {'audio_enhance': 'auto'}

    # --- M1~M4 模式语义 ---
    check('M1 默认模式为 probe（只诊断不改音频）',
          AE.DEFAULTS['audio_enhance'] == 'probe')
    af, tier, _ = AE.decide_chain(prof(-20), {'audio_enhance': 'off'})
    check('M2 mode=off → 不处理', af == '' and tier == 'off')
    af, tier, info = AE.decide_chain(prof(-20), {'audio_enhance': 'probe'})
    check('M3 mode=probe → 产出画像但不动音频', af == '' and tier == 'probe')
    check('M3b probe 画像数值透出', info.get('speech_db') == -20.0)
    lg = _Collect()
    af, tier, info = AE.decide_chain(prof(-46, floor=-42, dyn=4),
                                     {'audio_enhance': 'probe'}, log=lg)
    check('M4 低动态范围 → 低信噪比告警且仍不改音频',
          af == '' and info.get('low_snr') is True
          and any(lv == 'WARN' for lv, _ in lg.msgs))

    # --- M5~M9 auto 判档表 ---
    af, tier, _ = AE.decide_chain(prof(-20), auto)
    check('M5 正常电平 -20dB → 完全不介入', af == '' and tier == 'off')
    af, tier, _ = AE.decide_chain(prof(-24), auto)
    check('M5b 阈值边界 -24dB（阈值 -26）→ 不介入', af == '' and tier == 'off')
    _, tier, info = AE.decide_chain(prof(-27), auto)
    check('M6 轻度 -27dB → light 档 / 抬升 6dB',
          tier == 'light' and info['gain_db'] == 6.0)
    _, tier, info = AE.decide_chain(prof(-38), auto)
    check('M7 中度 -38dB → mid 档 / 抬升 17dB',
          tier == 'mid' and info['gain_db'] == 17.0)
    _, tier, info = AE.decide_chain(prof(-46), auto)
    check('M8 重度 -46dB → strong 档 / 抬升 25dB',
          tier == 'strong' and info['gain_db'] == 25.0)
    _, tier, info = AE.decide_chain(prof(-60), auto)
    check('M9 极端 -60dB → 抬升被 max_gain(26dB) 截断',
          tier == 'strong' and info['gain_db'] == 26.0)
    _, _, info = AE.decide_chain(prof(-60), {'audio_enhance': 'auto',
                                             'enhance_max_gain_db': 10.0})
    check('M9b 自定义 max_gain 生效', info['gain_db'] == 10.0)

    # --- M10~M11 探测失败兜底 ---
    lg = _Collect()
    af, tier, info = AE.decide_chain(None, auto, log=lg)
    check('M10 探测失败 + auto → 保守不处理（宁可不救也不瞎改）',
          af == '' and tier == 'off')
    af, tier, _ = AE.decide_chain(None, {'audio_enhance': 'on'})
    check('M11 探测失败 + on → 保守中档非空链', tier == 'mid' and af != '')

    # --- M12~M13 滤镜链构造 ---
    af, _, _ = AE.decide_chain(prof(-46), {'audio_enhance': 'auto',
                                           'enhance_denoise': True})
    af_off, _, _ = AE.decide_chain(prof(-46), auto)
    check('M12 降噪开关：开→含 afftdn，关→不含',
          'afftdn' in af and 'afftdn' not in af_off)
    parts = af_off.split(',')
    order = [next((i for i, p in enumerate(parts) if p.startswith(k)), -1)
             for k in ('highpass', 'volume', 'acompressor', 'alimiter')]
    check('M13 滤镜顺序：抬电平→压缩→限幅，且限幅在最后',
          order[1] < order[2] < order[3] and order[3] == len(parts) - 1)
    check('M13b 整链以 aformat=fltp 开头（避免中间在 s16 上削波）',
          parts[0].startswith('aformat=sample_fmts=fltp'))

    # --- M14 增益 clamp ---
    check('M14 增益 clamp：无需抬升时返回 0，负值方向不衰减',
          AE._gain_db(-10, -21.0, 26.0) == 0.0
          and AE._gain_db(-40, -21.0, 26.0) == 19.0)

    # --- M15 端到端（需 ffmpeg）：合成固定电平信号，验证探测准确性 ---
    ff = shutil.which('ffmpeg')
    if not ff:
        check('M15 电平探测端到端（无 ffmpeg，跳过）', True)
        return
    sig = os.path.join(tmp, 'sig.wav')
    subprocess.run([ff, '-y', '-v', 'error', '-f', 'lavfi', '-i',
                    'sine=frequency=440:sample_rate=16000:duration=6',
                    '-af', 'volume=-30dB', '-c:a', 'pcm_s16le', sig],
                   check=True)
    p = AE.probe_profile(ff, sig, log=_Collect())
    # ★ 不写死数值：以 ffmpeg 官方 volumedetect 为基准交叉验证。
    #   （曾写死期望 -33dB 而失败，实测源信号本身就是 -51.1dB —— lavfi sine
    #    的基准电平不是满量程；错的是期望值，不是探测逻辑。）
    import re
    vd = subprocess.run([ff, '-hide_banner', '-i', sig,
                         '-af', 'volumedetect', '-f', 'null', '-'],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    m = re.search(r'mean_volume:\s*(-?[\d.]+) dB',
                  vd.stderr.decode('utf-8', 'ignore'))
    ref_db = float(m.group(1)) if m else None
    check('M15 电平探测与 ffmpeg volumedetect 交叉一致（±1.5dB）',
          p is not None and ref_db is not None
          and abs(p['mean_db'] - ref_db) < 1.5,
          'probe=%s / volumedetect=%s'
          % (p['mean_db'] if p else None, ref_db))
    check('M15b 帧数与时长自洽（6s ≈ 60 帧 @100ms）',
          p is not None and 55 <= p['frames'] <= 65,
          '%s' % (p['frames'] if p else None))


def test_docx_toc(tmp, v):
    """[N] 标题样式 / 大纲级别 / 自动目录：导航窗格与 TOC 域的可用性。

    要点：每场标题必须带内置标题样式 + 显式大纲级别；文档开头有且只有一个
    「标题 1（月份）+ 可更新 TOC 域」，且目录必须排在所有场次标题之前。
    front matter 的插入必须幂等（重复跑管线不能堆出一堆目录）。
    """
    print('\n[N] 标题样式 / 大纲级别 / 自动目录（导航窗格 + TOC 域）')
    try:
        import summarize_gemini as S
        from docx import Document
        from docx.oxml.ns import qn
    except Exception as e:
        check('N0 导入 docx 相关模块', False, str(e))
        return

    def levels(d):
        return [x for x in (S.heading_level(p) for p in d.paragraphs) if x]

    p = os.path.join(tmp, 'toc.docx')
    doc, _ = S.open_or_create(p, '宋体')
    S.append_section(doc, '测试场A 2026年10月01日20点场', ['直播日期 2026-10-01'],
                     ['[00:00:10]正文一'], '宋体', True)
    S.append_section(doc, '测试场B 2026年10月02日20点场', ['直播日期 2026-10-02'],
                     ['[00:00:10]正文二'], '宋体', False)

    lv = levels(doc)
    check('N1 月份大标题为大纲级别 1（唯一）', lv.count(1) == 1, str(lv))
    check('N2 每场标题为大纲级别 2', lv.count(2) == 2, str(lv))
    check('N3 存在可更新的 TOC 域', S.has_toc(doc))
    instr = ''.join((t.text or '') for t in doc.element.body.iter(qn('w:instrText')))
    check('N4 TOC 收录 1-3 级并带超链接开关',
          'TOC' in instr and '\\o "1-3"' in instr and '\\h' in instr, instr)
    ps = doc.paragraphs
    first_h2 = next(i for i, x in enumerate(ps) if S.heading_level(x) == 2)
    toc_idx = next(i for i, x in enumerate(ps)
                   if any((t.text or '') for t in x._p.iter(qn('w:instrText'))))
    check('N5 目录排在所有场次标题之前', toc_idx < first_h2,
          'toc=%d first_h2=%d' % (toc_idx, first_h2))
    check('N6 front matter 顺序 标题1→目录→TOC→分页→标题2',
          S.heading_level(ps[0]) == 1 and ps[1].text.strip() == '目 录'
          and toc_idx == 2 and S.heading_level(ps[4]) == 2,
          [x.text[:12] for x in ps[:5]])

    n_before = len(ps)
    S.ensure_front_matter(doc, '测试场A 2026年10月01日20点场', '宋体')
    check('N7 重复调用不重复插入 front matter',
          len(doc.paragraphs) == n_before and levels(doc).count(1) == 1,
          '%d -> %d' % (n_before, len(doc.paragraphs)))

    # 旧文档（已有正文、无目录）追加章节时：补 front matter 且不得吞掉原有正文
    d2 = Document()
    d2.add_paragraph('旧正文段')
    S.append_section(d2, '测试场C 2026年10月03日20点场', [], ['正文三'], '宋体', False)
    texts = [x.text for x in d2.paragraphs]
    check('N8 旧文档补目录后原有正文仍在', '旧正文段' in texts and '正文三' in texts,
          str(texts[:6]))
    check('N9 旧文档同样获得 H1 + TOC',
          levels(d2).count(1) == 1 and S.has_toc(d2))
    first_h2_2 = next(i for i, x in enumerate(d2.paragraphs) if S.heading_level(x) == 2)
    check('N10 front matter 插到文档最前（旧正文在其后）',
          d2.paragraphs[0].text.startswith('2026年10月') and first_h2_2 > 0,
          'p0=%r first_h2=%d' % (d2.paragraphs[0].text, first_h2_2))

    # heading_level / set_outline_level 的单元行为
    d3 = Document()
    pa = d3.add_paragraph('普通段')
    check('N11 普通段落不是标题', S.heading_level(pa) is None, str(S.heading_level(pa)))
    S.set_outline_level(pa, 3)
    check('N12 手工写的大纲级别可被识别', S.heading_level(pa) == 3)
    ol = pa._p.pPr.find(qn('w:outlineLvl'))
    check('N13 outlineLvl 存的是 level-1',
          ol is not None and ol.get(qn('w:val')) == '2',
          ol.get(qn('w:val')) if ol is not None else None)
    S.set_outline_level(pa, 99)
    check('N14 越界级别被夹到 9', S.heading_level(pa) == 9, str(S.heading_level(pa)))

    # 重排章节不能把 front matter（月份标题 + 目录）当成章节搬走
    try:
        import reorder_doc as RD
    except Exception as e:
        check('N15 导入 reorder_doc', False, str(e))
        return
    p3 = os.path.join(tmp, 'toc_reorder.docx')
    S.save_doc_atomic(doc, p3)
    RD.reorder(p3)
    d4 = Document(p3)
    check('N15 重排后 front matter 仍在最前',
          S.heading_level(d4.paragraphs[0]) == 1 and S.has_toc(d4),
          d4.paragraphs[0].text[:20])
    check('N16 重排后场次数量不变', levels(d4).count(2) == 2, str(levels(d4)))

    # 月度文档：按实际年月新建，往月文档只读归档，绝不跨月混写
    import datetime as _dt
    docs = os.path.join(tmp, 'docs')
    os.makedirs(docs, exist_ok=True)
    cfg = {'output': {'doc_dir': docs, 'font': '宋体',
                      'doc_name_pattern': '岁己直播总结_{year}年{month}月.docx'},
           'summarize': {}}
    p1, c1 = S.ensure_month_doc(cfg, None, today=_dt.date(2026, 10, 1))
    check('N17 月度文档按实际年月新建',
          c1 and os.path.basename(p1) == '岁己直播总结_2026年10月.docx',
          os.path.basename(p1))
    d5 = Document(p1)
    check('N17b 新文档一建立就带月份大标题与目录',
          S.heading_level(d5.paragraphs[0]) == 1
          and d5.paragraphs[0].text == '2026年10月 直播总结' and S.has_toc(d5),
          d5.paragraphs[0].text[:24])
    p2, c2 = S.ensure_month_doc(cfg, None, today=_dt.date(2026, 10, 1))
    check('N18 重复调用不重建也不重复插目录',
          (not c2) and p2 == p1
          and len(Document(p2).paragraphs) == len(d5.paragraphs),
          'created=%s %d vs %d' % (c2, len(Document(p2).paragraphs), len(d5.paragraphs)))
    p3, c3 = S.ensure_month_doc(cfg, None, today=_dt.date(2026, 11, 1))
    check('N18b 换月即新建另一份文档（不跨月混写）',
          c3 and os.path.basename(p3) == '岁己直播总结_2026年11月.docx',
          os.path.basename(p3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-v', '--verbose', action='store_true')
    args = ap.parse_args()
    tmp = tempfile.mkdtemp(prefix='robust_selftest_')
    print('临时目录：%s' % tmp)
    try:
        test_download(tmp, args.verbose)
        test_atomic(tmp, args.verbose)
        test_lock(tmp, args.verbose)
        test_docx(tmp, args.verbose)
        test_compress(tmp, args.verbose)
        test_cleanup_lock(tmp, args.verbose)
        test_ffmpeg_part_output(tmp, args.verbose)
        test_hotwords(tmp, args.verbose)
        test_docx_healthcheck(tmp, args.verbose)
        test_bili_upload(tmp, args.verbose)
        test_audio_enhance(tmp, args.verbose)
        test_docx_toc(tmp, args.verbose)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print('\n' + '=' * 60)
    print('通过 %d 项，失败 %d 项' % (len(PASS), len(FAIL)))
    for name, detail in FAIL:
        print('  ❌ %s  %s' % (name, detail))
    print('=' * 60)
    return 1 if FAIL else 0


if __name__ == '__main__':
    sys.exit(main())
