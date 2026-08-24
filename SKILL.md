---
name: bili-live-summary
description: |
  B站直播录播全自动总结 skill。将指定 B站合集的最新录播自动下载（多分P合并）、
  FunASR 带时间轴转写、调用大模型按 30 分钟切块分步总结、追加写入月度 Word 文档。
  当用户要求「自动总结某个 B站主播的直播录播」「每天自动抓取并总结直播回放」
  「把某个 B站合集做成定期总结」「定时把直播转文字加 AI 总结成 Word」时触发。
  需要用户提供：B站合集 URL、AI API Key、主播名与观众名（用于总结人称）。
agent_created: true
---

# B站直播录播全自动总结 (bili-live-summary)

把「B站合集里的新录播」变成「带时间轴的 Word 总结文档」，全流程无需人工干预。

管道（**统一入口 `daily_pipeline.py`**，2026-08-24 重构为 5 步流程）：**每天由自动化触发一次，先监测本地录播根目录 `D:\录播\25788785-岁己SUI` 的新文件 → 有则本地转写总结、无则去 B站合集兜底下载合并 → 总结后导出中质量 MP3 → 最后压成 ~5GB HEVC MP4**。
全流程：监测本地根目录(flv/mp4) → FunASR 转写(带 `[HH:MM:SS]`) → 热词表纠正替换 → 大模型按 30 分钟切块逐块总结 → 追加写入月度 Word（每场分页 + 标题区分）→ 导出中质量 MP3 → 压缩 HEVC MP4 到「根目录/YYYY-MM-DD/」日期子目录。

> 历史脚本 `local_priority.py`（本地优先扫描）+ `run_pipeline.py`（B站管线）已被 `daily_pipeline.py` 取代，保留仅作参考，不要再在新自动化里调用。

说明：下文 `SKILL_DIR` 指本 skill 的安装目录（即 `~/.workbuddy/skills/bili-live-summary`）。

## 何时使用
- 用户要给某位 B站虚拟主播 / UP主的直播合集做「定期自动总结」
- 用户说「每天自动抓取最新直播录播并总结」「把录播转文字加 AI 总结成文档」
- 需要把长直播回放变成可读、带时间戳、分主题的摘要

## 即开即用工作流

### 第 1 步：首次配置（仅一次）
运行 skill 的 `setup.py` 向导，自动检测 ffmpeg/aria2c/python、生成工作目录配置、写入指针文件。
收集到用户参数后，可用命令行一次性完成（便于智能体直接调用）：

```
python SKILL_DIR/scripts/setup.py \
  --series-url "https://space.bilibili.com/MID/lists/SERIES_ID?type=series" \
  --api-key "API_KEY" \
  --streamer "岁己" --audience "饼干岁" \
  --doc-dir "C:/Users/Tsing/Desktop" \
  --schedule-hours "2,10,18"
```

缺参数时 setup 会交互式询问。默认工作目录 `D:\岁己直播自动总结`（可用 `--workdir` 改）。
配置生成后，后续直接 `python SKILL_DIR/scripts/run_pipeline.py` 即可，**无需任何参数**。

### 第 2 步：验证
```
python SKILL_DIR/scripts/run_pipeline.py --dry-run
```
观察「本次待处理 N 期」，确认选片逻辑正确（检测到目标期尚未上传而空转等待属于正常状态）。

### 第 3 步：创建每日自动化（已配置）
每日自动化（`automation_update` 的 `automation-1786280495470`，名称「岁己直播录播 · 每日4点自动总结」）调度 `FREQ=DAILY;BYHOUR=4;BYMINUTE=0;BYSECOND=0`，命令为：
```
"C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe" -u "C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\daily_pipeline.py"
```
即开即用：无需任何参数，脚本内置完整 5 步流程 + 单实例锁 + 断点续跑。改调度/手动补跑用 `automation_update`；定向重跑某天加 `--date 2026-08-24` 参数（如某天漏跑）。

## 配置要点（config.json，位于工作目录根）
- **series**：合集 mid / series_id / url
- **latest_mode.mode**：「最新录播」判定（见下）
- **summarize**：provider / model / api_key / chunk_minutes(30) / streamer / audience。
  **首选 `provider:"google"`、`model:"gemini-3.7-flash"`**（见下「大模型调用约定」），勿默认用 `zhipu/glm`（曾因不稳改过，现已回归 Gemini 首选）。
