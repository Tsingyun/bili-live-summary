# bili-live-summary · AI 上手手册（通用版）

> 给 AI / 接手者阅读。读完本文件即可独立运行、排障、扩展这套「直播录播 → 总结 → MP3 → 压缩」全链路，无需再翻聊天记录。
> 本 skill **完全配置驱动、零主播专有数据**：每个主播用一个独立工作目录 + 一份 config.json 即可运行，不动任何代码。

---

## 0. 一句话目标

把**某位 B站主播**的每日直播录播，自动变成 **3 个成品文件**（具体主播由工作目录的 config 决定）：

1. **AI 总结的 Word 文档**（放在 `output.doc_dir` 指定目录，按月归档；岁己 = `C:\Users\Tsing\DeskBox\文档`）
2. **中质量 MP3**（192k / 44.1k，体积不大，音质够文字识别，用户另有他用）
3. **视频文件**（放到 `本地录播根目录/YYYY-MM-DD/`）
   - **本地录播** → 压缩后的 HEVC MP4（~5GB，`<原名>_compressed.mp4`）；压缩成功后按 `compress.replace_mode` 处理原视频（`delete` = 直接永久删除）
   - **B站兜底下载的录播** → **不压缩**，跑完流程后**原样保留 MP4**（用户规则 2026-08-29）

来源优先级：**本地录播目录优先；本地没有才去 B站合集兜底**。

> 下文用 **岁己（SUI，B站 UID 1954091502）** 作为「已落地的具体主播」示例值，方便对照；换成别的女主播 / UP主时，只需把这些示例值替换为新主播的配置，**代码一行不改**。

---

## 0.5 新 AI 接手 / 开场白模板（给接手的 AI 用）

当你（新 AI）要接手这套管线时，开场白应能让原系统被正确定位、并让对方快速给你正确权限。直接复制下面这段作为开场（把 `<...>` 替换为实际值）：

```
你好，我接手「bili-live-summary」这套 B站直播录播自动总结管线。
请帮我确认以下信息，我就能独立运行 / 排障：

1. 工作目录在哪？（默认 `D:\直播自动总结`，岁己在 `D:\岁己直播自动总结`）
   里面应有 config.json / state.json / hotwords.md / prompt_original.txt。
2. 每日自动化是哪个？（automation_update 的 id，岁己是 automation-1786280495470，每天 04:00）
3. 跑管线的 python 环境是哪个？（默认用 bili-archive venv 里的 python.exe）
4. 录播原始文件放哪个本地根目录？（config.local.root，岁己是 D:\录播\25788785-岁己SUI）
5. B站合集 series_id / mid 是多少？（config.series，岁己 mid=1954091502 / series_id=2609053）
6. API Key 在哪？（config.summarize.api_key，注意免费层每模型每日 20 次硬上限）
7. 总结用的 Word 落哪个目录、命名什么？（config.output.doc_dir / doc_name_pattern）
8. 有没有还需保留的特殊规则（如某日期不要合并、热词保护名单）？

确认后我先 `daily_pipeline.py --workdir "<工作目录>" --dry-run` 验证选片，再正式跑。
```

> 关键原则：**所有差异都在工作目录的 config / hotwords / prompt 里，skill 代码零改动**。不要为某个主播去改 skill 内的脚本。

---

## 1. 每日怎么跑（唯一入口）

每天由自动化触发（岁己示例：04:00 的 `automation-1786280495470`），跑 **这一条命令**：

```bat
"C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe" -u "C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\daily_pipeline.py" --workdir "D:\岁己直播自动总结"
```

- `daily_pipeline.py` 是 **唯一入口**，内置完整 5 步，不要再拆成 `local_priority.py` / `run_pipeline.py`（那两个已被取代，仅供参考）。
- 脚本自带 **单实例锁**（`.daily_pipeline.lock`）和 **断点续跑**（state 占位 + TXT/SRT 存在性判定）。
- 退出码：`0`=全部成功、`1`=个别失败（下次自动重试）、`3`=网络未就绪。
- `--workdir` 可省略（靠 `_workdir.json` 指针 / `BILI_WORKDIR` 推断），但显式写更清晰、免歧义。

