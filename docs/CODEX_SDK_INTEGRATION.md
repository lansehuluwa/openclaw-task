# Codex Python SDK 集成说明

项目固定使用 `openai-codex==0.144.4`，要求 Python 3.10+。SDK 自带同版本
Codex CLI 运行时，无需预装系统级 CLI。

## 1. 接入方式

自动化启动时初始化一个 `AsyncCodex` app-server，整个 run 复用该实例。
`(agent_name, session_name)` 是进程内缓存键：同一键复用同一 Codex thread，
不同键创建独立 thread；进程退出后不恢复旧 thread。

SDK 的 `thread.run(query)` 是“创建 turn 并等待结果”的便捷方法。项目显式使用
`thread.turn(query)` + `turn.run()`，只为在超时或取消时取得 turn 句柄并调用
`interrupt()`；它不是 Python 多线程，与当前 asyncio 执行器兼容。

## 2. Codex 配置

部署前将配置写入标准路径 `~/.codex/config.toml`。项目只读取该配置，不生成、
复制或修改它。

自定义服务必须完整兼容 Responses API。`wire_api` 只使用 `responses`；
Chat Completions、Anthropic Messages、Gemini 等协议不能直接接入。

Codex 不会从中转服务自动发现模型。当前 CLI 已收录的 GPT 模型无需
`model_catalog_json`；自定义模型名、非 OpenAI 模型或 CLI 尚未收录的模型可用
模型目录显式描述上下文窗口、推理能力等信息。

### 2.1 GPT 中转服务

一个配置可声明多个 provider。顶层 `model` 和 `model_provider` 是默认值；
provider 的书写顺序没有默认含义。

```toml
# 未按 Agent 覆盖时使用的默认模型与 provider。
model = "gpt-5.6-terra"
model_provider = "primary_gateway"
approval_policy = "never"
sandbox_mode = "workspace-write"

# 主服务；primary_gateway 是任务配置引用的 provider ID。
[model_providers.primary_gateway]
name = "Primary GPT Gateway"                 # 展示名称
base_url = "https://primary.example.com/v1" # Responses API 基础地址
experimental_bearer_token = "primary-key"   # 明文 API key
wire_api = "responses"                       # 固定使用 Responses 协议

# 第二个供应商；配置方式相同，可继续增加更多 provider。
[model_providers.backup_gateway]
name = "Backup GPT Gateway"
base_url = "https://backup.example.com/v1"
experimental_bearer_token = "backup-key"
wire_api = "responses"
```

不配置 Agent 模型时，任务使用上述顶层默认值。`agents[].model` 可写成
`provider/model`；如果 `simulator_config` 中存在同名 Agent，则其中的 `model` 和
`provider` 优先，并在与 `agents[].model` 不一致时记录警告：

```json
{
  "main": {
    "model": "gpt-5.6-terra",
    "provider": "primary_gateway"
  },
  "evaluator": {
    "model": "gpt-5.5",
    "provider": "backup_gateway"
  }
}
```

`provider` 必须与 `model_providers.<id>` 一致；Codex 不使用该 JSON 中的
`base_url` 和 `api_key`，连接信息统一来自 `config.toml`。

### 2.2 DeepSeek

DeepSeek 这类非内置模型除配置 Responses 兼容 provider 外，建议用模型目录显式
描述模型能力：

```toml
model = "deepseek-v4-flash"
model_provider = "deepseek"
model_catalog_json = "/etc/codex/deepseek-models.json"
approval_policy = "never"
sandbox_mode = "workspace-write"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com/"
experimental_bearer_token = "deepseek-key"
wire_api = "responses"
```

模型目录只描述能力，不能把其他 API 协议转换成 Responses。

## 3. Workspace 与会话

- Codex 固定使用 `~/.codex/workspace`，任务 JSON 中的 `workspace_base` 不改变该路径；
- `~/.codex/workspace/<agent>` 是 Agent 模板目录，`<agent>` 直接使用配置中的
  `agent_name`；配置文件和 skill 先放入模板，首次创建 thread 时再复制到实际
  session 目录；
- 普通 Agent 的运行时 session 名为
  `<query.session_name 或 main>_<run_id>`；evaluator 为
  `eval_<evaluate.session_name 或 query.session_name 或 main>_<run_id>`。
  `run_id` 是进程启动时间，格式为 `YYYYMMDDTHHMMSS`；
- thread 的实际 `cwd` 为
  `~/.codex/workspace/<agent>/.sessions/<运行时 session 名>`。例如一次运行的
  `run_id=20260820T104927`，query 使用 `agent_name=main`、
  `session_name=eval_test`，则路径为：

  ```text
  主 Agent 模板：~/.codex/workspace/main
  主 Agent cwd：~/.codex/workspace/main/.sessions/eval_test_20260820T104927
  Evaluator 模板：~/.codex/workspace/evaluator
  Evaluator cwd：~/.codex/workspace/evaluator/.sessions/eval_eval_test_20260820T104927
  ```

- session 的路径片段只保留字母、数字、`.`、`_`、`-`，其他字符替换为 `-`；
- 当前进程内，相同 `(agent_name, 运行时 session 名)` 复用同一个 thread；session
  目录保留文件，但进程退出后不会恢复旧 thread 或对话上下文；
- `system_prompt` 作为 thread 的开发者指令；
- Agent 配置文件按原文件名复制，不合并；只有 `AGENTS.md` 会被 Codex 自动加载；
- skill 位于实际 session 目录的 `.agents/skills`；
- 每次向 Agent 发起请求对应一个 turn；启用 User Simulator 后，一个 query 可以有
  多个 turn。外层执行器串行调用，不额外加锁；
- 超时或取消时调用 `turn.interrupt()`，失败 turn 不自动重放。

## 4. 运行与验收

```bash
python -m pip install openai-codex==0.144.4

python harness_automation.py --harness codex --config configs/config_simple.json
python harness_automation.py --harness codex --config configs/config_user.json
python harness_automation.py --harness codex --config configs/config_simple_eval.json

python test/test_codex_client.py
```