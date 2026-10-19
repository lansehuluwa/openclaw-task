"""通过官方 CLI 同款 /tui 协议接入 JiuwenSwarm，不依赖其 Python 运行时。

复用公共工作区、查询循环、OpenCode 返回类型及凭证遮蔽逻辑。
服务端管理模型与原生技能；每个逻辑 Agent/session 复用真实 session_id。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from pathlib import Path

import aiohttp

from src.config import warn_agent_model_conflict
from src.opencode_client import ExecutionOptions, ExecutionResult, _redact_error_text
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")


class JiuwenSwarmError(RuntimeError):
    """TUI 连接、协议或 Agent 执行失败。"""


def resolve_jiuwenswarm_workspace_root() -> Path:
    """与其他独立 harness 一致，使用环境变量指定工作区。"""
    return Path(os.environ.get(
        "JIUWENSWARM_HARNESS_WORKSPACE", "~/.jiuwenswarm/harness-workspaces",
    )).expanduser().resolve()


async def build_jiuwenswarm_client(workspace_manager):
    """读取部署环境配置，不向公共配置模型增加 Agent 专属字段。"""
    return JiuwenSwarmClient(
        os.environ.get("JIUWENSWARM_GATEWAY_URL", "ws://127.0.0.1:19001/tui"),
        os.environ.get("JIUWENSWARM_MODE", "code.normal"),
        workspace_manager,
    )


def make_jiuwenswarm_get_agent(client, evaluator_names=()):
    """评估器每次使用干净上下文，其他 Agent 继续复用会话。"""
    evaluators = set(evaluator_names)

    def get_agent(agent_name, session_name):
        if agent_name in evaluators and session_name.startswith("eval_"):
            return JiuwenSwarmAgent(client, agent_name, session_name)
        return client.get_agent(agent_name, session_name)

    return get_agent


class JiuwenSwarmWorkspaceManager(BaseWorkspaceManager):
    """本项目的配置、技能和输入文件按 Agent 隔离，不改服务端全局文件。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser().resolve()

    def get_agent_workspace(self, agent_name: str) -> Path:
        workspace = self.base_dir / agent_name
        workspace.mkdir(parents=True, exist_ok=True)
        return workspace

    def _copy_agent_configs(self, workspace, config_files, agent_dir) -> None:
        for name in config_files:
            source = Path(agent_dir).expanduser() / name
            if source.exists():
                target = workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                copy_path(source, target)
            else:
                logger.warning("Agent 配置文件不存在: %s", source)


class JiuwenSwarmClient:
    def __init__(self, gateway_url: str, mode: str, workspace_manager):
        self.gateway_url = gateway_url
        self.mode = mode
        self.workspace_manager = workspace_manager
        self._configs = {}
        self._models = {}
        self._agents = {}
        # 兼容只允许一个 TUI 客户端的服务版本；包括 evaluator 在内统一排队。
        self._lock = asyncio.Lock()
        self._http = None

    async def __aenter__(self):
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=30),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self._http.close()

    def setup_agent(self, config, override=None) -> None:
        self._configs[config.name] = config
        if override:
            warn_agent_model_conflict(config.name, config.model, override)
        self._models[config.name] = (
            override.resolved_model if override and override.model else config.model
        )
        if override and (override.base_url or override.api_key or override.api):
            logger.warning(
                "JiuwenSwarm Agent '%s' 的模型选择已生效；"
                "服务地址、密钥和协议请配置在 JiuwenSwarm 的 config.yaml 中",
                config.name,
            )

    def get_agent(self, agent_name: str, session_name: str):
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = JiuwenSwarmAgent(self, agent_name, session_name)
        return self._agents[key]


