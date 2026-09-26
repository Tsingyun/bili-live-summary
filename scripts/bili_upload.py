#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_upload.py —— 把压缩后的录播视频（默认 HEVC/H.265 MP4）自动投稿到 B站「小号」。

设计原则（与 bili-live-summary 其它模块一致）：
- 复用 common.load_cookies / common.bili_get，直接读取项目既有的 Netscape 格式 cookie 文件
  （config.paths.cookies，默认 C:\\Users\\Tsing\\.bilibili_cookies.txt），不引入 biliup 的
  cookie.json 格式分歧；
- 默认**直接上传现有 HEVC 副本**（用户 2026-09-26 明确：先用真实 HEVC 录播试传，
  H.264 转码仅作兜底开关 block_hevc）；
- 完整实现 bilibili-API-collect 文档的投稿链路：
  preupload(GET) → 上传元数据(POST) → 分片 PUT → 结束上传(POST) → 提交发布(POST add/v3)；
- 分片级重试 + 整体重试 + 风控码识别（-352/-412/-111 等立即失败，不空等）；
- H.265 / 体积 / 时长**只 WARN 不阻断**（除非显式 block_hevc=true），尊重用户「先试传」的意愿；
- 通过 state['uploads'] 记录已上传的直播日期与 bvid，幂等防重传；
- 与 daily_pipeline 解耦：既可在 compress 之后被 process_video_source 调用，也可独立运行：
    python bili_upload.py --src <mp4> --dry-run
    python bili_upload.py --check-cookie
    python bili_upload.py --src <mp4> --date 2026-09-23

配置（config.upload）：
  enabled         是否启用自动上传（默认 false，避免无 cookie 时影响原有流程）
  backend         'api'（默认，纯 requests，复用 Netscape cookie） | 'biliup'（外部二进制，需另行提供 cookie.json）
  biliup_bin / biliup_cookie_json   backend=biliup 时的二进制路径与 biliup 生成的 cookie.json
  tid             分区 ID（默认 171；VTuber 回放可按需改）
  tags            标签列表（默认 ["直播回放","虚拟主播","岁己"]，最多 10 个）
  copyright       1 自制 / 2 转载（默认 1，自家直播回放）
  title_template  标题模板，可用 {streamer}{live_date}{title}{fname}（默认 "{streamer} {live_date} 直播回放"）
  desc_template / dynamic / source  简介 / 粉丝动态 / 转载来源（自制留空）
  no_reprint      0 允许转载 / 1 禁止转载（默认 1）
  dtime_hours     延时发布小时数（默认 6；B站要求距提交 >4h、≤15 天，代码会自动夹到合法区间）
  line            backend=biliup 时的上传线路（默认 AUTO）
  proxy           上传代理（默认空 = 直连，不绕 Clash，避免大文件上传被代理拖慢）
  max_retries / retry_delay  整体与分片重试次数 / 退避秒数
  block_hevc      默认 false —— 若为 true 则遇 HEVC 直接报错阻断（目前不启用，先试传）
  max_size_gb_warn / max_duration_hour_warn  仅告警阈值（不阻断）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import requests
from common import load_cookies, bili_get, Logger, BILI_HEADERS

# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

DEFAULTS = {
    'enabled': False,
    'backend': 'api',
    'biliup_bin': '',
    'biliup_cookie_json': '',
    'tid': 171,
    'tags': ['直播回放', '虚拟主播', '岁己'],
    'copyright': 1,
    'title_template': '{streamer} {live_date} 直播回放',
    'desc_template': '',
    'dynamic': '',
    'source': '',
    'no_reprint': 1,
    'dtime_hours': 6,
    'line': 'AUTO',
    'proxy': '',
    'max_retries': 3,
    'retry_delay': 10,
    'block_hevc': False,
    'max_size_gb_warn': 4,
    'max_duration_hour_warn': 5,
}


