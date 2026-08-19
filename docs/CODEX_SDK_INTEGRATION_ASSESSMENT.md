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
`model_catalog_json`；自定义模型名、非 OpenAI 模型或 CLI 尚未收录的模型需要
提供模型目录。

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

不配置 Agent 覆盖时，任务直接使用上述顶层默认值。需要为不同 Agent 选择服务时，
在项目已有的 `simulator_config` JSON 中配置 `model` 和 `provider`：

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

DeepSeek 除 Responses 兼容 provider 外，还需模型目录描述模型能力：

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

- 每个 Agent 使用独立 workspace，thread 的 `cwd` 指向该目录；
- `system_prompt` 作为 thread 的开发者指令；
- Agent 配置文件按原文件名复制，不合并；只有 `AGENTS.md` 会被 Codex 自动加载；
- skill 复制到 workspace 的 `.agents/skills`；
- 每个 query 对应一个 turn，外层执行器串行调用，不额外加锁；
- 超时或取消时调用 `turn.interrupt()`，失败 turn 不自动重放。

## 4. 运行与验收

```bash
python -m pip install openai-codex==0.144.4

python harness_automation.py --harness codex --config configs/config_simple.json
python harness_automation.py --harness codex --config configs/config_user.json
python harness_automation.py --harness codex --config configs/config_simple_eval.json

python test/test_codex_client.py
```
