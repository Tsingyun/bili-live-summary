# 运行环境要求

## Python 与深度学习栈
- Python 3.10+（3.13 已验证），建议使用独立 venv。
- **PyTorch ≥ 2.7**：RTX 50 系（Blackwell，sm_120）必需此版本；CUDA 驱动版本需与 torch 的 CUDA 编译版本匹配（12.x→cu128，13.x→cu130）。
- **FunASR ≥ 1.2**、**ModelScope ≥ 1.15**。
- 纯 Python 依赖：`requests`、`python-docx`。

首次运行会自动从 ModelScope 下载模型（SenseVoiceSmall + fsmn-vad + punc_ct-transformer），需联网。模型缓存默认在 `~/.cache/modelscope`。

## 系统二进制
- **FFmpeg**：音频转码/切片必需，须在 PATH 或 config `paths.ffmpeg` 指定。
- **aria2c**：16 线程加速下载；缺失则下载变慢甚至失败，强烈建议安装（可通过 winget/choco 安装）。
- **B站 cookies**：Netscape 格式，路径填在 config `paths.cookies`。公开合集通常可无 cookies 拉列表，但下载音流可能需要登录态；建议配置。

## GPU 说明
- 自动检测 `cuda:0`；无 GPU 退回 CPU（长音频极慢，>30min 建议先确认）。
- `import torch` 偶发 CUDA 初始化段错误，脚本内置 5 次重试。

## 网络
- 凌晨调度时代理/网络可能未就绪，`run_pipeline.py` 启动会等待网络（DNS 探测），接口层带指数退避重试。

## 安装依赖（全新环境）
```bash
# 1) 建 venv 并装 Python 依赖
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
# 2) 安装与 CUDA 匹配的 torch（示例 CUDA 12.8）
pip install torch --index-url https://download.pytorch.org/whl/cu128
# 3) 系统二进制：ffmpeg、aria2c（winget install ffmpeg aria2.aria2 或手动下载）
```
若复用已含完整栈的现有 venv（如本机 `bili-archive`），在 setup 时把其 python 路径填入 `paths.python` 即可，无需重装。
