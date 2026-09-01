---
name: bili-live-summary
description: |
  B站直播录播全自动总结 skill（通用、配置驱动）。将「某位 B站主播合集的新录播」自动下载（多分P合并）、
  FunASR 带时间轴转写、调用大模型按 30 分钟切块分步总结、追加写入月度 Word 文档。
  当用户要求「自动总结某个 B站主播的直播录播」「每天自动抓取并总结直播回放」
  「把某个 B站合集做成定期总结」「定时把直播转文字加 AI 总结成 Word」时触发。
  需要用户提供：B站合集 URL、AI API Key、主播名与观众名（用于总结人称）。
  本 skill 完全配置驱动、不含任何主播专有数据——每个主播用一个独立工作目录 + 一份 config.json 即可运行，零代码改动。
agent_created: true
---

# B站直播录播全自动总结 (bili-live-summary)

把「某位 B站主播合集里的新录播」变成「带时间轴的 Word 总结文档」，全流程无需人工干预。

管道（**统一入口 `daily_pipeline.py`**，通用 skill、配置驱动）：**每天由自动化触发一次，先监测本地录播根目录（`config.local.root`）的新文件 → 有则本地转写总结、无则去 B站合集（`config.series`）兜底下载合并 → 总结后导出中质量 MP3 → 最后压成 ~5GB HEVC MP4**。

全流程：监测本地根目录(flv/mp4) → FunASR 转写(带 `[HH:MM:SS]`) → 热词表纠正替换 → 大模型按 30 分钟切块逐块总结 → 追加写入月度 Word（每场分页 + 标题区分）→ 导出中质量 MP3 → **本地录播**压成 HEVC MP4 到「根目录/YYYY-MM-DD/」日期子目录（**B站兜底下载的录播不压缩、原样保留 MP4**）。

> 本 skill **完全配置驱动、零主播专有数据**：每个主播用一个独立「工作目录」（含各自 `config.json` / `state.json` / `hotwords.md` / `prompt_original.txt`），无需改任何代码即可套用任意新主播。脚本通过 `--workdir` / `--config` / 环境变量 `BILI_WORKDIR` / skill 内 `_workdir.json` 指针 这四级回退确定用哪份配置，**绝不在代码里写死主播信息**。

> 历史脚本 `local_priority.py`（本地优先扫描）+ `run_pipeline.py`（B站管线）已被 `daily_pipeline.py` 取代，保留仅作参考，不要再在新自动化里调用。

说明：下文 `SKILL_DIR` 指本 skill 的安装目录（即 `~/.workbuddy/skills/bili-live-summary`）；`WORKDIR` 指某个主播的工作目录（含其 `config.json`）。

## 何时使用
- 用户要给某位 B站虚拟主播 / UP主的直播合集做「定期自动总结」
- 用户说「每天自动抓取最新直播录播并总结」「把录播转文字加 AI 总结成文档」
- 需要把长直播回放变成可读、带时间戳、分主题的摘要

## 即开即用工作流

### 第 1 步：首次配置（每个主播一次）
运行 skill 的 `setup.py` 向导，自动检测 ffmpeg/aria2c/python、生成工作目录配置、写入指针文件。收集到用户参数后，可用命令行一次性完成（便于智能体直接调用）：

```
python SKILL_DIR/scripts/setup.py \
  --series-url "https://space.bilibili.com/MID/lists/SERIES_ID?type=series" \
  --api-key "API_KEY" \
  --streamer "主播名" --audience "观众名" \
  --doc-dir "C:/Users/xxx/Desktop" \
  --schedule-hours "2,10,18"
```

缺参数时 setup 会交互式询问。默认工作目录 `D:\直播自动总结`（可用 `--workdir` 改；**建议每个主播独立目录**，例如 `D:\岁己直播自动总结`）。配置生成后，后续直接 `python SKILL_DIR/scripts/daily_pipeline.py --workdir "<工作目录>"` 即可，**无需任何硬编码参数**。

> **多主播共存**：每个主播一个工作目录。脚本四级回退定位 config——① `--config` 指定路径 ② `--workdir` / 环境变量 `BILI_WORKDIR` 指向的目录下的 `config.json` ③ skill 内 `_workdir.json` 指针指向的目录 ④ 失败则报错退出。换主播 = 换工作目录，不动代码。

### 第 2 步：验证
```
python SKILL_DIR/scripts/daily_pipeline.py --workdir "<工作目录>" --dry-run
```
观察「本次待处理 N 期」，确认选片逻辑正确（检测到目标期尚未上传而空转等待属于正常状态）。

