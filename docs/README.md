# 文档索引

本项目是配置驱动的 AI Agent 自动化任务系统，入口统一为
`python harness_automation.py --config <config>`，通过 `harness_type` 切换后端。

## 核心文档

| 文档 | 说明 |
|---|---|
| [OPENCODE_INTEGRATION.md](OPENCODE_INTEGRATION.md) | OpenCode 接入方式、opencode.json、workspace、skill、session |
| [CONFIG_STRUCTURE.md](CONFIG_STRUCTURE.md) | 配置结构说明 |
| [QUICKSTART.md](QUICKSTART.md) | 快速开始 |
| [DESIGN.md](DESIGN.md) | 架构与设计 |
| [DIRECTORY_STRUCTURE.md](DIRECTORY_STRUCTURE.md) | 目录结构 |
| [PROJECT_SUMMARY.md](PROJECT_SUMMARY.md) | 项目总结 |
| [CHANGELOG.md](CHANGELOG.md) | 变更记录 |

> `PROJECT_WORKFLOW.md` 仅保存在本地，不提交仓库。

## 常用命令

```bash
# OpenClaw（默认）
python harness_automation.py --config configs/config_simple.json

# Hermes / OpenCode / Claude-Code / Openjiuwen
python harness_automation.py --harness hermes --config configs/config_simple.json
python harness_automation.py --harness opencode --config configs/config_opencode.json
python harness_automation.py --harness claudecode --config configs/config_simple.json
python harness_automation.py --harness openjiuwen --config configs/config_simple.json

# OpenCode 专项测试
python harness_automation.py --config configs/config_opencode.json
```

## 测试

```bash
python -m unittest discover -s test -p '*_test.py' -v
```
