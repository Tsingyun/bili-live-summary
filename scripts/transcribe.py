#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 3：FunASR 语音转文字，输出带时间轴的 TXT 与标准 SRT。

模型：SenseVoiceSmall + fsmn-vad + punc_ct-transformer（首次运行自动从 ModelScope 下载）
超过阈值时长的音频自动切块转写并做时间轴偏移，避免显存溢出。

用法:
  python transcribe.py --mp3 "WORKDIR/MP3/20260801-xxx.mp3"
  python transcribe.py --all          # 处理 MP3 目录下所有还没转写的
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import time

from common import load_config, Logger, hms, probe_duration

# SenseVoice 富文本标签：<|zh|><|NEUTRAL|><|Speech|><|woitn|> 等
TAG_RE = re.compile(r'<\|([^|]*)\|>')
PUNCT = '，。、？！；：,.?!;:~…'

# 非语音事件标签：BGM / 掌声 / 笑声等，配上极短文本基本都是幻听
NON_SPEECH_EVENTS = {'BGM', 'Applause', 'Laughter', 'Cry', 'Sneeze', 'Cough', 'Breath'}

# 开播前放 BGM 时 SenseVoice 常幻听出的垃圾短句
JUNK_RE = re.compile(
    r'^(the|a|an|and|so|oh|ah|um|uh|uhh|yeah|ok|okay|hmm+|mm+|mhm|bye|you|i|it|is|to|of|'
    r'thank you|thanks|thank you\.?|please|music|applause|'
    r'嗯+|呃+|啊+|哦+|噢+|唔+|哈+|喔+|の|な|は|、)'
    r'[\s，。、？！；：,.?!;:~…\-]*$',
    re.IGNORECASE)


def is_junk(text, events=()):
    """判断是否为 BGM/静音段落产生的幻听短句。"""
    t = (text or '').strip()
    if not t:
        return True
    if JUNK_RE.match(t):
        return True
    core = re.sub(r'[\s' + re.escape(PUNCT) + r']', '', t)
    if len(core) <= 1:
        return True
    # 被标注为非语音事件，且内容极短
    if events and set(events) & NON_SPEECH_EVENTS and len(core) <= 6:
        return True
    return False


def init_torch(log):
    """import torch 带重试 —— 部分环境存在偶发 CUDA 初始化段错误。"""
    import faulthandler
    faulthandler.enable()
    for attempt in range(1, 6):
        try:
            import torch
            return torch
        except Exception as e:
            log.error(f'import torch 第 {attempt}/5 次失败：{e}')
            if attempt == 5:
                raise
            time.sleep(3)


def clean_text(t):
    """清洗 SenseVoice 输出：去标签、折叠重复标点、去句首标点。"""
    t = TAG_RE.sub('', t or '')
    t = t.replace('\u3000', ' ').strip()
    t = re.sub(r'([，。、？！；：,.?!;:])\1{1,}', r'\1', t)
    t = re.sub(r'[，。、？！；：,.?!;:]{3,}', lambda m: m.group(0)[0], t)
    t = t.lstrip(PUNCT + ' ')
    return t.strip()


def is_meaningful(t):
    return bool(t) and bool(re.sub(r'[\s' + re.escape(PUNCT) + r']', '', t))


def load_model(cfg, log):
    torch = init_torch(log)
    want = cfg['transcribe'].get('device', 'auto')
    if want == 'cpu':
        device = 'cpu'
    elif torch.cuda.is_available():
        device = 'cuda:0'
    else:
        device = 'cpu'
    log(f'PyTorch {torch.__version__} / CUDA {torch.version.cuda} / 设备 {device}')
    if device.startswith('cuda'):
        log(f'GPU: {torch.cuda.get_device_name(0)}')
    else:
        log('未检测到可用 GPU，退回 CPU（长音频会非常慢）', 'WARN')

    from funasr import AutoModel
    log('加载 FunASR 模型 (SenseVoiceSmall + VAD + PUNC) ...')
    model = AutoModel(
        model='iic/SenseVoiceSmall',
        vad_model='iic/speech_fsmn_vad_zh-cn-16k-common-pytorch',
        vad_kwargs={'max_single_segment_time': 30000},
        punc_model='iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch',
        device=device,
        disable_update=True,
    )
    log('模型加载完成')
    return model


_VAD_MODEL = None

def _get_vad_model(device, log):
    """独立加载 VAD 模型（仅用于时间戳回退），进程内缓存一次。"""
    global _VAD_MODEL
    if _VAD_MODEL is None:
        from funasr import AutoModel
        _VAD_MODEL = AutoModel(
            model='iic/speech_fsmn_vad_zh-cn-16k-common-pytorch',
            device=device, disable_update=True)
        if log:
            try:
                log('VAD 模型(独立)已加载，用于时间戳回退')
            except Exception:
                pass
    return _VAD_MODEL