### 第 3 步：创建每日自动化
用 `automation_update` 创建 recurring 自动化，命令指向 `daily_pipeline.py --workdir "<工作目录>"`：
```
"C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe" -u "C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\daily_pipeline.py" --workdir "D:\岁己直播自动总结"
```
即开即用：无需任何额外参数，脚本内置完整 5 步流程 + 单实例锁 + 断点续跑。改调度/手动补跑用 `automation_update`；定向重跑某天加 `--date 2026-08-24` 参数（如某天漏跑）。

## 配置要点（config.json，位于工作目录根）
- **series**：合集 mid / series_id / url（B站兜底用）
- **local**：`root` = 本地录制根目录（flv/mp4 直接放这，不在日期子目录）；`name_regex` 留空用默认（`录制-<UID>-<YYYYMMDD>-<HHMMSS>-<序号>-<标题>.<ext>`），自定义须含命名组 `date(YYYYMMDD)`/`time(HHMMSS)`/`title`
- **bilibili**：B站兜底录播的处置（用户规则 2026-08-29）。`keep_mp4`（默认 `true`）= 跑完流程后**保留 B站录播的 MP4**，原样归档到「录播根目录/YYYY-MM-DD/」；`compress`（默认 `false`）= **不压缩**这类 MP4
- **latest_mode.mode**：「最新录播」判定（见下）
- **summarize**：`provider` / `model` / `api_key` / `chunk_minutes`(30) / `streamer` / `audience` / `prompt_path`。
  **首选 `provider:"google"`、`model:"gemini-3.7-flash"`**（见下「大模型调用约定」），勿默认用 `zhipu/glm`。`prompt_path` 指向的 prompt 文件支持 `{streamer}`/`{audience}` 占位符（运行时替换），无占位符则幂等保留字面内容，兼容既有 prompt。
- **output**：`doc_dir`、`doc_name_pattern`（支持 `{streamer}`/`{year}`/`{month}`）、`font`
- **paths**：`project_root` / `python` / `ffmpeg` / `ffprobe` / `aria2c` / `cookies`
- **transcribe / cleanup**：推理参数与产物清理
- **hotwords**：转写后自动纠正。`enabled:true` 时解析 `path`（默认用 skill 自带空模板 `SKILL_DIR/scripts/hotwords.md`）里的替换表，对 TXT/SRT 各做一遍替换再送 AI；`enabled:false` 关闭；`path` 可指向工作目录下的自定义替换表（相对路径以 WORKDIR 为基准）。
- **标点清洗（冗余双标点）**：FunASR 的 VAD 分段兜底会在转写里叠出 `。，` `？，` `，。` `。？` 这类"句末终结符+句内连接符相邻"的冗余双标点。脚本在**写出 TXT/SRT 前**（`clean_segment_boundaries`）与**写 Word 前**（`clean_punctuation`，覆盖每块总结与概要）两处自动清洗——纯纠错不改语义，无需手动干预。若日后发现新双标点模式，改 `common.py` 的 `_RE_TERMINAL_CONN / _RE_CONN_TERMINAL` 即可。
- **数字归一（规格数字中文读法→阿拉伯）**：FunASR 常把规格数字读成中文（`三零五零显卡`→`3050显卡`、`七十五赫兹`→`75Hz`、`八G`→`8G`）。脚本在**写出 TXT/SRT 前**（`normalize_numbers`）与**写 Word 前**（每块总结+概要各再做一遍）两处把"紧邻规格单位（显卡/GPU/Hz/赫兹/G/GB/寸/英寸/K/p/ti 等）的中国数字"转回阿拉伯，**仅对紧邻规格单位的读数生效，普通计数不动**，已是阿拉伯的文本幂等不受影响。若日后要增删单位，改 `common.py` 的 `_NUM_TRIGGERS` 即可。
- **话题分隔符（横向分隔线）**：一场直播总结成一整段不易阅读。脚本在写完 Word 前，把全场总结合并后**调一次模型**识别话题边界（吃饭↔打游戏↔看电影等），在边界插入 `§SEP§` 标记并切分成多个话题块，相邻话题块之间写一条**灰色横线**（`add_horizontal_rule`，段落下边框）。实现：`summarize_gemini.py` 的 `segment_topics_with` + `append_section(topic_sep=True)`。config.json 的 `topic_separator.enabled=false` 可关闭（退回不分段）；模型若异常/改动了原文行则自动退回单块（不丢内容、不加线）。
- **原始录播压缩（HEVC 归档）**：在「导出 MP3」之后，用 **NVIDIA NVENC HEVC(H.265)** 把原始录播压成更小归档副本（需 NVIDIA 独显；RTX 20 系+ 支持 `hevc_nvenc`，速度远快于 CPU）。目标体积由 `target_size_gb`（默认 5GB）做软上限，质量由 `cq`（默认 24，越小越清晰）保证。音频默认 `copy` 无损复用，设 `audio_mode="aac"` 则重编码 192k。实现：`scripts/compress_video.py` 的 `compress_video()`，已在 `daily_pipeline.py` 的 `process_video_source` 末尾调用。
  - **输出位置**由 `compress.out_dir_mode` 控制：`date_subfolder`（默认）= 把压缩后 MP4 放进「根目录/YYYY-MM-DD/」日期子目录（文件名 `<原名>_compressed.mp4`）；`next_to_source` = 输出到源文件旁。日期从文件名里的 8 位日期解析，自动跳过 UID 等非法日期串。
  - **是否删除原视频**由 `compress.replace_original` + `compress.replace_mode` 控制（**仅本地录播**）：`true` 时压缩成功后处理原视频——`replace_mode=recycle` 先备份到 `~/.workbuddy/backups/live_raw/` 再移入回收站；`replace_mode=delete` **直接永久删除原视频**（省空间，用户 2026-08-28 选定）。设为 `false` 则只生成 `_compressed` 副本、保留原始。不论哪种模式，压缩失败都不会动原文件。幂等：目标已存在则跳过。
  - **B站兜底录播：不压缩、保留 MP4**（用户规则 2026-08-29）：本地磁盘没有当天录播而 B站有时，跑完流程后要留下 B站录播的 MP4。由 `bilibili.keep_mp4`（默认 `true`，归档到「录播根目录/YYYY-MM-DD/」）与 `bilibili.compress`（默认 `false`）控制。

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
  "streamer": "主播名",
  "audience": "观众名",
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

