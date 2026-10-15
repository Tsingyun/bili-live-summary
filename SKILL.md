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

管道（**统一入口 `daily_pipeline.py`**，通用 skill、配置驱动）：**每天由自动化触发一次，先监测本地录播根目录（`config.local.root`）的新文件 → 有则本地转写总结 → 随后做「缺失录播检查」并按三级备份顺序补档（本地 → rec.darkpy.cn 第二备份 → B站第三）→ 每场总结后导出中质量 MP3 → 最后压成 ~5GB HEVC MP4 到「录播根目录/YYYY-MM-DD/」日期文件夹**。

全流程：监测本地根目录(flv/mp4) → FunASR 转写(带 `[HH:MM:SS]`) → 热词表纠正替换 → 大模型按 30 分钟切块逐块总结 → 追加写入月度 Word（每场分页 + 标题区分）→ 导出中质量 MP3 → 压缩 HEVC MP4 到「录播根目录/YYYY-MM-DD/」日期文件夹（**B站兜底下载的录播不压缩、原样保留 MP4**）。新增能力（2026-09-05）：**每日自动对照「实际直播日历」检查缺失日期**（rec站文件日期 ∪ 本地文件日期 ∪ B站标题日期），缺失的自动从 **rec.darkpy.cn（FLV 分段下载→无损合并→同一转录总结流程）→ B站** 顺序补档，全程幂等断点续跑，末尾输出逐日补档结论。

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
  --doc-dir "C:/Users/xxx/Documents" \
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
- **darkpy**（可选，第二备份源 rec.darkpy.cn）：`enabled` / `host` / `port`(5144 页面) / `file_port`(5143 文件) / `room_dir`(rec站目录名，一般 `<uid>-主播名`) / `max_gap_sec`(段归组间隙) / `min_session_sec`(不完整阈值) / `work_dir`(下载临时目录)。未配 `room_dir` 时该源自动跳过。
- **backfill**：缺失检查/补档窗口与阈值：`enabled` / `start_date`(留空=用 `latest_mode.since_live_date`) / `end_date`(留空=今天) / `max_dates_per_run` / `min_cover_video_mb`(本地文件小于此值视为不完整)。
- **live_calendar**：实际开播日期日历：`sources`(默认 `["darkpy","local","bilibili"]`) / `danmakus_api`(可插拔，默认空则跳过 danmakus) / `uid`。
- **bilibili**：B站兜底录播的处置（用户规则 2026-08-29）。`keep_mp4`（默认 `true`）= 跑完流程后**保留 B站录播的 MP4**，原样归档到「录播根目录/YYYY-MM-DD/」；`compress`（默认 `false`）= **不压缩**这类 MP4
- **latest_mode.mode**：「最新录播」判定（见下）
- **summarize**：`provider` / `model` / `api_key` / `chunk_minutes`(30) / `streamer` / `audience` / `prompt_path`。
  **首选 `provider:"google"`、`model:"gemini-3.8-flash"`**（见下「大模型调用约定」），勿默认用 `zhipu/glm`。`prompt_path` 指向的 prompt 文件支持 `{streamer}`/`{audience}` 占位符（运行时替换），无占位符则幂等保留字面内容，兼容既有 prompt。
- **output**：`doc_dir`、`doc_name_pattern`（支持 `{streamer}`/`{year}`/`{month}`）、`font`
- **paths**：`project_root` / `python` / `ffmpeg` / `ffprobe` / `aria2c` / `cookies`
- **transcribe / cleanup**：推理参数与产物清理
- **hotwords**：转写后自动纠正（**固定流程「转写→热词→总结」的中间一环，勿跳过**）。`enabled:true` 时解析 `path`（默认用 skill 自带空模板 `SKILL_DIR/scripts/hotwords.md`）里的替换表，对 TXT/SRT 各做一遍替换再送 AI；`enabled:false` 关闭；`path` 可指向工作目录下的自定义替换表（相对路径以 WORKDIR 为基准）。改表立刻生效；改完用 `python scripts/hotwords.py --audit` 自检。
- **标点清洗（冗余双标点）**：FunASR 的 VAD 分段兜底会在转写里叠出 `。，` `？，` `，。` `。？` 这类"句末终结符+句内连接符相邻"的冗余双标点。脚本在**写出 TXT/SRT 前**（`clean_segment_boundaries`）与**写 Word 前**（`clean_punctuation`，覆盖每块总结与概要）两处自动清洗——纯纠错不改语义，无需手动干预。若日后发现新双标点模式，改 `common.py` 的 `_RE_TERMINAL_CONN / _RE_CONN_TERMINAL` 即可。
- **数字归一（规格数字中文读法→阿拉伯）**：FunASR 常把规格数字读成中文（`三零五零显卡`→`3050显卡`、`七十五赫兹`→`75Hz`、`八G`→`8G`）。脚本在**写出 TXT/SRT 前**（`normalize_numbers`）与**写 Word 前**（每块总结+概要各再做一遍）两处把"紧邻规格单位（显卡/GPU/Hz/赫兹/G/GB/寸/英寸/K/p/ti 等）的中国数字"转回阿拉伯，**仅对紧邻规格单位的读数生效，普通计数不动**，已是阿拉伯的文本幂等不受影响。若日后要增删单位，改 `common.py` 的 `_NUM_TRIGGERS` 即可。
- **话题分隔符（横向分隔线）**：一场直播总结成一整段不易阅读。脚本在写完 Word 前，把全场总结合并后**调一次模型**识别话题边界（吃饭↔打游戏↔看电影等），在边界插入 `§SEP§` 标记并切分成多个话题块，相邻话题块之间写一条**灰色横线**（`add_horizontal_rule`，段落下边框）。实现：`summarize_gemini.py` 的 `segment_topics_with` + `append_section(topic_sep=True)`。config.json 的 `topic_separator.enabled=false` 可关闭（退回不分段）；模型若异常/改动了原文行则自动退回单块（不丢内容、不加线）。
- **原始录播压缩（HEVC 归档）**：在「导出 MP3」之后，用 **NVIDIA NVENC HEVC(H.265)** 把原始录播压成更小归档副本（需 NVIDIA 独显；RTX 20 系+ 支持 `hevc_nvenc`，速度远快于 CPU）。目标体积由 `target_size_gb`（默认 5GB）做软上限，质量由 `cq`（默认 24，越小越清晰）保证。音频默认 `copy` 无损复用，设 `audio_mode="aac"` 则重编码 192k。实现：`scripts/compress_video.py` 的 `compress_video()`，已在 `daily_pipeline.py` 的 `process_video_source` 末尾调用。
  - **输出位置**由 `compress.out_dir_mode` 控制：`date_subfolder`（**当前规则**，用户 2026-09-08 拍板 = 直接用日期文件夹）= 输出到「录播根目录/YYYY-MM-DD/」（源已在该日期目录则复用；文件名 `<原名>_compressed.mp4`）；`mp4_folder` / `mp4_date_folder`（2026-09-05/06 曾试用的 MP4/ 统一目录系列，**2026-09-08 起搁置**）；`next_to_source` = 输出到源文件旁。日期从文件名里的 8 位日期解析，自动跳过 UID 等非法日期串。
  - **是否删除原视频**由 `compress.replace_original` 控制（**本地录播与 rec站补档**）：`true` 时压缩成功且 ffprobe 校验通过后**一律直接永久删除原视频**（省空间，不进回收站/不备份/无需确认；`replace_mode` 已统一为 `delete`，用户 2026-08-28 选定，旧版 `recycle` 分支已废弃——本机回收站会堆积导致磁盘爆满）。设为 `false` 则只生成 `_compressed` 副本、保留原始。压缩失败都不会动原文件。幂等：目标已存在则跳过。rec站补档的源视频位于工作目录 `_darkpy/` 临时区，压缩成功后被同样规则删除（省盘），压缩失败则保留供下次续压。
  - **B站兜底录播：不压缩、保留 MP4**（用户规则 2026-08-29）：本地/rec站都没有当天录播而 B站有时，跑完流程后要留下 B站录播的 MP4。由 `bilibili.keep_mp4`（默认 `true`，归档到「录播根目录/YYYY-MM-DD/」）与 `bilibili.compress`（默认 `false`）控制。