class JiuwenSwarmAgent:
    def __init__(self, client: JiuwenSwarmClient, agent_name: str, session_name: str):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = f"harness-{uuid.uuid4().hex}"
        self._initialized = False

    def _prompt(self, query: str) -> str:
        if self._initialized:
            return query
        config = self._client._configs[self.agent_name]
        workspace = self._client.workspace_manager.get_agent_workspace(self.agent_name)
        parts = [config.system_prompt] if config.system_prompt else []
        for name in config.config:
            path = workspace / name
            if path.is_file():
                parts.append(f"## {name}\n{path.read_text(encoding='utf-8')}")
        # 原生技能库位于服务端全局目录；项目技能用显式路径提供，保持 Agent 隔离。
        for name in config.skills:
            path = self._client.workspace_manager.get_skills_dst(workspace) / Path(name).name / "SKILL.md"
            if path.is_file():
                parts.append(f"## 项目技能：{path}\n{path.read_text(encoding='utf-8')}")
        return "\n\n".join([*parts, query])

    def _request(self, method: str, **params) -> dict:
        return {
            "type": "req", "id": uuid.uuid4().hex, "method": method,
            "is_stream": method == "chat.send",
            "params": {"session_id": self.session_key, "mode": self._client.mode, **params},
        }

    async def _model_params(self, ws) -> dict:
        """按本次 Agent 配置选择模型，不切换服务端全局默认值。"""
        selected = (self._client._models.get(self.agent_name) or "").strip()
        if not selected:
            return {}
        model_id = selected.removeprefix("model:").strip() if selected.startswith("model:") else None
        if model_id == "":
            raise JiuwenSwarmError("model: 后必须填写服务端配置的 model_id")

        request = self._request("models.list")
        await ws.send_json(request)
        while True:
            frame = await ws.receive_json()
            if frame.get("type") != "res" or frame.get("id") != request["id"]:
                continue
            if frame.get("ok") is False:
                raise JiuwenSwarmError(str(frame.get("error") or "无法读取 JiuwenSwarm 模型列表"))
            models = (frame.get("payload") or {}).get("models") or []
            break
        if model_id is not None:
            if not any(m.get("model_id") == model_id for m in models):
                raise JiuwenSwarmError("服务端未提供该 model_id，请使用 config.yaml 中的唯一 alias")
            return {"model_selection": {"type": "model", "id": model_id}}
        # 别名优先，允许多个服务配置同名模型。
        matches = [m for m in models if selected == m.get("alias")]
        if not matches:
            matches = [m for m in models if selected == m.get("model_name")]
        if not matches and "/" in selected:
            provider, name = selected.split("/", 1)
            candidates = [m for m in models
                          if (m.get("model_provider") or "").casefold() == provider.casefold()]
            matches = [m for m in candidates if name == m.get("alias")]
            if not matches:
                matches = [m for m in candidates if name == m.get("model_name")]
        if len(matches) != 1:
            raise JiuwenSwarmError(
                f"JiuwenSwarm 模型 '{selected}' 未配置或名称不唯一；"
                "请在 config.yaml 配置唯一 alias，或使用 model:<model_id>"
            )
        model = matches[0]
        if model.get("model_id"):
            return {"model_selection": {"type": "model", "id": model["model_id"]}}
        return {"model_name": model.get("alias") or model["model_name"]}

    async def execute(self, query: str, options: ExecutionOptions = None) -> ExecutionResult:
        timeout = getattr(options, "timeout_seconds", None) or 3600
        # 每轮使用独立连接，避免上一轮终止后的尾随事件被下一轮误读。
        async with self._client._lock:
            try:
                async with asyncio.timeout(timeout):
                    async with self._client._http.ws_connect(
                        self._client.gateway_url, max_msg_size=8 * 2**20,
                    ) as ws:
                        try:
                            ack = await ws.receive_json()
                            if ack.get("type") != "event" or ack.get("event") != "connection.ack":
                                raise JiuwenSwarmError("未收到 TUI connection.ack")
                            workspace = str(self._client.workspace_manager.get_agent_workspace(self.agent_name))
                            prompt = self._prompt(query)
                            model_params = await self._model_params(ws)
                            await ws.send_json(self._request(
                                "chat.send", content=prompt, query=prompt,
                                cwd=workspace, project_dir=workspace, trusted_dirs=[workspace],
                                supports_user_interaction=False,
                                agent_ref={"mode": self._client.mode, "id": "default"},
                                **model_params,
                            ))
                            result = await self._receive(ws)
                            self._initialized = True
                            return result
                        except (Exception, asyncio.CancelledError):
                            # 先通知服务端停止，单纯断开连接不能保证后台任务已取消。
                            if not ws.closed:
                                try:
                                    await asyncio.wait_for(ws.send_json(self._request("chat.interrupt")), 2)
                                except (Exception, asyncio.CancelledError):
                                    pass
                            raise
            except asyncio.TimeoutError:
                return ExecutionResult(success=False, stop_reason="timeout", error_message=f"JiuwenSwarm 执行超过 {timeout} 秒")
            except (aiohttp.ClientError, OSError, ValueError, TypeError, JiuwenSwarmError) as exc:
                return ExecutionResult(success=False, stop_reason="error", error_message=_redact_error_text(str(exc)))

    async def _receive(self, ws) -> ExecutionResult:
        content = ""
        messages = []
        usage = None
        mode = self._client.mode
        team_mode = mode.startswith("team") or mode in ("code.team", "agent.team")
        while True:
            frame = await ws.receive_json()
            if frame.get("type") == "res":
                if frame.get("ok") is False:
                    raise JiuwenSwarmError(str(frame.get("error") or frame.get("payload")))
                continue
            if frame.get("type") != "event":
                continue
            payload = frame.get("payload") or {}
            if payload.get("session_id") not in (None, "", self.session_key):
                continue
            event = frame.get("event")
            inner = payload.get("event_type")
            if event == "chat.error" or inner == "team.error":
                raise JiuwenSwarmError(str(payload.get("error") or payload.get("message") or "Agent 执行失败"))
            if event in ("chat.ask_user_question", "plan.approval_required"):
                raise JiuwenSwarmError("当前为非交互执行，Agent 请求人工输入或审批；请使用普通模式或在 TUI 中处理")
            if event == "chat.delta":
                content += payload.get("content") or ""
            elif event == "chat.llm_usage" or (event == "chat.final" and inner == "chat.llm_usage"):
                usage = payload
            elif event == "chat.tool_call":
                tool = payload.get("tool_call") or payload
                arguments = tool.get("arguments", tool.get("input", {}))
                messages.append({"role": "assistant", "tool_calls": [{
                    "id": tool.get("tool_call_id") or tool.get("call_id") or tool.get("id"),
                    "function": {
                        "name": tool.get("tool_name") or tool.get("name"),
                        "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                    },
                }]})
            elif event == "chat.tool_result":
                messages.append({
                    "role": "tool", "tool_call_id": payload.get("tool_call_id") or payload.get("call_id"),
                    "content": payload.get("result", payload.get("output", "")),
                })
            elif event == "chat.final" and inner in (None, "", "chat.final"):
                content = payload.get("content") or content
                if not team_mode:
                    break
            elif event == "chat.processing_status" and payload.get("is_processing") is False:
                break
        if not content:
            raise JiuwenSwarmError("JiuwenSwarm 未返回回复内容")
        messages.append({"role": "assistant", "content": content})
        return ExecutionResult(content=content, usage=usage, messages=messages)


async def execute_jiuwenswarm(agent: JiuwenSwarmAgent, query_text: str, options):
    """复用公共执行器约定；失败可能已有副作用，不自动重放。"""
    result = await agent.execute(query_text, options)
    if result.success and result.content:
        return result, (result.stop_reason or "complete") != "complete"
    raise JiuwenSwarmError(result.error_message or "JiuwenSwarm 返回空结果")