def load_upload_cfg(cfg):
    uc = dict(DEFAULTS)
    uc.update((cfg.get('upload') or {}))
    # 小号 cookie 优先用 upload.cookies_path，否则回落到 paths.cookies（项目既有文件）
    uc['cookies_path'] = (cfg.get('upload') or {}).get('cookies_path') \
        or (cfg.get('paths') or {}).get('cookies')
    # streamer 取自 summarize.streamer（用于标题模板）
    uc['streamer'] = (cfg.get('summarize') or {}).get('streamer', '岁己')
    return uc


# ---------------------------------------------------------------------------
# 视频预检（ffprobe）
# ---------------------------------------------------------------------------

def probe_video(ffprobe, path):
    """返回 {codec, width, height, duration, size}；取不到返回空字典。"""
    out = {}
    try:
        import subprocess
        r = subprocess.run(
            [ffprobe, '-v', 'error', '-show_entries',
             'stream=codec_name,width,height:format=duration',
             '-select_streams', 'v:0', '-of', 'json', path],
            capture_output=True, text=True, timeout=60)
        d = json.loads(r.stdout or '{}')
        st = (d.get('streams') or [{}])[0]
        out['codec'] = st.get('codec_name', '')
        out['width'] = int(st.get('width') or 0)
        out['height'] = int(st.get('height') or 0)
        fmt = d.get('format') or {}
        out['duration'] = float(fmt.get('duration') or 0)
    except Exception:
        pass
    try:
        out['size'] = os.path.getsize(path)
    except Exception:
        out['size'] = 0
    return out


def validate_video(cfg, uc, path, log):
    """校验视频：H.265 / 体积 / 时长只 WARN 不阻断（除非 block_hevc）。返回 probe dict。"""
    ffprobe = (cfg.get('paths') or {}).get('ffprobe')
    info = probe_video(ffprobe, path) if ffprobe else {}
    codec = (info.get('codec') or '').lower()
    if 'hevc' in codec or 'h265' in codec or 'hvc1' in codec or 'hev1' in codec:
        msg = '[upload] 检测到 HEVC/H.265 编码（小号非大会员可能无法播放/被拒稿）'
        if uc.get('block_hevc'):
            log.error(msg + ' —— block_hevc=true，已阻断上传')
            raise RuntimeError('HEVC blocked by config')
        log(msg + ' —— 按你的要求先直接试传（如需兜底请在 config 开启转码步骤）', 'WARN')
    sz_gb = (info.get('size') or 0) / 1024.0 ** 3
    if sz_gb > float(uc.get('max_size_gb_warn', 4)):
        log('[upload] 文件 %.2fGB 超过 %.0fGB 告警线（网页投稿常见上限 4~8GB，小号等级低建议 ≤4GB），'
            '若提交被拒再考虑拆分/重压' % (sz_gb, float(uc.get('max_size_gb_warn', 4))), 'WARN')
    dur_h = (info.get('duration') or 0) / 3600.0
    if dur_h > float(uc.get('max_duration_hour_warn', 5)):
        log('[upload] 时长 %.1f 小时超过 %.0f 小时告警线（长直播如需投稿可能需拆多P）'
            % (dur_h, float(uc.get('max_duration_hour_warn', 5))), 'WARN')
    return info


# ---------------------------------------------------------------------------
# Cookie 校验
# ---------------------------------------------------------------------------