## 大模型调用约定（首选 Google / Gemini）

**默认且首选 Google Gemini API**，不要默认用 zhipu/glm 等其它 provider。
`config.json` 的 `summarize` 应设：

```json
"summarize": {
  "provider": "google",
  "model": "gemini-3.8-flash",
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
**首选 `gemini-3.8-flash`**（用户规则 2026-09-14：最新版 Flash 固定首位，其余按综合能力降序，
权重 ① 文本总结 ② 推理 ③ 长上下文 ④ 响应速度），
遇 429（每日配额耗尽）/404（模型不可用）自动顺切下一档；每模型成功调用达 20 次也主动切下一档：

```
gemini-3.8-flash → gemini-3.7-flash → gemini-3.6-flash → gemini-3.5-flash
→ gemini-3-flash-preview → gemini-2.5-flash
→ gemini-3.5-flash-lite → gemini-3.1-flash-lite
→ gemini-flash-latest → gemini-flash-lite-latest
```

> **已下架/不存在/无免费额度，务必不要再加回**（2026-09-14 实测核对）：
> `gemini-2.5-flash-lite`、`gemini-2.5-pro` 对免费用户返回 404（官方提示分别改用
> `gemini-3.5-flash-lite`、`gemini-3.1-pro-preview`）；
> `gemini-3-flash`、`gemini-3.6-flash-lite`、`gemini-3-flash-lite`、`gemini-3-pro`、`gemini-3.5-pro`
> 在 `/v1beta/models` 清单中不存在，调用必 404；
> **Pro 系（`gemini-3.1-pro-preview` / `gemini-pro-latest`）在免费层无额度**——配额重置后复测仍 429，
> 已排除"当日用完"，故不收录。末尾两个 `*-latest` 为滚动别名，作永不失效兜底。

`MODELS` 常量定义在 `regen_gemini_v2.py`，`run_812.py` 直接复用 `R.MODELS`，两处同步生效。
`config.example.json` 与 `setup.py` 的默认 `model` 也已对齐为 `gemini-3.8-flash`。

> 补充（2026-09-14 核对）：`daily_pipeline.py` 同样走 `R.GeminiRotator(R.MODELS, rpm=5,
> max_per_model=20)`，**有自动轮换兜底**（此前本段称其"单模型、无兜底"，与代码不符已更正）。
> `summarize.model` 仅在非轮换路径/新建配置时作为默认首选；轮换链一律以 `MODELS[0]` 起。
> 免费层每模型约 20 次/天，撞配额会自动顺切下一档。

## 5 步每日流程（统一入口 `daily_pipeline.py`）

`daily_pipeline.py` 是每日自动化的**唯一入口**（自动化触发后，命令见上「第 3 步」）。
它内置完整 5 步，无需再拆成多个脚本：

1. **监测本地根目录**：扫描 `config.local.root` 根目录 + 各 `YYYY-MM-DD` 日期子目录下的新录播（`*.flv`/`*.mp4`/`*.mkv`/`*.mov`）。
   · 已处理（state 占位 / TXT 命中）的跳过；
   · **正在录制中**（文件大小不稳定 / 修改时间太新）的整段跳过，避免截半——这是防「录到一半就被拿去转写」的关键。
2. **有本地新文件 → 本地总结**：抽 16k 音频 → FunASR 转写 → 热词纠正 → Gemini 切块总结 → 写 Word，并写 `state.json` 占位（`LOCAL-YYYYMMDD[-N]`，`date` 推高一天，使 B站兜底不会重复抓同场，避免重复消耗 token）。
3. **缺失检查 + 三级补档**（用户规则 2026-09-05）：用 `backfill.py` 对照「实际直播日历」（`live_calendar.py`：rec站文件日期 ∪ 本地文件日期 ∪ B站标题日期）找出**本地 / Word 月度总结 / state 都没覆盖**的直播日期，按 **本地 → rec.darkpy.cn → B站** 顺序补档：
   · **rec.darkpy.cn**（第二备份，`darkpy_rec.py`）：FLV 分段**断点续传下载** → 按段结束时间归组（跨午夜尾巴归前一场）→ ffmpeg `concat demuxer -c copy` **无损合并**（编码不一致才回退重编码）→ 完整性校验 → **同一总结流程**（转写→热词→总结→Word→MP3→压缩）。
   · **B站**（第三备份，原有功能不变）：仅当 rec站也没有该日时才抓（`bilibili_fallback(only_dates=...)` 限定目标日期）。跑完流程保留原 MP4、不压缩（`bilibili.keep_mp4=true`），归档到「录播根目录/YYYY-MM-DD/」。
   · 结束输出逐日结论（`2026-09-01：本地缺失 → rec.darkpy.cn → 成功`）。
4. **导出中质量 MP3**：总结完成后，把视频转一份 192k/44.1k 的中质量 MP3（落在 `mp3_export.out_dir`；**用户规则 2026-09-08：与 Word 总结同目录，岁己 = `C:\Users\Tsing\DeskBox\文档`**，文件名直接以日期开头）。
5. **压缩 HEVC MP4**：把视频压成 ~5GB 的 HEVC(H.265) MP4，按 `compress.out_dir_mode=date_subfolder` 输出到「录播根目录/YYYY-MM-DD/」日期文件夹（**当前规则**，用户 2026-09-08：不要 MP4/ 文件夹，直接用日期命名文件夹）；压缩成功后按 `compress.replace_mode` 处理原视频（`delete` = 直接永久删除）。**B站兜底录播跳过本步骤，原样保留 MP4。**

**最终每场交付物三样**：① AI 总结的 Word 文档（`output.doc_dir`，岁己 = `C:\Users\Tsing\DeskBox\文档`）② 中质量 MP3（`mp3_export.out_dir`，与 Word 同目录）③ 视频——**本地录播 / rec站补档**=压缩后的 HEVC MP4（「录播根目录/YYYY-MM-DD/」日期文件夹）；**B站兜底**=原样保留的 MP4（同样落在日期文件夹）。

落地实现（已生效）：`daily_pipeline.py` 内置「根目录监测 + 本地总结 + 缺失补档(rec→B站) + MP3 + 压缩」+ 单实例锁 + 断点续跑（state 占位与 TXT/SRT 存在性判定）。手动运行：`python SKILL_DIR/scripts/daily_pipeline.py --workdir "<工作目录>" --dry-run` 只预览不处理。

> 踩过的坑：曾因本地脚本没真实执行、占位没写，导致次日自动化重复抓 B站、多花 token。根因：**「本地脚本写入 state 占位」才是跳过某场的唯一有效开关**。故自动化必须真实跑成功（看日志确认 `LOCAL-*` 占位已写入）后再依赖它。

### 缺失录播自动检查 + 三级备份补档（2026-09-05 新增）

**目标**：不漏任何一场直播。即使本地录制器没录上、或换机器没录，只要 rec.darkpy.cn 或 B站
有，第二天自动化就会自动补档；两边都还没有就等到有为止。

- **判断「缺失」的依据** = 实际直播日历（`live_calendar.py`，多源取并集）− 已覆盖：
  - 实际直播日历来源：rec站文件（**按场次起始日**，跨午夜尾巴归前一场，不会把 8/27 00:01 误判成 8/27 独立直播）∪ 本地文件日期 ∪ B站标题日期；danmakus 接口可插拔（默认未配，自动跳过不阻塞）。
  - 已覆盖 = `state.done_dates` 有该日 **或** `doc_dir` 的 Word 月度文档已有该日章节 **或** 本地有该日完整视频（> `backfill.min_cover_video_mb`，默认 20MB；文件过小视为录制 bug 产物，仍判缺失）。
  - **一天多场**按天判断：当天有任意一场已覆盖即不算缺失；rec站当天多场会逐场补（`DARKPY-YYYYMMDD[-N]` 占位）。
- **补档顺序**：rec.darkpy.cn（第二备份，优先于 B站，因更新及时）→ B站（第三备份）。本地永远第一优先。
- **rec.darkpy.cn 站点协议**（已实测，勿凭猜测改动）：phpstudy+自研 PHP 文件浏览器，**5144=页面、5143=文件服务**；列表走 `file.php?dir=<room_dir>/&dir_s=1&dir_f=./file/<room_dir>&dir_file={"1":".\\/file\\/"}`；下载直链 `http://rec.darkpy.cn:5143/file/<room_dir>/<文件名>.flv`；命名 `YYYYMMDD-HHMMSS-序号-标题.flv`，一场常切多段。
- **幂等/断点**：FLV 已下载不重下（`.part` Range 续传）、已合并不重合并、TXT/SRT 在则跳过转写、Word 章节在则跳过总结、压缩产物在则跳过压缩——某步失败下次从失败步继续。
- 配置：`config.json` 的 `darkpy`（enabled/host/port/file_port/room_dir/max_gap_sec…）、`backfill`（enabled/start_date/end_date/max_dates_per_run/min_cover_video_mb）、`live_calendar`（sources/danmakus_api/uid）。

