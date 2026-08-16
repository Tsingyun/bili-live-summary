#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一次性干净重建：仅对 4 场目标录播（8/9、8/10、8/11 两场）重新总结并写入 Word。
排除 20260801（误跑期）。单进程顺序执行，依赖 summarize_gemini 的写锁保证安全。"""
import os, re, sys, traceback, atexit, time as _time, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common, summarize_gemini as S

cfg = common.load_config()
log = S.Logger(cfg, 'regen')

# ---- 启动级单实例锁：防止被后台机制重复拉起多份导致并发写同一个 doc ----
# 关键点：拿到锁后【不关闭 fd】，Windows 下被占用的文件无法被其他进程删除/抢占；
# 只有本进程退出（fd 释放）后锁文件才会被 atexit 清理。其它副本只会等待并在超时后退出。
_lock_path = os.path.join(cfg.get('_dirs', {}).get('work', os.getcwd()), 'regen.lock')
_regen_lock_fd = None

def _release_lock():
    global _regen_lock_fd
    try:
        if _regen_lock_fd is not None:
            os.close(_regen_lock_fd)
    except Exception:
        pass
    try:
        if os.path.exists(_lock_path):
            os.remove(_lock_path)
    except Exception:
        pass

def _acquire_single_instance():
    global _regen_lock_fd
    # 只给很短的窗口抢锁：抢不到就认定已有实例在跑，直接退出，
    # 绝不长时间等待——否则宿主跑完后本副本可能接管重跑一遍。
    deadline = _time.time() + 15
    while True:
        try:
            fd = os.open(_lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except OSError:
            if _time.time() > deadline:
                log.error('另一实例已在运行（15s 内未抢到单实例锁），本实例退出')
                sys.exit(1)
            _time.sleep(1)
            continue
        # 成功拿到锁：保持 fd 打开，写入 pid
        os.write(fd, str(os.getpid()).encode())
        _regen_lock_fd = fd
        atexit.register(_release_lock)
        return

_acquire_single_instance()

targets = [
    '20260809-重温第一次3D演出！ 2026年08月09日20点场',
    '20260810-陪你这个猪看滴嘟侠炒饭2！ 2026年08月10日20点场',
    '20260811-和米帕栞栞玩机械狂欢！ 2026年08月11日15点场',
    '20260811-陪你这个猪过周二! 2026年08月11日20点场',
]
txt_dir = cfg['_dirs']['txt']
ok = 0
for stem in targets:
    tp = os.path.join(txt_dir, stem + '.txt')
    if not os.path.exists(tp):
        log.error(f'缺失 TXT：{tp}')
        continue
    m = re.match(r'^(\d{4})(\d{2})(\d{2})-', stem)
    ds = f'{m.group(1)}-{m.group(2)}-{m.group(3)}'
    # 断点续跑：章节已存在于文档则跳过（不重复生成，避免被环境回收后重跑时反复重写）
    docx_path = S.doc_path_for(cfg, datetime.date.fromisoformat(ds))
    if os.path.exists(docx_path):
        try:
            from docx import Document as _D
            if S.has_section(_D(docx_path), stem):
                log(f'章节已存在，跳过：{stem}')
                ok += 1
                continue
        except Exception:
            pass
    log(f'==== 重建 {stem} (date={ds}) ====')
    try:
        r = S.summarize_one(cfg, tp, log, date_str=ds, title=stem)
        if r:
            ok += 1
            log(f'完成 {stem}')
        else:
            log.error(f'失败（返回空）{stem}')
    except Exception as e:
        log.error(f'异常 {stem}: {e}')
        traceback.print_exc()
log(f'重建结束 {ok}/{len(targets)}')
log('REBUILD DONE')
