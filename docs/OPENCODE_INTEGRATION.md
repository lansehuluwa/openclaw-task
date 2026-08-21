# OpenCode 集成说明

> 本文档说明本项目如何通过本地 `opencode run --format json` 子进程接入
> OpenCode CLI 的设计——进程无状态，会话状态靠 --session <sessionID> 从本地 DB（~/.local/share/opencode/opencode.db）恢复。所以每次 execute 创建新进程 + --session 续接
> OpenCode，以及配置文件、workspace、skill、session 的管理方式。

## 1. 接入方式

实现位于 `src/opencode_client.py`

```text
HarnessAutomation
  └─ OpenCodeClient / OpenCodeAgent
       └─ 每次 execute 启动一个子进程：
            opencode run --format json
              --dir <agent workspace>
              --agent <opencode agent 名>
              [--session <真实 sessionID>]
              [--model <provider/model>]
```

- prompt 通过 stdin 传入，不走 shell 字符串拼接；
- 模型、provider、baseURL、apiKey 全部由 opencode.json 管理；
- harness 不读取 `configs/user_proxy_model.json`，也不向命令行传凭证；
- 失败重试 5 次、间隔 60s，与其他 harness（hermes / claude-code /
  openjiuwen）一致；重试时用 `--session <sessionID>` 续接同一会话；
- `step_finish.reason` 映射为 `stop_reason`：`stop` → `complete`，其余
  （如达到 maxTokens 的 `length`）原样透传，该轮会被标为「证据可能
  不完整」，与其它 harness 的 `evidence_incomplete` 语义对齐。

## 2. opencode.json 配置

生产环境在启动前手动配置好 opencode.json（默认读取全局`~/.config/opencode/opencode.json`。

一个“不同 skill 对应不同模型”的配置示例（当前测试环境已按此配置）：

```json
{
  "$schema": "https://opencode.ai/config.json",
  "instructions": ["SOUL.md"],
  "provider": {
    "deepseek": {
      "npm": "@ai-sdk/openai-compatible",
      "options": {
        "baseURL": "https://api.deepseek.com/v1",
        "apiKey": "{env:DEEPSEEK_API_KEY}"
      },
      "models": {
        "deepseek-v4-flash": { "name": "DeepSeek V4 Flash" },
        "deepseek-v4-pro": { "name": "deepseek-v4-pro" }
      }
    }
  },
  "agent": {
    "secret-checker": {
      "description": "执行 secret-checker 技能",
      "mode": "primary",
      "model": "deepseek/deepseek-v4-flash",
      "permission": {
        "skill": { "secret-checker": "allow", "*": "deny" }
      }
    },
    "agent-browser": {
      "description": "执行 agent-browser 技能",
      "mode": "primary",
      "model": "deepseek/deepseek-v4-pro",
      "permission": {
        "skill": { "agent-browser": "allow", "*": "deny" }
      }
    },
    "evaluator": {
      "description": "多轮任务评估器",
      "mode": "primary",
      "model": "deepseek/deepseek-v4-pro",
      "permission": {
        "skill": { "*": "deny" }
      }
    },
    "audit-assistant": {
      "description": "SOUL.md 加载验证（复用 agents/audit_assistant）",
      "mode": "primary",
      "model": "deepseek/deepseek-v4-flash",
      "permission": {
        "skill": { "*": "deny" }
      }
    },
    "multi-skill": {
      "description": "单 agent 多技能验证",
      "mode": "primary",
      "model": "deepseek/deepseek-v4-pro",
      "permission": {
        "skill": {
          "secret-checker": "allow",
          "agent-browser": "allow",
          "*": "deny"
        }
      }
    }
  }
}
```

要点：

- `baseURL` / `apiKey` 是 **provider 级别**配置，SKILL.md 本身不能带凭证；
- 一个 skill 对应一个 OpenCode agent，agent 的 `model` 决定走哪个 provider/model；
- `permission.skill` 限制该 agent 只能加载指定 skill；
- harness 配置里 `agents[].name` 必须与 opencode.json 中的 agent 名一致，
  因为 `--agent <name>` 直接使用这个名字。

## 3. Workspace

每个 Agent 使用独立 workspace：

```text
~/.config/opencode/workspace-<agent_name>/
├── .opencode/skills/<skill_name>/SKILL.md   # 该 agent 可用的技能
├── memories/                                 # 记忆目录
└── AGENTS.md / SOUL.md / ...                # agent 配置/规则文件
```

- 不用 `~/.opencode/workspace`：OpenCode 会把祖先目录里的 `.opencode`
  当成配置目录，导致 `~/.opencode` 被扫描并写入 package.json/node_modules；
- 执行时通过 `--dir <workspace>` 指定目录。`cd <workspace>` 后直接启动
  `opencode run` 效果等价（`run` 默认使用当前目录），但 harness 用 `--dir`
  不会污染 Python 进程的全局 cwd，且多 Agent 并发时更安全；
- workspace 目前是持久目录，同名 Agent 跨 run 会复用；需要严格隔离时再按
  run/task 重建目录。

## 4. Skill

harness 启动时会把 `input_dir.skill_dir` 下声明的技能复制到对应 workspace：

```text
skills/<skill_name>/  ->  <workspace>/.opencode/skills/<skill_name>/
```

OpenCode 的 SKILL.md 要求：