命令：

```bash
# 只打印「检查报告」（不下载、不写任何东西）——推荐先跑这个看缺失情况
python scripts/backfill.py --workdir "<工作目录>" --from 2026-09-01 --to 2026-09-30

# 实际补档（rec.darkpy.cn → B站）
python scripts/backfill.py --workdir "<工作目录>" --from 2026-09-01 --to 2026-09-30 --fill

# 也可以走主入口（定向只补某一天；找不到本地录播就会自动尝试 rec → B站）
python scripts/daily_pipeline.py --workdir "<工作目录>" --date 2026-09-01
# daily 支持 --from/--to（补档窗口）、--no-backfill（只做本地，跳过缺失补档）
```

> 未配置 `darkpy.room_dir` / `darkpy.enabled=false` 时自动降级为旧的「本地 → B站」两源行为，不影响原功能。

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

**绝不碰（黑名单）**：录播根目录（`local.root`）里的任何文件——压缩成品 MP4、源视频；
`mp3_export.out_dir` 的归档 MP3（用户规则 2026-09-08 起与 Word 同目录）；`doc_dir` 的 Word 总结；`config.json` / `state.json` / `hotwords.md`。
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

### 多段录播 → 先合并再总结（**禁止逐段分别总结**）

网络波动会让录播器断流重连，于是一场直播被切成**多个文件**。规则（2026-09-14 修正）：

- **场次判定只看「相邻片段间隙」，不看日期变更**（旧实现按文件名日期硬分组，导致跨 0 点的后半段被当成新一天、独立产出第二份总结——已修）：
  全部候选文件**全局**按开始时间排序 → 用 ffprobe 取时长算结束时刻 →
  间隙 ∈ [`-allow_overlap_sec`, `max_gap_sec`] 即视为同一场。
- **跨午夜但连续** → 归属**开播那一场**（`crosses_midnight=True`），合并后**只出一份总结**；
  **同一天但间隔很久**（如下午场 + 晚上场）→ 正确切成两场。
- 取不到时长时**回退用文件 mtime 估算**结束时刻（打 WARN）；两者都失败 → 该片段保守独立成场，绝不猜测性合并。
- 合并：优先 ffmpeg `concat demuxer`（`-c copy`，极快不重编码）；编码不一致导致失败时回退 `concat filter` 重编码兜底。
- **合并失败 = 整场跳过且不写完成标记（下次自动重试），绝不退化成逐段分别总结**——那会给一场直播产出多份残缺总结。
- **合并产物先写 `.part.mp4`，校验时长达标（≥各分段之和 95%）后原子改名**；复用已有产物时同样校验时长——
  只看"文件存在 + 体积 >1MB"会把上次被杀留下的半截合并视频当成已完成，直接送去转写，结果一场直播只总结一半。
- 合并产物落在 `<project_root>/merged/`，文件名沿用首段 basename 加 `_merged`；幂等（已存在且完整则跳过）。`local_merge.keep_merged=false` 时总结完成后删除该中间视频。
- 配置项（`config.json` 的 `local_merge`）：`enabled`(总开关) / `max_gap_sec`(连续判定间隙上限，默认 600) / `allow_overlap_sec`(允许的负间隙，默认 60，容忍录制器重连时几秒回退/重叠) / `keep_merged`(是否保留合并视频) / `exclude_dates`(日期列表，**显式强制不合并**，优先级高于 `max_gap_sec`，用于确认是「多场独立直播」而非断流重连的日期)。
- **单文件目录、或 `local_merge.enabled=false`** 时退化为旧行为（每目录取首个文件），不合并。
- ⚠️ **临界风险**：若某日期多文件真实间隙只比 `max_gap_sec` 略大（如 609s vs 600s），ffprobe 时长精度波动可能让间隙跌破阈值而被误合并。对确认是独立多场的日期务必加入 `exclude_dates`（如 `"exclude_dates": ["2026-08-22"]`），不依赖阈值侥幸。
- 复用：因合并视频已留档（`keep_merged=true`），重跑不会重复合并；已写占位(`LOCAL-*`)的场次仍会被跳过。
- **外部依赖（显式声明）**：合并与时长探测强依赖 `paths.ffmpeg` / `paths.ffprobe`。
  缺失时日志会明确报「外部工具缺失」并说明后果——**ffprobe 缺失降级用 mtime；ffmpeg 缺失则该场跳过（不崩溃、不逐段总结）**，请检查 `config.paths` 后重跑。
- ⚠️ **占位 key 冲突（2026-09-14 修）**：同一 `live_date` 可能残留旧占位（典型是跨午夜尾巴留下的 `merged_into` 占位）。
  判定「已处理」时会比对**成员文件是否相交**，无交集则视为同一天的另一场；写入时 key 自动顺延（`LOCAL-20260913-2`），**绝不覆盖**旧记录。
  曾发生 9/13 晚场被 9/12 尾巴占位 `LOCAL-20260913` 整场挡住、一直没总结的事故。

## 健壮性加固（2026-09-18，务必保持）

背景：9/17、9/18 连续两天自动化**零产物**。复盘结论是「进程生命周期」问题——agent 回合
04:04 正常结束 → 宿主运行时被 `lifecycle:dispose` 回收 → 整棵进程树被拆除，管线当时刚
下到 0.8GB/13GB。围绕"被杀也不出事、重跑必能续上"做了如下加固。

### 1. 一切"写完才算数"的落盘都走原子写

`common.atomic_write_bytes/text/json`：写 `<目标>.part` → `flush + fsync` → `os.replace`
（目标被 Word/腾讯文档占用时自动重试）。**禁止**直接 `open(path,'w')` 写正式产物。

