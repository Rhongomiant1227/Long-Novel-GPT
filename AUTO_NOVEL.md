# 全自动长篇模式

这个模式是在 `Long-Novel-GPT` 现有三阶段能力基础上，额外补的一层本地自动编排器。

目标：

- 用户只提供一次设定
- 自动生成系列圣经
- 自动规划分卷
- 自动按批次生成章节大纲
- 自动扩写剧情梗概
- 自动生成正文
- 自动维护“故事记忆”并支持断点续跑

## 入口

- 直接运行：`run_auto_novel.bat`
- 查看状态：`powershell -ExecutionPolicy Bypass -File .\status_auto_novel.ps1`

默认项目目录：`auto_projects\default_project`

## 关键文件

- 设定：`auto_projects\default_project\brief.md`
- 状态：`auto_projects\default_project\state.json`
- 日志：`auto_projects\default_project\logs\runner.log`
- 系列圣经：`auto_projects\default_project\memory\series_bible.md`
- 压缩记忆：`auto_projects\default_project\memory\story_memory.md`
- 全书正文：`auto_projects\default_project\manuscript\full_novel.txt`

## 断点续跑

再次运行同一条命令即可自动续跑。

脚本会优先读取已有的：

- `state.json`
- 已生成的单章 `outline.md` / `plot.md` / `draft.md` / `summary.md`

## 写作 Skill

自动长篇默认启用 [webnovel-writing](skills/webnovel-writing/SKILL.md)：规划时检查开篇兑现和待回收伏笔，写作时检查场景推进、对白筹码和既有规则。原有审稿的第一轮加入 `brief` 和最近两章的原文片段；跨章提示要有可回查的两侧引文，只记日志，不进入自动修订项。Humanizer 仍是独立的可选语言润色环节。

CLI 和可见守护进程均支持 `--disable-webnovel-skill`，用于同模型、同剧情的对照。对照时使用两个新项目目录，避免复用缓存稿。

## 本地数据

作品目录、单本启停脚本、导入快照与实际 `fanqie_daily_jobs.json` 不随仓库发布。上传调度可参考没有启用任何任务的 `fanqie_daily_jobs.example.json`。

项目专用覆盖项放在 `.run/project_overrides.json`，或用 `LONG_NOVEL_PROJECT_OVERRIDES` 指定文件。支持 `text_replacements`、`role_only_rules` 和 `humanizer_excluded_projects`。兼容模型使用环境变量 `LOCAL_BASE_URL`、`LOCAL_API_KEY`、`LOCAL_AVAILABLE_MODELS`，无需在源码里放端点或密钥。

部分本地兼容端点会将思考 token 计入输出额度。确认端点支持 `chat_template_kwargs` 后，可显式设置 `LOCAL_ENABLE_THINKING=false`；留空则不改变服务端默认行为。

## 当前默认参数

- 目标字数：`2000000`
- 单章目标：`2200`
- 每卷章节：`30`
- 每批规划：`5`
- 线程数：`1`
- 主/副模型：`gpt/gpt-5.4`

## 已修复的关键问题

- OpenAI 兼容层支持 `reasoning_effort`
- OpenAI 兼容层支持更长超时和重试
- 原仓库隐藏的一键长篇链路里存在 `auto_write()` 调用缺失问题，已改为显式提示词调用
- 新增自动长篇编排器 `auto_novel.py`