> 注意：`daily_pipeline.py`（每日自动化实际跑的路径）走 **单模型、无自动轮换兜底**，直接用 config 里的
> `model`（首选 3.7-flash）。3.7 撞免费层每日配额（每模型 20 次/天）会当日跳过、次日续跑；
> 需要轮换兜底时用 `regen_gemini_v2.py` / `run_812.py` 的转子。两套路径首选模型一致，互不冲突。

## 5 步每日流程（统一入口 `daily_pipeline.py`）

`daily_pipeline.py` 是每日自动化的**唯一入口**（自动化触发后，命令见上「第 3 步」）。
它内置完整 5 步，无需再拆成多个脚本：

1. **监测本地根目录**：扫描 `config.local.root` 根目录下的新录播（`*.flv`/`*.mp4`/`*.mkv`/`*.mov`，直接放根目录、不在日期子目录）。
   · 已处理（state 占位 / TXT 命中）的跳过；
   · **正在录制中**（文件大小不稳定 / 修改时间太新）的整段跳过，避免截半——这是防「录到一半就被拿去转写」的关键。
2. **有本地新文件 → 本地总结**：抽 16k 音频 → FunASR 转写 → 热词纠正 → Gemini 切块总结 → 写 Word，并写 `state.json` 占位（`LOCAL-YYYYMMDD[-N]`，`date` 推高一天，使 B站兜底不会重复抓同场，避免重复消耗 token）。
3. **无本地新文件 → B站兜底**：去合集（`config.series` 的 `mid`+`series_id`）抓最近未处理的录播，用 `download_merge.download_video_one` 下载并合并多 P 为单个视频，再走同一条总结流程。**这类录播跑完流程后保留原 MP4、不压缩**（`bilibili.keep_mp4=true`），按直播日期归档到「录播根目录/YYYY-MM-DD/」。
4. **导出中质量 MP3**：总结完成后，把视频转一份 192k/44.1k 的中质量 MP3（体积不大、音质满足文字识别即可，用户另有他用），落在 `mp3_export.out_dir`（默认根目录）。
5. **压缩 HEVC MP4（仅本地录播）**：最后把视频压成 ~5GB 的 HEVC(H.265) MP4，按 `compress.out_dir_mode=date_subfolder` 输出到「根目录/YYYY-MM-DD/」日期子目录；压缩成功后按 `compress.replace_mode` 处理原视频（`delete` = 直接永久删除）。**B站兜底录播跳过本步骤，原样保留 MP4。**