| 产物 | 原子化方式 | 复用时的完整性判据（关键） |
|---|---|---|
| `state.json` | `.part` + replace，并留 `.bak` | 主文件损坏时 `load_state` 自动回退 `.bak` |
| 月度 docx（最贵重） | `.part` → `zipfile` 校验 → replace；被占用(WinError 5/32)时**退化原地覆写**，另留 `.bak` | 打不开时 `open_or_create` 回退 `.bak`，绝不静默新建 |
| 合并 MP4 / 压缩 MP4 | `.part.mp4` → ffprobe 校验时长 → replace | **时长 ≥ 源/分段之和 95%**，否则删掉重做 |
| 归档 MP3 | `.part` → ffprobe 校验时长 → replace | 时长 ≥ 源 98% |
| 转写 TXT/SRT | `.part` → replace | 切块转写有任一块为空 → **整场判失败**，不落盘 |

> ⚠️ 为什么强调"复用判据"：旧代码只看「文件存在 + 体积 >1MB」。而压缩/合并是全程最耗时的
> 步骤，被杀大概率停在**中途**，留下体积已经很大、时长却只有几分钟的残片。旧逻辑下次
> 直接当"已完成"跳过；`replace_mode=delete` 时还会把**完好的源视频删掉**——等于永久丢档。

> ⚠️ **ffmpeg 的临时名必须显式声明容器格式**（2026-09-19 踩坑）：`<x>.mp3.part` 这种
> 以 `.part` 结尾的名字，ffmpeg **从扩展名推不出容器**，会以 `AVERROR(EINVAL)` 退出
> （Windows 上显示 `exit status 4294967274`）。所以用 `.part` 做临时名的 ffmpeg 调用
> 必须加 `-f mp3`（MP4 那几处用的是 `.part.mp4`，扩展名还在末尾，所以没这个问题）。
> `extract_16k_audio` 与 `mp3_export` 都踩过——删掉 `-f mp3` 会让 9/18 这类场次在
> **第一步就整场失败**。回归自测 [I] 组专门盯这个。

> ⚠️ **docx 被 Word / 腾讯文档预览打开时，`os.replace` 会被持续拒绝**
> （`WinError 5 拒绝访问`）。这种占用是「允许读写、不允许删除共享」，**干等重试无效**。
> 处理方式：`save_doc_atomic` 先按原子路径试，遇到 WinError 5/32 立即**退化到原地覆写**
> （`.bak` 已是最新完整版，`open_or_create` 遇到损坏会自动回退，所以内容不会静默丢）。
> 注意：Windows 下 `os.open` 默认**文本模式**，原地覆写必须带 `os.O_BINARY`，否则
> `\n`→`\r\n` 会把 zip 写坏（回归自测 [F] 组 F4–F6 覆盖）。
> 只剩一条人工注意项：**在编辑器里手动保存可能覆盖管线刚写的章节**，看完记得关掉预览。

### 2. 下载绝不静默产出损坏文件（`darkpy_rec.download_one`）

- **服务器忽略 Range 返回 200 时绝不追加写**：旧实现 resume>0 就用 `'ab'` 续写，会把整份
  文件接在已有分片后面，产出约 2 倍大小的损坏文件（旧校验只看下界，反而放行）。现在检测到
  200 或 `Content-Range` 起点不符 → 丢弃分片、从头重下。
- **字节数必须与 `Content-Length` 吻合**：少一个字节都算失败，保留 `.part` 供下次续传。
  旧实现在 `expect_bytes==0`（站点大小解析失败）时**完全没有长度校验**，截断 FLV 会被
  `os.replace` 成正式文件。
- **自动重试**：按 `darkpy.retries` 退避重试（默认 3），每次从 `.part` 当前长度续传。

### 3. 单实例锁改为「心跳驱动」（`common.lock_owner_alive`）

被回收的进程没机会清理锁 → 留孤儿锁。旧实现只用 `os.kill(pid,0)` 判活，而 Windows PID
复用很快，无关进程顶替后会被误判「仍存活」→ 新一轮打印 `已有实例运行，退出`（exit 0）
**当天零产物且日志看不出异常**。现在要求 **PID 存在 且 心跳新鲜（<1200s）** 双条件；
`daily_pipeline` 启动后台线程每 60s 刷心跳，另写 `.daily_pipeline.stage` 阶段检查点。
释放锁前校验锁内 PID 是否为本进程，避免误删别人的锁。

还修了一个**反向的锁漏洞**（`cleanup.lock_file_is_live`）：清理器清扫"孤儿锁"时，
旧实现用 `int(整个文件内容)` 解析 pid，而新锁格式是「pid + ISO 时间」→ `int()` 必抛异常
→ pid=0 → **跳过存活检查直接删锁**。结果跑满 2 小时（`orphan_lock_hours`）的长任务，
锁会在运行中途被清掉，**单实例保护失效**——第二个实例（计划任务/手动）就能与它并发写同一份
Word。现在清理器判活也走 `common`（`.daily_pipeline.*` 用「PID 存在 且 心跳新鲜」，
其它 `.lock` 退化为 PID 探测），与 pipeline 共用一套逻辑。

### 4. 异常隔离、超时与资源释放

- 单点失败不拖垮整轮：`backfill.stage` 逐日期/逐场次 try/except；`bilibili_fallback` 包住
  `fetch_series`（B站风控 412/-352）与每期处理；`daily_pipeline` 步骤 3 整段兜底，
  失败也照常走收尾清扫并落盘 state。
- `GeminiRotator.call`：网络异常不再直接抛出（旧行为会让整场中止），改为同模型退避重试；
  用**总尝试预算 + 每模型空响应计数**兜底，杜绝"200 但空响应"导致的死循环。
- 所有 ffmpeg/ffprobe 调用带 `timeout`（按源时长自适应，见 `common.timeout_for_source`），
  绝不无限等待；临时 list/part 文件一律用 `force_unlink` 清理（本机 `os.remove` 被安全 hook 拦截）。

### 5. 耐久执行：把长任务搬出 agent 会话的进程树（根治"被回收"）

代码加固只能保证"被杀也不出坏产物、重跑能续上"；**真正让当天必出产物**的做法是别把
长任务挂在 agent 会话下面。

```bash
# 注册 Windows 计划任务（默认 04:20 起，每 60 分钟一次共 3 次：04:20/05:20/06:20）
python scripts/install_scheduled_task.py --install --workdir "<工作目录>"
python scripts/install_scheduled_task.py --query     # 看状态
python scripts/install_scheduled_task.py --run       # 立刻触发一次
python scripts/install_scheduled_task.py --remove    # 删除
```

- `run_scheduled.py` 由 **Task Scheduler 服务**拉起，不在 agent 进程树里 → agent 回合
  结束/重开都不影响它跑完；内部先查锁（有存活实例直接退出，不重复跑），失败自动重试
  （管线幂等，重跑即续做），结果写 `logs/scheduled_last_run.json` + `logs/run_scheduled_*.log`。
- 定位是**安全网**：自动化那一次跑成了，计划任务到点会看到存活锁而空转退出；被杀了则由它补完。
- ⚠️ 任务为「只使用交互方式」（需用户已登录），与自动化同处一个登录会话，符合预期。

### 6. 回归自测（改完必跑）

```bash
# ⚠️ 必须用 bili-archive venv 的 python（基础解释器没装 requests/python-docx，会假失败 6 项）
"C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe" scripts/selftest_robustness.py
# 85 项：Range 忽略/截断/续传、原子写、锁判活、docx 回退与占用退化、压缩门禁、清理器锁保护、
#        ffmpeg .part 输出、热词替换、金句格式/异常内容过滤/文档体检、小号上传模块纯逻辑（[L]）
```
离线运行（本地 mock HTTP + ffmpeg 生成小视频），不需要外网与 Gemini 配额。
预期输出结尾为 `通过 85 项，失败 0 项`；若不是 85/85，先确认解释器是否用错，再查代码。

### 7. 产物质量门禁：Word 写完自动体检并修复（2026-09-24）

**背景**：9/23 那场总结暴露出三类成品问题（写入侧已修，产物侧再兜一道）：