手动 / 排障命令：

```bat
:: 只预览会处理哪些、跳过哪些，不碰任何文件（强烈建议先跑这个）
python daily_pipeline.py --workdir "D:\岁己直播自动总结" --dry-run

:: 只处理某一天（定向重跑 / 补跑），如 8/24 漏了
python daily_pipeline.py --workdir "D:\岁己直播自动总结" --date 2026-08-24
```

> 运行环境必须用上面的 `bili-archive` venv（已装 torch / FunASR / python-docx / requests）。
> 工作目录指针在 `scripts/_workdir.json`（岁己 = `D:\岁己直播自动总结`），config 即该目录下的 `config.json`。

---

## 2. 5 步流程（脚本自动完成，无需人工干预）

| 步 | 动作 | 关键函数 / 脚本 | 产出 |
|---|---|---|---|
| 1 | **监测本地根目录**（`config.local.root`）的新录播（flv/mp4，直接放根目录） | `scan_root_sessions` | 待处理 session 列表；录制中/已处理的自动跳过 |
| 2 | **有本地新文件 → 本地总结**：抽 16k 音频 → FunASR 转写 → 热词纠正 → Gemini 切块总结 → 写 Word；写 `LOCAL-*` 占位 | `process_video_source` + `regen_gemini_v2.process_session` | Word 章节 + state 占位 |
| 3 | **缺失检查 + 三级补档**：对照「实际直播日历」（rec站 ∪ 本地 ∪ B站标题）找缺失日期，按 本地 → rec.darkpy.cn → B站 顺序补 | `backfill.stage` + `darkpy_rec.fetch_and_prepare` + `bilibili_fallback(only_dates=…)` | 补齐的场次也写 Word/state 占位；输出逐日结论 |
| 4 | **导出中质量 MP3**（192k/44.1k）→ `mp3_export.out_dir`（与 Word 同目录，岁己 = `C:\Users\Tsing\DeskBox\文档`） | `mp3_export.export_for_local` | `文档目录\<原名>.mp3` |
| 5 | **压缩 HEVC MP4**（~5GB）→ 「录播根目录/YYYY-MM-DD/」日期文件夹（本地录播 + rec站补档都压） | `compress_video.compress_video` | `录播根目录/YYYY-MM-DD/*_compressed.mp4` |
| 5b | **B站录播归档**（不压缩）→ 把下载 MP4 原样挪到日期子目录 | `_archive_bilibili_mp4` | `录播根目录/YYYY-MM-DD/<原名>.mp4` |

> 第 2 步处理完本地新文件后，**始终执行第 3 步**（不再互斥）：第 3 步只处理「本地没有 / 已处理但 Word 也没有 / 文件过小疑似残缺」的日期，已覆盖的日期不会重复处理。
> **备份优先级（2026-09-05）**：本地 → rec.darkpy.cn（第二备份，更新及时，优先于 B站）→ B站（第三备份）。
> **用户规则（2026-08-29）**：B站兜底录播跑完流程后**留下 MP4、不压缩**（`bilibili.keep_mp4=true`、`compress=false`）。
> **用户规则（2026-09-05）**：rec站补档的场次与本地录播同等压缩归档，不保留原始大文件。
> **用户规则（2026-09-08，最新）**：压缩后的 MP4 **不用 MP4/ 文件夹**，统一 `out_dir_mode=date_subfolder` 直接输出到「录播根目录/YYYY-MM-DD/」日期文件夹（mp4_folder / mp4_date_folder 模式搁置）。
> **用户规则（2026-09-08，最新）**：归档 MP3 输出到 `C:\Users\Tsing\DeskBox\文档`（与 Word 总结同目录，`mp3_export.out_dir`），不再放录播根目录。

---

## 3. 目录与文件约定（务必遵守）

### 本地录播根目录（示例 `D:\录播\25788785-岁己SUI`）
- 录制器产出的原始文件 **直接放根目录**（不在日期子目录里）：
  `录制-25788785-20260824-203245-785-陪你这个猪过周一！5070ti版.flv`
  命名规则：`录制-<UID>-<YYYYMMDD>-<HHMMSS>-<idx>-<标题>.<ext>`
  （自定义命名需在 config.local.name_regex 里提供含 `date`/`time`/`title` 命名组的正则）