def _try_sentence_timestamp(model, audio_path, cfg, offset_sec, log):
    """首选：funasr 原生 sentence_timestamp。失败或未产出时间戳则返回 []。"""
    try:
        res = model.generate(
            input=audio_path, cache={}, language='auto', use_itn=True,
            batch_size_s=cfg['transcribe'].get('batch_size_s', 300),
            ncpu=cfg['transcribe'].get('ncpu', 4),
            merge_vad=True, merge_length_s=15, sentence_timestamp=True)
    except Exception as e:
        if log:
            try:
                log(f'sentence_timestamp 失败（{e}），回退 VAD 分段', 'WARN')
            except Exception:
                pass
        return []
    if not res:
        return []
    item = res[0]
    out = []
    sents = item.get('sentence_info') or []
    if sents:
        for s in sents:
            raw = s.get('text', '')
            events = TAG_RE.findall(raw)
            txt = clean_text(raw)
            if not is_meaningful(txt) or is_junk(txt, events):
                continue
            st = float(s.get('start', 0)) / 1000.0 + offset_sec
            en = float(s.get('end', st * 1000)) / 1000.0 + offset_sec
            out.append((st, max(en, st + 0.5), txt))
        return out
    ts = item.get('timestamp')
    txt = clean_text(item.get('text', ''))
    if ts and txt:
        st = float(ts[0][0]) / 1000.0 + offset_sec
        en = float(ts[-1][1]) / 1000.0 + offset_sec
        return [(st, en, txt)]
    return []


def _vad_fallback(model, audio_path, cfg, offset_sec, log):
    """兜底：VAD 分段 + 逐段 ASR，兼容 funasr 1.3.x 不再返回时间戳的情况。
    返回 [(start_sec, end_sec, text), ...]，start/end 为绝对秒（含 offset）。"""
    import soundfile as sf
    import re as _re
    try:
        import torch as _torch
        device = 'cuda:0' if _torch.cuda.is_available() else 'cpu'
    except Exception:
        device = 'cpu'
    try:
        vad = _get_vad_model(device, log)
        vr = vad.generate(input=audio_path)
        raw = vr[0].get('value') or []
        if not raw:
            return []
        wav, sr = sf.read(audio_path, dtype='float32')
        out = []
        for (s_ms, e_ms) in raw:
            s = int(s_ms / 1000.0 * sr)
            e = int(e_ms / 1000.0 * sr)
            if e <= s:
                continue
            slice_w = wav[s:e]
            try:
                r = model.generate(input=slice_w, cache={}, language='auto',
                                   use_itn=True, batch_size_s=300,
                                   ncpu=cfg['transcribe'].get('ncpu', 4),
                                   merge_vad=True, merge_length_s=15)
                txt = _re.sub(r'<\|[^|]+\|>', '', r[0].get('text', '') or '').strip()
            except Exception:
                continue
            if not txt:
                continue
            events = TAG_RE.findall(txt)
            ct = clean_text(txt)
            if not is_meaningful(ct) or is_junk(ct, events):
                continue
            st = s_ms / 1000.0 + offset_sec
            en = e_ms / 1000.0 + offset_sec
            out.append((st, max(en, st + 0.5), ct))
        if log:
            try:
                log(f'VAD 回退产出 {len(out)} 句')
            except Exception:
                pass
        return out
    except Exception as e:
        if log:
            try:
                log(f'VAD 回退也失败：{e}', 'ERROR')
            except Exception:
                pass
        return []


def asr_segments(model, audio_path, cfg, offset_sec=0.0, log=None):
    """转写一个音频（块），返回 [(start_sec, end_sec, text), ...]
    优先用 funasr 原生 sentence_timestamp；若该版本不出时间戳，则 VAD 分段兜底。"""
    segs = _try_sentence_timestamp(model, audio_path, cfg, offset_sec, log)
    if segs:
        return segs
    return _vad_fallback(model, audio_path, cfg, offset_sec, log)


def split_audio(ffmpeg, src, work_dir, chunk_sec, log):
    os.makedirs(work_dir, exist_ok=True)
    pattern = os.path.join(work_dir, 'chunk_%04d.wav')
    subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src,
                    '-f', 'segment', '-segment_time', str(int(chunk_sec)),
                    '-ac', '1', '-ar', '16000', pattern],
                   capture_output=True, text=True, timeout=7200)
    chunks = sorted(glob.glob(os.path.join(work_dir, 'chunk_*.wav')))
    log(f'  切分为 {len(chunks)} 块，每块 {chunk_sec // 60} 分钟')
    return chunks


