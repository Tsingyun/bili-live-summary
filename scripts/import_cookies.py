#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B站 cookie 快速导入（投稿前置）。

为什么需要它：
  投稿走 `upload.backend=api`，靠 Netscape 格式 cookie 文件里的
  SESSDATA / bili_jct / DedeUserID 三件套维持登录态。B站会话过期后
  上传会在 member.bilibili.com/preupload 报 HTTP 403
  （cookie 校验则是 code=-101 账号未登录）。本脚本把用户从浏览器
  拿到的 cookie 一次性写回该文件并立刻做在线校验。

用法（两种输入，任选其一）：

  1) 浏览器插件导出的整份 Netscape cookies.txt
     python import_cookies.py --netscape "C:/Users/Tsing/Downloads/cookies.txt"

  2) 手动从 Chrome DevTools 复制的一行串
     （Application → Storage → Cookies → https://bilibili.com，
       把 SESSDATA / bili_jct / DedeUserID 三个值拼成 key=value; key=value）
     python import_cookies.py --kv "SESSDATA=xxx; bili_jct=yyy; DedeUserID=zzz"

安全约定：绝不打印任何 cookie 明文，只报告「已加载（长度 N）」。
"""
from __future__ import print_function

import argparse
import io
import json
import os
import shutil
import sys
import time

# 投稿最小必需集（这三个缺一不可）
REQUIRED = ('SESSDATA', 'bili_jct', 'DedeUserID')
# 顺手保留的辅助项（有则带上，无不影响）
NICE_TO_HAVE = ('DedeUserID__ckMd5', 'sid', 'buvid3', 'buvid4', 'b_nut', 'CURRENT_FNVAL')


def _log(msg):
    print('[%s] [INFO] %s' % (time.strftime('%H:%M:%S'), msg))


def parse_netscape(text):
    """解析 Netscape cookies.txt，返回 {name: value}（只取 bilibili 域）。"""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split('\t')
        if len(parts) < 7:
            parts = re_split_fallback(line)
            if not parts:
                continue
        # domain, flag, path, secure, expiry, name, value
        domain, name, value = parts[0], parts[5], parts[6]
        if 'bilibili' not in domain:
            continue
        if name in REQUIRED or name in NICE_TO_HAVE:
            out[name] = value
    return out


def re_split_fallback(line):
    """容忍空格分隔的变体（部分导出工具会用空格）。"""
    parts = line.split()
    if len(parts) >= 7:
        return parts
    return None


def parse_kv(text):
    """解析 'k=v; k=v' / 逗号分隔 / 多行 key=value。

    ★ 逗号不能无脑替换：cookie 值本身可能含逗号。只有「逗号 + 紧跟
    key=」这种形态才当分隔符，否则会把合法值截断（实测踩过）。
    """
    import re
    text = re.sub(r';\s*', '\n', text)
    # 仅当逗号后面紧跟 key= 时才算分隔符
    text = re.sub(r',\s*(?=[A-Za-z_][A-Za-z0-9_]*\s*=)', '\n', text)
    text = text.replace('\r', '\n')
    out = {}
    for chunk in text.split('\n'):
        chunk = chunk.strip()
        if not chunk or '=' not in chunk:
            continue
        k, v = chunk.split('=', 1)
        k, v = k.strip(), v.strip()
        if k in REQUIRED or k in NICE_TO_HAVE:
            out[k] = v
    return out


def render_netscape(cookies):
    """渲染成 Netscape 格式文本。"""
    lines = ['# Netscape HTTP Cookie File',
             '# 由 import_cookies.py 生成，请勿外传',
             '']
    for name, value in cookies.items():
        lines.append('\t'.join([
            '.bilibili.com', 'TRUE', '/', 'TRUE', '0', name, value]))
    return '\n'.join(lines) + '\n'


def resolve_cfg_path(cfg_arg):
    if cfg_arg:
        return os.path.abspath(cfg_arg)
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.path.join(here, '..', '..', 'config.json'),
                 os.path.join(here, 'config.json')):
        cand = os.path.abspath(cand)
        if os.path.exists(cand):
            return cand
    raise SystemExit('找不到 config.json，请用 --config 指定')


def main():
    ap = argparse.ArgumentParser(description='导入 B站投稿 cookie')
    ap.add_argument('--config', default=None)
    ap.add_argument('--netscape', default='', help='Netscape cookies.txt 路径')
    ap.add_argument('--kv', default='', help='key=value; key=value 形式的一行串')
    ap.add_argument('--no-verify', action='store_true', help='只写盘不做在线校验')
    args = ap.parse_args()

    if not args.netscape and not args.kv:
        ap.error('必须给 --netscape 或 --kv 之一')

    cookies = {}
    if args.netscape:
        p = os.path.abspath(args.netscape)
        if not os.path.exists(p):
            raise SystemExit('文件不存在：%s' % p)
        with io.open(p, 'r', encoding='utf-8', errors='replace') as f:
            cookies = parse_netscape(f.read())
        _log('已解析 %s' % p)
    if args.kv:
        cookies.update(parse_kv(args.kv))
        _log('已解析 --kv 串（长度 %d）' % len(args.kv))

    missing = [k for k in REQUIRED if not cookies.get(k)]
    if missing:
        raise SystemExit('缺少必需项：%s（投稿三项缺一不可）' % '、'.join(missing))

    cfg_path = resolve_cfg_path(args.config)
    with io.open(cfg_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)
    dest = cfg.get('upload', {}).get('cookies_path') or os.path.join(
        os.path.dirname(cfg_path), 'cookies.txt')
    dest = os.path.abspath(dest)

    if os.path.exists(dest):
        bak = dest + '.bak_%s' % time.strftime('%Y%m%d_%H%M%S')
        try:
            shutil.copy2(dest, bak)
            _log('旧 cookie 已备份：%s' % os.path.basename(bak))
        except Exception as e:
            _log('备份失败（继续）：%s' % e)

    d = os.path.dirname(dest)
    if d and not os.path.isdir(d):
        os.makedirs(d)
    tmp = dest + '.part'
    with io.open(tmp, 'w', encoding='utf-8') as f:
        f.write(render_netscape(cookies))
    os.replace(tmp, dest)

    # 关键：只报长度，绝不回显明文
    _log('已写入 %s（%d 项：%s）'
         % (dest, len(cookies), '、'.join(sorted(cookies))))
    _log('SESSDATA 已加载（长度 %d）' % len(cookies['SESSDATA']))

    if args.no_verify:
        return 0

    # 在线校验（复用 bili_upload 的同一套判定，避免两处标准漂移）
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        from common import load_config, Logger
        from bili_upload import check_cookie, load_upload_cfg
        rcfg = load_config(cfg_path)
        uc = load_upload_cfg(rcfg)
        uc['enabled'] = True
        ok, msg = check_cookie(rcfg, uc, Logger(rcfg, 'import_cookies'))
    except Exception as e:
        _log('校验调用异常（不影响 cookie 写入）：%s' % e)
        return 0
    if ok:
        _log('在线校验通过：%s' % msg)
        return 0
    _log('在线校验失败：%s' % msg)
    _log('提示：B站会话可能未登录小号，或 cookie 复制不全/已过期')
    return 2


if __name__ == '__main__':
    sys.exit(main())
