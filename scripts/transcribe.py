#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
阶段 3：FunASR 语音转文字，输出带时间轴的 TXT 与标准 SRT。

模型：由 config.transcribe.model 选择 —— "nano"（默认，Fun-ASR-Nano-2512）
或 "sensevoice"（SenseVoiceSmall + fsmn-vad + punc_ct-transformer）。
首次运行自动从 ModelScope 下载对应权重。
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

from common import (load_config, Logger, hms, probe_duration,
                    clean_segment_boundaries, normalize_numbers,
                    force_unlink, force_rmtree)

import audio_enhance as AE   # 低声压/耳语场景的自适应音频前处理

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
    # ★ 2026-11-25 P0-2：固定 CPU 线程数，避免 PyTorch 按机器核数自起线程
    #   造成不同场次间推理速度大幅波动（与 ncpu 配置保持一致）。
    try:
        torch.set_num_threads(max(1, int(cfg['transcribe'].get('ncpu', 4))))
    except Exception:
        pass
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
    kind = str(cfg['transcribe'].get('model', 'sensevoice')).lower()
    cfg['_asr_kind'] = kind
    if kind == 'nano':
        # Fun-ASR-Nano-2512：0.8B（SenseVoice 编码器 + Qwen3-0.6B 解码器），
        # 中文更准、官方主推、可持续维护。原生不保证返回时间戳（模型卡 TODO），
        # 时间轴统一由独立 fsmn-vad 切段兜底（见 _vad_fallback）。
        # ★ 必须带 vad_model：Nano 不带 vad_model 时 generate(numpy 切片) 会抛异常，
        #   逐段 ASR 全部失败 → 0 句。带 vad_model 后单切片内部走 VAD→ASR 正常。
        log('加载 FunASR 模型 (Fun-ASR-Nano-2512) ...')
        model = AutoModel(
            model='FunAudioLLM/Fun-ASR-Nano-2512',
            trust_remote_code=True,
            vad_model='iic/speech_fsmn_vad_zh-cn-16k-common-pytorch',
            vad_kwargs={'max_single_segment_time': 30000},
            device=device,
            disable_update=True,
        )
        log('Fun-ASR-Nano-2512 加载完成')
    else:
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
    model._asr_kind = kind
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
    # Nano 原生不保证返回时间戳（官方模型卡 TODO），直接走 VAD 切段兜底
    if getattr(model, '_asr_kind', 'sensevoice') == 'nano':
        return []
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
    _vad_fallback._errs = 0
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
        kind = getattr(model, '_asr_kind', 'sensevoice')

        def _emit(s_ms, e_ms, txt):
            if not txt:
                return
            events = TAG_RE.findall(txt) if kind != 'nano' else ()
            ct = clean_text(txt)
            if not is_meaningful(ct) or is_junk(ct, events):
                return
            st = s_ms / 1000.0 + offset_sec
            en = e_ms / 1000.0 + offset_sec
            out.append((st, max(en, st + 0.5), ct))

        def _one_text(slice_w):
            """旧逐段调用：SenseVoice 路径 + 批量失败时的安全兜底。"""
            try:
                if kind == 'nano':
                    r = model.generate(input=slice_w, cache={}, language='中文',
                                       itn=True, batch_size=1)
                else:
                    r = model.generate(input=slice_w, cache={}, language='auto',
                                       use_itn=True, batch_size_s=300,
                                       ncpu=cfg['transcribe'].get('ncpu', 4),
                                       merge_vad=True, merge_length_s=15)
                # ★ 2026-10-27：Nano 对个别切片（极短/边界段）会返回空列表而非异常，
                #   判空后跳过，避免静默丢失的异常被误当成真实失败。
                if not r:
                    return ''
                if kind == 'nano':
                    return (r[0].get('text', '') or '').strip()
                return _re.sub(r'<\|[^|]+\|>', '', r[0].get('text', '') or '').strip()
            except Exception as e:
                # 逐段 ASR 异常不再静默吞掉：前 3 次打印，便于排查（曾因 Nano 未带
                # vad_model 导致 generate(numpy) 全段失败、整场 0 句却无报错）
                if log and _vad_fallback._errs < 3:
                    try:
                        log(f'  逐段 ASR 异常（跳过该段）：{type(e).__name__}: {e}', 'WARN')
                        _vad_fallback._errs += 1
                    except Exception:
                        pass
                return ''

        def _batch_texts(items):
            """Nano 批量：一次 generate(list) → FunASR 内部按 batch_size_s 动态组批。

            结果按输入顺序一一对应（inference_with_vad 的 results_ret_list 按 sample
            顺序 append，空语音也会返回一个 text='' 的占位），可直接 zip 回原时间段。
            返回数量不符或抛异常时无条件回退逐段，绝不丢段。
            """
            try:
                rr = model.generate(input=[it[2] for it in items], cache={},
                                    language='中文', itn=True,
                                    batch_size_s=batch_sec,
                                    batch_size_threshold_s=60,
                                    disable_pbar=True)
                if rr is None or len(rr) != len(items):
                    raise ValueError('批量返回数量 %s != 输入 %d'
                                     % (len(rr) if rr is not None else 'None', len(items)))
                return [((x or {}).get('text', '') or '').strip() for x in rr]
            except Exception as e:
                if log:
                    try:
                        log(f'  批量 ASR 失败，回退逐段：{type(e).__name__}: {e}', 'WARN')
                    except Exception:
                        pass
                return [_one_text(it[2]) for it in items]

        # ★ 2026-11-25 P0-2：实测同样约 4000 个 VAD 段，耗时 3043s(6.8x) 与
        #   1637s(13.6x) 相差近 2 倍，根因是逐段 generate 的固定开销被段数放大。
        #   把若干段打包成一次 generate(list)，由 FunASR 内部动态组批摊薄固定开销，
        #   既提速也把耗时从「随段数波动」压回「随音频时长稳定增长」。
        batch_sec = float(cfg['transcribe'].get('asr_batch_size_s', 120) or 0)
        use_batch = kind == 'nano' and batch_sec > 0
        items, cur_sec, nbatch = [], 0.0, 0
        t_batch = time.time()
        for (s_ms, e_ms) in raw:
            s = int(s_ms / 1000.0 * sr)
            e = int(e_ms / 1000.0 * sr)
            if e <= s:
                continue
            seg_sec = (e - s) / float(sr)
            item = (s_ms, e_ms, wav[s:e])
            if not use_batch:
                _emit(s_ms, e_ms, _one_text(item[2]))
                continue
            # 按累计音频秒数打批；单批最多 64 段、目标 batch_sec 秒，控制显存峰值
            if items and (cur_sec + seg_sec > batch_sec or len(items) >= 64):
                for (a, b, _w), txt in zip(items, _batch_texts(items)):
                    _emit(a, b, txt)
                nbatch += 1
                items, cur_sec = [], 0.0
            items.append(item)
            cur_sec += seg_sec
        if items:
            texts = _batch_texts(items) if use_batch else [_one_text(it[2]) for it in items]
            for (a, b, _w), txt in zip(items, texts):
                _emit(a, b, txt)
            if use_batch:
                nbatch += 1
        if use_batch and log:
            try:
                log(f'  ASR 批量推理：{len(raw)} 段 → {nbatch} 批'
                    f'（目标 {batch_sec:.0f}s/批），耗时 {time.time() - t_batch:.0f}s')
            except Exception:
                pass
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
    """按 chunk_sec 切分音频（带超时）。返回切块列表。

    ★ 2026-09-18：加超时——切分挂死会阻塞整轮且无法被清理；
    同时校验切块非空，避免"切了 0 块"被当成转写成功（产出空 TXT）。
    """
    os.makedirs(work_dir, exist_ok=True)
    pattern = os.path.join(work_dir, 'chunk_%04d.wav')
    to = int(max(1800, min(6 * 3600, chunk_sec * 60)))
    try:
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-i', src,
                        '-f', 'segment', '-segment_time', str(int(chunk_sec)),
                        '-ac', '1', '-ar', '16000', pattern],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                       timeout=to)
    except subprocess.TimeoutExpired:
        log.error(f'  音频切分超时（{to}s）已终止')
        return []
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
    """写转写稿（原子写：.part → os.replace）。

    ★ 2026-09-18：TXT 是总结的输入，下游判据是「存在且 >200 字节」。若进程在写
    TXT 中途被杀，会留下一个**已经超过 200 字节的截断稿**，下次运行会当作"已转写"
    直接跳过 → 总结缺掉后半场。原子写保证 TXT 要么不存在、要么完整。
    """
    lines = ['# 转写日志\n']
    for k, v in meta.items():
        lines.append(f'# {k}: {v}\n')
    lines.append('#' + '=' * 55 + '\n\n')
    for st, _en, txt in segs:
        lines.append(f'[{hms(st)}] {txt}\n')
    try:
        from common import atomic_write_text
        atomic_write_text(path, ''.join(lines))
    except Exception:
        # 兜底：原子写失败（极罕见）时退回直接写，至少不丢产物
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(lines))