- **output**：doc_dir、doc_name_pattern（支持 `{streamer}`/`{year}`/`{month}`）、font
- **paths**：project_root / python / ffmpeg / ffprobe / aria2c / cookies
- **transcribe / cleanup**：推理参数与产物清理
- **hotwords**：转写后自动纠正。`enabled:true` 时解析 `path`（默认 `SKILL_DIR/scripts/hotwords.md`）里的替换表，对 TXT/SRT 各做一遍替换再送 AI；`enabled:false` 关闭；`path` 可指向自定义替换表
- **标点清洗（冗余双标点）**：FunASR 的 VAD 分段兜底会在转写里叠出 `。，` `？，` `，。` `。？` 这类"句末终结符+句内连接符相邻"的冗余双标点。脚本在**写出 TXT/SRT 前**（`clean_segment_boundaries`）与**写 Word 前**（`clean_punctuation`，覆盖每块总结与概要）两处自动清洗——纯纠错不改语义，无需手动干预。若日后发现新双标点模式，改 `common.py` 的 `_RE_TERMINAL_CONN / _RE_CONN_TERMINAL` 即可。
- **数字归一（规格数字中文读法→阿拉伯）**：FunASR 常把规格数字读成中文（`三零五零显卡`→`3050显卡`、`七十五赫兹`→`75Hz`、`八G`→`8G`、`一二四零零CPU`→`12400CPU`）。脚本在**写出 TXT/SRT 前**（`normalize_numbers`）与**写 Word 前**（每块总结+概要各再做一遍）两处把"紧邻规格单位（显卡/GPU/Hz/赫兹/G/GB/寸/英寸/K/p/ti 等）的中国数字"转回阿拉伯，**仅对紧邻规格单位的读数生效，普通计数（三个/七十五块钱/二千元）不动**，已是阿拉伯的文本幂等不受影响。若日后要增删单位，改 `common.py` 的 `_NUM_TRIGGERS` 即可。
- **话题分隔符（横向分隔线）**：一场直播总结成一整段不易阅读。脚本在写完 Word 前，把全场总结合并后**调一次模型**识别话题边界（吃饭↔打游戏↔看电影等），在边界插入 `§SEP§` 标记并切分成多个话题块，相邻话题块之间写一条**灰色横线**（`add_horizontal_rule`，段落下边框）。实现：`summarize_gemini.py` 的 `segment_topics_with` + `append_section(topic_sep=True)`，两个入口 `summarize_one` 与 `regen_gemini_v2.process_session` 都已接入。`config.json` 的 `topic_separator.enabled=false` 可关闭（退回不分段）；模型若异常/改动了原文行则自动退回单块（不丢内容、不加线）。
- **原始录播压缩（HEVC 归档）**：用户规则(2026-08-24)。在「导出 MP3」之后，用 **NVIDIA NVENC HEVC(H.265)** 把原始录播压成更小归档副本（需 NVIDIA 独显；RTX 20 系+ 支持 `hevc_nvenc`，速度远快于 CPU）。目标体积由 `target_size_gb`（默认 5GB）做软上限，质量由 `cq`（默认 24，越小越清晰）保证——通常原始 1080p60 在 HEVC 下可轻松压到目标且画质不降甚至更优。音频默认 `copy` 无损复用，设 `audio_mode="aac"` 则重编码 192k。实现：`scripts/compress_video.py` 的 `compress_video()`，已在 `daily_pipeline.py` 的 `process_video_source` 末尾调用。
  - **输出位置**由 `compress.out_dir_mode` 控制：`date_subfolder`（默认，用户 tips）= 把压缩后 MP4 放进「根目录/YYYY-MM-DD/」日期子目录（文件名 `<原名>_compressed.mp4`）；`next_to_source` = 输出到源文件旁。日期从文件名里的 8 位日期（`20260824`）解析，自动跳过 UID 等非法日期串。
  - **是否删除原视频**由 `compress.replace_original` 控制：本地源 `true` 时，压缩成功后先把原文件备份到 `~/.workbuddy/backups/live_raw/`，再用回收站机制删除原始视频（绝不裸删），真正释放空间；B站兜底路径压缩成功后直接删除临时下载视频。设为 `false` 则只生成 `_compressed` 副本、保留原始。不论哪种模式，压缩失败都不会动原文件。幂等：目标已存在则跳过。

