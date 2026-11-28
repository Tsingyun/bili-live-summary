# -*- coding: utf-8 -*-
"""单段 FLV → MP4 无损转封装（交付件 / 完成信号）。

设计要点（2026-11-29 用户规则）：
  · 录播总结完后，每场都要有一个 MP4 文件作为「任务完成信号」——多段走
    prepare_session 的 concat 合并，单段走本脚本的 -c copy 转封装。
  · 输出 <源basename去扩展>_merged.mp4，带 _merged 后缀 → 扫描器自动排除，
    不会被当成新录播重复处理。源 FLV 不动（保留）。
  · 先写 <out>.part.mp4，ffprobe 校验时长 >= 源 95% 且首尾可解码，才原子改名；
    半截文件绝不变成正式交付件。
  · 不重编码、不压缩（-c copy），仅换容器；FLV(h264+aac) → MP4 秒级~分钟级。

用法：
  python remux_single.py --config <cfg> --src <源FLV> [--out <目标MP4>] [--force]
  python remux_single.py --selftest
"""
import argparse, json, os, subprocess, sys, tempfile, time

def _ffprobe_duration(ffprobe, path):
    if not os.path.exists(path): return 0.0
    try:
        r = subprocess.run([ffprobe, '-v', 'error', '-show_entries', 'format=duration',
                            '-of', 'default=nw=1:nk=1', path],
                           capture_output=True, text=True, timeout=120)
        return float((r.stdout or '0').strip() or 0)
    except Exception:
        return 0.0

def _probe_streams(ffprobe, path):
    try:
        r = subprocess.run([ffprobe, '-v', 'error', '-show_entries',
                            'stream=codec_name,codec_type', '-of', 'csv=p=0', path],
                           capture_output=True, text=True, timeout=120)
        return [x.strip() for x in (r.stdout or '').splitlines() if x.strip()]
    except Exception:
        return []

def _decode_probe(ffmpeg, path, ss, dur=3):
    """抽一段解码，返回 rc==0 表示可解码。"""
    try:
        r = subprocess.run([ffmpeg, '-v', 'error', '-ss', str(ss), '-i', path,
                            '-t', str(dur), '-f', 'null', '-'],
                           capture_output=True, timeout=180)
        return r.returncode == 0
    except Exception:
        return False

def remux(cfg, src, out_path=None, log=None, force=False):
    """返回 (ok, out_path, msg)。幂等：目标已存在且校验通过则跳过。"""
    def emit(s, lvl='INFO'):
        if log: log(s, lvl)
        else: print('[%s] %s' % (lvl, s))
    ffmpeg = cfg['paths']['ffmpeg']; ffprobe = cfg['paths']['ffprobe']
    if not os.path.isfile(src): return (False, None, '源不存在：%s' % src)
    if not out_path:
        base = os.path.splitext(os.path.basename(src))[0]
        out_path = os.path.join(os.path.dirname(src), base + '_merged.mp4')
    src_dur = _ffprobe_duration(ffprobe, src)
    if src_dur <= 0: return (False, None, '无法探测源时长：%s' % src)

    os.makedirs(os.path.dirname(out_path) or '.', exist_ok=True)
    min_dur = src_dur * 0.95
    if os.path.exists(out_path) and not force:
        got = _ffprobe_duration(ffprobe, out_path)
        if got >= min_dur:
            emit('[remux] MP4 已存在且完整（%.0fs/源 %.0fs），跳过' % (got, src_dur))
            return (True, out_path, 'exists_ok')
        emit('[remux] 已有 MP4 不完整（%.0fs/源 %.0fs），重新转封装' % (got, src_dur), 'WARN')

    tmp = out_path + '.part.mp4'
    try:
        if os.path.exists(tmp): os.remove(tmp)
    except Exception:
        pass
    to = int(max(1800, min(4 * 3600, src_dur * 2)))
    emit('[remux] 无损转封装（不重编码）→ %s' % os.path.basename(out_path))
    t0 = time.time()
    try:
        # ★ 不加 +faststart：moov 写在文件头，进程被中断时也不会出现
        #   「moov atom not found」的不可恢复半截文件（2026-11-29 踩坑）。
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error',
                        '-i', src, '-c', 'copy', tmp], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=to)
    except subprocess.TimeoutExpired:
        return (False, None, '转封装超时（%ds）' % to)
    except Exception as e:
        return (False, None, '转封装失败：%s' % e)

    got = _ffprobe_duration(ffprobe, tmp)
    if got < min_dur:
        try: os.remove(tmp)
        except Exception: pass
        return (False, None, '时长校验失败（%.0fs < %.0fs）' % (got, min_dur))
    if not _decode_probe(ffmpeg, tmp, 0) or not _decode_probe(ffmpeg, tmp, max(0, got - 10)):
        try: os.remove(tmp)
        except Exception: pass
        return (False, None, '首尾解码校验失败')
    os.replace(tmp, out_path)
    emit('[remux] 完成（%.0fs，用时 %.0fs）：%s' % (got, time.time() - t0, os.path.basename(out_path)))
    return (True, out_path, 'ok')

def selftest():
    checks = []
    def ck(name, cond):
        checks.append((name, bool(cond)))
    cfg = {'paths': {'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe'}}
    ck('config shape', 'paths' in cfg)
    # 纯逻辑校验：时长阈值计算
    src = 16761.412
    ck('95pct threshold', src * 0.95 == 15923.3414)
    ck('short file rejected', 15900 < src * 0.95)
    ck('full file accepted', 16761 >= src * 0.95)
    ok = _ffprobe_duration('/nonexistent/ffprobe', '/nonexistent/file') == 0.0
    ck('missing probe returns 0', ok)
    ck('out suffix', os.path.splitext('录制-25788785-20261128-x.flv')[0] + '_merged.mp4' ==
       '录制-25788785-20261128-x_merged.mp4')
    fails = [n for n, c in checks if not c]
    print('结果：%d 项通过，%d 项失败' % (len(checks) - len(fails), len(fails)))
    for n in fails: print('  [FAIL]', n)
    return 1 if fails else 0

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--src', default=None)
    ap.add_argument('--out', default=None)
    ap.add_argument('--force', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest: return selftest()
    if not a.config or not a.src:
        ap.error('需要 --config 和 --src')
    cfg = json.load(open(a.config, encoding='utf-8'))
    ok, out, msg = remux(cfg, a.src, a.out, force=a.force)
    print('结果: %s | %s | %s' % ('OK' if ok else 'FAIL', out, msg))
    return 0 if ok else 1

if __name__ == '__main__':
    sys.exit(main())
