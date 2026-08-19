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

```toml
model = "gpt-5.6-terra"
model_provider = "company_responses"
approval_policy = "never"
sandbox_mode = "workspace-write"
web_search = "disabled"

[model_providers.company_responses]
name = "Company Responses Service"
base_url = "https://llm.example.com/v1"
env_key = "COMPANY_CODEX_API_KEY"
wire_api = "responses"
```

`model`、`base_url` 和环境变量名按实际服务修改，API key 只通过环境变量注入。

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
python test/test_codex_integration.py
```

当前真实 E2E 验证 Responses 调用、多 provider、skill、多轮 thread 复用和临时
`CODEX_HOME` 隔离；超时回收由离线测试覆盖。生产验收还应检查错误信息不泄露
API key。

## 5. 当前边界

- 当前只记录最终文本、usage、thread ID 和文件证据，原生工具事件尚未写入
  trajectory；
- Responses 兼容性不能由静态配置证明，必须对生产 provider 做真实 E2E；
- 操作系统级子进程回收仍需在目标 Linux 服务器验收。