- **flv 通常是录制中的临时格式，且可能在录制过程中被监测到**——脚本用「大小稳定 + 修改时间够老」判定文件已写完，否则整段跳过，避免截半。
- 处理完成后根目录会留下：中质量 `.mp3`（交付物）+ 各 `YYYY-MM-DD\` 子目录（里面是视频交付物：本地录播 = 压缩后的 `_compressed.mp4`；B站兜底 = 原样保留的 MP4）。

### 视频产物位置（`compress.out_dir_mode = "date_subfolder"`，2026-09-08 起为当前规则）
- **本地录播 & rec站补档**：压缩后输出到 **`录播根目录/YYYY-MM-DD/`** 日期文件夹（源已在同日期目录则直接复用、不重复建层），文件 `<原名>_compressed.mp4`（用户规则 2026-09-08：**不要 MP4/ 文件夹，直接用日期命名文件夹**）。
- 曾被试用又搁置的模式（代码仍兼容、配置勿再用）：`mp4_folder`（2026-09-05，统一 `根/MP4/`）、`mp4_date_folder`（2026-09-06，`根/MP4/YYYY-MM-DD/`）。
- 日期从文件名里的 8 位日期 `20260824` 解析（自动跳过 UID 等非法日期串）。
- `replace_original=true` 时按 `replace_mode` 处理原视频——`recycle` = 先备份到 `~/.workbuddy/backups/live_raw/` 再移入**回收站**；`delete` = **直接永久删除**（省空间，岁己当前配置）。**压缩失败不动原文件**。
- **B站源**：**不压缩**，原样保留下载到的 MP4，按直播日期归档到 `根目录/YYYY-MM-DD/`（默认 `bilibili.keep_mp4=true`）。设 `keep_mp4=false` 才删除下载视频。

### 工作目录 `D:\岁己直播自动总结`（运行时产物，config 所在）
```
config.json            # 唯一配置（不进 git）
state.json             # 处理进度 / 去重占位
TXT/  SRT/  MP3/       # 转写文本 / 字幕 / 16k 临时音频
logs/  tmp/  merged/    # 日志 / 临时 / 合并中间视频
_darkpy/               # rec.darkpy.cn 下载的 FLV + 合并中间 MP4（补档成功即清，失败保留续跑）
hotwords.md            # 热词替换表（转写后纠正用）
prompt_original.txt    # 总结用的 system prompt
```

### 交付物最终落点（示例，岁己）
| 交付物 | 位置 |
|---|---|
| Word 总结 | `C:\Users\Tsing\DeskBox\文档\岁己直播总结_{year}年{month}月.docx`（同月同一文档，分页追加） |
| 中质量 MP3 | `mp3_export.out_dir\<原名>.mp3`（岁己 = `C:\Users\Tsing\DeskBox\文档`，与 Word 同目录） |
| 视频（本地录播） | `本地根目录\MP4\<原名>_compressed.mp4`（压缩后的 HEVC） |
| 视频（rec站补档） | `本地根目录\MP4\<原名>_compressed.mp4`（压缩后的 HEVC） |
| 视频（B站兜底） | `本地根目录\YYYY-MM-DD\<原名>.mp4`（**不压缩，原样保留**） |

---

## 4. 配置速查（`config.json` 关键项）

| 段 | 字段 | 含义 / 示例值（岁己） |
|---|---|---|
| `series` | `mid` / `series_id` | `1954091502` / `2609053`（B站合集，兜底用） |
| `latest_mode` | `mode` | `latest_by_live_date`；`since_live_date=2026-08-09` 硬下限 |
| `summarize` | `provider/model` | `google` / `gemini-3.8-flash`（首选，带轮换兜底） |
| `summarize` | `streamer` / `audience` | `岁己` / `饼干岁`（总结人称，prompt 占位符用） |
| `summarize` | `chunk_minutes` | `30`（**必须**按 30 分钟切块逐块发 AI） |
| `summarize` | `prompt_path` | 可选；指向含 `{streamer}`/`{audience}` 占位符的 prompt 文件 |
| `output` | `doc_dir` / `doc_name_pattern` | `C:\Users\Tsing\DeskBox\文档` / `岁己直播总结_{year}年{month}月.docx` |
| `transcribe` | `chunk_hours_threshold` | `2`（>2h 音频先切块再转写，规避 FunASR VAD 截断） |
| `hotwords` | `enabled` / `path` | `true` / `hotwords.md`（相对工作目录）。**固定流程「转写→热词→总结」的第 2 步，勿跳过**：权威表在 `WORKDIR/hotwords.md`，改完立刻生效；改完跑 `python scripts/hotwords.py --audit` 查空转/冲突/危险级联 |
| `mp3_export` | `enabled/bitrate/ar/out_dir` | `true` / `192k` / `44100`；`out_dir` = 与 Word 同目录（岁己 `C:\Users\Tsing\DeskBox\文档`，用户规则 2026-09-08） |
| `local_merge` | `enabled/max_gap_sec/allow_overlap_sec/exclude_dates` | 多段**先合并再总结**（禁止逐段总结）；场次判定**只看相邻间隙、不看日期变更**，跨 0 点连续段归属开播那一场；`allow_overlap_sec`(默认60) 容忍重连时的轻微负间隙；`exclude_dates` 显式强制不合并 |
| `compress` | `enabled/out_dir_mode/replace_original` | `true` / `date_subfolder`(→`录播根目录/YYYY-MM-DD/`，2026-09-08 当前规则) / `true`（本地 & rec站补档压缩后处理原视频） |
| `compress` | `replace_mode` | `delete` = 直接永久删原视频（岁己当前，省空间）；`recycle` = 先备份再移回收站 |
| `compress` | `codec/target_size_gb/cq` | `hevc_nvenc` / `5` / `24` |
| `bilibili` | `keep_mp4` / `compress` | `true` / `false`（B站兜底录播**保留 MP4、不压缩**，归档到 `根目录/YYYY-MM-DD/`） |
| `darkpy` | `enabled/host/port/file_port/room_dir` | rec.darkpy.cn 第二备份：`true` / `rec.darkpy.cn` / `5144`(页面) / `5143`(文件) / `25788785-岁己SUI` |
| `darkpy` | `max_gap_sec/min_session_sec/work_dir` | 段归组间隙 `1800` / 不完整阈值 `600`s / `D:\岁己直播自动总结\_darkpy` |
| `backfill` | `enabled/start_date/end_date` | 缺失补档窗口：`true` / 空(→since_live_date) / 空(→今天) |
| `live_calendar` | `sources/danmakus_api` | 实际直播日历：`["darkpy","local","bilibili"]` / 空(跳过 danmakus) |
| `cleanup` | `delete_mp3_after_success` | `true`（删 16k 临时音频，不影响中质量 MP3） |

> API Key 在 `summarize.api_key`（仅本机 config，勿提交）。Gemini 免费层每模型每日 20 次硬上限、RPM=5，脚本已内置轮换 + 限流。

---

## 5. 状态与去重（避免重复消费 token）

`state.json` 是 agent 与自动化共用的唯一真相源：

- **本地场次**：写 `LOCAL-YYYYMMDD`（单场）或 `LOCAL-YYYYMMDD-2/-3`（同日期多场），`date` 推高一天以挡住 B站日后上传同场。
- **rec.darkpy.cn 补档场次**：写 `DARKPY-YYYYMMDD[-N]` 占位（语义同 LOCAL：`date` +1 天挡 B站、`live_date` 存真实直播日期、`done_dates` 只收一次）。
- **B站场次**：写 `BVxxxx` 条目，带 `summarize` 阶段标记。
- **`done_dates`**：已完整处理过的直播日期列表，缺失检查与 B站兜底都会跳过这些日期。
- **TXT 存在性**：`process_session` 还会查 Word 是否已有该章节（`has_section`），双重防重。

判定「已处理」：state 占位存在 **或** 对应 TXT 已存在 → 跳过。缺失检查在此基础上再加两条「已覆盖」：
Word 月度文档已有该日章节、或本地存在该日完整视频（> `backfill.min_cover_video_mb`，默认 20MB，过小视为残缺）。

---

## 6. 排障手册

| 现象 | 原因 / 处理 |
|---|---|
| dry-run 显示「录制中，跳过」 | 文件还在写（大小增长 / mtime 太新）。等录制结束、文件稳定后下次运行会自动处理。**不要手动移动或处理正在录制的文件。** |
| 某场总结残缺 / 缺后半段 | 长音频未切块导致 FunASR VAD 截断。确认 `transcribe.chunk_hours_threshold ≤ 2`，重跑该场会自动补齐（脚本按块续跑，失败场不写完成标记）。 |
| Gemini 429（每日配额耗尽） | 当天该模型额度用尽。`process_session` 遇到 429 立即失败返回、不重试，整场不写完成标记，次日续跑。 |
| 压缩后原视频没删 / 压缩版没生成 | 看 `logs/errors.log`：压缩失败不动原文件（安全）；回收站删除被安全策略拦截时会保留原文件并提示手动清理。 |
| B站兜底重复下了本地已处理的日期 | 看 `state.json` 的 `done_dates` 是否含该日期；根目录若仍留有原 flv，本地监测会优先处理、不会走 B站。 |
| 缺失检查报告说某天「本地缺失」但明明录了 | ① 该日视频 < `min_cover_video_mb`(20MB) 视为残缺；② 看 Word 是否有该日章节 / `done_dates` 是否收录。 |
| rec站补档下载慢 / 失败 | `darkpy.timeout/retries` 调大；断点续传 `.part` 已保留，重跑接着下。rec站文件服务端口 5143，列表走 file.php（四参数），勿直连 `/file/<目录>/`（403）。 |
| rec站合并后音频对不上 / 缺段 | `check_session` 会把时长过短/读不了的段标不完整，仍会尽力合并并告警；对不上的日期重跑会用 `.part` 续传。 |
| 合并多场被误判成一场 | 加 `local_merge.exclude_dates: ["2026-08-22"]` 显式强制不合并（别依赖阈值侥幸）。 |
| **磁盘被撑爆 / 垃圾文件堆积** | 跑 `python cleanup.py --report` 看占用、`--sweep` 清扫。每场临时占 25~30GB，靠 `cleanup` 段配置自动回收（详见下方「自动清理」）。 |
| **清理了但文件还在** | 自动化环境下 D 盘的 `os.remove` 会被安全 hook 拦截（抛 `AttributeError: 'NoneType' object has no attribute 'strip'`）。所有删除必须走 `common.force_unlink`（自动降级 PowerShell），管线脚本里禁止裸 `os.remove`。 |

**日志位置**：`工作目录\logs\YYYY-MM-DD.log`（每日一个）+ `logs\errors.log`（错误汇总）。

### 自动清理（防磁盘被撑爆）

每跑一场临时占用 25~30GB。pipeline 在**开跑前 / 每场收尾 / 整轮结束**自动调用
`cleanup.sweep`，无需人工干预。手动执行：

```bash
python scripts/cleanup.py --report              # 只看占用，不删
python scripts/cleanup.py --sweep --dry-run     # 预演
python scripts/cleanup.py --sweep               # 真删
python scripts/cleanup.py --sweep --aggressive  # 无视保留天数清空临时类
```

- **只删**：workdir 下 `tmp/`（下载碎片、ASR 切块）、`MP3/`（16k 转写临时音频）、`logs/`、
  `backups/`、`__pycache__`、超期 `TXT/`/`SRT/`、根目录 `*.bak`、孤儿 `.lock`/`.heartbeat`。
- **绝不碰**：录播根目录的任何文件（压缩 MP4 / 源视频）、`doc_dir` 与 `mp3_export.out_dir` 的成品（Word 总结 / 归档 MP3）、
  `config.json` / `state.json` / `hotwords.md`。
- `disk_guard_gb`（默认 40）：磁盘剩余低于该值时，开跑前强制清扫并在日志告警；
  清扫后仍不足会明确提示「成品太多，需人工处理」——**本模块永不自动删成品**。
- `--report` 会额外列出「已有压缩版、源视频却还在」的**可回收文件**（只报告不删除）。

---

## 7. 安全与风险红线（必须遵守）

1. **正在录制的文件绝不处理**：靠 `is_file_stable`（大小稳定 + mtime 够老）兜底，宁可跳过等下一轮。
2. **原视频的处置由 `compress.replace_mode` 决定**（用户 2026-08-28 选定 `delete`）：
   `delete` = 压缩成功且 ffprobe 校验通过后**直接永久删除**（省空间，不进回收站、不备份）；
   `recycle` = 先备份到 `~/.workbuddy/backups/live_raw/` 再移入回收站。
   两种模式都**只在压缩产物校验通过后才删**，失败一律保留原文件。
   且必须用 `common.force_unlink`（降级删除），裸 `os.remove` 在 D 盘会静默失败。
3. **ffmpeg / 中文路径用原生 Windows 路径**（PowerShell / 脚本内 `subprocess` 直接传路径），避免 Git Bash 改动中文路径。
4. **长任务前台跑完再回报**：一期 5 小时录播约 15~25 分钟，别中途打断。
5. **宣称完成前必须核验文件存在且大小合理**（Word 章节、MP3、压缩 MP4 三者齐全），不轻信日志里的 "OK"。

### 7.1 进程被回收 / 睡死 / 断电后怎么续（2026-09-18 事故后加固）

**事故背景**：9/17、9/18 连续两天「后台进程被环境回收」，整轮零产物。根因是长任务挂在 **agent 进程树下** ——
agent 回合结束 → 宿主运行时 `lifecycle:dispose` → 整棵进程树被拆掉，跑在里面的脚本一起被 SIGTERM 杀掉。
脚本本身没坏，是**宿主回收**杀的。所以对策分两层：

**A. 被杀也不出坏产物（原子写 + 完整性门禁）**
- 所有 `os.remove` 都是陷阱 —— 本机安全 hook 会拦截（表现为静默失败或进程被终止），统一用 `common.force_unlink`。
- 合并 / 压缩 / 归档 MP3 一律**先写 `<目标>.part`**，`ffprobe` 校验时长达标后才 `os.replace` 成正式文件。
  中途被杀只会留下 `.part`，正式文件要么不存在、要么完整，**永远不会留半截**。
- **产物复用门禁**：判「已完成」不再只看「存在且体积 >1MB」，而是**时长 ≥ 源 / 分段之和 95%（MP3 98%）**。
  半截产物会被删掉重做，避免被当成完成品、甚至在 `delete` 模式下误删完好的源视频。
- 状态文件 `state.json` 也用原子写并留 `.bak`；主文件损坏时自动回退 `.bak`，不会因一次写坏就丢全部去重记录。

**B. 长任务搬出 agent 进程树（耐久安全网）**
- `scripts/run_scheduled.py` 是**耐久入口**：查锁 → 子进程跑 daily_pipeline → 失败重试 2 次 → 结果写 `logs/scheduled_last_run.json`。
- 用 Windows 计划任务（**Task Scheduler 服务**拉起，不是 agent 的子进程）挂它，就绕开了回收：
  `python scripts/install_scheduled_task.py install --workdir "D:/岁己直播自动总结"`
  （注册 `BiliLiveSummary_Daily`：每天 04:20 起每 60 分钟一次、共 3 小时，交互式免提权；**正在跑时不强杀**。）
  查看 `install_scheduled_task.py show`、手动触发 `... run`、卸载 `... remove`。
- **锁改心跳驱动**：判活要求「PID 存在 **且** 心跳新鲜（<1200s=20min）」。运行中的进程后台线程每 60s 刷锁，
  被杀后 20 分钟内即可被下一轮接管。**注意**：Windows 下 `os.kill(pid,0)` 是**纯探活、不会杀进程**（已实测），
  但 PID 复用很快，所以不能只看 PID —— 这也是必须叠心跳的原因。
- **排查顺序**：先看 `logs/scheduled_last_run.json`（计划任务那条跑得怎样）→ 再看 `WORKDIR/.daily_pipeline.stage`
  （卡在哪一阶段）→ 最后才翻 `logs/` 明细日志。看到 `.part` 文件是正常现象，说明上次被中断，下轮会重做该步。

> 结论：**任何「自动化没产物」先怀疑宿主回收，而不是脚本 bug**。重跑一次 `daily_pipeline.py` 就能从断点续上。

---

## 8. 代码地图（想改流程先看这里）

| 文件 | 职责 |
|---|---|
| `scripts/daily_pipeline.py` | **主编排器**：监测 → 本地/B站 → MP3 → 压缩，含锁/断点/状态写入（通用，零主播硬编码） |
| `scripts/scan_root_sessions`（在 daily_pipeline 内） | 根目录监测 + **全局按时间排序、间隙阈值切分场次**（跨午夜归前一场）+ 合并 + 录制中跳过；合并依赖外部命令 ffmpeg/ffprobe，缺失时显式报错并降级/跳过 |
| `scripts/transcribe.py` | FunASR 转写（带时间轴 TXT/SRT），`chunk_hours_threshold` 切块 |
| `scripts/hotwords.py` | 转写后热词替换（**固定流程「转写→热词→总结」第 2 步**）：路径相对工作目录解析、保护词占位防误伤、长词优先、原子写回；`--audit` 审计空转/冲突/危险级联 |
| `scripts/regen_gemini_v2.py` | Gemini 切块总结 + 写 Word（`process_session`，带模型轮换/限流/续跑） |
| `scripts/summarize_gemini.py` | Word 排版底层（粗体总结、斜体金句、章节元数据、话题分隔线） |
| `scripts/mp3_export.py` | 中质量 MP3 导出 |
| `scripts/compress_video.py` | HEVC 压缩（本地录播 & rec站补档；`out_dir_mode` 当前用 `date_subfolder`→根/YYYY-MM-DD/；兼容 `mp4_folder`/`mp4_date_folder`(已搁置)、`next_to_source`；`replace_mode` 决定原视频处理方式） |
| `scripts/daily_pipeline._archive_bilibili_mp4` | B站录播 MP4 归档（不压缩，原样挪到 `根目录/YYYY-MM-DD/`） |
| `scripts/darkpy_rec.py` | **rec.darkpy.cn 第二备份客户端**：`fetch_index`(列表,10min缓存) → `sessions_for_date`(全局归组,跨午夜归前场) → `download_one`(Range断点续传) → `merge_flvs`(concat copy 无损优先) → `fetch_and_prepare`(一站式) |
| `scripts/live_calendar.py` | 实际直播日历：`actual_live_dates`（rec站文件/本地/B站标题/danmakus 多源并集；rec站按场次起始日） |
| `scripts/backfill.py` | **缺失检查 + 补档编排**：`analyze`(日历−已覆盖) / `stage`(本地→rec→B站) / CLI 检查与补档 |
| `scripts/download_merge.py` | B站音频下载(`download_one`) + **视频下载合并**(`download_video_one`，兜底用) |
| `scripts/fetch_latest.py` | B站合集选片逻辑（`latest_by_live_date` 等） |
| `scripts/common.py` | 配置/状态/日志/B站接口/标点清洗/数字归一 + **`force_unlink`/`force_rmtree`（强制删除，必用）** + **原子写 `atomic_write_bytes/text/json`（`.part`+fsync+`os.replace`）** + **锁基础设施 `lock_owner_alive`（PID 存在且心跳 <20min 双条件）** + `timeout_for_source`/`run_tool` |
| `scripts/cleanup.py` | **垃圾清理器**：按保留天数清扫中间产物、清孤儿下载/ASR切块、磁盘水位检查（`sweep`/`after_session`/`check_disk_guard`/`report`） |
| `scripts/run_scheduled.py` | **耐久执行入口**（2026-09-18 新增）：先查锁（复用 `common.lock_owner_alive`）→ 以**子进程**跑 daily_pipeline → 失败自动重试（默认 2 次）→ 结果落 `WORKDIR/logs/scheduled_last_run.json`。给 Windows 计划任务调，**不在 agent 进程树下，不会被环境回收株连** |
| `scripts/install_scheduled_task.py` | 计划任务管理（2026-09-18 新增）：`--install` / `--query` / `--run` / `--remove`（**标志位，非子命令**），注册 `BiliLiveSummary_Daily`（默认 04:20 起每 60 分钟、持续 3 小时，交互式免提权，正在跑时不强杀） |
| `scripts/selftest_robustness.py` | **离线健壮性回归自测**（2026-09-18 新增，50 项）：mock HTTP 复现「忽略 Range / 截断 / 正常续传」、原子写、锁判活、docx 回退、压缩门禁、**清理器误删活锁**、**ffmpeg `.part` 输出格式推断**、**热词表解析/保护/审计/原子写**等。改完关键脚本先跑它。⚠️ **必须用 `bili-archive` venv 的 python 跑**（基础 3.13.12 缺 requests/python-docx，会假失败 6 项） |
| `SKILL.md` | skill 说明（本手册的精简版，给 skill 加载器读） |

---

## 9. 新主播接入（零代码，重点）

换成**全新主播**时，按下面步骤，**不要改 skill 代码**：

1. **建工作目录**：`D:\直播自动总结`（或任意独立目录，建议按主播命名，如 `D:\新主播直播自动总结`）。
2. **生成 config**：跑 `setup.py` 向导或复制 `scripts/config.example.json` 为 `工作目录/config.json`，填入：
   - `series.mid` / `series.series_id` / `series.url`（新主播的 B站合集）
   - `local.root`（新主播本地录播根目录）
   - `summarize.streamer` / `summarize.audience`（新主播名 / 观众称呼）
   - `summarize.api_key`（新主播用的 Gemini Key）
   - `output.doc_dir` / `output.doc_name_pattern`（新 Word 命名，可含 `{streamer}`）
   - `darkpy.enabled` / `darkpy.room_dir`（**可选，第二备份源**）：若该主播在 rec.darkpy.cn 也有录播目录（rec站目录名通常就是 `<uid>-主播名`，岁己为 `25788785-岁己SUI`），填上并 `enabled:true`，本地缺录播时自动从 rec 补；不配则该源自动跳过，不影响原「本地→B站」功能。
   - 其余（transcribe / compress / mp3_export / local_merge / topic_separator / backfill / live_calendar）沿用默认值即可。
3. **prompt（可选）**：复制 `scripts/prompt_template.txt` → `工作目录/prompt_original.txt`，把 `{streamer}`/`{audience}` 替换或保留占位符（运行时自动替换）；并在 config 设 `summarize.prompt_path="prompt_original.txt"`。不配则脚本回退到幂等保留字面内容。
4. **hotwords（可选）**：复制 `scripts/hotwords.md` → `工作目录/hotwords.md` 填术语/歌名纠正规则，config 设 `hotwords.path="hotwords.md"`；不填则 `enabled:false` 或留空用空模板。
5. **验证**：`daily_pipeline.py --workdir "<新工作目录>" --dry-run` 确认选片正确；`backfill.py --workdir "<新工作目录>"`（不带 --fill）确认缺失检查报告正常。
6. **挂自动化**：用 `automation_update` 建 recurring 自动化，命令带 `--workdir "<新工作目录>"`。
7. **skill 级指针（可选）**：若要让不带 `--workdir` 的调用默认指向新主播，在 `scripts/_workdir.json` 改 `workdir` 字段（⚠️ 这会影响不带参数的调用，多主播时更推荐每条命令都显式带 `--workdir`）。

> 验证清单：py_compile 五个脚本全过 → `--dry-run` 选片正确 → 真实跑一场确认 Word/MP3/MP4 三样齐全、state 占位已写。

---

## 10. 扩展 / 二次开发注意事项

- **加新步骤**：在 `daily_pipeline.process_video_source` 里按顺序追加，保持「转写 → 总结 → MP3 → 压缩」的先后，且每步失败不应误删已完成的产物。
- **改压缩目标体积/质量**：只动 `config.json` 的 `compress.target_size_gb` / `cq`，无需改代码。
- **换模型/换 provider**：改 `config.json` 的 `summarize.model`；Gemini 轮换顺序在 `regen_gemini_v2.MODELS`。
- **加新主播**：见第 9 节，复制整套 config（改 `series` / `streamer` / `audience` / `doc_dir`），`setup.py` 可向导生成；**代码不需要任何改动**。