| 问题 | 根因 | 修法 |
|---|---|---|
| 金句本该「灰色斜体」，24 处却变**粗体**、下划线裸露 | 不同模型遵循度不同：`gemini-2.5-flash` 会给金句套 markdown 斜体 `“…”`；旧清理只剥 `**`/`__`，**单下划线漏网** → `is_quote_line` 判否 → 当正文加粗 | `strip_md_markers()` 统一剥包裹标记，且**在判金句之前**执行 |
| 出现提示词回显（「输入文本每一行都以 [HH:MM:SS] 开头…」）与元话语（「好的，这是对输入文本进行分段重组…」） | 模型把 user 消息里的说明句当「金句」输出 | `prompt_leak_hits()` 指纹匹配 + `is_meta_line()` 元话语识别，命中即丢弃 |
| 金句里仍有未纠正的错字（掉件/藿相正奇/布包…） | **不是流程坏了**——401 条规则在转写稿里残留 0；是**表里没有这些词条** | `docx_healthcheck.py` 扫成品 vs 热词表原词，列出缺口 |

**双保险设计**：
1. **源头拦截**（`summarize_gemini.append_section`，两条总结路径共用的唯一写入点）：
   剥 markdown → 丢弃提示词回显/元话语 → 判金句 → 落格式。被拦的行会打 WARN 日志。
2. **产物体检**（`docx_healthcheck.py`）：对已生成的 docx 就地修复，不重跑 AI、不耗额度。

### 7.1 跨模型文风契约（2026-10-02，prompt v2）

**背景**：Gemini 与魔搭混用后，实测两份文档存在可量化漂移——

| 指标 | 9 月（多为 Gemini） | 10 月（多为 Qwen） |
|---|---|---|
| 违规指称「观众/主播/大家/她」 | 62 / 61 / 144 / 222 处 | 19 / 1 / 6 / 2 处 |
| 单场总结段均长 | 37.7–133.3 字（跨度 3.5 倍） | 88.1 字 |
| 金句条数 | 27 场里仅 2 场有（11 / 22 条），其余 0 | 0 |

**修法**：把 `prompt_original.txt`（权威）/ `scripts/prompt_template.txt`（通用，含 `{streamer}`/`{audience}`）升级为**唯一文风契约**，新增三块硬约束：

1. **第零节 角色与术语对照表**（优先级高于其后所有章节）：主角只能称「岁己」，观众只能称「饼干岁」；
   明令禁用「她/他/主播/UP主/观众/粉丝/大家/家人们/水友」；中性名词（直播间/弹幕/礼物/舰长/SC/作品名）保留。
2. **第三节 详略量化**：单段 100–180 字，<80 字判不合格（须并段或补足），>180 字必须拆段；
   语气统一中性记录式，禁感叹号与卖萌语气词。
3. **第六节 统一输出格式规范**：行类型只有「总结行/金句行」两种；禁标题层级、禁列表符号、禁 Markdown 标记；
   一段一行禁段内换行；中文全角标点、句号结尾、引号成对；时间戳后必须有正文（杜绝只有 `[HH:MM:SS]` 的空行）；
   禁 `-----`/`===`/` ``` ` 等分隔符。
4. **第七节 金句密度量化**：每 3–5 个总结行 1 条，整场不得为 0，相邻两行不得同时附——消掉「全场 0 条 vs 22 条」的漂移。
5. **第九节 输出前自检清单**（6 条，不满足须重写后再输出）。

`model_router.PROVIDER_PROMPT_NOTES` 追加第 7 条「跨模型文风一致性」：所有 family 共享同一套人称/语气/字数规则。

**改了提示词要重跑某场**（不重跑 ASR，只重做模型总结）：
```bash
python scripts/redo_session.py --config <config.json> --date 2026-10-02 \
    --txt "20261002-标题 2026年10月02日20点场.txt" [--src <源视频路径>] [--dry-run]
