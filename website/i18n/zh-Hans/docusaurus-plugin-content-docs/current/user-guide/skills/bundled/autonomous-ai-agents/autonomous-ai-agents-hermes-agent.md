---
title: "Hermes Agent — 配置、扩展或贡献 Hermes Agent"
sidebar_label: "Hermes Agent"
description: "配置、扩展或贡献 Hermes Agent"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Hermes Agent

配置、扩展或贡献 Hermes Agent。

## Skill 元数据

| | |
|---|---|
| 来源 | 内置（默认安装） |
| 路径 | `skills/autonomous-ai-agents/hermes-agent` |
| 版本 | `2.3.0` |
| 作者 | Hermes Agent + Teknium |
| 许可证 | MIT |
| 平台 | linux, macos, windows |
| 标签 | `hermes`, `setup`, `configuration`, `multi-agent`, `spawning`, `cli`, `gateway`, `development` |
| 相关 skill | [`claude-code`](/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`opencode`](/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-opencode) |

## 参考：完整 SKILL.md

:::info
以下是此 skill 触发时 Hermes 加载的完整定义，也是 skill 激活后 agent 看到的指令。
:::

# Hermes Agent

当用户询问如何安装、配置、运行、排查、扩展或贡献 Hermes Agent 时，使用此 skill。

Hermes 是一个支持 profile 的 AI agent 框架，包含 CLI/TUI、gateway、dashboard、插件、skill、cron、记忆提供商、MCP 和模型提供商等界面。优先使用本地仓库和文档证据，不要依赖记忆猜测。

## 前置条件

- 明确所涉及的界面：CLI、TUI、gateway、dashboard、插件、skill、cron、记忆、MCP 或 provider。
- 处理仓库任务前，先阅读 `AGENTS.md`、相关文档、直接调用方和聚焦测试。
- 对 profile 范围内的文件使用 `HERMES_HOME` 以及 `get_hermes_home()` / `display_hermes_home()` 等仓库 helper。
- 绝不输出 `$HERMES_HOME/.env`、配置、日志、token 或平台凭据中的秘密。

## 工作流程

1. 仅在答案会实质改变范围或风险时澄清目标。
2. 先检查本地事实：仓库文件、文档、配置、状态、日志、测试，以及已安装的插件和 skill 目录。
3. 行为已可配置时，优先改配置而不是代码。
4. 进行小而精准的改动，保留 profile、prompt cache、插件边界和 gateway/session 契约。
5. 使用能证明改动的最小检查；本仓库优先运行 `scripts/run_tests.sh`，不要直接运行 `pytest`。
6. 准确报告修改文件、运行命令、跳过的检查和剩余风险。

## 常用入口

- Agent 循环：`run_agent.py`、`agent/`、`model_tools.py`、`toolsets.py`、`tools/`
- CLI、配置和斜杠命令：`cli.py`、`hermes_cli/`、`hermes_cli/commands.py`
- Gateway 和平台：`gateway/`、`plugins/platforms/`
- Skills：`skills/`、`optional-skills/`、`tools/skill_manager_tool.py`、`tools/skills_tool.py`
- TUI 和 dashboard：`ui-tui/`、`tui_gateway/`、`hermes_cli/web_server.py`
- Cron 和 Kanban：`cron/`、`hermes_cli/kanban*`

## 参考资料

- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/full-guide.md) — 已归档的完整 Hermes 指南，包含详细安装、配置、功能、平台和排障说明。
- [references/native-mcp.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md) — 原生 MCP 集成详情。
- [references/webhooks.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/webhooks.md) — Webhook 运行详情。
- 按需阅读以下仓库文档：
  - `AGENTS.md`：贡献流程和关键仓库结构。
  - `docs/context.md`：持久的 Sligo/Hermes 产品上下文。
  - `docs/project-state.md`：精简的任务路由游标。
  - `website/docs/`：面向用户的文档。

仅当用户需要详细命令或广泛功能对比时加载完整指南。实现任务应优先使用实时仓库文件和聚焦测试。

## 常见陷阱

- 不要硬编码 `~/.hermes`；profile 通过 `HERMES_HOME` 隔离。
- 除非运行时路径明确支持相应失效流程，否则不要在 turn 中途修改 prompt 历史或 toolset。
- 不要为插件专用行为修改 core；应扩展通用插件接口。
- 不遵循仓库 pinning 策略时不要新增依赖。
- 当代码、测试或运行时证据拥有事实时，不要把 skill、记忆或文档当作当前状态账本。

## 验证

- 仅配置或文档改动：运行相关源文件/文档测试。
- Python 行为改动：运行聚焦的 `scripts/run_tests.sh ...` 目标。
- UI/TUI 改动：运行对应的 npm type-check/test/build 以及必要的 Python endpoint 测试。
- 运行时或 gateway 问题：提供日志或状态证据，不要只做代码检查。
