# 岁己直播录播 · AI 上手手册（bili-live-summary）

> 给 AI / 接手者阅读。读完本文件即可独立运行、排障、扩展这套「直播录播 → 总结 → MP3 → 压缩」全链路，无需再翻聊天记录。

---

## 0. 一句话目标

把岁己（SUI，B站 UID 1954091502）的每日直播录播，自动变成 **3 个成品文件**：

1. **AI 总结的 Word 文档**（放在桌面，按月归档）
2. **中质量 MP3**（192k / 44.1k，体积不大，音质够文字识别，用户另有他用）
3. **压缩后的 HEVC MP4**（~5GB，放到 `D:\录播\25788785-岁己SUI\YYYY-MM-DD\`）

来源优先级：**本地录播目录优先；本地没有才去 B站合集兜底**。

---

## 1. 每日怎么跑（唯一入口）

每天 **04:00**（自动化 `automation-1786280495470`）触发，跑 **这一条命令**：

```bat
"C:\Users\Tsing\.workbuddy\binaries\python\envs\bili-archive\Scripts\python.exe" -u "C:\Users\Tsing\.workbuddy\skills\bili-live-summary\scripts\daily_pipeline.py"
```

- `daily_pipeline.py` 是 **唯一入口**，内置完整 5 步，不要再拆成 `local_priority.py` / `run_pipeline.py`（那两个已被取代，仅供参考）。
- 脚本自带 **单实例锁**（`.daily_pipeline.lock`）和 **断点续跑**（state 占位 + TXT/SRT 存在性判定）。
- 退出码：`0`=全部成功、`1`=个别失败（下次自动重试）、`3`=网络未就绪。

手动 / 排障命令：

```bat
:: 只预览会处理哪些、跳过哪些，不碰任何文件（强烈建议先跑这个）
python daily_pipeline.py --dry-run

