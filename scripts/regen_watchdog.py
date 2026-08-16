#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
regen_watchdog.py —— 守护 regen_gemini_v2.py 的看门狗。

背景：本沙箱环境会把后台长任务（约 6 分钟）回收致死。regen_gemini_v2 本身有
「进度缓存 + 按场跳过 + 启动级单实例锁」的抗回收设计，但被回收后需要有人重新拉起。
本看门狗循环检测：
  1. 若桌面 docx 已包含全部 4 场标题 → 任务完成，退出；
  2. 若 regen_gemini_v2 仍在运行（持有启动锁且进程存活）→ 等待；
  3. 否则重新拉起 regen_gemini_v2（子进程），等其退出后再循环。
这样即使环境反复回收，也能自动续跑直到 4 场全部生成完毕。
"""
import os
import time
import subprocess

WORK = r'D:\岁己直播自动总结'
DOC = r'C:\Users\Tsing\Desktop\岁己直播总结_2026年8月.docx'
LOCK = os.path.join(WORK, '.regen_gemini_v2.lock')
HEARTBEAT = os.path.join(WORK, '.regen_gemini_v2.heartbeat')
SCRIPT = r'C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\regen_gemini_v2.py'
PY = r'C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe'
LOG = os.path.join(WORK, 'logs', 'watchdog.log')
KEYS = ['重温第一次3D演出', '看滴嘟侠炒饭', '和米帕栞栞玩机械狂欢', '陪你这个猪过周二']


def log(s):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%H:%M:%S') + ' ' + s + '\n')
    except Exception:
        pass


def doc_complete():
    if not os.path.exists(DOC):
        return False
    try:
        import docx
        d = docx.Document(DOC)
        txt = '\n'.join(p.text for p in d.paragraphs)
        return all(k in txt for k in KEYS)
    except Exception:
        return False


def regen_running():
    """以心跳文件新鲜度判断 regen 是否存活（抗 Windows PID 复用：不依赖 PID）。"""
    if not os.path.exists(HEARTBEAT):
        return False
    try:
        age = time.time() - float(open(HEARTBEAT).read().strip() or '0')
    except Exception:
        return False
    return age < 240


def main():
    log('watchdog 启动')
    i = 0
    while True:
        i += 1
        if doc_complete():
            log('✅ 4 场全部完成，watchdog 退出 (iter %d)' % i)
            return
        if regen_running():
            time.sleep(15)
            continue
        # 无存活实例：若存在孤儿锁/心跳则强制清除，避免永久死锁
        for p in (LOCK, HEARTBEAT):
            if os.path.exists(p):
                try:
                    os.remove(p)
                    log('清除孤儿锁/心跳: ' + os.path.basename(p))
                except OSError:
                    pass
        log('启动 regen_gemini_v2 (iter %d)' % i)
        try:
            subprocess.run([PY, SCRIPT], check=False)
        except Exception as e:
            log('regen 异常: ' + str(e))
        time.sleep(5)


if __name__ == '__main__':
    main()