def check_cookie(cfg, uc, log):
    """校验 cookie：必要字段齐全 + 能拉到登录态。返回 (ok, info)。"""
    path = uc.get('cookies_path')
    if uc.get('backend') == 'biliup':
        cj = uc.get('biliup_cookie_json')
        if not (cj and os.path.exists(cj)):
            return False, {'error': 'backend=biliup 但 biliup_cookie_json 不存在，请先 `biliup login` 生成'}
        return True, {'backend': 'biliup', 'cookie_json': cj}
    if not (path and os.path.exists(path)):
        return False, {'error': 'cookie 文件不存在：%s' % path}
    cookies = load_cookies(path)
    for k in ('SESSDATA', 'bili_jct', 'DedeUserID'):
        if not cookies.get(k):
            return False, {'error': 'cookie 缺少必要字段 %s' % k}
    try:
        d = bili_get('https://api.bilibili.com/x/space/myinfo', cookies=cookies, retries=2, timeout=20)
        if d.get('code') != 0:
            return False, {'error': 'cookie 无效（space/myinfo code=%s %s）'
                                   % (d.get('code'), d.get('message'))}
        data = d.get('data') or {}
        return True, {'mid': data.get('mid'), 'name': (data.get('name') or '').strip(),
                      'level': (data.get('level') or 0)}
    except Exception as e:
        return False, {'error': 'cookie 校验请求失败：%s' % e}


# ---------------------------------------------------------------------------
# 元数据构建
# ---------------------------------------------------------------------------

class _SafeDict(dict):
    def __missing__(self, key):
        return '{%s}' % key


def build_metadata(cfg, uc, ctx):
    """根据直播日期 / 文件名 / 配置模板生成投稿元数据。"""
    date_iso = ctx.get('date_iso', '')
    fname = ctx.get('fname', '') or ''
    stem = re.sub(r'_compressed$', '', os.path.splitext(os.path.basename(fname))[0])
    live_date = date_iso or stem
    title = (uc.get('title_template') or '{streamer} {live_date} 直播回放').format_map(
        _SafeDict(streamer=uc.get('streamer', '岁己'), live_date=live_date,
                  title=stem, fname=fname))
    title = title[:80]
    desc = (uc.get('desc_template') or '').format_map(
        _SafeDict(streamer=uc.get('streamer', '岁己'), live_date=live_date,
                  title=stem, fname=fname))[:2000]
    tags = uc.get('tags') or []
    tags = [t for t in tags if t][:10]
    return {
        'title': title,
        'desc': desc,
        'tags': tags,
        'tid': int(uc.get('tid', 171)),
        'copyright': int(uc.get('copyright', 1)),
        'dynamic': uc.get('dynamic') or '',
        'source': uc.get('source') or '',
        'no_reprint': int(uc.get('no_reprint', 1)),
    }


def _norm_dtime(uc, submit_ts):
    """把 dtime_hours 夹到 B站合法区间（距提交 >4h、≤15 天），返回 10 位秒级时间戳或 None。"""
    h = float(uc.get('dtime_hours') or 0)
    if h <= 0:
        return None
    import datetime
    dt = submit_ts + max(4.1 * 3600, h * 3600)
    upper = submit_ts + 15 * 24 * 3600
    dt = min(dt, upper)
    return int(dt)


# ---------------------------------------------------------------------------
# 后端：纯 requests 实现（默认）
# ---------------------------------------------------------------------------

def _new_session(uc):
    s = requests.Session()
    s.headers.update(BILI_HEADERS)
    if uc.get('proxy'):
        s.proxies = {'http': uc['proxy'], 'https': uc['proxy']}
    return s


def _req_json(s, method, url, *, params=None, headers=None, data=None, json_body=None,
              timeout=60, retries=3, delay=10, is_upload=False):
    """统一的请求重试：网络层 / 429 / 5xx 退避重试；4xx 立即失败。"""
    last = None
    for i in range(max(1, retries)):
        try:
            r = s.request(method, url, params=params, headers=headers,
                          data=data, json=json_body, timeout=timeout)
            if r.status_code == 200:
                try:
                    return r.json()
                except Exception as e:
                    last = e
                    if i < retries - 1:
                        time.sleep(delay * (i + 1))
                    continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                last = RuntimeError('HTTP %s' % r.status_code)
                if i < retries - 1:
                    time.sleep(delay * (2 ** i))
                continue
            raise RuntimeError('HTTP %s（不可重试）%s: %s'
                               % (r.status_code, url, (r.text or '')[:200]))
        except RuntimeError:
            raise
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(delay * (2 ** i))
    raise RuntimeError('请求失败 %s: %s' % (url, last))