## 大模型调用约定（首选 Google / Gemini）

**默认且首选 Google Gemini API**，不要默认用 zhipu/glm 等其它 provider。
`config.json` 的 `summarize` 应设：

```json
"summarize": {
  "provider": "google",
  "model": "gemini-3.7-flash",
  "api_key": "<Google AI Studio / Gemini API Key>",
  "api_host": "https://generativelanguage.googleapis.com/v1beta/models",
  "proxy": "http://127.0.0.1:7897",
  "thinking_level": "low",
  "chunk_minutes": 30,
  ...
}
```

### 模型轮换顺序（GeminiRotator）
旁路重建 / 重跑脚本（`regen_gemini_v2.py`、`run_812.py`）使用 `GeminiRotator` 按下列顺序轮换，
**首选 `gemini-3.7-flash`**，遇 429（每日配额耗尽）/404（模型不可用）自动顺切下一档；每模型成功调用达 20 次也主动切下一档：

```
gemini-3.7-flash → gemini-3.6-flash → gemini-3.5-flash → gemini-3-flash → gemini-2.5-flash
→ gemini-3.5-flash-lite → gemini-3.6-flash-lite → gemini-3-flash-lite → gemini-2.5-flash-lite
→ gemini-2.5-pro → gemini-3-pro → gemini-3.5-pro
```

`MODELS` 常量定义在 `regen_gemini_v2.py`，`run_812.py` 直接复用 `R.MODELS`，两处同步生效。
`config.example.json` 与 `setup.py` 的默认 `model` 也已对齐为 `gemini-3.7-flash`。

> 注意：`run_pipeline.py`（每日自动化实际跑的路径）走 **单模型、无自动轮换兜底**，直接用 config 里的
> `model`（首选 3.7-flash）。3.7 撞免费层每日配额（每模型 20 次/天）会当日跳过、次日续跑；
> 需要轮换兜底时用 `regen_gemini_v2.py` / `run_812.py` 的转子。两套路径首选模型一致，互不冲突。

## 5 步每日流程（统一入口 `daily_pipeline.py`，2026-08-24 起）

`daily_pipeline.py` 是每日自动化的**唯一入口**（自动化 4:00 触发，命令见下「每日自动化」）。
它内置完整 5 步，无需再拆成多个脚本：

1. **监测本地根目录**：扫描 `D:\录播\25788785-岁己SUI` 根目录下的新录播（`*.flv`/`*.mp4`/`*.mkv`/`*.mov`，直接放根目录、不在日期子目录）。
   · 已处理（state 占位 / TXT 命中）的跳过；
   · **正在录制中**（文件大小不稳定 / 修改时间太新）的整段跳过，避免截半——这是防「录到一半就被拿去转写」的关键。
2. **有本地新文件 → 本地总结**：抽 16k 音频 → FunASR 转写 → 热词纠正 → Gemini 切块总结 → 写 Word，并写 `state.json` 占位（`LOCAL-YYYYMMDD[-N]`，`date` 推高一天，使 B站兜底不会重复抓同场，避免重复消耗 token）。
3. **无本地新文件 → B站兜底**：去合集 `series_id=2609053` 抓最近未处理的录播，用 `download_merge.download_video_one` 下载并合并多 P 为单个视频，再走同一条总结流程。
4. **导出中质量 MP3**：总结完成后，把视频转一份 192k/44.1k 的中质量 MP3（体积不大、音质满足文字识别即可，用户另有他用），落在 `mp3_export.out_dir`（根目录）。
5. **压缩 HEVC MP4**：最后把视频压成 ~5GB 的 HEVC(H.265) MP4，按 `compress.out_dir_mode=date_subfolder` 输出到「根目录/YYYY-MM-DD/」日期子目录；本地源在压缩成功后移入回收站（先备份到 `~/.workbuddy/backups/live_raw/`）。

**最终每场交付物只有三样**：① AI 总结的 Word 文档（桌面）② 中质量 MP3（根目录）③ 压缩后的 HEVC MP4（根目录/日期子目录）。

