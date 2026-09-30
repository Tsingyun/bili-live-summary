#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audio_enhance —— 低声压 / 耳语（ASMR）场景的音频前处理自适应模块。

背景（2026-10-01 实测定论 —— 与初版结论相反，以此为准）
------------------------------------------------------
初版认为「低声压 → 前处理可把识别率补回 100%」，依据是单个 20 秒片段上
15 字 vs 17 字的差异。★ 该结论已被更大样本的受控对照推翻：

    8 片段 × 3 条件（均为真实直播素材经数字域衰减 -30dB 至 -45~-57dB）：
        不处理        95.27%
        仅抬电平      92.43%
        抬电平+压缩   93.07%
    → 前处理非但没有收益，还引入约 2~3 个百分点的损失。

两个根因：
  · 数字域衰减 ≠ 真实轻声录制：真实低声压伴随 ADC 量化噪声、麦克风本底噪声、
    房间噪声（即 SNR 下降）；而把样本乘个系数不引入任何噪声，FunASR 的 CMN
    归一化本身就能吸收，所以 VAD/ASR 几乎不受影响。
  · 压缩器改变了语音的原有动态，属于「无损素材上做有损干预」。

因此本模块**默认不动手**（mode='probe'），只产出诊断画像；确有真实耳语素材
并做过 A/B 验证后才可切 'auto' / 'on'。真正的漏字主因是 SNR，只能从主播端解决。

设计原则
--------
1. **自然判据**：不看文件名/标题，只读音频自身的帧级能量分布。
2. **正常场次零扰动**：电平达标时一条滤镜都不加（返回空链），与原链路字节一致。
3. **失败即降级**：探测或转码失败 → 不处理（保守），只打日志，绝不中断流程。
4. **增益有上限**：max_gain 限制可抬升量，避免把纯噪声放大成满幅。

用法
----
    from audio_enhance import probe_profile, decide_chain, enhance_to_wav
    prof = probe_profile(ffmpeg, src, log)
    af, tier, info = decide_chain(prof, cfg['transcribe'], log)
    if af:
        enhance_to_wav(ffmpeg, src, dst, af, log)