def _upload_one_file(s, log, uc, video_path, cookies, submit_ts):
    """投稿链路前 4 步（preupload→元数据→分片→结束），返回 (upos_basename, biz_id)。"""
    if not cookies.get('bili_jct'):
        raise RuntimeError('cookie 缺少 bili_jct（提交发布需要 CSRF）')
    size = os.path.getsize(video_path)
    filename = os.path.basename(video_path)
    submit_ts = submit_ts or int(time.time())

    # 1) preupload
    pre = _req_json(s, 'GET', 'https://member.bilibili.com/preupload',
                    params={'name': filename, 'r': 'upos', 'profile': 'ugcfx/bup',
                            'ssl': '0', 'version': '2.9.0.0', 'build': '2090100',
                            'webVersion': '1.9.0', 'size': size},
                    retries=uc.get('max_retries', 3), delay=uc.get('retry_delay', 10),
                    timeout=30)
    if pre.get('OK') != 1:
        raise RuntimeError('preupload 返回异常：%s' % (pre,))
    auth = pre['auth']
    biz_id = pre['biz_id']
    chunk_size = int(pre.get('chunk_size') or 8 * 1024 * 1024)
    endpoint = pre['endpoint']              # 形如 //upos-cs-upcdntxa.bilivideo.com（无结尾斜杠）
    upos_uri = pre['upos_uri']              # 形如 upos://ugcfx2lf/<name>
    upos_path = upos_uri[len('upos://'):] if upos_uri.startswith('upos://') else upos_uri
    # 拼出正确的上传 URL：endpoint 与 upos_path 之间必须有一个 '/'，否则主机名会被拼接成
    # upos-cs-xxx.bilivideo.comugcfx... 导致代理 502。
    url = 'https:' + endpoint
    if not url.endswith('/'):
        url += '/'
    if upos_path.startswith('/'):
        upos_path = upos_path[1:]
    url += upos_path
    log('[upload] preupload OK：biz_id=%s chunk=%d url=%s' % (biz_id, chunk_size, url))

    # 2) 上传元数据（拿 upload_id）
    meta_url = (url + '?uploads&output=json&profile=ugcfx/bup&filesize=%d'
                '&partsize=%d&biz_id=%s' % (size, chunk_size, biz_id))
    up = _req_json(s, 'POST', meta_url,
                   headers={'X-Upos-Auth': auth, 'Content-Type': 'application/json'},
                   data=b'', timeout=30, retries=uc.get('max_retries', 3),
                   delay=uc.get('retry_delay', 10))
    upload_id = str(up.get('upload_id') or up.get('id') or pre.get('upload_id') or '')
    if not upload_id:
        raise RuntimeError('上传元数据未返回 upload_id：%s' % (up,))
    log('[upload] 上传元数据 OK：upload_id=%s' % upload_id)

    # 3) 分片上传（PUT）
    import math
    nchunks = max(1, math.ceil(size / chunk_size))
    log('[upload] 开始分片上传：%d 片，共 %.2fGB' % (nchunks, size / 1024.0 ** 3))
    parts = []
    with open(video_path, 'rb') as f:
        for i in range(nchunks):
            start = i * chunk_size
            chunk = f.read(chunk_size)
            end = start + len(chunk)
            params = {'partNumber': i + 1, 'uploadId': upload_id, 'chunk': i,
                      'chunks': nchunks, 'size': len(chunk), 'start': start,
                      'end': end, 'total': size}
            ok = False
            for attempt in range(max(1, uc.get('max_retries', 3))):
                try:
                    r = s.put(url, params=params,
                              headers={'X-Upos-Auth': auth,
                                       'Content-Type': 'application/octet-stream'},
                              data=chunk, timeout=180)
                    if r.status_code == 200 and 'MULTIPART_PUT_SUCCESS' in r.text:
                        ok = True
                        break
                    log('[upload] 分片 %d 返回 %s：%s（重试 %d）'
                        % (i + 1, r.status_code, (r.text or '')[:120], attempt), 'WARN')
                except Exception as e:
                    log('[upload] 分片 %d 异常：%s（重试 %d）' % (i + 1, e, attempt), 'WARN')
                time.sleep(uc.get('retry_delay', 10) * (attempt + 1))
            if not ok:
                raise RuntimeError('分片 %d 上传失败，终止' % (i + 1))
            parts.append({'partNumber': i + 1, 'eTag': ''})
            if (i + 1) % 20 == 0:
                log('[upload] 分片进度 %d/%d' % (i + 1, nchunks))
    log('[upload] 分片上传完成')

    # 4) 结束上传
    finish_url = (url + '?output=json&name=%s&profile=ugcfx/bup&uploadId=%s&biz_id=%s'
                  % (urllib.parse.quote(filename), upload_id, biz_id))
    fin = _req_json(s, 'POST', finish_url,
                    headers={'X-Upos-Auth': auth, 'Content-Type': 'application/json'},
                    json_body={'parts': parts}, timeout=60,
                    retries=uc.get('max_retries', 3), delay=uc.get('retry_delay', 10))
    if fin.get('OK') != 1:
        raise RuntimeError('结束上传返回异常：%s' % (fin,))
    log('[upload] 结束上传 OK')
    filename_nohash = os.path.basename(upos_path).rsplit('.', 1)[0]
    return filename_nohash, biz_id


