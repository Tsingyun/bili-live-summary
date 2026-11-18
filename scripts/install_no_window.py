#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给专用 venv 安装「子进程不弹控制台黑框」补丁（幂等，可重复运行）。

为什么需要
----------
计划任务用 pythonw.exe（无控制台）跑管线，但管线调用的 ffmpeg / ffprobe / aria2c /
powershell 是控制台子系统程序：Windows 会给每个这样的子进程弹一个 conhost 黑框。
一晚总结弹出几十个，严重影响用户工作和游戏（2026-11-19 用户反馈）。

做法
----
把 sitecustomize.py 放进目标 venv 的 site-packages。任何用该 venv 的 python.exe /
pythonw.exe 启动的进程，在加载用户代码前都会自动给所有 subprocess.Popen 追加
creationflags |= CREATE_NO_WINDOW (0x08000000)，run/call/check_output 全覆盖。

只加 flag，不改命令/参数/IO，不杀任何进程，安全可回滚（--uninstall）。

用法
----
    python install_no_window.py                    # 自动探测 config.paths.python 的 venv
    python install_no_window.py --python <python.exe>   # 指定解释器
    python install_no_window.py --check            # 只检查，不写入
    python install_no_window.py --uninstall        # 移除补丁
"""
import argparse
import os
import sys

# 与 sitecustomize 内容一致（内嵌，避免安装时依赖源文件）
PATCH = r'''# -*- coding: utf-8 -*-
"""bili-archive 专用 venv 全局补丁：子进程不弹控制台黑框（Windows）。
由 bili-live-summary/scripts/install_no_window.py 安装，可安全删除。
"""
import sys

if sys.platform == "win32":
    try:
        import subprocess

        _CREATE_NO_WINDOW = 0x08000000
        if not getattr(subprocess.Popen, "_bls_no_window_patched", False):
            _orig_init = subprocess.Popen.__init__

            def _no_window_init(self, args, *a, **kw):
                kw["creationflags"] = (kw.get("creationflags") or 0) | _CREATE_NO_WINDOW
                return _orig_init(self, args, *a, **kw)

            _no_window_init.__name__ = "Popen.__init__ (no-window)"
            subprocess.Popen.__init__ = _no_window_init
            subprocess.Popen._bls_no_window_patched = True
    except Exception:
        pass
'''

HERE = os.path.dirname(os.path.abspath(__file__))


def site_packages_of(python_exe):
    """问解释器本人它自己的 site-packages 在哪（最可靠）。"""
    import subprocess
    code = ("import sysconfig,sys;sys.stdout.write(sysconfig.get_paths()['purelib']"
            " or sysconfig.get_paths()['platlib'])")
    r = subprocess.run([python_exe, "-c", code], capture_output=True, text=True)
    p = (r.stdout or "").strip()
    if not p or not os.path.isdir(p):
        # 回退：venv 常见布局
        root = os.path.dirname(os.path.dirname(python_exe))
        cand = os.path.join(root, "Lib", "site-packages")
        p = cand if os.path.isdir(cand) else root
    return p


def default_python():
    # 优先从工作目录 config.json 读 paths.python
    try:
        ptr = os.path.join(HERE, "_workdir.json")
        if os.path.isfile(ptr):
            import json
            wd = json.load(open(ptr, encoding="utf-8")).get("workdir")
            cfg = os.path.join(wd or "", "config.json")
            if os.path.isfile(cfg):
                py = json.load(open(cfg, encoding="utf-8")).get("paths", {}).get("python")
                if py and os.path.isfile(py):
                    return py
    except Exception:
        pass
    return sys.executable


def main():
    ap = argparse.ArgumentParser(description="安装/检查/移除子进程不弹窗补丁")
    ap.add_argument("--python", default=None, help="目标 venv 的 python.exe")
    ap.add_argument("--check", action="store_true", help="只检查是否已安装")
    ap.add_argument("--uninstall", action="store_true", help="移除补丁文件")
    a = ap.parse_args()

    py = a.python or default_python()
    if not py or not os.path.isfile(py):
        print("[!] 找不到目标 python.exe：%s" % py)
        return 2
    sp = site_packages_of(py)
    target = os.path.join(sp, "sitecustomize.py")
    print("解释器：%s" % py)
    print("site-packages：%s" % sp)
    print("补丁文件：%s" % target)

    if a.check:
        ok = os.path.isfile(target) and "_bls_no_window_patched" in open(
            target, encoding="utf-8", errors="ignore").read()
        print("状态：%s" % ("已安装" if ok else "未安装"))
        return 0 if ok else 1

    if a.uninstall:
        if os.path.isfile(target):
            os.remove(target)
            print("[OK] 已移除补丁（下次启动新进程生效）")
        else:
            print("补丁不存在，无需移除")
        return 0

    if os.path.isfile(target):
        cur = open(target, encoding="utf-8", errors="ignore").read()
        if "_bls_no_window_patched" in cur:
            print("[OK] 补丁已存在且为当前版本，无需改动")
            return 0
        # 存在别的 sitecustomize：不覆盖，提示人工处理
        print("[!] 检测到第三方 sitecustomize.py，为免覆盖未改动，请手动合并补丁内容")
        return 1

    with open(target, "w", encoding="utf-8") as f:
        f.write(PATCH)
    print("[OK] 补丁已安装：%s" % target)

    # 立即验证
    import subprocess
    code = "import subprocess;print('PATCH_OK',getattr(subprocess.Popen,'_bls_no_window_patched',False))"
    r = subprocess.run([py, "-c", code], capture_output=True, text=True)
    print("验证：" + (r.stdout or "").strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