"""

import os
import subprocess
import time

import numpy as np

EPS = 1e-9

# 默认oil参数（均可由 config.transcribe.* 覆盖）
# ★ audio_enhance 默认 'probe'：只诊断不动手。理由见文件头「实测定论」——
#   2026-10-01 八片段受控对照显示前处理净收益为负（-2~-3pp），未经真实
#   耳语素材验证前不得默认开启。
DEFAULTS = {
    'audio_enhance': 'probe',      # off | probe | auto | on
    'enhance_enable_below_db': -26.0,   # 典型发声段电平低于此值才介入
    'enhance_target_db': -21.0,         # 抬升目标：典型发声段 RMS
    'enhance_max_gain_db': 26.0,        # 单rei次最大抬升（防止把底噪放大到满幅）
    'enhance_denoise': False,           # 保守降噪（默认关：会削气声）
    'enhance_denoise_nf_db': -30.0,     # 降噪强度（越大越保守，单位 dB）
    'enhance_low_snr_warn_db': 12.0,    # 动态范围低于此值 → 疑似低信噪比，日志告警
    'enhance_probe_timeout_sec': 900,   # 探测最长耗时保护
    'enhance_ffmpeg_timeout_sec': 3600, # 增强转码最长耗时保护
}


def _get(tcfg, key):
    v = (tcfg or {}).get(key, None)
    return DEFAULTS[key] if v is None else v


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def probe_profile(ffmpeg, src, log=None, max_sec=0, timeout=None):
    """扫描整段音频的帧级能量分布，返回电平画像 dict；失败返回 None。

    判据含义（全部基于 100ms 帧 RMS，单位 dBFS）：
      speech_db : 帧能量的 75 分位 —— 代表「典型发声段」电平
                  （不用全局均值：长时间静音/停顿会把均值拉低到没有代表性）
      floor_db  : 帧能量的  5 分位 —— 代表「停顿/底噪段」电平
      dyn_db    : speech - floor   —— 粗略的动态范围 ／ SNR 代理
      peak_db   : 帧能量的 99.5 分位
    """
    to = int(timeout or DEFAULTS['enhance_probe_timeout_sec'])
    win_sec = 0.10
    sr = 8000                      # 只为估能量，带宽无关紧要，省一半 IO
    win = int(sr * win_sec)
    cmd = [ffmpeg, '-hide_banner', '-nostats', '-loglevel', 'error',
           '-i', src, '-vn', '-sn', '-dn']
    if max_sec and max_sec > 0:
        cmd += ['-t', str(int(max_sec))]
    cmd += ['-ac', '1', '-ar', str(sr), '-f', 's16le', '-']

    t0 = time.time()
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as e:
        if log:
            log(f'音频电平探测启动失败（跳过前处理）：{e}', 'WARN')
        return None

    frames = []
    total = 0
    try:
        while True:
            if time.time() - t0 > to:
                if log:
                    log(f'音频电平探测超时（{to}s），按已扫描部分判定', 'WARN')
                break
            raw = p.stdout.read(65536)
            if not raw:
                break
            x = np.frombuffer(raw, dtype='<i2').astype(np.float32) / 32768.0
            total += len(x)
            n = len(x) // win
            if n:
                blk = x[:n * win].reshape(n, win)
                frames.append(np.sqrt((blk * blk).mean(axis=1) + EPS))
    except Exception as e:
        if log:
            log(f'音频电平探测中断（跳过前处理）：{e}', 'WARN')
        return None
    finally:
        try:
            p.stdout.close()
        except Exception:
            pass
        try:
            p.wait(timeout=10)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    if not frames:
        if log:
            log('音频电平探测无帧数据（跳过前处理）', 'WARN')
        return None

    f = np.concatenate(frames)
    f_db = 20.0 * np.log10(f + EPS)
    f_db = np.clip(f_db, -100.0, 0.0)
    prof = {
        'frames': int(f.size),
        'seconds': round(total / float(sr), 1),
        'speech_db': round(float(np.percentile(f_db, 75)), 1),
        'floor_db': round(float(np.percentile(f_db, 5)), 1),
        'peak_db': round(float(np.percentile(f_db, 99.5)), 1),
        'mean_db': round(float(f_db.mean()), 1),
        'scan_sec': round(time.time() - t0, 1),
    }
    prof['dyn_db'] = round(prof['speech_db'] - prof['floor_db'], 1)
    return prof


def _snr_warn(profile, low_snr_db, log, info):
    """动态范围过小 → 疑似低信噪比。这是前处理**救不了**的情况，明确告警。"""
    if not profile or profile.get('dyn_db') is None:
        return
    if profile['dyn_db'] < low_snr_db:
        info['low_snr'] = True
        if log:
            log(f'疑似低信噪比：动态范围仅 {profile["dyn_db"]}dB '
                f'（< {low_snr_db}dB）。前处理只能救电平、救不了 SNR，'
                f'应在主播端处理噪声/增益', 'WARN')


def _gain_db(speech_db, target_db, max_gain_db):
    """需要的线性抬升量（≥0，受 max_gain 上限约束）。"""
    return _clamp(target_db - speech_db, 0.0, float(max_gain_db))


def decide_chain(profile, tcfg, log=None):
    """依据电平画像决定前处理滤镜链。

    返回 (af, tier, info)：
      af   —— ffmpeg -af 字符串；'' 表示不处理
      tier —— 'off' | 'light' | 'mid' | 'strong'
      info —— 诊断信息 dict（写日志/测试断言用）
    """
    mode = str(_get(tcfg, 'audio_enhance') or 'auto').strip().lower()
    enable_below = float(_get(tcfg, 'enhance_enable_below_db'))
    target_db = float(_get(tcfg, 'enhance_target_db'))
    max_gain_db = float(_get(tcfg, 'enhance_max_gain_db'))
    denoise = bool(_get(tcfg, 'enhance_denoise'))
    nf_db = float(_get(tcfg, 'enhance_denoise_nf_db'))
    low_snr = float(_get(tcfg, 'enhance_low_snr_warn_db'))

    off_info = {'mode': mode, 'reason': ''}

    if mode in ('off', 'false', 'none', '0', 'no'):
        off_info['reason'] = '配置为关闭'
        return '', 'off', off_info

    if profile is None and mode in ('probe', 'auto'):
        off_info['reason'] = '电平探测失败，保守不处理'
        if log:
            log('音频电平探测失败 → 不做前处理（保持原链路）', 'WARN')
        return '', 'off', off_info

    # probe：只产出画像与告警，绝不改音频
    if mode == 'probe':
        info = {'mode': mode, 'tier': 'probe',
                'speech_db': (profile or {}).get('speech_db'),
                'floor_db': (profile or {}).get('floor_db'),
                'dyn_db': (profile or {}).get('dyn_db')}
        _snr_warn(profile, low_snr, log, info)
        return '', 'probe', info

    if profile is None:
        if mode == 'on':
            # 强制模式但探测失败：按「中档 + 保守 12dB」处理，不盲抬
            info = {'mode': mode, 'tier': 'mid',
                    'reason': '探测失败，强制模式采用保守中档', 'gain_db': 12.0}
            return _build('mid', 12.0, 6, denoise, nf_db, info), 'mid', info
        off_info['reason'] = '电平探测失败，保守不处理'
        if log:
            log('音频电平探测失败 → 不做前处理（保持原链路）', 'WARN')
        return '', 'off', off_info

    speech = float(profile['speech_db'])
    if speech >= enable_below:
        off_info['reason'] = f'发声段电平 {speech}dB 达标（阈值 {enable_below}dB）'
        off_info['speech_db'] = speech
        return '', 'off', off_info

    need = _gain_db(speech, target_db, max_gain_db)
    if speech >= -34.0:
        tier, ratio = 'light', 4
    elif speech >= -42.0:
        tier, ratio = 'mid', 6
    else:
        tier, ratio = 'strong', 8

    info = {
        'mode': mode, 'tier': tier, 'speech_db': speech,
        'floor_db': profile.get('floor_db'), 'dyn_db': profile.get('dyn_db'),
        'gain_db': round(need, 1), 'ratio': ratio, 'denoise': denoise,
    }
    if profile.get('dyn_db') is not None and profile['dyn_db'] < low_snr:
        info['low_snr'] = True
    _snr_warn(profile, low_snr, log, info)
    af = _build(tier, need, ratio, denoise, nf_db, info)
    return af, tier, info


def _build(tier, gain_db, ratio, denoise, nf_db, info):
    """组装 ffmpeg 滤镜链。顺序不可调换：

      aformat → highpass → [降噪] → 抬电平 → 压缩动态 → 限幅

    ★ 抬电平必须在压缩**之前**：先抬到统一基准，压缩器才能用同一个固定阈值工作；
    ★ 限幅必须最后：这是输出前的削波保险；
    ★ aformat=fltp 让整条链在 float 下运算，避免中间结果在 s16 上削波。
    """
    parts = ['aformat=sample_fmts=fltp', 'highpass=f=80:p=1']
    if denoise:
        # 只去稳态底噪；ASMR 的气声频谱跟房间本底很接近，宁弱勿强
        parts.append('afftdn=nf=%.1f:nt=w' % float(nf_db))
    parts.append('volume=%.2fdB' % float(gain_db))
    parts.append('acompressor=threshold=0.05:ratio=%d:attack=15:release=250'
                 ':makeup=2:knee=6' % int(ratio))
    parts.append('alimiter=limit=0.95')
    af = ','.join(parts)
    info['af'] = af
    return af


def enhance_to_wav(ffmpeg, src, dst, af, log=None, timeout=None):
    """按给定 af 把 src 转成 16k 单声道 wav（ASR 专用中间产物）。成功返回 True。"""
    if not af:
        return False
    to = int(timeout or DEFAULTS['enhance_ffmpeg_timeout_sec'])
    os.makedirs(os.path.dirname(os.path.abspath(dst)) or '.', exist_ok=True)
    cmd = [ffmpeg, '-y', '-hide_banner', '-nostats', '-loglevel', 'error',
           '-i', src, '-vn', '-sn', '-dn', '-af', af,
           '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', dst]
    try:
        r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=to)
    except subprocess.TimeoutExpired:
        if log:
            log(f'音频前处理转码超时（{to}s），放弃前处理', 'WARN')
        return False
    except Exception as e:
        if log:
            log(f'音频前处理转码异常（放弃前处理）：{e}', 'WARN')
        return False
    if r.returncode != 0:
        if log:
            msg = (r.stderr or b'').decode('utf-8', 'ignore')[:200]
            log(f'前处理转码失败（rc={r.returncode}），放弃前处理：{msg}', 'WARN')
        return False
    if not (os.path.exists(dst) and os.path.getsize(dst) > 1024):
        if log:
            log('前处理产物为空，放弃前处理', 'WARN')
        return False
    return True


def describe(profile, tier, info):
    """一行诊断文本（写日志用）。"""
    if tier == 'off':
        return '音频前处理：未启用（%s）' % (info.get('reason') or '-')
    if tier == 'probe':
        extra = '' if not info.get('low_snr') else '；⚠ 疑似低信噪比'
        return ('仅诊断（不改音频）：发声段 %sdB / 停顿-底噪 %sdB / '
                '动态范围 %sdB%s'
                % (info.get('speech_db'), info.get('floor_db'),
                   info.get('dyn_db'), extra))
    gain = info.get('gain_db')
    return ('音频前处理：%s 档（发声段 %.1fdB → 抬升 %.1fdB，压缩比 %s，降噪 %s）'
            % (tier,
               info.get('speech_db', float('nan')),
               gain if gain is not None else float('nan'),
               info.get('ratio'), '开' if info.get('denoise') else '关'))