def _submit(s, log, uc, meta, videos, cookies, submit_ts):
    """提交发布（单 P / 多 P 共用）。videos=[{filename,title,desc,cid}...]，顺序即分P顺序。"""
    body = {
        'copyright': meta['copyright'],
        'videos': videos,
        'title': meta['title'],
        'tid': meta['tid'],
        'tag': ','.join(meta['tags']),
        'desc': meta['desc'],
        'desc_format_id': 9999,
        'dynamic': meta['dynamic'],
        'source': meta['source'],
        'recreate': -1,
        'interactive': 0,
        'no_reprint': meta['no_reprint'],
        'subtitle': {'open': 0, 'lan': ''},
        'dolby': 0,
        'lossless_music': 0,
        'web_os': 3,
    }
    dtime = _norm_dtime(uc, submit_ts)
    if dtime:
        body['dtime'] = dtime
    r = s.post('https://member.bilibili.com/x/vu/web/add/v3',
               params={'ts': int(time.time() * 1000), 'csrf': cookies['bili_jct']},
               headers={'Content-Type': 'application/json'}, json=body, timeout=60)
    try:
        j = r.json()
    except Exception:
        raise RuntimeError('提交发布响应非 JSON：%s' % (r.text or '')[:200])
    if j.get('code') != 0:
        # -352/-412/-111 等风控码立即失败，不重试
        raise RuntimeError('提交发布失败 code=%s message=%s'
                           % (j.get('code'), j.get('message')))
    bvid = (j.get('data') or {}).get('bvid')
    log('[upload] 发布成功：%s（tid=%s，延时发布=%s，分P=%d）'
        % (bvid, meta['tid'], dtime, len(videos)))
    return bvid


def upload_api(cfg, log, uc, video_path, meta, cookies, submit_ts=None):
    """单文件投稿（兼容旧调用）。"""
    s = _new_session(uc)
    s.cookies.update(cookies)
    fn, biz = _upload_one_file(s, log, uc, video_path, cookies, submit_ts)
    return _submit(s, log, uc, meta,
                   [{'filename': fn, 'title': meta['title'], 'desc': meta['desc'], 'cid': biz}],
                   cookies, submit_ts)


