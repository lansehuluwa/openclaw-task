# OpenCode CLI 集成与任务隔离测试报告（精简版）

> - 初版日期：2026-08-10 ｜ 精简日期：2026-08-11
> - 本机 OpenCode 版本：`1.18.16` ｜ 平台：Windows / Python 3.12
> - 关联文档：[项目完整工作流程](./PROJECT_WORKFLOW.md)

## 0. 执行结论

`opencode run --format json` 适合作为本项目低并发 MVP transport：CLI 启动、NDJSON 解析、同一会话续接、按规范化 Agent 名分目录均已实现并验证。但"完成接口接线"不等于"严格任务隔离"：

| 能力 | 结论 |
|---|---|
| CLI 启动、NDJSON 解析 | 已验证可用 |
| 同一逻辑会话内续接（`--session`） | 已验证可用 |
| Agent skills 落入 `.opencode/skills` | 已实现 |
| 任务级 workspace 隔离 / 旧文件清理 | **未实现** |
| 全局配置/插件/skills/DB 隔离 | **未实现**（已有零代码方案，见 §4） |
| Evaluator 逐轮无状态 | **未实现**（无 gateway 时 reset 失效） |
| 工具轨迹审计 | **不完整**（`tool_use` 被解析器忽略） |

正式并发运行多个任务前，必须先引入稳定的 `task_id`、任务级 workspace、精确 skills 同步和任务级产物路径。

## 1. 与本项目直接相关的 CLI 能力

```text
run                 非交互执行消息（本项目使用）
serve / attach      启动 Server / 连接已运行 Server
session             列出/删除会话；export / import 导出导入
db / models / stats 查询会话库、模型与用量
agent / plugin      原生 Agent / 插件管理
```

`opencode run` 参数：

| 参数 | 含义 | 集成 |
|---|---|---|
| `--format json` | NDJSON 原始事件输出 | 使用 |
| `--dir <path>` | 指定运行目录 | 使用 |
| `--session <id>` | 精确续接会话 | 第二轮起使用 |
| `--model <model>` | 覆盖模型 | 按 Agent 可选使用 |
| `--auto` | 自动批准权限 | **不使用**（避免扩大权限） |
| stdin | 输入 prompt | 使用（不经 shell 拼接，无长度限制） |

`text` 事件在 text part 完成后发出（非逐 token 流）；`tool_use` 在工具完成或失败后发出。Headless 下需要人工确认的权限请求默认自动拒绝，只有 `--auto` 或显式权限配置才会放行——纯问答通过不代表编码/工具任务一定可用。

## 2. 当前调用链

```text
HarnessAutomation → OpenCodeClient → OpenCodeAgent.execute
  └─ opencode run --format json --dir <workspace> [--model ...] [--session ...]
  └─ _parse_run_output: text → content；step_finish → usage；sessionID → 句柄内存缓存
```

关键实现：`src/opencode_client.py` 的 `_build_command` / `_build_prompt` / `execute` / `_parse_run_output`。

### 会话映射

- 以 `(agent_name, session_name)` 缓存句柄；首轮不传 `--session`，从事件中取真实 sessionID 后续精确续接。
- 真实 sessionID 仅存于 Python 进程内；重启后不能恢复逻辑映射。

### 模型与凭证

- 各 agent 的模型在 `simulator_config`（如 `user_proxy_model.json`）中配置，写模型名即可（如 `deepseek-v4-flash`），harness 自动适配，兼容任意服务与模型，无需关心底层细节。
- 模型无法匹配到已声明的服务时，告警并回退 OpenCode 默认。
- Agent 命令行不携带 API Key 或 endpoint；User Simulator 走独立的 OpenAI 兼容调用链。

## 3. Skills 与规则加载语义

- 技能落盘位置：`<workspace>/.opencode/skills/<name>/SKILL.md`（**不是** `<workspace>/skills`）；`skills_subdir` 定义于 `OpenCodeWorkspaceManager`。
- 技能**按需加载**：opencode 先向模型广告技能名与描述，模型调用 `skill` 工具后完整 `SKILL.md` 正文才进入会话。验证分两层：发现测试（能列出）≠ 调用测试（真实执行了 SKILL.md 指令）。
- `AGENTS.md` 默认来源：工作目录向上的项目级规则、全局 `~/.config/opencode/AGENTS.md`、`opencode.json` 显式 `instructions`；`.opencode/AGENTS.md` 不是默认规则位置。
- `system_prompt` 当前只在首轮与 query 拼成普通 user 消息，角色语义不等价于系统指令；目标实现应写入任务专属 `AGENTS.md`。

## 4. 隔离：现状、方案与契约

**现状**：会话新建/续接/DB 落盘/目录归属均实测通过；但**同名 Agent 跨任务共享 workspace**——任务 B 会继承任务 A 的 skill、AGENTS.md 和文件。这是**全项目统一特性**：OpenCode / Hermes / Claude Code / OpenJiuwen / OpenClaw 五个后端的 workspace 都仅按 agent_name 区分、不按任务/run 隔离，属预期行为，暂不改变；需要强隔离时按下方方案处理。当前隔离键 `(agent_name, session_name + run_id)` 只保证同进程内区分逻辑会话；Evaluator 无 gateway 时 reset 失效（旧判词可进入下一轮）；日志/trajectory/stats 固定路径、同名配置会覆盖。

**完全隔离方案（每 Agent 独立，已实测可行）**——`OpenCodeAgentHandle.execute()` 按 agent 注入子进程 env 即可：

```text
XDG_CONFIG_HOME=<runs>/<run>/<agent>/xdg-config   # 独立全局配置（provider/密钥/skills/plugins/MCP）
XDG_DATA_HOME  =<runs>/<run>/<agent>/xdg-data     # 独立会话库 DB / log / repos
# 可选叠加：OPENCODE_CONFIG_DIR / OPENCODE_PURE=1 / OPENCODE_DISABLE_EXTERNAL_SKILLS=1
#          OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 / OPENCODE_DISABLE_PROJECT_CONFIG=1
```

代价：每个 Agent 需自带 provider/密钥（可用 `{file:...}` 引用共享密钥文件）、独立 DB 后无法单一 `session list`。定位：日常运行保留共享全局配置的开箱即用；严格评测/基准测试时启用环境级隔离。

**契约要点**：每任务显式 `task_id` + 高精度 `run_id`，磁盘状态显式二选一（`persistent` 复用记忆 / `clean-run` 每 run 独立，评测默认推荐）；skills/`AGENTS.md`/content_root 改为任务目录内精确同步（旧内容必须删除、map 目标限制在任务根内）；oracle/rubrics 不应通过删除共享源文件隔离（应放任务私有 vault，输入 hash 前后不变）。

## 5. 排障速查

```bash
opencode --version
opencode run --dir <ws> --format json "<prompt>"            # 单轮
opencode run --dir <ws> --format json --session <id> "..."  # 续接
cd <ws> && opencode session list --format json              # 会话列表
opencode export <sessionID>                                 # 导出会话（含工具调用轨迹）
opencode db path / opencode stats                           # 会话库位置 / 用量
```

数据库查询只用于版本绑定的排障，不应成为运行时依赖。官方资料：https://opencode.ai/docs/ （CLI / Skills / Rules / Config / Server / SDK）。