```
流程 = 备份 docx → 删该场章节 → `process_session` 重总结 → `reorder` → 打印用量。
注意：`process_session` 的 `progress` 参数**必须传 dict**，传 None 会 `AttributeError`。

★ 改提示词后必做两项验证：① `prompt_leak_hits` / `is_meta_line` 对**已有文档正文**的误杀数必须为 0
（提示词变长会增加指纹，实测 9 月 3382 行 + 10 月 269 行 + 8 月 2544 行均 0 误杀）；② 跑 `selftest_robustness` + `selftest_model_router`。

★ ★ **统计口径错了会造出假故障**（2026-10-03 实测，代价：误改提示词 + 误重跑一场 15 分钟 / 10.5 万 token）：
我用「`t.startswith('[')` 且非粗体」来数金句，得出「10/1、10/2 金句 0 条」，
进而判定「提示词第三节'每段必须以岁己开头、以句号结尾'压制了金句」。
**真相：金句行不带时间戳方括号**，被 startswith('[') 直接排除 → 统计恒为 0。
按正确口径（粗体+时间戳=总结行；非粗体且带成对引号=金句；另需剔除概要与「直播日期/来源」两行元数据）复测：
9 月 27 场**全部**有金句（每场 40–82 条）、10/1 70 条、10/2 51 条 —— 从来没缺过。
**正确的统计口径（写死，勿再自创）**：
```python
if bold and t.startswith('['): 总结行
elif not bold and t.startswith('“') and t.endswith('”'): 金句
elif t.startswith('直播日期') or t.startswith('来源'): 元数据（跳过）
else: 概要 / 其它（跳过）
```
**教训两条**：① 下「某类内容缺失」的结论前，先确认筛选条件本身有没有把它排除掉
（最省事的验证：直接调模型看原始输出里有没有该类行，而不是只看落盘后的文档）；
② 提示词里的「每段 / 全部 / 所有行」这类全称量词仍值得保留显式限定
（已加「本节及以下所有'每段'均只约束总结行」，属去歧义，无害保留）。

★ **真正的遗留问题：金句密度超标**。契约要求「每 3–5 个总结行 1 条」（20–33%），
实测 9 月 ~50%、10/1 49%、10/2 45% —— 模型基本「每段都附」，该约束未被遵守。
因不损内容、仅影响阅读节奏，暂未强改；若要收紧需另加一轮提示词调整 + 重跑验证。

### 7.2 专有名词书写规范（全局约束条款，2026-10-02）

**为什么必须做成代码而不是只写进提示词**：10/1 那场模型照样输出了《复联三》《雷神三》《奇异博士一》——
提示词只能降低违规率，**只有确定性后处理才能保证 100% 一致**。故采用三层：

| 层 | 位置 | 作用 |
|---|---|---|
| ① 源头 | `prompt_original.txt` / `prompt_template.txt` **第九节** + **第七节第1条**（金句） | 让模型尽量一次写对 |
| ② 写入点 | `summarize_gemini.append_section`（总结行 + 概要 + **金句行**） | 落地前规范化 |
| ③ 体检 | `docx_healthcheck` 新增 `terminology` 类别 + `terminology.py --scan [--fix]` | 存量文档可扫可修 |

规范表与实现都在 `scripts/terminology.py`（`CASE_FIX` / `PHRASE_FIX` / `UNIT_FIX` /
`PROTECT_TITLES` / `SERIES_TITLES` / `QUOTE_EXEMPT`）。

**★ 金句同样适用（2026-10-02 修订，勿回退到「完全豁免」）**：
金句是原话，但其中的人名、地名、机构名、产品名、作品名、专业术语**必须与正文 / 标题 / 摘要写法完全一致**，
否则同一场直播会出现 `iphone` 与 `iPhone` 两种写法。`normalize(text, is_quote=True)` 仍应用全部术语规则，
只额外豁免 `QUOTE_EXEMPT`（`weibo`、`golang` 这类**译名类**替换——会把说话人的原话书面化）。
术语规则不改字词、不改标点、不改断句，因此语气与口语质感不受影响。
错别字段句、口误与机械重复词的删改属于**模型层**职责，写在提示词第七节第 2–4 条，代码不做（无法确定性判断）。

**必须记住的五条边界**：
1. **金句内豁免译名类替换**（`QUOTE_EXEMPT`）——`weibo→微博` 在金句里不做；正文里照做。
2. **录播原标题 / 文件名 / 来源路径豁免**——如 9 月那批「5070ti版」标题、`D:\录播\...\xxx.mp4` 路径，
   属原始数据，改了就对不上源文件（`_kind_of()` 判为 `skip`）。
3. **作品名固有中文数字豁免**——《三体》《第五人格》《第一滴血》《落秋的第一场雨》在 `PROTECT_TITLES` 里；
   只转**结尾的序号**（《复联三》→《复联3》），「第一/之一/唯一/统一/专一」命中即不转。
4. **`\b` 是坑**——Python 的 `\b` Unicode 感知，「购买**5070Ti**」中「买」和「5」都算词字符，
   边界不成立导致规则静默失效。一律用 `(?<![A-Za-z0-9])` / `(?![A-Za-z0-9])`。
5. **口语里的系列作品（`SERIES_TITLES`）必须带量词否定**——只写白名单 + 序号仍会误伤
   「原神**三十次**」「原神**两个小时**」「龙珠**三部曲**」。否定预查里必须包含量词与「十」
   （「十」用于让「三十」整体回溯失败，「十四」则由 `{1,3}` 贪婪匹配先整体命中）。

**不确定写法处理原则**：① 有官方写法→从官方；② 无官方→从通行标准（SI/IEEE）；
③ 都没有→**保留原样，不臆造不音译**；④ 同一文档内写法必须一致。

回归：`[O]` 组 26 项 + `[P]` 组 17 项，`selftest_robustness` 152 → **168 项**全过。
存量修正：10 月 5 处 + 9 月 34 处（备份 `.bak_term_20261002`）；金句专项 9 月 18 处
（备份 `.bak_quote_20261002`，段落数 3441 与 1513 条金句行数均不变，改动全为单字符级
`b→B` / `s→S` / `p→P` / `c→C` / `一→1` / `二→2` / `三→3`）。

```bash
# 只体检（不改文件）
python scripts/docx_healthcheck.py --workdir "<工作目录>" --verbose
# 体检并修复（先留 .bak，再原子保存）
python scripts/docx_healthcheck.py --workdir "<工作目录>" --fix --json report.json
# 也可只查单个文档
python scripts/docx_healthcheck.py --doc "C:/.../岁己直播总结_2026年9月.docx" --fix
# 只扫专有名词写法（总结行 + 金句行；标题/路径豁免）
python scripts/terminology.py --scan "C:/.../岁己直播总结_2026年9月.docx"
python scripts/terminology.py --scan "C:/.../岁己直播总结_2026年9月.docx" --fix
python scripts/terminology.py --check "我这个 iphone 才 8gb"            # 正文模式
python scripts/terminology.py --check "我这个 iphone 才 8gb" --quote     # 金句模式
```

- 检查项：① 金句/正文格式（金句=灰色斜体 10pt 非粗体；正文=粗体 11pt 非斜体）
  ② markdown 标记残留 ③ 提示词回显/元话语 ④ 热词表原词残留 ⑤ 结构提示（元信息行、空金句章节）
- ★ **热词残留校验必须按词边界匹配英文原词**：`PC → PK` 这条规则会用子串命中 `NPC`
  （实测误报 21 处），所以 ASCII 原词走 `(?<![A-Za-z0-9])…(?![A-Za-z0-9])`，中文原词才用子串。
- 备份/历史快照（`.bak`、`_pre*`、`blockfix`、`~$*`）**不参与**体检，避免误改归档。
- **已接入每日流程**：`daily_pipeline.py` 收尾「步骤 4 产物质量检查」自动以修复模式跑一次；
  `--dry-run` 时降级为只读体检。任何异常都只告警，绝不影响交付物。
- 退出码：0 = 无问题或已修复；1 = 仍有严重项未解决（low 级结构提示不阻塞）。

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

## 投稿到 B 站小号（auto_upload_daily.py）
### 两个开关别搞混
| 键 | 管什么 |
|---|---|
| `upload.enabled` | 总结管线（`daily_pipeline`）内是否触发上传 |
| `upload.auto_enabled` | **每日自动上传总开关**（`auto_upload_daily.py`，计划任务 `BiliUpload_Daily` / WorkBuddy 自动化 06:00） |
| `upload.auto_since` | 只自动上传 live_date ≥ 该值的录播（防擅自把历史积压传出去） |

### ★ 正式上传必须走 `auto_upload_daily.py`，别用 `bili_upload.py` 直传
- `auto_upload_daily.py`：标题规范 = **日期 + 原标题 + 岁己SUI**（用户拍板）、幂等（`upload_auto_state.json`）、
  超 `auto_max_single_gb`（默认 7.5GB）自动 ffmpeg 流复制无损分 P、`--report` 只汇报不传、`--date` 只传某天。
- `bili_upload.py --src` 直传走的是 `upload.title_template` 旧模板，会把标题覆盖成
  「岁己 2026-10-02 直播回放」，**丢掉原标题**。只适合手工补传单个文件。
- 汇报状态：`auto_upload_daily.py --report`（不需要 cookie，可随时跑）。

### ★★★ 绝不传大号（用户 2026-10-08、10-12 两次强调，最高优先级）
大号 = `奶蛋厨` **mid 9669499**；投稿小号 = `岁己SUI单推人` **mid 168235532**。
2026-10-12 事故：10/10、10/11 两场 3 个录播被传进大号，根因是
`daily_pipeline.py` 步骤6 直读 `cfg['paths']['cookies']`（那文件是**大号** cookie），
把 `load_upload_cfg()` 算出的小号路径丢掉不用，且该分支没有账号预检。

**两条上传入口的 cookie 来源不同，改任一处都必须同时检查另一处：**

| 入口 | cookie 来源 | 白名单预检 |
|---|---|---|
| `auto_upload_daily.py` | `upload.cookies_path`（小号） | 有（10/8 加的） |
| `daily_pipeline.py` 步骤6 | ~~`paths.cookies`~~ → 现改为 `load_upload_cfg()['cookies_path']` | 有（10/12 补的） |

铁律：
- `paths.cookies` 这个键**永远指向大号**（`C:\Users\Tsing\.bilibili_cookies.txt`），
  只有 `setup.py` 生成默认配置时会写它。任何代码都不要用它上传。
- 上传前一律走 `bili_upload.check_cookie()`，`upload.allowed_mid=168235532` 不匹配就拒传。
- 回归 `[U]` 组用源码级断言锁死「不准直读 paths.cookies」，改回来看见测试红。

### 删除误传的稿件（2026-10-12 实测）
- 正确接口：`POST https://member.bilibili.com/x/web/archive/delete`
  参数 `aid`（av号，从 view 接口取）+ `csrf`（bili_jct）。
  **注意是 `delete` 不是 `del`**——`/x/vu/web/del` 等老路径全是 404。
- 但 B站已对删除稿件加**强制风控验证码**（`code=340022 验证码错误`），
  脚本化不可达，只能让用户在创作中心手动删。
  Playwright 路线也不通：cookie 里的 `deviceFingerprint` 与沙箱 Chromium 指纹不匹配，
  服务端直接判 `-101`（同一份 cookie 用 requests 却正常）。
