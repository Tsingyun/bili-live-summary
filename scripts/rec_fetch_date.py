# -*- coding: utf-8 -*-
"""从录播站（rec.darkpy.cn）按「直播日期」拉取一整场，核对后落位并可选跑全套管线。

用法
----
    python rec_fetch_date.py --date 2026-09-30                # 下载 + 落位 + 跑 daily_pipeline
    python rec_fetch_date.py --date 2026-09-30 --no-pipeline  # 只下载落位
    python rec_fetch_date.py --date 2026-09-30 --session 1    # 该日有多场时选第 2 场
    python rec_fetch_date.py --date 2026-09-30 --dry-run      # 只列清单不下载

流程
----
    ① 列目录 → ② 按「场次首段所在日」全局归组（跨午夜尾巴自动归入前一场，不会误并）
      → ③ 逐段下载（大小校验）→ ④ 逐段 ffprobe 能否打开
      → ⑤ check_session 会话完整性 → ⑥ 无损合并（concat copy）
      → ⑦ 合并产物核对（大小 / 时长 / 偏差 < 60s）
      → ⑧ 改名为「录制器格式」落位到 local.root
      → ⑨★ 落位文件 mtime 设为该场真实收尾时刻（见下方「为什么必须改 mtime」）
      → ⑩ 回收中间 FLV → ⑪ 可选跑 daily_pipeline 全套

★ 为什么必须改 mtime（2026-10-01 踩到的必现 bug）
------------------------------------------------
daily_pipeline.is_file_stable 判定「是否还在录制」的条件之一是
    mtime 距现在 >= min_age_sec（默认 120s）
而从录播站下载的文件落位时 mtime 必然是「刚刚」，于是**第一次扫描 100% 被判为
「录制中」而整场跳过**，且 pipeline 退出码仍是 0 —— 看起来像"跑成功但什么都没做"。

本脚本把落位文件的 mtime 设为「该场最后一段的站点结束时间 end」（即真实录制收尾
时刻，通常已是数小时前），语义正确且令 is_file_stable 立即通过。
若取不到 end（站点没给时间），退化为等待 min_age + 10s 后再交棒。

命名规则
--------
darkpy 文件名：20260930-220040-780-陪你这个猪过周三.flv
本地录制器名：录制-25788785-20260930-220040-780-陪你这个猪过周三.mp4
   ↑ uid 取自 darkpy.room_dir / local.root 的前缀数字。
   daily_pipeline 默认正则要求必含 uid 组，用原名会匹配不上（已验证 NO MATCH），
   所以这一步改名是必需的，不是可选美化。
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from common import load_config, Logger, hms, force_unlink  # noqa: E402
import darkpy_rec as D  # noqa: E402

# 与 daily_pipeline.is_file_stable 的默认 min_age_sec 保持一致
DEFAULT_MIN_AGE_SEC = 120

_NAME_RE = re.compile(r"^(?P<date>\d{8})-(?P<time>\d{6})-(?P<num>\d+)-(?P<title>.+)\.flv$")


# --------------------------------------------------------------------------- 工具
def _bytes_of(v):
    """把 '3.68GB' / 3947430653 统一成字节数。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper()
    for unit, mult in (("TB", 1024.0 ** 4), ("GB", 1024.0 ** 3),
                       ("MB", 1024.0 ** 2), ("KB", 1024.0), ("B", 1.0)):
        if s.endswith(unit):
            s = s[: -len(unit)]
            try:
                return int(float(s) * mult)
            except ValueError:
                return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _dur_of(ffprobe, path, timeout=180):
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=timeout)
        return float((out.stdout or "0").strip().splitlines()[0])
    except Exception:
        return 0.0


def _room_uid(cfg):
    """从 darkpy.room_dir 或 local.root 解析房间号（'25788785-岁己SUI' → 25788785）。"""
    for src in ((cfg.get("darkpy") or {}).get("room_dir"),
                (cfg.get("local") or {}).get("root")):
        s = os.path.basename((src or "").rstrip("\\/"))
        m = re.match(r"^(\d+)", s)
        if m:
            return m.group(1)
    return ""


def _rec_name(uid, first):
    """darkpy 原名 → 本地录制器命名 mp4（daily_pipeline 默认正则可匹配）。"""
    m = _NAME_RE.match(first["name"])
    if m:
        return "录制-%s-%s-%s-%s-%s.mp4" % (uid, m.group("date"), m.group("time"),
                                            m.group("num"), m.group("title"))
    # 解析不出就退回原名（大概率匹配不上，交给下游报错，而不是悄悄产出错名）
    return first["name"].rsplit(".", 1)[0] + ".mp4"


