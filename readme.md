# Long-Novel-GPT Enhanced Fork

> 基于 `MaoXiaoYuZ/Long-Novel-GPT` 的增强版分支，重点补齐了 Windows 本地运行、OpenAI 兼容接口接入、自动长篇编排、可见流式输出和断点续跑能力。

## 这个分支解决什么问题

这个 fork 的目标不是改 UI，而是把“给一个小说设定，然后长期稳定跑完整部长篇”的链路补齐。

当前公开代码已经支持：

- 单次提供设定后，自动生成系列圣经、分卷规划、分批章节规划、剧情梗概、正文和章节摘要
- 断点续跑，重复启动同一项目时会自动接着已有状态继续写
- 可见 CLI 流式输出，适合只开终端、不用 GUI 的场景
- watchdog 自动拉起、卡住检测、崩溃后重启
- OpenAI 兼容接口的 `reasoning_effort`、流式、较大 token 预算和更稳的长时请求
- 多项目并行，只要使用不同的 `project-dir`

## 自动长篇能力概览

核心入口是 [auto_novel.py](auto_novel.py)。

它会围绕一个项目目录持续维护完整状态，包括：

- `brief.md`：小说设定
- `state.json`：运行状态、当前阶段、已生成章节数、下一章编号
- `memory/series_bible.md`：系列圣经
- `memory/story_memory.md`：压缩记忆
- `memory/completion_report.md`：完结评估
- `manuscript/full_novel.txt`：完整拼接后的正文

正文保存和终端流式输出是分开的：

- 实时流式文本会出现在终端窗口，并写入 `logs/console.out.log`
- 最终正文由章节文件同步汇总到 `manuscript/full_novel.txt`
- 所以流式显示内容不会混进最终正文文件

## 多本小说能不能并行

可以。

并行的关键不是开线程，而是为每本小说使用独立的项目目录和设定文件，例如：

```powershell
python .\watch_auto_novel_visible.py `
  --project-dir .\auto_projects\novel_a `
  --brief-file .\briefs\novel_a.md `
  --target-chars 2000000

python .\watch_auto_novel_visible.py `
  --project-dir .\auto_projects\novel_b `
  --brief-file .\briefs\novel_b.md `
  --target-chars 2000000
```

这样每条线都会拥有各自独立的：

- 状态文件
- 日志
- 记忆
- 分卷与章节目录
- 最终正文

## 当前默认启动方式

### 1. Web UI

```bat
run.bat
```

或：

```powershell
.\run.ps1
```

默认地址：

- Frontend: `http://127.0.0.1:8000`
- Backend: `http://127.0.0.1:7869`

### 2. 可见自动长篇模式

先复制：

```text
novel_brief.example.md -> novel_brief.md
```

然后运行：

```bat
run_auto_novel.bat
```

这个入口会启动可见 watchdog，并把 LLM 流式输出直接显示在当前窗口。

查看状态：

```powershell
powershell -ExecutionPolicy Bypass -File .\status_auto_novel.ps1
```

默认项目目录：

```text
auto_projects/default_project/
```

### 3. 直接运行编排器

如果你不需要外层 watchdog，可以直接运行：

```bat
run_auto_novel_direct.bat
```

## 重要参数

当前代码支持的关键参数包括：

- `--project-dir`：指定项目目录，用于区分不同小说
- `--brief-file` / `--brief-text`：提供小说设定
- `--target-chars`：目标字数
- `--completion-mode`：完结模式
- `--min-target-chars`：最小字数下限
- `--max-target-chars`：最大字数上限，`0` 表示不限制
- `--live-stream`：开启终端流式输出
- `--max-retries 0`：无限重试

关于完结模式：

- `hard_target`：更偏向按目标字数收束
- `min_chars_and_story_end`：先达到最小字数，再要求主线、终局、尾声和后日谈自然收束后结束

如果你想要“至少写到某个体量，但不要为了字数硬截断”，建议使用：

```powershell
python .\watch_auto_novel_visible.py `
  --project-dir .\auto_projects\default_project `
  --brief-file .\novel_brief.md `
  --completion-mode min_chars_and_story_end `
  --target-chars 2000000 `
  --min-target-chars 2000000 `
  --max-target-chars 0 `
  --max-retries 0
```

## OpenAI 兼容接口支持

这个 fork 补强了 OpenAI 兼容接入，适合接各种兼容 Responses API / Chat 风格的服务。

已经覆盖的重点包括：

- `reasoning_effort`
- 流式输出
- 更大的输入/输出 token 预算
- 更长的超时与重试
- Windows 本地环境下更稳定的运行体验

常见环境变量仍然通过 `.env` 配置，例如：

- `GPT_BASE_URL`
- `GPT_API_KEY`
- `GPT_AVAILABLE_MODELS`
- `GPT_MAX_INPUT_TOKENS`
- `GPT_MAX_OUTPUT_TOKENS`

## 文件保存位置

以默认项目为例，长篇相关文件主要在：

- `auto_projects/default_project/brief.md`
- `auto_projects/default_project/state.json`
- `auto_projects/default_project/logs/runner.log`
- `auto_projects/default_project/logs/watchdog.log`
- `auto_projects/default_project/logs/console.out.log`
- `auto_projects/default_project/memory/series_bible.md`
- `auto_projects/default_project/memory/story_memory.md`
- `auto_projects/default_project/memory/completion_report.md`
- `auto_projects/default_project/manuscript/full_novel.txt`

## 不会上传到仓库的本地内容

这个仓库默认不应提交以下内容：

- `.env`
- API Key
- `novel_brief.md`
- `auto_projects/`
- `.run/`
- `test_output/`
- 本地临时日志、PID 文件、个人项目启动器

## 相关文件

- [AUTO_NOVEL.md](AUTO_NOVEL.md)
- [auto_novel.py](auto_novel.py)
- [watch_auto_novel_visible.py](watch_auto_novel_visible.py)
- [run_auto_novel.bat](run_auto_novel.bat)
- [run_auto_novel_direct.bat](run_auto_novel_direct.bat)
- [status_auto_novel.ps1](status_auto_novel.ps1)
- [novel_brief.example.md](novel_brief.example.md)

## 上游项目

原始仓库：

- `https://github.com/MaoXiaoYuZ/Long-Novel-GPT`

这个分支的定位是继续保留原项目 UI 能力的同时，把 unattended 的长篇自动生成链路补完整，尤其偏向 Windows 本地 CLI 场景。
