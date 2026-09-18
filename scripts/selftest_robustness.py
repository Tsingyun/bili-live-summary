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
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead_pid = child.pid
    child.wait()
    del child
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
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    dead_pid = child.pid
    child.wait()
    del child
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
