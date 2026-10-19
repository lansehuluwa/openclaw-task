# JiuwenSwarm 集成说明

项目通过官方 JiuwenSwarm CLI 同款 `/tui` WebSocket 协议运行 JiuwenSwarm Agent。使用前先配置自己的模型服务，再通过 `agents[].model` 为编写、审查等任务指定不同模型。

## 部署前置条件

在独立 Python 环境安装 JiuwenSwarm，避免其 OpenJiuwen 依赖与本项目固定版本冲突。本项目的依赖文件不作修改。

```bash
pip install workswarm==0.2.7
jiuwenswarm-init
```

## 1. 接入方式

每次 harness 运行创建一个 `JiuwenSwarmClient`，每个 `(agent_name, session_name)` 对应一个独立的 JiuwenSwarm Agent：

- 首轮通过 `chat.send` 创建原生会话，传入独立的 `session_id`、工作目录和任务内容；
- 每次调用根据当前 Agent 的模型配置传入 `model_name` 或 `model_selection`，可分别使用不同服务；
- 后续轮次使用相同 `session_id` 延续上下文；
- 每轮建立一个 WebSocket 连接，接收文本、工具调用和工具结果事件；
- 超时、取消或错误时尝试发送 `chat.interrupt`，失败请求不会自动重试。

默认模式为 `code.normal`，也可选择服务端原生的 `agent`、`team`、`code.team` 等模式。
团队模式等待 `chat.processing_status.is_processing=false` 后结束本轮；若服务端未发出完成信号，则等待到任务超时。

`JiuwenSwarmClient` 使用非交互调用，请求中设置 `supports_user_interaction=false`。
收到人工输入或规划审批请求时，本轮明确失败，需要在原生 TUI 中处理或改用普通模式。
各 Agent 的请求串行发送，以兼容单客户端 TUI 通道。

## 2. JiuwenSwarm 配置

模型的服务地址、协议和凭据写入 JiuwenSwarm 原生配置文件；任务配置引用其中的模型别名或 ID。
每个 Agent 可以选择不同服务、不同模型，未指定时使用服务端默认模型。

### 2.1 配置多个服务

第一步，编辑初始化生成的 `~/.jiuwenswarm/config/config.yaml`。将自己的服务写入
`models.defaults`，保留其他配置。下面包含两个服务、三个模型配置；替换服务地址、模型名和密钥，示例中的密钥已隐藏。

```yaml
models:
  defaults:
    # 服务一：DSH Desktop 使用的 DeepSeek 服务
    - model_id: mdl_deepseek_flash
      alias: deepseek-flash
      is_default: true
      model_client_config:
        client_provider: OpenAI
        model_name: deepseek-flash
        api_base: https://api.deepseek.com
        api_key: "***"
        endpoint_profile: openai
      model_config_obj: {}

    # 服务二：审查服务
    - model_id: mdl_reviewing
      alias: reviewing
      is_default: false
      model_client_config:
        client_provider: OpenAI
        model_name: your-review-model
        api_base: https://review.example.com/v1
        api_key: "***"
        endpoint_profile: openai
      model_config_obj: {}

    # 服务二的另一个模型
    - model_id: mdl_review_flash
      alias: review-flash
      is_default: false
      model_client_config:
        client_provider: OpenAI
        model_name: deepseek-flash
        api_base: https://review.example.com/v1
        api_key: "***"
        endpoint_profile: openai
      model_config_obj: {}
```

`model_id` 和 `alias` 各自保持唯一，`is_default: true` 只设置一项。
不同服务提供同名模型时，通过 `alias` 区分。修改后启动或重启 JiuwenSwarm：

```bash
jiuwenswarm-start app
```

连接地址和执行模式由运行本项目的终端环境变量指定：