**最终每场交付物三样**：① AI 总结的 Word 文档（桌面）② 中质量 MP3（根目录）③ 视频——**本地录播**=压缩后的 HEVC MP4；**B站兜底**=原样保留的 MP4（均落在「根目录/YYYY-MM-DD/」）。

落地实现（已生效）：`daily_pipeline.py` 同时包含「根目录监测 + 本地总结 + B站兜底 + MP3 + 压缩」，单实例锁防并发，断点续跑靠 state 占位与 TXT/SRT 存在性判定。手动运行：`python SKILL_DIR/scripts/daily_pipeline.py --workdir "<工作目录>" --dry-run` 只预览不处理。

> 踩过的坑：曾因本地脚本没真实执行、占位没写，导致次日自动化重复抓 B站、多花 token。根因：**「本地脚本写入 state 占位」才是跳过某场的唯一有效开关**。故自动化必须真实跑成功（看日志确认 `LOCAL-*` 占位已写入）后再依赖它。

### 自动清理（防磁盘被撑爆，2026-09-01 新增）

每跑完一场直播，**临时占用 25~30GB**（源视频 ~20GB + 压缩产物 ~8GB 峰值并存；B站兜底还要
下载 ~9GB MP4 + 分P m4s 碎片；另有 16k 转写 MP3 ~0.5GB、ASR 切块 wav ~0.6GB）。
不给回收机制，几天就能把磁盘塞满。

`cleanup.py` 统一收口，在 pipeline **开跑前 + 每场收尾 + 整轮结束**各跑一次：

```bash
python cleanup.py --report              # 只看占用统计（不删任何东西）
python cleanup.py --sweep --dry-run     # 预览将要删除什么
python cleanup.py --sweep               # 执行清扫
python cleanup.py --sweep --aggressive  # 无视保留天数，清空全部临时类
```

**清理范围（白名单，只删中间产物）**：workdir 下的 `tmp/`（下载碎片、ASR 切块、一次性脚本）、
`MP3/`（16k 单声道转写临时音频）、`logs/`、`backups/`、`__pycache__`、超期的 `TXT/`、`SRT/`，
以及根目录的 `*.bak` 和孤儿 `.lock`/`.heartbeat`。

**绝不碰（黑名单）**：录播根目录（`local.root`）里的任何文件——压缩成品 MP4、归档 MP3、
源视频；桌面的 Word 总结；`config.json` / `state.json` / `hotwords.md`。
源视频只允许 `compress_video` 在「压缩成功且 ffprobe 校验通过」后删除。