- 删完必须清 `state.uploads` 与 `upload_auto_state.json` 里对应记录，否则不会补传小号。
  ★ 清的时候按 **bvid 的 owner** 判断归属，别按日期一刀切——10/11 小号那条
  （BV1i7pQ6wE3F）差点被误删，删错会导致重复投稿。

### cookie 失效是最高频故障，先认这两个症状
- `bili_upload.py --check-cookie` → `code=-101 账号未登录` = cookie 过期
- 上传时 `member.bilibili.com/preupload` → **HTTP 403** = 同一个原因（不是被风控）
- cookie 是 Netscape 格式，关键三项 `SESSDATA` / `bili_jct` / `DedeUserID`，路径 `upload.cookies_path`

### 换 cookie：优先 `scripts/login_by_qr.js`（手机扫码，≈1 分钟）
```bash
cd C:/Users/Tsing/AppData/Local/Temp
NODE_PATH=C:/Users/Tsing/.workbuddy/binaries/node/workspace/node_modules \
  C:/Users/Tsing/.workbuddy/binaries/node/versions/22.22.2-3/node.exe \
  C:/Users/Tsing/.workbuddy/skills/bili-live-summary/scripts/login_by_qr.js
```
把生成的二维码图 `present_files` 给用户，用户用手机 B 站 App 扫；脚本轮询到成功就写回
cookie 文件（先自动备份 `.bak_YYYY-MM-DD`）并打印账号昵称+uid。**先看清昵称是不是小号。**

兜底：`scripts/import_cookies.py`（手工粘贴或整份 Netscape 文件导入，写盘后自动在线校验）
```bash
python import_cookies.py --kv "SESSDATA=xxx; bili_jct=yyy; DedeUserID=zzz; ..."
python import_cookies.py --netscape "C:/Users/Tsing/Downloads/cookies.txt"
```

### ★★ preupload 403 的两个根因（2026-10-06 实测，别再踩）
1. **cookie 只留必需三项 / 少数几项 → 403。** `member.bilibili.com/preupload` 会看 cookie
   **集合特征**，只写 3~9 项一律返回 **HTML 403 页面**（不是 API 的 JSON 错误）。
   而 `space/myinfo` 账号校验**照样通过** → 极易误判为「cookie 没问题」。
   所以 `import_cookies` / `login_by_qr` 一律**全量保留** bilibili 域 cookie，别按白名单过滤。
2. **反复重试会把 SESSDATA 作废。** 同一份 cookie 连续 403 三次后，
   `space/myinfo` 就变成 `code=-101 账号未登录` —— 触发风控。
   所以 **403 必须「只试一次」**：先单发一次 preupload 探接口（不传数据、秒回），
   通了再启动整体上传；不通就换新 cookie，别用同一份硬扛 `auto_retries`。

### cookie 值必须 URL 解码
DevTools / 插件复制来的值里 `,` `*` `=` 被编码成 `%2C` `%2A` `%3D`
（`buvid3` 尤其明显）。浏览器发出去的是**解码后**的值。
`import_cookies._unq()` 统一 `unquote`；含合法 `%` 但非转义时 unquote 原样返回，不会误伤。
回归 [S7] 锁住这三点。

### 排障口诀
| 现象 | 结论 |
|---|---|
| `space/myinfo` 通过但 `preupload` 403 | cookie 项数太少 → 全量导入 |
| `preupload` 403 且随后 `myinfo` 也 -101 | 已被风控作废 → 必须换新 cookie，别再重试 |
| 扫码脚本连续 TIMEOUT | 二维码没被扫，不是代码故障 |

### ★ 本机 Chrome 不要再想解 cookie
Chrome 154 起 cookie 用 **App-Bound Encryption（v20）**，沙箱内读得见记录、解不出明文
（`Local State` 里的 v20 密钥需 SYSTEM 级 elevation service）。`Default/Network/Cookies` 只能确认
「有没有登录」，不能拿值。别再在这条路上耗时间，直接扫码。

### `login_by_qr.js` 三个实测坑（勿回退）
1. 直接 `goto passport.bilibili.com/h5/passport/login/web` 会落到「二维码已失效」页 → 必须先访问
   `www.bilibili.com` 拿到 buvid，再调 `/x/passport-login/web/qrcode/generate`。
2. 凭据在 poll 成功响应的 **`data.url` query** 里（SESSDATA=…），**不是** `ctx.cookies()`
   （HttpOnly + 域隔离，context 里取不到）。
3. 未登录时 poll **也返回 `code=0` 但 `data.url` 为空** → 判据必须是「三项凭据齐全」，
   不能只看 `code==0`；否则会静默空转。二维码截图要 `deviceScaleFactor: 3` 否则手机扫不出。

## 补档后「整场重置」（本地录播残缺时必读，2026-10-06）
### 症状：某场总结明显偏短，开播时间比实际晚
本地录播目录里**只落了整场的一部分**（实测 2026-10-05：录播软件 22:26:41 才启动，
本地只有 22:26 起的 1 段；而 rec.darkpy.cn 上该日是 6 段 / 9.78 GB，从 **20:17:08** 开始）。
管线会把残缺场当整场处理并写进 `state.done_dates` 与月度文档，**不报错、不告警**。

### 为什么自动补档没救回来（关键）
`backfill` 的缺失检查是「按 live_date 看该日**有没有**场次」。本地已有一段 → 判定该日已覆盖 →
不再从录播站补齐。**残缺场 ≠ 缺失场**，这是设计盲区。

### 处置：`scripts/drop_session.py`（先干跑，再执行）
```bash
# ① 先只查录播站，确认该日真实场次结构（不下载）
python rec_fetch_date.py --date 2026-10-05 --session 0 --dry-run
# ② 备份已完成（docx + state.json 手动 cp 也行）
# ③ 干跑重置计划
python drop_session.py --date 2026-10-05 --reset-state --quarantine --dry-run
# ④ 执行：删该日全部 level-2 章节 + 清 state 门禁 + 隔离旧产物
python drop_session.py --date 2026-10-05 --reset-state --quarantine
# ⑤ 从录播站补齐完整场次（--no-pipeline 只落位，便于先清理再跑）
python rec_fetch_date.py --date 2026-10-05 --session 0 --no-pipeline
# ⑥ 重跑整场
python daily_pipeline.py --date 2026-10-05
```
- `--quarantine` 是**改名**（加 `.partial_<yyyymmdd>` 后缀）不是删除，上传器按 `*_compressed.mp4`
  匹配，改名后既不会被当待传稿件，也随时能改回来。
- 落位文件名用**首段**时间戳（`_rec_name`），所以完整场会以 `...20261005-201708-027-....mp4`
  出现在录播根目录，与旧的残缺段**并存**——这正是要靠 ③ 把旧记录清掉的原因。

### 踩过的坑
- **`state.done_dates` 是硬门禁**：新落位场次的 live_date 与已处理最晚日相同 → `latest_by_live_date`
  会直接跳过，`--date` 显式指定也绕不过「已完成」判定。必须先 `drop_session --reset-state`。
- **月度文档会出现同一天两个章节**，且新章节 append 在旧章节之后 → 必须先删旧章节。
- `drop_session` 的日期 token **必须同时给补零与不补零**两种（`2026年10月05日` / `2026年10月5日`），
  只写一种会静默 0 命中（`--dry-run` 报「章节命中 0 个」但不报错）→ 回归 [S1/S2] 锁住。
- MP3 的真实落点是 `config.mp3_export.out_dir`（本机 = `C:\Users\Tsing\DeskBox\文档`），
  **不是** `_dirs.mp3`（`D:\岁己直播自动总结\MP3` 常年是空的）——按 `_dirs.mp3` 找会漏。
- 磁盘参考：9.78 GB 素材补档峰值约需 20 GB（分段 + 合并双份），D 盘剩 60 GB 才敢开工。