落地实现（已生效）：`daily_pipeline.py` 同时包含「根目录监测 + 本地总结 + B站兜底 + MP3 + 压缩」，单实例锁防并发，断点续跑靠 state 占位与 TXT/SRT 存在性判定。手动运行：`python SKILL_DIR/scripts/daily_pipeline.py --dry-run` 只预览不处理。

> 踩过的坑（2026-08-16）：曾因本地脚本没真实执行、占位没写，导致次日自动化重复抓 B站、多花 token。根因：**「本地脚本写入 state 占位」才是跳过某场的唯一有效开关**。故自动化必须真实跑成功（看日志确认 `LOCAL-*` 占位已写入）后再依赖它。

### 同日期多段连续录播 → 先合并再总结（2026-08-22 新增规则）
网络波动会导致录播器断流重连，于是**同一日期目录下/根目录下出现多个录播文件**。规则：
- `scan_root_sessions` 扫描根目录，按文件名里的时间(`HHMMSS`)排序，用 ffprobe 取每段时长算出结束时间。
- **相邻两段「前段结束 ≈ 后段开始」且间隙 ≤ `local_merge.max_gap_sec`（默认 600s）、且能取到时长** → 视为同一场（网络断开重连），并入同一 chain。
- 间隙过大、或某段取不到时长 → 视为**独立场次**，各自成 chain、各自总结（同日期多场用编号占位 `LOCAL-YYYYMMDD-2`、`-3`… 区分）。
- 合并：优先 ffmpeg `concat demuxer`（`-c copy`，极快不重编码）；编码不一致导致失败时回退 `concat filter` 重编码兜底。
- 合并产物落在 `<project_root>/merged/`，文件名沿用首段 basename 加 `_merged`；幂等（已存在则跳过）。`local_merge.keep_merged=false` 时总结完成后删除该中间视频。
- 配置项（`config.json` 的 `local_merge`）：`enabled`(总开关) / `max_gap_sec`(连续判定间隙上限) / `keep_merged`(是否保留合并视频) / `exclude_dates`(日期列表，**显式强制不合并**，优先级高于 `max_gap_sec`，用于用户确认是「多场独立直播」而非断流重连的日期)。
- **单文件目录、或 `local_merge.enabled=false`** 时退化为旧行为（每目录取首个文件），不合并。
- ⚠️ **临界风险**：若某日期多文件真实间隙只比 `max_gap_sec` 略大（如 609s vs 600s），ffprobe 时长精度波动可能让间隙跌破阈值而被误合并。对确认是独立多场的日期务必加入 `exclude_dates`（如 `"exclude_dates": ["2026-08-22"]`），不依赖阈值侥幸。
- 复用：因合并视频已留档（`keep_merged=true`），重跑不会重复合并；已写占位(`LOCAL-*`)的场次仍会被跳过。

### 同日期多段连续录播 → 先合并再总结（2026-08-22 新增规则）
网络波动会导致录播器断流重连，于是**同一日期目录下出现多个录播文件**。规则：
- `local_priority.py` 扫描每个日期目录，按文件名时间(`HHMMSS`)排序，用 ffprobe 取每段时长算出结束时间。
- **相邻两段「前段结束 ≈ 后段开始」且间隙 ≤ `local_merge.max_gap_sec`（默认 600s）、且能取到时长** → 视为同一场（网络断开重连），并入同一 chain。
- 间隙过大、或某段取不到时长 → 视为**独立场次**，各自成 chain、各自总结（同日期多场用编号占位 `LOCAL-YYYYMMDD-2`、`-3`… 区分）。
- 合并：优先 ffmpeg `concat demuxer`（`-c copy`，极快不重编码）；同 recorder 编码一致时直接命中。编码不一致导致失败时回退 `concat filter` 重编码兜底。
- 合并产物落在 `<project_root>/merged/`，文件名沿用首段 basename 加 `_merged`；幂等（已存在则跳过）。`local_merge.keep_merged=false` 时总结完成后删除该中间视频。
- 合并后整场走原「抽 16k 音频 → FunASR 转写 → 热词 → 30min 切块总结 → 写 Word → 导出归档 MP3」流程；归档 MP3 文件名带 `_merged` 后缀，与源命名规则一致（剥 `录制-25788785-` 前缀）。
- 配置项（`config.json` 的 `local_merge`）：`enabled`(总开关) / `max_gap_sec`(连续判定间隙上限) / `keep_merged`(是否保留合并视频) / `exclude_dates`(日期列表，**显式强制不合并**，优先级高于 `max_gap_sec`，用于用户确认是「多场独立直播」而非断流重连的日期)。
- **单文件目录、或 `local_merge.enabled=false`** 时退化为旧行为（每目录取首个文件），不合并。
- ⚠️ **临界风险**：若某日期多文件真实间隙只比 `max_gap_sec` 略大（如 609s vs 600s），ffprobe 时长精度波动可能让间隙跌破阈值而被误合并。对确认是独立多场的日期务必加入 `exclude_dates`（如 `"exclude_dates": ["2026-08-22"]`），不依赖阈值侥幸。
- 复用：因合并视频已留档（`keep_merged=true`），重跑不会重复合并；已写占位(`LOCAL-*`)的场次仍会被跳过。