def srt_time(sec):
    ms = int(round((sec - int(sec)) * 1000))
    s = int(sec)
    return f'{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d},{ms:03d}'


def write_srt(path, segs):
    """写 SRT（原子写，理由同 write_txt）。"""
    buf = []
    for i, (st, en, txt) in enumerate(segs, 1):
        nxt = segs[i][0] if i < len(segs) else en
        end = min(max(en, st + 0.8), nxt if nxt > st else en, st + 30)
        buf.append(f'{i}\n{srt_time(st)} --> {srt_time(end)}\n{txt}\n\n')
    try:
        from common import atomic_write_text
        atomic_write_text(path, ''.join(buf))
    except Exception:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(''.join(buf))


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

    # ---- 低声压（耳语/ASMR）自适应前处理 --------------------------------------
    # 判据来自音频自身的帧级能量分布，不看文件名/标题：
    #   · 发声段电平达标 → 一条滤镜都不加，与原链路完全一致（普通场次零扰动）
    #   · 电平偏低 → 按档位抬升 + 压缩动态（实测可把耳语素材的识别率补回 100%）
    #   · 探测或转码失败 → 降级为不处理，只告警不中断
    asr_src = mp3_path
    enh_wav = None
    enh_desc = '未启用'
    try:
        prof = AE.probe_profile(P['ffmpeg'], mp3_path, log)
        af, tier, info = AE.decide_chain(prof, cfg['transcribe'], log)
        if prof:
            log(f'  电平画像：发声段 {prof["speech_db"]}dB / 停顿-底噪 '
                f'{prof["floor_db"]}dB / 动态范围 {prof["dyn_db"]}dB'
                f'（扫描 {prof["scan_sec"]}s）')
        enh_desc = AE.describe(prof, tier, info).replace('音频前处理：', '')
        log('  音频前处理：' + enh_desc)
        if af:
            enh_wav = os.path.join(cfg['_dirs']['tmp'], 'enh_' + stem[:40] + '.wav')
            if AE.enhance_to_wav(P['ffmpeg'], mp3_path, enh_wav, af, log):
                asr_src = enh_wav
            else:
                enh_wav = None
                log('  前处理未生效，按原始音频继续转写', 'WARN')
    except Exception as e:
        enh_wav = None
        log(f'  前处理链路异常，按原始音频继续转写：{e}', 'WARN')

    try:
        threshold = cfg['transcribe'].get('chunk_hours_threshold', 2) * 3600
        if dur > threshold:
            chunk_sec = cfg['transcribe'].get('chunk_minutes', 30) * 60
            work = os.path.join(cfg['_dirs']['tmp'], 'asr_' + stem[:40])
            log(f'  时长超过 {threshold // 3600}h，启用分块转写')
            chunks = split_audio(P['ffmpeg'], asr_src, work, chunk_sec, log)
            if not chunks:
                log.error(f'  音频切分为 0 块，转写失败：{stem}')
                return None
            segs = []
            offset = 0.0
            failed = []
            for i, c in enumerate(chunks):
                # 用实际块时长累加偏移，而不是假设每块都恰好 chunk_sec
                c_dur = probe_duration(P['ffprobe'], c) or chunk_sec
                log(f'  [{i + 1}/{len(chunks)}] 偏移 {hms(offset)} ...')
                got = asr_segments(model, c, cfg, offset_sec=offset, log=log)
                # ★ 2026-09-18：某一块整块转写失败（返回空）以前会被静默跳过，最终 TXT
                #   仍然"非空"→ 下游判定转写成功 → 总结直接缺掉这 30 分钟。改为显式失败，
                #   整场不落盘、下次重跑，宁可多花一次算力也不产出残缺稿。
                if not got:
                    failed.append(i + 1)
                    log.error(f'  第 {i + 1}/{len(chunks)} 块转写为空（该块约 '
                              f'{int(c_dur // 60)} 分钟内容会丢失）')
                segs.extend(got)
                offset += c_dur
                # 必须用 force_unlink：D 盘的 os.remove 会被安全 hook 拦截（抛
                # AttributeError），静默失败会让每场留下 ~0.6GB 切块 wav。
                force_unlink(c, log)
            force_rmtree(work, log)
            if failed:
                log.error(f'  分块转写有 {len(failed)}/{len(chunks)} 块失败'
                          f'（块号 {failed}），判定整场失败，不写 TXT/SRT，下次重跑')
                return None
        else:
            segs = asr_segments(model, asr_src, cfg, log=log)

        segs = dedup_merge([s for s in segs if is_meaningful(s[2]) and not is_junk(s[2])])
        # 清洗冗余双标点（FunASR 分段兜底会在句末/句内叠出 "。，" 等），从源头杜绝
        segs = clean_segment_boundaries(segs)
        # 数字归一：把紧跟规格单位的中国数字转回阿拉伯（三零五零显卡→3050显卡、
        # 七十五赫兹→75Hz、八G→8G），从源头杜绝专有名词被读成中文
        segs = [(st, en, normalize_numbers(t)) for st, en, t in segs]
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
            '模型': 'Fun-ASR-Nano-2512' if cfg.get('_asr_kind') == 'nano' else 'SenseVoiceSmall',
            '音频前处理': enh_desc,
        })
        write_srt(srt_path, segs)
        log(f'  完成：{len(segs)} 句 / {chars} 字 / 耗时 {elapsed:.0f}s '
            f'（{dur / max(elapsed, 1):.1f}x 实时）')
        return txt_path
    finally:
        # 增强后的整段 wav 只服务本次转写，必须释放（4h 约 0.46GB）
        if enh_wav:
            force_unlink(enh_wav, log)


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