def upload_multi(cfg, log, uc, segments, meta, cookies, submit_ts=None):
    """多 P 投稿：segments=[{path, part_title}]，列表顺序即分P顺序；流复制无损，画质不变。"""
    s = _new_session(uc)
    s.cookies.update(cookies)
    videos = []
    for idx, seg in enumerate(segments, 1):
        log('[upload] === 分P %d/%d：%s ===' % (idx, len(segments), os.path.basename(seg['path'])))
        fn, biz = _upload_one_file(s, log, uc, seg['path'], cookies, submit_ts)
        videos.append({'filename': fn, 'title': seg.get('part_title', 'P%d' % idx),
                       'desc': '', 'cid': biz})
    return _submit(s, log, uc, meta, videos, cookies, submit_ts)


# ---------------------------------------------------------------------------
# 后端：biliup CLI（可选）
# ---------------------------------------------------------------------------

def upload_biliup(cfg, log, uc, video_path, meta):
    binp = uc.get('biliup_bin') or 'biliup'
    cj = uc.get('biliup_cookie_json')
    if not (cj and os.path.exists(cj)):
        raise RuntimeError('backend=biliup 需要 biliup_cookie_json（由 `biliup login` 生成）')
    cmd = [binp, '-u', cj, 'upload', video_path,
           '--title', meta['title'], '--tag', ','.join(meta['tags']),
           '--tid', str(meta['tid']), '--copyright', str(meta['copyright']),
           '--line', uc.get('line', 'AUTO'), '--no-reprint', str(meta['no_reprint'])]
    dtime = _norm_dtime(uc, int(time.time()))
    if dtime:
        cmd += ['--dtime', str(dtime)]
    import subprocess
    log('[upload] 调用 biliup：%s ...' % ' '.join(cmd[:6]))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60 * 3)
    if r.returncode != 0:
        raise RuntimeError('biliup 返回非零：%s' % (r.stderr or r.stdout or '')[:300])
    # 从输出里粗略抓 bvid
    m = re.search(r'(BV[0-9A-Za-z]+)', r.stdout or r.stderr or '')
    return m.group(1) if m else 'unknown'


# ---------------------------------------------------------------------------
# 编排 + 幂等
# ---------------------------------------------------------------------------

def is_uploaded(st, date_iso):
    return (st.get('uploads') or {}).get(date_iso)


def mark_uploaded(st, date_iso, bvid, backend):
    st.setdefault('uploads', {})[date_iso] = {
        'bvid': bvid, 'ts': int(time.time()), 'backend': backend}


def upload_one(cfg, log, uc, video_path, ctx, cookies, st=None, force=False):
    """上传一整个视频到小号。已上传且非 force 则跳过。返回 bvid 或 None（失败）。"""
    date_iso = ctx.get('date_iso', '')
    if not force:
        rec = is_uploaded(st, date_iso) if st is not None else None
        if rec:
            log('[upload] %s 已上传（%s），跳过' % (date_iso, rec.get('bvid')))
            return rec.get('bvid')
    if not (video_path and os.path.exists(video_path)):
        log('[upload] 源文件不存在，跳过：%s' % video_path, 'WARN')
        return None
    validate_video(cfg, uc, video_path, log)
    ok, info = check_cookie(cfg, uc, log)
    if not ok:
        log.error('[upload] cookie 校验失败：%s' % info.get('error'))
        return None
    meta = build_metadata(cfg, uc, ctx)
    log('[upload] 元数据：title=%s tid=%s tags=%s copyright=%s'
        % (meta['title'], meta['tid'], ','.join(meta['tags']), meta['copyright']))
    backend = uc.get('backend', 'api')
    try:
        if backend == 'biliup':
            bvid = upload_biliup(cfg, log, uc, video_path, meta)
        else:
            bvid = upload_api(cfg, log, uc, video_path, meta, cookies)
    except Exception as e:
        log.error('[upload] 上传失败：%s' % e)
        return None
    if bvid and st is not None and date_iso:
        mark_uploaded(st, date_iso, bvid, backend)
    return bvid