- 目录名、frontmatter `name` 必须一致，且为小写连字符格式；
- `name`、`description` 为必填；
- 技能按需加载，模型通过调用 `skill` 工具后才会读到完整正文。

注意：`skills/agent-browser/SKILL.md` 的 `name` 已由 `Agent Browser` 修正为
`agent-browser`，否则 OpenCode 无法按目录名发现该技能。

## 5. Session

- 每个 `(agent_name, session_name)` 在 Python 内存中对应一个 `OpenCodeAgent`
  句柄；
- 首轮执行后从 JSON 事件中记录真实 `sessionID`，后续轮次自动追加
  `--session <sessionID>` 续接；
- OpenCode 会话本体持久化在 `~/.local/share/opencode/opencode.db`，但
  harness 侧的“逻辑会话名 → 真实 sessionID”映射只存在于内存，进程重启后
  不会自动恢复；
- 需要管理历史会话时使用 `opencode session list/delete` 或
  `opencode export <sessionID>`。

## 6. 运行

```bash
# 全特性测试：SOUL.md 加载 + 单 agent 多技能 + simulator + evaluator
python harness_automation.py --config configs/config_opencode.json

# 也可用 --harness 覆盖
python harness_automation.py --harness opencode --config configs/config_opencode.json
```

`configs/config_opencode.json` 是唯一测试配置，覆盖：

- `audit-assistant`：验证 workspace 中的 `SOUL.md` 被加载（复用
  `agents/audit_assistant/SOUL.md`，要求回答中原样包含
  「资深财务审计助手」「审计师的智能协作伙伴」）；
- `multi-skill`：单 agent 同时加载 `secret-checker` 与 `agent-browser`
  两个技能，并启用 simulator + evaluator 验证多轮与评分。

## 7. md 文件与多技能测试

- `AGENTS.md`：OpenCode 会自动从 workspace 根目录加载；
- `SOUL.md`：**不会自动加载**，需要在 opencode.json 的 `instructions`
  中显式声明（示例中声明为 `["SOUL.md"]`，相对 workspace 查找）；
- 仓库 `agents/` 目录没有现成 `AGENTS.md`，只有 `SOUL.md`/`USER.md`，
  因此测试 md 加载复用 `agents/audit_assistant/SOUL.md`；
- `multi-skill`：一个 agent 同时加载 `secret-checker` 与 `agent-browser`
  两个技能，要求回答包含验证码并说明 agent-browser 用途；
- `secret-checker` 是本地测试技能，不提交仓库（已加入 .gitignore）。

手工验证命令：

```bash
python harness_automation.py --config configs/config_opencode.json
```

离线单测：

```bash
python -m unittest discover -s test -p 'opencode_client_test.py' -v
```

真实调用（需要 opencode.json 已配置且 CLI 可用）：

```bash
OPENCODE_REAL_TEST=1 python -m unittest discover -s test -p 'opencode_client_test.py' -v
```

## 8. 重构变更记录（2026-08-18）

### 8.1 opencode_client.py 重构

- **os.name 分支删除**：明确操作系统为 Linux，移除 Windows `taskkill`/`_CREATE_NO_WINDOW` 逻辑。进程清理统一使用 `os.killpg(os.getpgid(pid), SIGKILL)`（依赖 `start_new_session=True` 创建的独立进程组）。
- **`start_new_session=True` 保留**：每次 `execute()` 启动子进程时创建独立进程组，确保 `os.killpg` 能可靠清理子进程树（包括 opencode 派生的 node 子进程）。start_new_session=True 让 opencode 子进程树成为一个可被整体 kill 的独立单元，不影响 Python 父进程。
- **`_opencode_session_id` 正确使用**：首次 `execute()` 从 JSON 事件中解析 `sessionID` 并存储；后续 `execute()` 通过 `--session <sessionID>` 续接同一会话。进程崩溃后 `execute_with_retry` 重新调用 `execute` 重建进程，sessionID 跨重试保持。
- **`ExecutionResult.messages`**：新增 `messages` 字段，由 `_parse_run_output` 从 JSON 事件构建 OpenAI 格式的消息列表（含 tool_calls），供 evaluator 的 `extract_tool_calls_openai` 解析工具调用证据。

### 8.2 executor.py 后台检测机制重构

基于 OpenClaw 网关的 `sessions_yield` / `NO_REPLY` 协议重构后台检测：

- **触发条件**（`_background_watch_engaged`）：只检查本轮 `execute()` 产生的新消息中是否包含 `sessions_yield` toolResult。不再扫描整个 history 中的 `sessions_spawn`（旧逻辑会导致只要历史出现过 spawn 就永远进入后台监测）。
- **等待逻辑**（`_background_watch`）：轮询 `chat_history` 增量消息，使用 `_new_messages_since` 取增量、`_latest_assistant_text` 提取最新 assistant 文本。`NO_REPLY` 表示后台还在跑（继续等），非 `NO_REPLY` 文本表示后台完成（返回交付内容）。
- **while 循环**：`BG_NEW_CONTENT` 后不消耗 turn，暂存内容并更新基线继续轮询；直到超时（`BG_TIMEOUT`）才把最终累积内容交给 simulator。
- 改为模块级常量 `BG_WATCH_INTERVAL=30s` / `BG_WATCH_TIMEOUT=300`。