def _apply_mtime(path, end_dt, log, min_age=DEFAULT_MIN_AGE_SEC):
    """★ 关键修复：把落位文件的 mtime 设为该场真实收尾时刻。

    不改任何文件内容，只改时间戳元数据。语义上，「这场录制的写入完成时间」
    本来就是 end_dt（站点给的最后一段 mtime），把它设回去是还原事实。
    """
    if end_dt is None:
        log("  未取到场次收尾时间，退化为等待 %ds 让 mtime 跨过稳定窗口" % (min_age + 10))
        time.sleep(min_age + 10)
        return False
    ts = end_dt.timestamp()
    try:
        os.utime(path, (ts, ts))
        log("  mtime 已还原为录制收尾时刻：%s（%.0f 分钟前）"
            % (end_dt.strftime("%Y-%m-%d %H:%M:%S"), (time.time() - ts) / 60))
        return True
    except Exception as e:
        log("  mtime 还原失败（%s），退化为等待稳定窗口" % e, "WARN")
        time.sleep(min_age + 10)
        return False


# --------------------------------------------------------------------------- 主流程
def fetch_date(cfg, log, ymd, gap, session_idx, dry_run=False,
               keep_intermediate=False, no_pipeline=False,
               pipeline_timeout=6 * 3600, min_age=DEFAULT_MIN_AGE_SEC):
    P = cfg["paths"]
    ffprobe = P["ffprobe"]
    work = (cfg.get("darkpy") or {}).get("work_dir") or os.path.join(
        cfg.get("_dirs", {}).get("base", "."), "_darkpy")
    os.makedirs(work, exist_ok=True)

    # 合并后保留分段，便于逐段 ffprobe 复验
    cfg.setdefault("darkpy", {})["keep_flv_after_merge"] = True

    uid = _room_uid(cfg)
    if not uid:
        log("无法从配置解析房间号（darkpy.room_dir / local.root 需以数字开头）", "ERROR")
        return 2
    log("房间号：%s    日期：%s" % (uid, ymd))

    # ---- ① 列目录 + ② 归组 ----
    files = D.fetch_index(cfg, log)
    if not files:
        log("录播站列目录为空", "ERROR")
        return 2
    sessions = D.sessions_for_date(files, ymd, gap, log)
    log("该日开播的场次：%d 场" % len(sessions))
    for i, s in enumerate(sessions):
        total = sum(_bytes_of(f.get("size")) for f in s)
        log("  [%d] %d 段 / %.2f GB：%s → %s"
            % (i, len(s), total / 1024.0 ** 3, s[0]["name"], s[-1]["name"]))
    if not sessions:
        log("该日无场次", "ERROR")
        return 1
    if session_idx >= len(sessions):
        log("--session %d 越界（只有 %d 场）" % (session_idx, len(sessions)), "ERROR")
        return 2
    session = sessions[session_idx]

    # ---- 清单 + 预期合计 ----
    total_expect = sum(_bytes_of(f.get("size")) for f in session)
    log("目标场次 [%d]：%d 段 / 预期 %.2f GB" % (session_idx, len(session),
                                              total_expect / 1024.0 ** 3))
    for f in session:
        log("    %-56s %6.2f GB  开始 %s"
            % (f["name"], _bytes_of(f.get("size")) / 1024.0 ** 3,
               f.get("start").strftime("%m-%d %H:%M:%S") if f.get("start") else "?"))
    if dry_run:
        log("--dry-run：不下不载，到此为止")
        return 0

    # ---- ③ 逐段下载 ----
    flv_paths = []
    for f in session:
        dest = os.path.join(work, f["name"])
        t0 = time.time()
        got = D.download_one(cfg, log, f["name"], dest,
                            expect_bytes=int(f.get("size_gb", 0) * 1024 ** 3))
        if not got:
            log("下载失败：%s" % f["name"], "ERROR")
            return 3
        real = os.path.getsize(got)
        exp = _bytes_of(f.get("size"))
        ok_size = (exp == 0) or (real >= exp * 0.90)
        log("  %s %s  本地 %.2f GB / 列表 %.2f GB  (%.0fs)"
            % ("✓" if ok_size else "✗", f["name"], real / 1024.0 ** 3,
               exp / 1024.0 ** 3, time.time() - t0))
        if not ok_size:
            log("  文件大小明显偏小，可能存在截断，终止", "ERROR")
            return 3
        flv_paths.append(got)

    # ---- ④ 逐段可打开性 ----
    seg_durs = []
    for p in flv_paths:
        d = _dur_of(ffprobe, p)
        seg_durs.append(d)
        log("  ffprobe %-52s %s  %s"
            % (os.path.basename(p)[:52], hms(d), "OK" if d > 0 else "打不开!"))
        if d <= 0:
            log("存在不可读分段，保留分片待排查", "ERROR")
            return 4

    # ---- ⑤ 会话完整性 ----
    try:
        ok, note = D.check_session(cfg, log, session)
        log("会话检查：%s（%s）" % ("通过" if ok else "告警", note))
    except Exception as e:
        log("会话检查异常（不阻断）：%s" % e, "WARN")

    # ---- ⑥ 合并 ----
    out_mp4 = os.path.join(work, session[0]["name"].rsplit(".", 1)[0] + ".mp4")
    merged, lossless = D.merge_flvs(cfg, log, flv_paths, out_mp4)
    if not merged:
        log("合并失败", "ERROR")
        return 5
    log("合并完成：%s（%s）" % (os.path.basename(merged), "无损流复制" if lossless else "重编码"))

    # ---- ⑦ 产物核对 ----
    md = _dur_of(ffprobe, merged)
    ms = os.path.getsize(merged)
    drift = abs(md - sum(seg_durs))
    log("合并产物：%.2f GB / %s（分段合计 %s，偏差 %.1fs）"
        % (ms / 1024.0 ** 3, hms(md), hms(sum(seg_durs)), drift))
    if md <= 0 or drift >= 60:
        log("合并产物时长异常，终止", "ERROR")
        return 5

    # ---- ⑧ 落位 ----
    root = (cfg.get("local") or {}).get("root")
    if not root or not os.path.isdir(root):
        log("local.root 不可用：%s" % root, "ERROR")
        return 6
    target = os.path.join(root, _rec_name(uid, session[0]))
    if os.path.exists(target):
        log("目标已存在，跳过落位：%s" % target)
    else:
        os.replace(merged, target)
        log("已落位：%s（%.2f GB）" % (target, os.path.getsize(target) / 1024.0 ** 3))

    # ---- ⑨ ★ 还原 mtime（否则首次扫描必被判「录制中」跳过）----
    _apply_mtime(target, session[-1].get("end"), log, min_age=min_age)

    # ---- ⑩ 回收中间 FLV ----
    if keep_intermediate:
        log("--keep-intermediate：保留中间 FLV")
    else:
        freed = 0
        for p in list(flv_paths) + ([merged] if os.path.exists(merged) else []):
            if os.path.exists(p):
                try:
                    freed += os.path.getsize(p)
                    force_unlink(p, log)
                except Exception as e:
                    log("清理中间文件失败 %s：%s" % (os.path.basename(p), e), "WARN")
        if freed:
            log("已回收中间文件：%.2f GB" % (freed / 1024.0 ** 3))

    result = {
        "ymd": ymd, "n_segments": len(flv_paths), "src_video": target,
        "duration": md, "expected_duration": sum(seg_durs),
        "size_bytes": os.path.getsize(target), "lossless": lossless,
        "title": session[0].get("title"), "hms": session[0].get("hms"),
        "local_name": os.path.basename(target),
        "segments": [os.path.basename(p) for p in flv_paths],
    }
    log("落位结果：%s" % json.dumps(result, ensure_ascii=False))

    # ---- ⑪ 跑全套管线 ----
    if no_pipeline:
        log("--no-pipeline：交棒结束")
        return 0
    log("=== 启动 daily_pipeline 全流程 ===")
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, os.path.join(_HERE, "daily_pipeline.py"),
         "--config", cfg.get("_config_path", "config.json")],
        cwd=_HERE, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=pipeline_timeout)
    tail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else ""))[-3000:]
    for line in tail.splitlines():
        log("| " + line)
    log("daily_pipeline 退出码=%d，耗时 %.0f 分钟"
        % (proc.returncode, (time.time() - t0) / 60))
    return 0 if proc.returncode == 0 else proc.returncode