# ---------------------------------------------------------------------------
# 独立运行入口（便于先单文件试传）
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', nargs='+', default=None, help='要上传的视频路径（多个=多P分P上传）')
    ap.add_argument('--config', default=None)
    ap.add_argument('--date', default='', help='直播日期 ISO（用于 state 幂等键，如 2026-09-23）')
    ap.add_argument('--title', default=None, help='覆盖总标题')
    ap.add_argument('--part-title', nargs='+', default=None, help='各分P标题（与 --src 数量对应）')
    ap.add_argument('--dry-run', action='store_true', help='只校验 + 打印元数据，不上传')
    ap.add_argument('--check-cookie', action='store_true', help='只校验 cookie 有效性')
    ap.add_argument('--force', action='store_true', help='忽略已上传标记强制上传')
    args = ap.parse_args()
    from common import load_config
    cfg = load_config(args.config)
    uc = load_upload_cfg(cfg)
    uc['enabled'] = True  # 独立运行默认允许
    log = Logger(cfg, 'bili_upload')
    if args.check_cookie:
        ok, info = check_cookie(cfg, uc, log)
        log('cookie 校验：%s %s' % ('OK' if ok else 'FAIL', info))
        return
    if not args.src:
        log.error('请指定 --src（可多个，多P）或 --check-cookie')
        return
    if args.title:
        uc['title_template'] = args.title
    if len(args.src) == 1:
        ctx = {'date_iso': args.date, 'fname': os.path.basename(args.src[0])}
        if args.dry_run:
            validate_video(cfg, uc, args.src[0], log)
            ok, info = check_cookie(cfg, uc, log)
            meta = build_metadata(cfg, uc, ctx)
            log('[dry-run] 元数据：%s' % json.dumps(meta, ensure_ascii=False))
            log('[dry-run] cookie=%s；将上传：%s' % ('OK' if ok else info, args.src[0]))
            return
        cookies = load_cookies(uc.get('cookies_path'))
        bvid = upload_one(cfg, log, uc, args.src[0], ctx, cookies, st=None, force=args.force)
        log('结果：%s' % bvid)
        return
    # 多 P 模式
    segments = []
    for i, p in enumerate(args.src):
        if not os.path.exists(p):
            log.error('[upload] 分P源不存在：%s' % p)
            return
        pt = (args.part_title[i] if args.part_title and i < len(args.part_title)
              else 'P%d' % (i + 1))
        segments.append({'path': p, 'part_title': pt})
    ctx = {'date_iso': args.date, 'fname': os.path.basename(args.src[0])}
    meta = build_metadata(cfg, uc, ctx)
    if args.dry_run:
        validate_video(cfg, uc, args.src[0], log)
        ok, info = check_cookie(cfg, uc, log)
        log('[dry-run] 总标题：%s' % meta['title'])
        for i, seg in enumerate(segments):
            log('[dry-run] 分P%d：%s | %s' % (i + 1, seg['part_title'], seg['path']))
        log('[dry-run] cookie=%s；将上传 %d 个分P' % ('OK' if ok else info, len(segments)))
        return
    # 幂等：用工作目录 state.json 防重传（避免断点重跑产生重复投稿）
    st_path = os.path.join((cfg.get('paths') or {}).get('project_root') or '.', 'state.json')
    st = {}
    if os.path.exists(st_path):
        try:
            st = json.load(open(st_path, encoding='utf-8'))
        except Exception:
            st = {}
    if not args.force and args.date and is_uploaded(st, args.date):
        log('[upload] %s 已上传（%s），跳过' % (args.date, is_uploaded(st, args.date).get('bvid')))
        return
    cookies = load_cookies(uc.get('cookies_path'))
    bvid = upload_multi(cfg, log, uc, segments, meta, cookies, submit_ts=None)
    if bvid and args.date:
        mark_uploaded(st, args.date, bvid, 'api')
        try:
            json.dump(st, open(st_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
        except Exception as e:
            log('[upload] 写入 state 失败（不影响已发布）：%s' % e)
    log('结果：%s' % bvid)


if __name__ == '__main__':
    main()
