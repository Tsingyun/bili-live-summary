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

管道（**默认「本地优先」**）：**先查本地录播目录 `D:\录播\25788785-岁己SUI`，某日期有本地 mp4/flv 就直接本地转写处理；只有本地没有该日期录播时，才去 B站合集抓取最新录播**
→ 下载并合并分P为单 MP3（仅 B站路径）/ 直接使用本地文件 → FunASR 转写(带 `[HH:MM:SS]`)
→ 热词表纠正替换 → 大模型按 30 分钟切块逐块总结 → 追加写入月度 Word（每场分页 + 标题区分）。

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

### 第 3 步：创建每日自动化（可选）
用 `automation_update` 创建任务，命令为上面「正式运行」那行；调度 RRULE 例如：
`FREQ=DAILY;BYHOUR=2,10,18;BYMINUTE=0;BYSECOND=0`
（上传时间不固定时，每天多次检查更稳）。

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

## 本地优先工作流（核心铁律，每次都先查本地）

**默认工作流：先去本地录播目录找，没有再去 B站。**

- 本地目录：`D:\录播\25788785-岁己SUI\<YYYY-MM-DD>\*.{mp4,flv}`
- 某日期**本地有**录播 → **直接用它转写+总结**，并写 `state.json` 占位（`LOCAL-YYYYMMDD`，`date` 推高一天）让 B站自动化跳过该场，**避免重复消耗 token**。
- 某日期**本地没有**录播 → 才去 B站合集（`fetch_latest.py`）抓该日期的新录播。

**落地实现（已生效，非可选）**：`local_priority.py` 是每日自动化的**前置闸门**——自动化 prompt 第一步即运行它（扫描本地未处理场次、优先本地总结写占位），第二步才 `run_pipeline.py` 抓 B站。从根上保证「本地提供的场次绝不被自动化重做」。单独手动运行：`python SKILL_DIR/scripts/local_priority.py`（加 `--dry-run` 只预览、不实际处理）。

踩过的坑（2026-08-16）：曾因本地脚本启动命令语法错误（`cd /d` 在 Git Bash 失效）导致脚本没跑、占位没写，次日自动化照常抓了 B站 8/15，多花约 10 次 Gemini 调用。根因：**「本地脚本写入 state 占位」才是自动化跳过某场的唯一有效开关**；占位没写，自动化只能按默认逻辑抓取。故务必保证本地优先步骤真实执行成功（看日志确认 `LOCAL-*` 占位已写入）后再依赖自动化。

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