:: 只处理某一天（定向重跑 / 补跑），如 8/24 漏了
python daily_pipeline.py --date 2026-08-24
```

> 运行环境必须用上面的 `bili-archive` venv（已装 torch / FunASR / python-docx / requests）。
> 工作目录指针在 `scripts/_workdir.json` → `D:\岁己直播自动总结`，config 即该目录下的 `config.json`。

---

## 2. 5 步流程（脚本自动完成，无需人工干预）

| 步 | 动作 | 关键函数 / 脚本 | 产出 |
|---|---|---|---|
| 1 | **监测本地根目录** `D:\录播\25788785-岁己SUI` 的新录播（flv/mp4，直接放根目录） | `scan_root_sessions` | 待处理 session 列表；录制中/已处理的自动跳过 |
| 2 | **有本地新文件 → 本地总结**：抽 16k 音频 → FunASR 转写 → 热词纠正 → Gemini 切块总结 → 写 Word；写 `LOCAL-*` 占位 | `process_video_source` + `regen_gemini_v2.process_session` | Word 章节 + state 占位 |
| 3 | **无本地新文件 → B站兜底**：抓合集最近未处理录播，下载合并多 P 视频 → 同一条总结流程 | `bilibili_fallback` + `download_merge.download_video_one` | 同上（源是下载的视频） |
| 4 | **导出中质量 MP3**（192k/44.1k） | `mp3_export.export_mp3` | 根目录下的 `.mp3` |
| 5 | **压缩 HEVC MP4**（~5GB）→ 日期子目录 | `compress_video.compress_video` | `根目录/YYYY-MM-DD/*_compressed.mp4` |

> 第 1 步有文件 → 走第 2 步；第 1 步**没有任何可处理文件** → 才走第 3 步 B站兜底（二者互斥，不会重复）。

---

## 3. 目录与文件约定（务必遵守）

### 本地录播根目录 `D:\录播\25788785-岁己SUI`
- 录制器产出的原始文件 **直接放根目录**（不在日期子目录里）：
  `录制-25788785-20260824-203245-785-陪你这个猪过周一！5070ti版.flv`
  命名规则：`录制-<UID>-<YYYYMMDD>-<HHMMSS>-<idx>-<标题>.<ext>`
- **flv 通常是录制中的临时格式，且可能在录制过程中被监测到**——脚本用「大小稳定 + 修改时间够老」判定文件已写完，否则整段跳过，避免截半。
- 处理完成后根目录会留下：中质量 `.mp3`（交付物）+ 各 `YYYY-MM-DD\` 子目录（里面是压缩后的 `_compressed.mp4` 交付物）。

### 压缩产物位置（用户 tips，2026-08-24）
- `compress.out_dir_mode = "date_subfolder"`：压缩后 MP4 进 **`根目录/YYYY-MM-DD/`** 子目录（子目录不存在则新建；已存在则跳过这步）。
- 日期从文件名里的 8 位日期 `20260824` 解析（自动跳过 UID 等非法日期串）。
- 本地源：`replace_original=true` → 压缩成功后原视频先备份到 `~/.workbuddy/backups/live_raw/` 再移入**回收站**（绝不裸删）。
- B站源：是临时下载文件，压缩成功后直接删除。

### 工作目录 `D:\岁己直播自动总结`（运行时产物，config 所在）
```
config.json            # 唯一配置（不进 git）
state.json             # 处理进度 / 去重占位
TXT/  SRT/  MP3/       # 转写文本 / 字幕 / 16k 临时音频
logs/  tmp/  merged/    # 日志 / 临时 / 合并中间视频
prompt_original.txt    # 总结用的 system prompt
```

### 交付物最终落点
| 交付物 | 位置 |
|---|---|
| Word 总结 | 桌面 `岁己直播总结_{year}年{month}月.docx`（同月同一文档，分页追加） |
| 中质量 MP3 | `D:\录播\25788785-岁己SUI\<原名>.mp3` |
| 压缩 HEVC MP4 | `D:\录播\25788785-岁己SUI\YYYY-MM-DD\<原名>_compressed.mp4` |

---

## 4. 配置速查（`config.json` 关键项）

| 段 | 字段 | 含义 / 当前值 |
|---|---|---|
| `series` | `mid` / `series_id` | `1954091502` / `2609053`（B站合集，兜底用） |
| `latest_mode` | `mode` | `latest_by_live_date`；`since_live_date=2026-08-09` 硬下限 |
| `summarize` | `provider/model` | `google` / `gemini-3.7-flash`（首选，带轮换兜底） |
| `summarize` | `chunk_minutes` | `30`（**必须**按 30 分钟切块逐块发 AI） |
| `transcribe` | `chunk_hours_threshold` | `2`（>2h 音频先切块再转写，规避 FunASR VAD 截断） |
| `hotwords` | `enabled` | `true`（转写后送 AI 前做热词替换） |
| `mp3_export` | `enabled/bitrate/ar` | `true` / `192k` / `44100`；`out_dir` = 录播根目录 |
| `local_merge` | `enabled/max_gap_sec/exclude_dates` | 同日期连续段合并；`exclude_dates` 显式强制不合并 |
| `compress` | `enabled/out_dir_mode/replace_original` | `true` / `date_subfolder` / `true`（压缩后回收站删原视频） |
| `compress` | `codec/target_size_gb/cq` | `hevc_nvenc` / `5` / `24` |
| `cleanup` | `delete_mp3_after_success` | `true`（删 16k 临时音频，不影响中质量 MP3） |

> API Key 在 `summarize.api_key`（仅本机 config，勿提交）。Gemini 免费层每模型每日 20 次硬上限、RPM=5，脚本已内置轮换 + 限流。

---

## 5. 状态与去重（避免重复消费 token）

`state.json` 是 agent 与自动化共用的唯一真相源：

- **本地场次**：写 `LOCAL-YYYYMMDD`（单场）或 `LOCAL-YYYYMMDD-2/-3`（同日期多场），`date` 推高一天以挡住 B站日后上传同场。
- **B站场次**：写 `BVxxxx` 条目，带 `summarize` 阶段标记。
- **`done_dates`**：已完整处理过的直播日期列表，B站兜底会跳过这些日期。
- **TXT 存在性**：`process_session` 还会查 Word 是否已有该章节（`has_section`），双重防重。

判定「已处理」：state 占位存在 **或** 对应 TXT 已存在 → 跳过。

---

## 6. 排障手册

| 现象 | 原因 / 处理 |
|---|---|
| dry-run 显示「录制中，跳过」 | 文件还在写（大小增长 / mtime 太新）。等录制结束、文件稳定后下次运行会自动处理。**不要手动移动或处理正在录制的文件。** |
| 某场总结残缺 / 缺后半段 | 长音频未切块导致 FunASR VAD 截断。确认 `transcribe.chunk_hours_threshold ≤ 2`，重跑该场会自动补齐（脚本按块续跑，失败场不写完成标记）。 |
| Gemini 429（每日配额耗尽） | 当天该模型额度用尽。`process_session` 遇到 429 立即失败返回、不重试，整场不写完成标记，次日续跑。 |
| 压缩后原视频没删 / 压缩版没生成 | 看 `logs/errors.log`：压缩失败不动原文件（安全）；回收站删除被安全策略拦截时会保留原文件并提示手动清理。 |
| B站兜底重复下了本地已处理的日期 | 看 `state.json` 的 `done_dates` 是否含该日期；根目录若仍留有原 flv，本地监测会优先处理、不会走 B站。 |
| 合并多场被误判成一场 | 加 `local_merge.exclude_dates: ["2026-08-22"]` 显式强制不合并（别依赖阈值侥幸）。 |

**日志位置**：`D:\岁己直播自动总结\logs\YYYY-MM-DD.log`（每日一个）+ `logs\errors.log`（错误汇总）。

---

## 7. 安全与风险红线（必须遵守）

1. **正在录制的文件绝不处理**：靠 `is_file_stable`（大小稳定 + mtime 够老）兜底，宁可跳过等下一轮。
2. **删除原视频只用回收站**：`compress_video._replace_backup_then_delete` 先备份到 `~/.workbuddy/backups/live_raw/` 再移入回收站，**绝不裸 `rm`/`del`**。回收站不可用则保留原文件。
3. **ffmpeg / 中文路径用原生 Windows 路径**（PowerShell / 脚本内 `subprocess` 直接传路径），避免 Git Bash 改动中文路径。
4. **长任务前台跑完再回报**：一期 5 小时录播约 15~25 分钟，别中途打断。
5. **宣称完成前必须核验文件存在且大小合理**（Word 章节、MP3、压缩 MP4 三者齐全），不轻信日志里的 "OK"。

---

## 8. 代码地图（想改流程先看这里）

| 文件 | 职责 |
|---|---|
| `scripts/daily_pipeline.py` | **主编排器**：监测 → 本地/B站 → MP3 → 压缩，含锁/断点/状态写入 |
| `scripts/scan_root_sessions`（在 daily_pipeline 内） | 根目录监测 + 连续段合并 + 录制中跳过 |
| `scripts/transcribe.py` | FunASR 转写（带时间轴 TXT/SRT），`chunk_hours_threshold` 切块 |
| `scripts/hotwords.py` | 转写后热词替换 |
| `scripts/regen_gemini_v2.py` | Gemini 切块总结 + 写 Word（`process_session`，带模型轮换/限流/续跑） |
| `scripts/summarize_gemini.py` | Word 排版底层（粗体总结、斜体金句、章节元数据、话题分隔线） |
| `scripts/mp3_export.py` | 中质量 MP3 导出 |
| `scripts/compress_video.py` | HEVC 压缩（日期子目录输出 + 回收站删原视频） |
| `scripts/download_merge.py` | B站音频下载(`download_one`) + **视频下载合并**(`download_video_one`，兜底用) |
| `scripts/fetch_latest.py` | B站合集选片逻辑（`latest_by_live_date` 等） |
| `scripts/common.py` | 配置/状态/日志/B站接口/标点清洗/数字归一 |
| `SKILL.md` | skill 说明（本手册的精简版，给 skill 加载器读） |

---

## 9. 扩展 / 二次开发注意事项

- **加新步骤**：在 `daily_pipeline.process_video_source` 里按顺序追加，保持「转写 → 总结 → MP3 → 压缩」的先后，且每步失败不应误删已完成的产物。
- **改压缩目标体积/质量**：只动 `config.json` 的 `compress.target_size_gb` / `cq`，无需改代码。
- **换模型/换 provider**：改 `config.json` 的 `summarize.model`；Gemini 轮换顺序在 `regen_gemini_v2.MODELS`。
- **加新主播**：复制整套 config（改 `series` / `streamer` / `audience` / `doc_dir`），`setup.py` 可向导生成。