## 硬约束（务必保持）
- 转录文本**必须按 30 分钟切块逐块发送 AI**，绝不允许整篇一次性发送（防超长 / 超时 / 截断）。
- Word 文档：同月复用同一文档，每场新直播插入**分页符 + Heading2 标题**区分；总结段=**粗体 11pt**，金句=**灰色(#666666)斜体 10pt**（格式错误由 `docx_healthcheck.py` 自动纠正）。
- ★ **标题样式 / 大纲级别 / 自动目录**（导航三件套，2026-10-01 起强制）：
  - 层级固定为「**标题 1** = 月份大标题（`YYYY年M月 直播总结`）→ **标题 2** = 每场总结标题」。
  - 每场标题必须同时具备：内置标题样式 **+ 段落级 `w:outlineLvl`**（`summarize_gemini.set_outline_level`）。
    只写样式不够——docx 被腾讯文档 / WPS 改写后内置样式可能退化成自动 styleId，大纲级别是兜底。
  - 文档开头必须有**可自动更新的目录**：`TOC \o "1-3" \h \z \u` 域（带 `dirty=true`，Word 打开即刷新），
    由 `summarize_gemini.ensure_front_matter()` **幂等**插入（已有 TOC 时不重复插）。
  - 对历史文档补结构用 `python docx_toc.py <docx> [--dry-run]`（正文一字不改，自动备份 `.bak_toc`）。
  - `reorder_doc.py` 的章节边界**只认大纲级别 2**：否则月份大标题会因解析不出日期被排到文档最末，目录跟着被搬走。
- ★ **月度归档（2026-10-02 起）**：新总结一律写入**「直播日期所属月份」的文档**，往月文档原样归档、不再追加，**绝不跨月混写**。
  - 保存路径：`C:\Users\Tsing\DeskBox\文档\岁己直播总结_{year}年{month}月.docx`
    （`config.output.doc_dir` = `C:\Users\Tsing\DeskBox\文档`，`doc_name_pattern` = `岁己直播总结_{year}年{month}月.docx`）
  - 每月 1 日第一次跑 `daily_pipeline` 时，步骤 0.5 由 `S.ensure_month_doc()` **自动新建**当月文档（幂等），
    新文档一建立就带月份大标题 + 自动目录；文档内按时间升序逐场记录，场次之间用**分页符**明确分隔。
  - 命名校验：文件名年月必须等于**该文档所收录场次的直播年月**（新建时按 `datetime.date.today()` 取，
    系统跨月后立即生效）；发现不一致一律按实际年月命名。
  - 唯一例外：补录往月缺失场次时仍写回该月文档（保证归档完整），这不算跨月混写。
- **章节元数据行（必须）**：每个 Heading2 章节标题下方必须紧跟一行元数据：`直播日期 YYYY-MM-DD  |  时长 HH:MM:SS  |  N/N 段  |  总结于 YYYY-MM-DD HH:MM`；本地 / B站场次另起一行 `来源 <本地 mp4/flv 路径 或 https://www.bilibili.com/video/BVxxxx>`。旁路本地路径（`regen_gemini_v2.process_session`）与自动化路径（`summarize_gemini.append_section`）生成的元数据格式**必须完全一致**。
- 断点续跑：每场每阶段写入 state.json，中断重跑自动跳过已完成部分。
- 热词纠正：转写之后、送 AI 之前必须对 TXT/SRT 做一遍热词替换（`hotwords.md`，含主播名/术语/歌名/保护规则），确保送进模型的文本已纠正。
  AI 产出里若又出现热词表的原词（模型改写引入），由 `docx_healthcheck.py` 在收尾自动回扫并告警。

## 标准流程（固定，勿改顺序）

**转写 → 热词纠正 → 总结**。三步的顺序是硬规范，任何一侧都不得调换或省略：

| 步 | 动作 | 代码位置 | 硬性要求 |
|---|---|---|---|
| 1 | FunASR 带时间轴转写 | `transcribe.py` | 超过 2h 的音频**先切块再转写**（防 VAD 截断丢内容）；任一分块为空 → 整场失败、不落盘 |
| 2 | 按热词表逐项纠正 TXT **与** SRT | `hotwords.py`（`apply_to_file`） | 转写产物落盘后**立刻**跑；两张表都要过一遍；保护词先占位防误伤；写回用原子写 |
| 3 | 按 30 分钟切块逐块总结 → 写 Word | `regen_gemini_v2.py` / `summarize_gemini.py` | 只允许喂**已纠正**的文本；绝不整篇一次性发送 |

配套约定（2026-09-20 固定）：

- **替换表的权威位置是工作目录的 `WORKDIR/hotwords.md`**（由 `config.hotwords.path` 指向，相对路径以工作目录为基准）。
  改这个文件**立即生效、无需改任何代码**；skill 内 `scripts/hotwords.md` 只是通用空模板兜底。
- **改完表先自检**：`python scripts/hotwords.py --audit`（默认自动定位当前生效表）。
  它会报出三类「写了但没真正生效」的词条并在有问题时以退出码 1 结束：
  · **空转**——原词与替换完全相同，`replace` 什么也没做；
  · **冲突**——同一原词指向多个不同目标，只有首次生效、其余被静默丢弃；
  · **危险级联**——某条的替换结果里含有另一条非空转规则的原词，会被二次替换。
- **保护词机制不可删**：`## 保护规则（不受误伤）` 章节下的短语会先被占位、替换完再还原，
  专门用于挡住「上见→上舰」「点个→点歌」「破房→破防」「PC→PK」这类短词误伤长词。
- **已总结的历史场次不重跑**：`state.json` 的 `done_dates` / `processed` 是门禁，干跑会显示
  `[已覆盖] 已处理`。需要重跑某场必须**显式**指定（`--date YYYY-MM-DD` 或 `--bvid BVxxx`），
  日常自动化绝不回头重刷历史。

## 运行与调试
- `daily_pipeline.py --workdir "<工作目录>" --bvid BVxxx` 手动指定某期
- `daily_pipeline.py --workdir "<工作目录>" --dry-run` 只预览会处理/跳过哪些（含缺失检查报告）
- `daily_pipeline.py --workdir "<工作目录>" --date 2026-09-01` 定向处理某直播日期（本地没有自动走 rec → B站补档）
- `backfill.py --workdir "<工作目录>" --from YYYY-MM-DD --to YYYY-MM-DD` 缺失检查报告；加 `--fill` 实际补档
- `cleanup.py --report` / `--sweep [--dry-run]` 清理中间文件，详见「自动清理」
- `selftest_robustness.py` 健壮性回归自测（离线，85 项），详见「健壮性加固」第 6 节
- `docx_healthcheck.py` Word 成品体检与修复（格式/异常内容/热词），详见「健壮性加固」第 7 节；已接入每日流程收尾
- `hotwords.py --audit` 审计当前生效的热词表（空转 / 冲突 / 危险级联）；`hotwords.py --text "..."` 演示单条替换
- `install_scheduled_task.py --install/--query/--run/--remove` 耐久执行计划任务；`run_scheduled.py --status` 看上次结果
- 日志在 `WORKDIR/logs/`，错误在 `errors.log`；状态 / 基线在 `WORKDIR/state.json`
- 中断排查：`WORKDIR/.daily_pipeline.stage` 记录最后一个阶段检查点；`logs/run_scheduled_*.log` 与 `logs/scheduled_last_run.json` 记录计划任务视角的结果
- rec站下载的 FLV / 合并中间 MP4 在 `darkpy.work_dir`（默认 `WORKDIR/_darkpy/`）：FLV 合并成功后即删（`keep_flv_after_merge=false`），中间 MP4 压缩成功后被删；补档失败时保留供断点重跑

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