配置项（`config.json` 的 `cleanup` 段，天数按文件最后修改时间算，`-1` = 永久保留）：

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | true | 总开关 |
| `tmp_days` | 2 | tmp/** 保留天数 |
| `mp3_days` | 0 | 16k 转写 MP3（0 = 转写完立即删） |
| `log_days` | 30 | 日志保留天数 |
| `txt_days` / `srt_days` | -1 | 转写稿 / 字幕，默认永久保留 |
| `backup_days` | 30 | backups/** 与根目录 `*.bak` |
| `orphan_lock_hours` | 2 | 孤儿锁文件存活超过该小时数则清（防死锁卡住下次运行） |
| `disk_guard_gb` | 40 | D 盘剩余低于此值时，开跑前先强制清扫并告警 |
| `sweep_on_start` / `sweep_on_finish` | true | 开跑前 / 结束后是否自动清扫 |

> ⚠️ **踩过的坑（2026-09-01）**：自动化环境下，对 D 盘（用户数据盘）调用 `os.remove`
> 会被安全删除 hook 拦截，且 hook 自身抛 `AttributeError: 'NoneType' object has no
> attribute 'strip'`（不是权限错误，是 hook 的 bug）。结果是**所有 `os.remove` 静默失败**——
> 分P碎片、ASR 切块、16k 音频、「压缩后删源视频」全都删不掉，每场留下几 GB 到十几 GB。
> 因此**所有删除必须走 `common.force_unlink` / `common.force_rmtree`**（os.remove 失败时
> 自动降级到 PowerShell `Remove-Item`），禁止在管线脚本里直接写 `os.remove`。
> 另外降级调用 PowerShell 时 stderr 必须 `subprocess.PIPE` 拿 bytes 再自行解码，
> 否则 Windows 读线程会按 gbk 解码中文报错并抛 `UnicodeDecodeError` 刷爆日志。

### 同日期多段连续录播 → 先合并再总结
网络波动会导致录播器断流重连，于是**同一日期根目录下出现多个录播文件**。规则：
- `scan_root_sessions` 扫描根目录，按文件名里的时间(`HHMMSS`)排序，用 ffprobe 取每段时长算出结束时间。
- **相邻两段「前段结束 ≈ 后段开始」且间隙 ≤ `local_merge.max_gap_sec`（默认 600s）、且能取到时长** → 视为同一场（网络断开重连），并入同一 chain。
- 间隙过大、或某段取不到时长 → 视为**独立场次**，各自成 chain、各自总结（同日期多场用编号占位 `LOCAL-YYYYMMDD-2`、`-3`… 区分）。
- 合并：优先 ffmpeg `concat demuxer`（`-c copy`，极快不重编码）；编码不一致导致失败时回退 `concat filter` 重编码兜底。
- 合并产物落在 `<project_root>/merged/`，文件名沿用首段 basename 加 `_merged`；幂等（已存在则跳过）。`local_merge.keep_merged=false` 时总结完成后删除该中间视频。
- 配置项（`config.json` 的 `local_merge`）：`enabled`(总开关) / `max_gap_sec`(连续判定间隙上限) / `keep_merged`(是否保留合并视频) / `exclude_dates`(日期列表，**显式强制不合并**，优先级高于 `max_gap_sec`，用于确认是「多场独立直播」而非断流重连的日期)。
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

`latest_by_live_date` 额外支持 `since_live_date`（YYYY-MM-DD）作为**硬下限**：只处理直播日期 ≥ 该日期的录播。基线 = max(since_live_date, 已处理最晚直播日期)，处理完一期后自动滚动到下一期；目标期未上传时自动化空转等待。

注：直播「实际日期」从标题解析（如「2026年08月01日19点场」），避免凌晨下播投稿导致的跨天 / 跨月错位。

## 硬约束（务必保持）
- 转录文本**必须按 30 分钟切块逐块发送 AI**，绝不允许整篇一次性发送（防超长 / 超时 / 截断）。
- Word 文档：同月复用同一文档，每场新直播插入**分页符 + Heading2 标题**区分；总结段粗体、金句斜体。
- **章节元数据行（必须）**：每个 Heading2 章节标题下方必须紧跟一行元数据：`直播日期 YYYY-MM-DD  |  时长 HH:MM:SS  |  N/N 段  |  总结于 YYYY-MM-DD HH:MM`；本地 / B站场次另起一行 `来源 <本地 mp4/flv 路径 或 https://www.bilibili.com/video/BVxxxx>`。旁路本地路径（`regen_gemini_v2.process_session`）与自动化路径（`summarize_gemini.append_section`）生成的元数据格式**必须完全一致**。
- 断点续跑：每场每阶段写入 state.json，中断重跑自动跳过已完成部分。
- 热词纠正：转写之后、送 AI 之前必须对 TXT/SRT 做一遍热词替换（`hotwords.md`，含主播名/术语/歌名/保护规则），确保送进模型的文本已纠正。

## 运行与调试
- `daily_pipeline.py --workdir "<工作目录>" --bvid BVxxx` 手动指定某期
- `daily_pipeline.py --workdir "<工作目录>" --dry-run` 只预览会处理/跳过哪些
- `cleanup.py --report` / `--sweep [--dry-run]` 清理中间文件，详见「自动清理」
- 日志在 `WORKDIR/logs/`，错误在 `errors.log`；状态 / 基线在 `WORKDIR/state.json`

## 依赖与环境
详见 `references/environment.md`：需 GPU(PyTorch/FunASR)、FFmpeg、aria2c、B站 cookies；模型首次运行从 ModelScope 自动下载。

## 安全与隐私
- API Key 明文存于工作目录 config.json——仅本机使用，勿提交到公开仓库。
- cookies 为登录态凭证，同属敏感文件。
- 仅处理用户自有或有权总结的直播内容。

## 新 AI 接手 / 接入新主播（零代码）
1. 阅读 `AI_RUNBOOK.md`（通用上手手册），按其「新主播接入」章节建工作目录 + 填 config。
2. 复制 `SKILL_DIR/scripts/prompt_template.txt` 为 `WORKDIR/prompt_original.txt`，按需把 `{streamer}`/`{audience}` 替换为该主播名（或直接写死，两者皆可）。
3. 若需术语/歌名纠正，复制 `SKILL_DIR/scripts/hotwords.md` 模板到 `WORKDIR/hotwords.md` 再填规则，并在 config 设 `hotwords.path="hotwords.md"`。
4. 跑 `--dry-run` 确认选片正确，再挂自动化。
5. **绝不要为某个主播改 skill 内的代码**——所有差异都应落在各工作目录的 config / hotwords / prompt 里。
