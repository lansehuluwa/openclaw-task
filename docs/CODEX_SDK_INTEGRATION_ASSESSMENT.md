# Codex Python SDK 集成说明

项目固定使用 `openai-codex==0.144.4`，要求 Python 3.10+。SDK 默认安装并
启动同版本的 Codex CLI 运行时，无需预装系统级 CLI。仅在显式配置 CodexConfig(codex_bin=...) 时使用外部 CLI，建议外部 CLI 与 SDK 保持相同版本。

## 1. 接入方式

项目通过官方 `AsyncCodex` 接入 Codex app-server。一个自动化实例共用一个
app-server；每个 `(agent_name, session_name)` 使用独立 thread，每轮 query 对应
一个 turn。

`src/codex_client.py` 负责 provider、模型和 workspace 选择、thread 复用、结果
转换、重试及资源回收。prompt 直接传给 SDK，不拼接 shell 命令。

## 2. Codex 配置

部署流程启动前将配置写入 `$CODEX_HOME/config.toml`。项目只接收
`codex_home` 路径，不生成或修改 Codex 配置。

自定义 provider 必须提供完整的 Responses API。`wire_api` 仅支持 `responses`，
省略时也是该默认值。Chat Completions、Anthropic Messages、Gemini 等协议不能
直接接入；仅提供同名 `/v1/responses` 路径也不代表兼容。

Codex 不会从中转服务自动发现模型。使用当前 Codex CLI 已识别的官方 GPT 模型
ID 时，无需配置 `model_catalog_json`；使用自定义模型名、非 OpenAI 模型或当前
CLI 尚未收录的模型时，生产环境应提供该目录并保证模型能力信息准确。参见
[OpenAI 官方配置说明](https://learn.chatgpt.com/docs/config-file/config-advanced#custom-model-providers)。

### 2.1 GPT 中转服务

一个 `config.toml` 可以同时声明多个 Responses 兼容 provider。顶层
`model` / `model_provider` 是调用方未指定时使用的默认值；provider 的书写顺序
没有默认含义。

```toml
# 默认模型 ID：必须是默认 provider 实际暴露的模型名。
model = "gpt-5.6-terra"
# 默认 provider ID：必须对应下面某个 model_providers.<id>。
model_provider = "primary_gateway"
# 默认不允许模型请求人工审批。
approval_policy = "never"
# 默认只允许在当前 workspace 内写文件。
sandbox_mode = "workspace-write"
# 默认关闭联网搜索；这与模型服务的 Responses 兼容性无关。
web_search = "disabled"

# Provider 1：主中转服务。primary_gateway 是供 model_provider 引用的唯一 ID。
[model_providers.primary_gateway]
# 日志和界面中显示的名称，不参与路由。
name = "Primary GPT Gateway"
# Responses API 的基础地址，按供应商实际前缀填写，不写具体模型名。
base_url = "https://primary.example.com/v1"
# 保存 API key 的环境变量名，不是 API key 本身。
env_key = "PRIMARY_CODEX_API_KEY"
# Codex 使用 Responses 协议访问该服务。
wire_api = "responses"

# Provider 2：备用或另一供应商的中转服务。
[model_providers.backup_gateway]
name = "Backup GPT Gateway"
base_url = "https://backup.example.com/v1"
env_key = "BACKUP_CODEX_API_KEY"
wire_api = "responses"
```

项目任务配置可以覆盖上述默认值，并为不同 Agent 选择不同服务：

```json
{
  "agents": [
    {
      "name": "primary-agent",
      "model": "gpt-5.6-terra",
      "model_provider": "primary_gateway"
    },
    {
      "name": "backup-agent",
      "model": "gpt-5.5",
      "model_provider": "backup_gateway"
    }
  ]
}
```

这里的 `model_provider` 必须与 TOML 表名中的 ID 完全一致；`model` 则必须是该
供应商实际提供的模型 ID。增加更多供应商时，继续添加独立的
`[model_providers.<id>]` 即可。API key 只通过各自的 `env_key` 环境变量注入。

### 2.2 DeepSeek

DeepSeek 模型不在 Codex 内置 GPT 模型目录中。生产环境为稳定复现，应额外指定
模型目录：

```toml
model = "deepseek-v4-flash"
model_provider = "deepseek"
model_reasoning_effort = "high"
model_catalog_json = "/etc/codex/deepseek-models.json"
approval_policy = "never"
sandbox_mode = "workspace-write"
web_search = "disabled"

[model_providers.deepseek]
name = "DeepSeek"
base_url = "https://api.deepseek.com/"
env_key = "DEEPSEEK_API_KEY"
wire_api = "responses"
```

模型 ID、目录内容和服务能力必须一致。模型目录只描述能力，不能转换 API 协议。

## 3. Workspace 与会话

- 每个 agent 使用独立 workspace，thread(codex sdk逻辑会话) 的 `cwd` 指向该目录；
- agent 配置文件合并写入 `AGENTS.md`，`system_prompt` 作为 thread 开发者指令，
  skill 复制到该 workspace 的 `.agents/skills`；
- 模型绑定在 agent/thread，不绑定在 skill；
- 同一 session 复用 thread，并通过锁串行执行 turn；
- thread 映射只在当前 Python 进程内有效，不支持跨进程恢复；
- turn 超时或取消时先中断，无法确认终止则关闭 SDK，避免后台继续写文件。

## 4. 运行与验收

```bash
python -m pip install openai-codex==0.144.4
export COMPANY_CODEX_API_KEY='<API Key>'
python harness_automation.py --config <任务配置.json>
```

任务配置中的 `model_provider` 必须与 `config.toml` 中的 provider ID 一致。

```bash
# 离线生命周期回归
python test/test_codex_client.py

# 自定义 provider 真实 E2E
export CODEX_E2E_HOME='/path/to/preconfigured/codex-home'
export CODEX_E2E_MODEL='gpt-5.6-terra'
export CODEX_E2E_PROVIDER='company_responses'
python test/test_codex_integration.py
```
