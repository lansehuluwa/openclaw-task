"""
OpenCode CLI 子进程客户端封装。

每个 ``execute()`` 启动一个 ``opencode run --format json`` 子进程：

- ``--dir <agent workspace>``：每个 Agent 独立工作区，技能基于该工作区；
- ``--agent <agent>``：由 opencode.json 中定义的 Agent 决定模型/技能；
- ``--session <id>``：续接 OpenCode 真实会话（首轮自动记录 sessionID）。

公开 API（与 hermes/claudecode 等 harness 对齐,供 harness_automation 统一装配）:
  OpenCodeClient / OpenCodeAgent / ExecutionResult / ExecutionOptions / OpenCodeError
  build_opencode_client()
  OpenCodeWorkspaceManager / OpenCodeAgentManager
  make_opencode_execute_with_retry / make_opencode_get_agent
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager, copy_path

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60

_CREATE_NO_WINDOW = 0x08000000
_ERROR_TEXT_LIMIT = 4000


class OpenCodeError(RuntimeError):
    """OpenCode CLI 调用失败。"""


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)

    def model_copy(self, *, update: Optional[Dict[str, Any]] = None) -> "ExecutionResult":
        data = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage
        }
        if update:
            data.update(update)
        return ExecutionResult(**data)


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


def _redact_error_text(text: str) -> str:
    """遮蔽常见凭证形式，仅保留有限诊断文本。"""
    redacted = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+",
        "Bearer <redacted>",
        text,
    )
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|authorization|auth[_-]?token|bearer)"
        r"(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        redacted,
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", redacted)
    return redacted[-_ERROR_TEXT_LIMIT:].strip()


def _event_error_message(event: Dict[str, Any]) -> Optional[str]:
    value = event.get("error") or event.get("message")
    part = event.get("part")
    if value is None and isinstance(part, dict):
        value = part.get("error")
    if isinstance(value, dict):
        # v1.18 实测:error 事件形如
        # {"error": {"name": "UnknownError",
        #            "data": {"message": "Unexpected server error...", "ref": "err_xxx"}}}
        # 可读信息在 data.message,依次回退顶层 message/name。
        data = value.get("data")
        data = data if isinstance(data, dict) else {}
        value = (
            data.get("message")
            or value.get("message")
            or value.get("name")
        )
        ref = data.get("ref")
        if ref:
            value = f"{value} (ref={ref})"
    if value is None:
        return None
    return _redact_error_text(str(value))

def _parse_run_output(stdout: str) -> Dict[str, Any]:
    """解析 ``opencode run --format json`` 的逐行 JSON 事件。"""
    session_id: Optional[str] = None
    text_order: List[str] = []
    text_by_part: Dict[str, str] = {}
    tool_order: List[str] = []
    tool_by_id: Dict[str, Dict[str, Any]] = {}
    usage: Optional[Dict[str, Any]] = None
    stop_reason: Optional[str] = None
    errors: List[str] = []
    valid_events = 0

    for index, raw_line in enumerate(stdout.splitlines()):
        line = raw_line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            errors.append(f"OpenCode 第 {index + 1} 行不是合法 JSON 事件")
            continue
        if not isinstance(event, dict):
            continue

        valid_events += 1
        if event.get("sessionID"):
            session_id = str(event["sessionID"])

        event_type = event.get("type")
        part = event.get("part")
        part = part if isinstance(part, dict) else {}

        if event_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                part_id = str(part.get("id") or f"event-{index}")
                if part_id not in text_by_part:
                    text_order.append(part_id)
                # 同一 part 可能输出多次累计快照，保留最后一份。
                text_by_part[part_id] = text
        elif event_type == "tool":
            # v1.x tool part 形如 {type:"tool", tool:<name>, callID:<id>,
            # state:{status, input, output, ...}}。同一 callID 会多次快照
            # (running→completed),保留最后一份;字段名做多重兜底,未知结构降级为空
            # (拿不到 → tool_by_id 为空 → messages 为空,行为与改动前一致,不报错)。
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            call_id = str(
                part.get("callID") or part.get("id") or f"tool-{index}"
            )
            tool_name = (
                part.get("tool") or part.get("name") or state.get("tool") or ""
            )
            tool_input = state.get("input", part.get("input"))
            tool_output = state.get("output", part.get("output"))
            if call_id not in tool_by_id:
                tool_order.append(call_id)
            tool_by_id[call_id] = {
                "id": call_id,
                "name": tool_name,
                "input": tool_input,
                "output": tool_output,
            }
        elif event_type == "step_finish":
            tokens = part.get("tokens")
            usage = {
                "tokens": tokens if isinstance(tokens, dict) else {},
                "cost": part.get("cost"),
            }
            # v1.18 实测 step-finish part.reason:正常结束为 "stop";
            # 其余(如 "length" 达到 maxTokens)原样透传,供下游把
            # 非 complete 的轮次标为"证据可能不完整"(对齐其他 harness)。
            reason = part.get("reason")
            if isinstance(reason, str) and reason:
                stop_reason = "complete" if reason == "stop" else reason
        elif event_type == "error":
            errors.append(
                _event_error_message(event) or "OpenCode 返回 error 事件"
            )

    if stdout.strip() and valid_events == 0 and not errors:
        errors.append("OpenCode 未返回合法 JSON 事件")

    return {
        "content": "".join(text_by_part[key] for key in text_order).strip(),
        "session_id": session_id,
        "tool_calls": [tool_by_id[key] for key in tool_order],
        "usage": usage,
        "stop_reason": stop_reason or "complete",
        "error": "; ".join(errors) if errors else None,
    }


class OpenCodeAgent:
    """一个 ``(agent_name, session_name)`` 对应的 OpenCode 会话句柄。"""

    def __init__(
        self,
        client: "OpenCodeClient",
        agent_name: str,
        session_name: str,
        workspace: Optional[Path] = None,
        model_override: Optional[AgentModelConfig] = None,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_id = session_name
        self.session_key = session_name
        self._workspace = Path(workspace or Path.cwd()).expanduser().resolve()
        self._model_override = model_override
        self._opencode_session_id: Optional[str] = None
        self._process: Optional[asyncio.subprocess.Process] = None

    def _resolved_model(self) -> Optional[str]:
        override = self._model_override
        if override is None or not override.resolved_model:
            return None
        return override.resolved_model

    def _build_command(self) -> List[str]:
        command = [
            self._client.command,
            "run",
            "--format",
            "json",
            "--dir",
            str(self._workspace),
        ]
        if self.agent_name:
            command.extend(["--agent", self.agent_name])
        if self._opencode_session_id:
            command.extend(["--session", self._opencode_session_id])
        model = self._resolved_model()
        if model:
            command.extend(["--model", model])
        return command

    async def _stop_process(
        self, process: asyncio.subprocess.Process
    ) -> None:
        if process.returncode is not None:
            return

        if os.name == "nt":
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW,
                )
                try:
                    await asyncio.wait_for(killer.communicate(), timeout=5)
                except asyncio.TimeoutError:
                    killer.kill()
                    await killer.wait()
            except Exception as exc:
                logger.debug("taskkill 失败，回退 process.kill(): %s", exc)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass

        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(
                "OpenCode 子进程未在 5 秒内退出 (pid=%s)", process.pid
            )

    async def close(self) -> None:
        process = self._process
        if process is not None:
            await self._stop_process(process)
        self._process = None

    async def execute(
        self,
        query: str,
        options: Optional[ExecutionOptions] = None,
    ) -> ExecutionResult:
        timeout = (
            float(options.timeout_seconds)
            if options and getattr(options, "timeout_seconds", None)
            else None
        )

        self._workspace.mkdir(parents=True, exist_ok=True)
        command = self._build_command()
        prompt = query.encode("utf-8")
        subprocess_kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            subprocess_kwargs["creationflags"] = _CREATE_NO_WINDOW
        else:
            subprocess_kwargs["start_new_session"] = True

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._workspace),
                **subprocess_kwargs,
            )
        except Exception as exc:
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=(
                    "启动 OpenCode CLI 失败: "
                    f"{_redact_error_text(str(exc))}"
                ),
            )

        self._process = process
        try:
            communicate = process.communicate(input=prompt)
            if timeout is not None:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    communicate, timeout=timeout
                )
            else:
                stdout_bytes, stderr_bytes = await communicate
        except asyncio.TimeoutError:
            await self._stop_process(process)
            return ExecutionResult(
                success=False,
                stop_reason="timeout",
                error_message=f"OpenCode run timed out after {timeout}s",
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_process(process))
            raise
        except Exception as exc:
            await self._stop_process(process)
            return ExecutionResult(
                success=False,
                stop_reason="error",
                error_message=(
                    f"OpenCode run 失败: {_redact_error_text(str(exc))}"
                ),
            )
        finally:
            self._process = None

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = _redact_error_text(
            stderr_bytes.decode("utf-8", errors="replace")
        )
        parsed = _parse_run_output(stdout)
        if parsed["session_id"]:
            self._opencode_session_id = parsed["session_id"]

        error = parsed["error"]
        if process.returncode != 0:
            error = error or stderr or (
                f"OpenCode 进程退出码为 {process.returncode}"
            )
        if not parsed["content"]:
            error = error or stderr or "OpenCode 未返回 text 事件"

        if error:
            return ExecutionResult(
                success=False,
                content=parsed["content"],
                stop_reason="error",
                error_message=error,
                usage=parsed["usage"]
            )
        return ExecutionResult(
            success=True,
            content=parsed["content"],
            stop_reason=parsed["stop_reason"],
            usage=parsed["usage"]
        )


class OpenCodeClient:
    """进程内管理 OpenCode CLI 子进程与 Agent 句柄。"""

    def __init__(
        self,
        command: str = "opencode",
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.command = command
        self._agent_overrides: Dict[str, AgentModelConfig] = agent_overrides or {}
        self._agents: Dict[tuple, OpenCodeAgent] = {}

    async def __aenter__(self) -> "OpenCodeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        for agent in self._agents.values():
            await agent.close()
        self._agents.clear()

    def get_agent(
        self,
        agent_name: str,
        session_name: str,
        *,
        workspace: Optional[Path] = None,
        model_override: Optional[AgentModelConfig] = None,
    ) -> OpenCodeAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            self._agents[key] = OpenCodeAgent(
                client=self,
                agent_name=agent_name,
                session_name=session_name,
                workspace=workspace,
                model_override=model_override or self._agent_overrides.get(agent_name),
            )
        return self._agents[key]


async def build_opencode_client() -> OpenCodeClient:
    """OpenCodeClient 工厂:定位 opencode 二进制后构造客户端。"""
    binary = "/usr/local/node24/bin/opencode"
    if not (os.path.isfile(binary) and os.access(binary, os.X_OK)):
        raise OpenCodeError("未找到 OpenCode CLI；请先安装并确认 `opencode --version` 可用。")
    logger.info(
        "OpenCode 客户端就绪；模型与凭证由 opencode.json 管理。")
    return OpenCodeClient(command=binary)


class OpenCodeWorkspaceManager(BaseWorkspaceManager):
    """OpenCode 工作空间：``<base_dir>/<agent_name>``"""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        if agent_name == "main":
            workspace = self.base_dir
        else:
            parent = self.base_dir.parent
            base_name = self.base_dir.name
            workspace = parent / f"{base_name}-{agent_name}"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / ".opencode" / "skills").mkdir(parents=True, exist_ok=True)
        return workspace

    def get_skills_dst(self, workspace: Path) -> Path:
        """OpenCode skills 需放在 .opencode/skills 下才能被识别。"""
        return workspace / ".opencode" / "skills"

    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        agent_source = Path(agent_dir).expanduser()
        if agent_source.exists():
            for config_file in config_files:
                src = agent_source / config_file
                if src.exists():
                    dst = workspace / config_file
                    copy_path(src, dst)
                    logger.info("复制 Agent 配置: %s -> %s", config_file, dst)
                    dst_main = self.base_dir / config_file
                    copy_path(src, dst_main)
                    logger.info("复制 Agent 配置: %s -> %s", config_file, dst_main)
                else:
                    logger.warning("Agent 配置文件不存在: %s", src)
        else:
            logger.warning("Agent 源目录不存在: %s", agent_source)

class OpenCodeAgentManager:
    """OpenCode Agent 管理器：确保每个 Agent 的独立 workspace 存在。"""

    def __init__(
        self,
        client: OpenCodeClient,
        workspace_manager: OpenCodeWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides: Dict[str, AgentModelConfig] = (
            agent_overrides or {}
        )
        self.client.workspace_manager = workspace_manager
        self.client._agent_overrides.update(self.agent_overrides)

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)
        if agent_config.model:
            logger.info("设置 Agent: %s | model=%s", agent_name, agent_config.model)
        else:
            logger.info("设置 Agent: %s", agent_name)
        self.workspace_manager.get_agent_workspace(agent_name)


def make_opencode_execute_with_retry(client: OpenCodeClient):
    """返回 OpenCode 专用 execute_with_retry，行为与 Hermes/ClaudeCode 对齐。

    返回 `(result, evidence_incomplete)`,签名与 OpenClaw 对齐:
    - 正常返回 → `(result, False)`;
    - stop_reason 非 "complete"(如模型达到 maxTokens 的 "length")→
      `(result, True)`,提示下游 evaluator:本轮回复可能被截断,证据缺失
      不得当负面证据(D5)。

    任何失败都按 EXECUTION_MAX_ATTEMPTS / EXECUTION_RETRY_WAIT_SECONDS
    重试。opencode 子进程即使失败也会产出 sessionID,重试时自动
    ``--session <id>`` 续接同一会话,与其他 harness 在会话内重发同一条
    查询的行为一致(瞬时模型错误/超时均可恢复,不再一次失败即终止)。
    """

    async def execute_with_retry(agent, query_text: str, options):
        last_exc: Optional[BaseException] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            try:
                result = await agent.execute(query_text, options=options)
                if result is None:
                    raise OpenCodeError("OpenCode returned None")
                if result.success and result.content:
                    evidence_incomplete = (
                        result.stop_reason or "complete"
                    ) != "complete"
                    return result, evidence_incomplete
                if not result.success:
                    message = (
                        result.error_message or "OpenCode returned error"
                    )
                else:
                    message = "OpenCode returned empty content"
                raise OpenCodeError(message)
            except (OpenCodeError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt >= EXECUTION_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "调用失败 (第 %d/%d 次): %s; %ds 后重试",
                    attempt,
                    EXECUTION_MAX_ATTEMPTS,
                    exc,
                    EXECUTION_RETRY_WAIT_SECONDS,
                )
                await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
        if last_exc is not None:
            raise last_exc
        raise OpenCodeError("OpenCode: unknown error after retries")

    return execute_with_retry


def make_opencode_get_agent(
    client: OpenCodeClient,
    workspace_manager: Optional[OpenCodeWorkspaceManager] = None,
    agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
):
    """返回 OpenCode 专用 get_agent_fn，注入 workspace 与模型覆盖。"""
    overrides = agent_overrides or {}

    def get_agent(agent_name: str, session_name: str):
        workspace = (
            workspace_manager.get_agent_workspace(agent_name)
            if workspace_manager is not None
            else None
        )
        return client.get_agent(
            agent_name,
            session_name,
            workspace=workspace,
            model_override=overrides.get(agent_name),
        )

    return get_agent