| 环境变量 | 默认值 | 用途 |
| --- | --- | --- |
| `JIUWENSWARM_GATEWAY_URL` | `ws://127.0.0.1:19001/tui` | TUI 网关地址 |
| `JIUWENSWARM_MODE` | `code.normal` | 服务端原生执行模式 |
| `JIUWENSWARM_HARNESS_WORKSPACE` | `~/.jiuwenswarm/harness-workspaces` | 本项目的 Agent 工作区根目录 |

### 2.2 在任务中选择模型

通过 `agents[].model` 引用第 2.1 节的模型别名。本例均使用 `deepseek-flash`；
需要不同模型分工时，将 `reviewer.model` 改为 `reviewing` 或 `review-flash`。

```json
{
  "harness_type": "jiuwenswarm",
  "agents": [
    {
      "name": "main",
      "model": "deepseek-flash",
      "system_prompt": "你是一个编码助手。"
    },
    {
      "name": "reviewer",
      "model": "deepseek-flash",
      "system_prompt": "你负责代码审查。"
    }
  ],
  "queries": [
    {
      "agent_name": "main",
      "session_name": "main",
      "text": "编写一个计算器模块，并说明实现。",
      "timeout": 1800,
      "use_simulator": false
    },
    {
      "agent_name": "reviewer",
      "session_name": "review",
      "text": "审查这份实现，指出问题并给出建议：{result_main}",
      "timeout": 1800,
      "use_simulator": false
    }
  ]
}
```

## 3. Workspace 与会话

工作区按 Agent 名称隔离，配置、技能和用户文件自动复制到对应目录。
以下以主 Agent 的两个 session，以及 Evaluator 的两次评估为例：

```text
~/.jiuwenswarm/
├── harness-workspaces/                  # 任务执行目录
│   ├── main/                           # 主 Agent 的工作目录
│   │   ├── SOUL.md
│   │   ├── USER.md
│   │   └── skills/demo-skill/SKILL.md
│   └── evaluator/                      # Evaluator 的工作目录
└── agent/
    └── sessions/                       # JiuwenSwarm 原生会话记录
        ├── harness-<ID_A>/              # main：session_name=task1
        ├── harness-<ID_B>/              # main：session_name=task2
        ├── harness-<ID_C>/              # evaluator：第 1 次评估
        └── harness-<ID_D>/              # evaluator：第 2 次评估
```

`<ID_*>` 为自动生成的随机标识。同一次运行中，`task1` 的后续轮次复用 `ID_A`，
`task2` 使用独立的 `ID_B`；两者的任务文件都保存在 `harness-workspaces/main/`。
Evaluator 每次评估创建新会话，对应 `ID_C`、`ID_D`，工作目录均为 `harness-workspaces/evaluator/`。

通过 `JIUWENSWARM_HARNESS_WORKSPACE` 修改任务工作区根目录；
原生会话记录由 JiuwenSwarm 管理，任务 JSON 的 `workspace_base` 对此接入不生效。

`system_prompt`、配置文件和 `SKILL.md` 在首轮注入，JiuwenSwarm 服务需能访问相同文件路径。
原生技能库和技能演进由 JiuwenSwarm 管理。

## 4. 运行与验收

保持第 2.1 节的服务运行，按项目通用配置准备模拟用户和评估器后，执行以下测试：

```bash
python harness_automation.py --harness jiuwenswarm --config configs/config_simple.json
python harness_automation.py --harness jiuwenswarm --config configs/config_user.json
python harness_automation.py --harness jiuwenswarm --config configs/config_simple_eval.json
```

`--harness` 覆盖任务文件的 `harness_type`；运行自己的任务时，将 `--config` 改为对应文件路径。
测试时避免与手动 TUI 抢占连接。

第二项应输出 `【Task_Done】`；第三项还应输出 Evaluator 评分，轨迹保存到
`logs/trajectories/<run_id>/eval_test.json`。原生工具次数和 token 统计尚未适配，
当前使用通用回退统计（`fallback: true`）。

更多操作参见 [JiuwenSwarm 官方终端 CLI 文档](https://github.com/openJiuwen-ai/jiuwenswarm/blob/develop/docs/zh/命令行指令.md)。