## 「最新录播」的几种判定（latest_mode.mode）
| mode | 含义 |
|---|---|
| `latest_by_live_date` | **默认**。按直播实际日期滚动：只处理 live_date 大于「已处理最晚直播日期」的未处理录播。上传时间不固定时最稳，配合每日多次调度自动等待目标期上线 |
| `newer_than_last` | 比 state.json 已处理最新一期（按投稿日期）更新的未处理录播 |
| `latest_n` | 只看合集最新 latest_n 期里未处理的 |
| `since_date` | 投稿日期 ≥ since_date 且未处理的 |
| `backfill_all` | 全合集从旧到新补齐历史，受 max_per_run 限制 |

`latest_by_live_date` 额外支持 `since_live_date`（YYYY-MM-DD）作为**硬下限**：只处理直播日期 ≥ 该日期的录播（如用户明确「只要 2026-08-09 起的」）。基线 = max(since_live_date, 已处理最晚直播日期)，处理完一期后自动滚动到下一期；目标期未上传时自动化空转等待。

注：直播「实际日期」从标题解析（如「2026年08月01日19点场」），避免凌晨下播投稿导致的跨天 / 跨月错位。

## 硬约束（务必保持）
- 转录文本**必须按 30 分钟切块逐块发送 AI**，绝不允许整篇一次性发送（防超长 / 超时 / 截断）。
- Word 文档：同月复用同一文档，每场新直播插入**分页符 + Heading2 标题**区分；总结段粗体、金句斜体。
- **章节元数据行（必须）**：每个 Heading2 章节标题下方必须紧跟一行元数据：`直播日期 YYYY-MM-DD  |  时长 HH:MM:SS  |  N/N 段  |  总结于 YYYY-MM-DD HH:MM`；本地 / B站场次另起一行 `来源 <本地 mp4/flv 路径 或 https://www.bilibili.com/video/BVxxxx>`。旁路本地路径（`regen_gemini_v2.process_session`，见脚本第 275 行）与自动化路径（`summarize_gemini.append_section`，见脚本第 592 行）生成的元数据格式**必须完全一致**——曾因旁路漏写时长/段数/来源，导致 8/12、8/13 章节元数据残缺，已手动补正并修复 `process_session` 加 `src_path` 参数。
- 断点续跑：每场每阶段写入 state.json，中断重跑自动跳过已完成部分。
- 热词纠正：转写之后、送 AI 之前必须对 TXT/SRT 做一遍热词替换（`hotwords.md`，含主播名/术语/歌名/保护规则），确保送进模型的文本已纠正。

## 运行与调试
- `run_pipeline.py --bvid BVxxx` 手动指定某期
- `run_pipeline.py --skip-cleanup` 保留 MP3
- 日志在 `WORKDIR/logs/`，错误在 `errors.log`；状态 / 基线在 `WORKDIR/state.json`

## 依赖与环境
详见 `references/environment.md`：需 GPU(PyTorch/FunASR)、FFmpeg、aria2c、B站 cookies；模型首次运行从 ModelScope 自动下载。

## 安全与隐私
- API Key 明文存于工作目录 config.json——仅本机使用，勿提交到公开仓库。
- cookies 为登录态凭证，同属敏感文件。
- 仅处理用户自有或有权总结的直播内容。