def dedup_merge(segs):
    """同一秒的碎片合并成一行，并按时间排序。"""
    segs.sort(key=lambda x: x[0])
    merged = []
    for st, en, txt in segs:
        if merged and int(merged[-1][0]) == int(st):
            p = merged[-1]
            joiner = '' if p[2][-1:] in PUNCT else '，'
            merged[-1] = (p[0], max(p[1], en), p[2] + joiner + txt)
        else:
            merged.append((st, en, txt))
    return merged


def write_txt(path, segs, meta):
    with open(path, 'w', encoding='utf-8') as f:
        f.write('# 转写日志\n')
        for k, v in meta.items():
            f.write(f'# {k}: {v}\n')
        f.write('#' + '=' * 55 + '\n\n')
        for st, _en, txt in segs:
            f.write(f'[{hms(st)}] {txt}\n')


def srt_time(sec):
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec)
    return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}'


def write_srt(path, segs):
    with open(path, 'w', encoding='utf-8') as f:
        for i, (st, en, txt) in enumerate(segs, 1):
            nxt = segs[i][0] if i < len(segs) else en
            end = min(max(en, st + 0.8), nxt if nxt > st else en, st + 30)
            f.write(f'{i}\n{srt_time(st)} --> {srt_time(end)}\n{txt}\n\n')


def transcribe_one(cfg, mp3_path, model, log):
    stem = os.path.splitext(os.path.basename(mp3_path))[0]
    txt_path = os.path.join(cfg['_dirs']['txt'], stem + '.txt')
    srt_path = os.path.join(cfg['_dirs']['srt'], stem + '.srt')

    if os.path.exists(txt_path) and os.path.getsize(txt_path) > 200 and os.path.exists(srt_path):
        log(f'转写结果已存在，跳过：{stem}')
        return txt_path

    P = cfg['paths']
    dur = probe_duration(P['ffprobe'], mp3_path)
    t0 = time.time()
    log(f'开始转写 {stem}（{hms(dur)}）')

    threshold = cfg['transcribe'].get('chunk_hours_threshold', 4) * 3600
    if dur > threshold:
        chunk_sec = cfg['transcribe'].get('chunk_minutes', 30) * 60
        work = os.path.join(cfg['_dirs']['tmp'], 'asr_' + stem[:40])
        log(f'  时长超过 {threshold // 3600}h，启用分块转写')
        chunks = split_audio(P['ffmpeg'], mp3_path, work, chunk_sec, log)
        segs = []
        offset = 0.0
        for i, c in enumerate(chunks):
            # 用实际块时长累加偏移，而不是假设每块都恰好 chunk_sec
            c_dur = probe_duration(P['ffprobe'], c) or chunk_sec
            log(f'  [{i + 1}/{len(chunks)}] 偏移 {hms(offset)} ...')
            segs.extend(asr_segments(model, c, cfg, offset_sec=offset, log=log))
            offset += c_dur
            try:
                os.remove(c)
            except Exception:
                pass
        try:
            os.rmdir(work)
        except Exception:
            pass
    else:
        segs = asr_segments(model, mp3_path, cfg, log=log)

    segs = dedup_merge([s for s in segs if is_meaningful(s[2]) and not is_junk(s[2])])
    if not segs:
        log.error(f'  转写结果为空：{stem}')
        return None

    elapsed = time.time() - t0
    chars = sum(len(s[2]) for s in segs)
    write_txt(txt_path, segs, {
        '文件名': os.path.basename(mp3_path),
        '转写时间': time.strftime('%Y-%m-%d %H:%M:%S'),
        '音频时长': f'{int(dur)}s ({hms(dur)})',
        '文本字数': chars,
        '分句数量': len(segs),
        '模型': 'SenseVoiceSmall',
    })
    write_srt(srt_path, segs)
    log(f'  完成：{len(segs)} 句 / {chars} 字 / 耗时 {elapsed:.0f}s '
        f'（{dur / max(elapsed, 1):.1f}x 实时）')
    return txt_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--config', default=None)
    ap.add_argument('--mp3', default=None)
    ap.add_argument('--all', action='store_true')
    args = ap.parse_args()

    cfg = load_config(args.config)
    log = Logger(cfg, 'asr')

    if args.mp3:
        files = [args.mp3]
    else:
        files = sorted(glob.glob(os.path.join(cfg['_dirs']['mp3'], '*.mp3')))
    if not files:
        log('没有待转写的 MP3')
        return

    model = load_model(cfg, log)
    ok = 0
    for f in files:
        try:
            if transcribe_one(cfg, f, model, log):
                ok += 1
        except Exception as e:
            import traceback
            log.error(f'{os.path.basename(f)} 转写异常：{e}')
            traceback.print_exc()
    log(f'转写完成 {ok}/{len(files)}')
    sys.exit(0 if ok == len(files) else 1)


if __name__ == '__main__':
    main()