def main():
    global DEFAULT_MIN_AGE_SEC
    ap = argparse.ArgumentParser(description="从录播站按日期拉取一整场并跑全套管线")
    ap.add_argument("--date", required=True, help="直播日期 YYYY-MM-DD（场次首段所在日）")
    ap.add_argument("--config", help="配置文件路径")
    ap.add_argument("--gap", type=int, default=1800,
                    help="同一场相邻片段的最大间隙秒数（默认 1800；跨午夜约 1 小时分段）")
    ap.add_argument("--session", type=int, default=0, help="该日第几场（从 0 起，默认 0）")
    ap.add_argument("--dry-run", action="store_true", help="只列清单不下载")
    ap.add_argument("--no-pipeline", action="store_true", help="只落位，不跑后续管线")
    ap.add_argument("--keep-intermediate", action="store_true", help="保留中间 FLV")
    ap.add_argument("--min-age", type=int, default=DEFAULT_MIN_AGE_SEC,
                    help="文件稳定窗口秒数，与 daily_pipeline 一致（默认 120）")
    args = ap.parse_args()

    cfg_path = args.config or os.path.join(_HERE, "config.json")
    cfg = load_config(cfg_path)
    cfg["_config_path"] = cfg_path
    log = Logger(cfg, tag='rec-fetch')

    try:
        ymd = datetime.datetime.strptime(args.date, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        log("--date 格式须为 YYYY-MM-DD", "ERROR")
        return 2

    return fetch_date(cfg, log, ymd, args.gap, args.session,
                      dry_run=args.dry_run,
                      keep_intermediate=args.keep_intermediate,
                      no_pipeline=args.no_pipeline,
                      min_age=args.min_age)


if __name__ == "__main__":
    sys.exit(main())
