"""Codex Python SDK harness 客户端。

一个 ``CodexClient`` 维护一个 ``AsyncCodex``/app-server；每个
``(agent_name, session_name)`` 维护一个真实 Codex thread。provider 定义和
密钥引用由独立 ``CODEX_HOME/config.toml`` 管理，agent 只选择 provider/model。
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox

from src.config import AgentModelConfig, warn_agent_model_conflict
from src.workspace import BaseWorkspaceManager

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 3
EXECUTION_RETRY_WAIT_SECONDS = 2
TURN_INTERRUPT_GRACE_SECONDS = 5.0
_ERROR_TEXT_LIMIT = 4000


class CodexHarnessError(RuntimeError):
    """Codex SDK、配置或返回结果不可用。"""


@dataclass
class ExecutionResult:
    success: bool = True
    content: str = ""
    stop_reason: Optional[str] = "complete"
    error_message: Optional[str] = None
    usage: Optional[Dict[str, Any]] = field(default=None)
    session_id: Optional[str] = None
    model_provider: Optional[str] = None

    def model_copy(
        self, *, update: Optional[Dict[str, Any]] = None
    ) -> "ExecutionResult":
        data = {
            "success": self.success,
            "content": self.content,
            "stop_reason": self.stop_reason,
            "error_message": self.error_message,
            "usage": self.usage,
            "session_id": self.session_id,
            "model_provider": self.model_provider,
        }
        if update:
            data.update(update)
        return ExecutionResult(**data)


@dataclass
class ExecutionOptions:
    timeout_seconds: Optional[int] = None


@dataclass
class _AgentDefaults:
    system_prompt: Optional[str]
    model: Optional[str]
    model_provider: Optional[str]
    cwd: Path


def _redact_error_text(text: str) -> str:
    redacted = re.sub(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", text
    )
    redacted = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "<redacted>", redacted)
    return redacted[-_ERROR_TEXT_LIMIT:].strip()


def _usage_dict(usage: Any) -> Optional[Dict[str, Any]]:
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage
    if hasattr(usage, "model_dump"):
        value = usage.model_dump(mode="json", exclude_none=True)
        return value if isinstance(value, dict) else None
    return None


class CodexAgent:
    """一个逻辑 Agent 会话，对应一个长期复用的 Codex thread。"""

    def __init__(
        self,
        client: "CodexClient",
        agent_name: str,
        session_name: str,
        defaults: _AgentDefaults,
    ):
        self._client = client
        self.agent_name = agent_name
        self.session_name = session_name
        self.session_key = session_name
        self._defaults = defaults
        self._thread = None
        self._lock = asyncio.Lock()

    async def _ensure_thread(self):
        if self._thread is not None:
            return self._thread
        sdk = self._client.sdk
        self._thread = await sdk.thread_start(
            approval_mode=ApprovalMode.deny_all,
            cwd=str(self._defaults.cwd),
            developer_instructions=self._defaults.system_prompt,
            ephemeral=True,
            model=self._defaults.model,
            model_provider=self._defaults.model_provider,
            sandbox=Sandbox.workspace_write,
            service_name="openclaw_task_harness",
        )
        logger.info(
            "Codex thread 已创建: agent=%s session=%s thread=%s provider=%s model=%s",
            self.agent_name,
            self.session_name,
            self._thread.id,
            self._defaults.model_provider,
            self._defaults.model,
        )
        return self._thread

    async def _stop_active_turn(self, turn, run_task) -> Optional[str]:
        """中断当前 turn；无法确认终止时关闭整个 SDK 进程。"""
        fallback_reason: Optional[str] = None

        if turn is None:
            fallback_reason = "Codex turn 句柄尚未返回"
        else:
            try:
                await asyncio.wait_for(
                    turn.interrupt(), timeout=TURN_INTERRUPT_GRACE_SECONDS
                )
            except Exception as exc:  # 中断请求失败后仍等待 turn 自行结束
                fallback_reason = f"Codex turn 中断请求失败: {_redact_error_text(str(exc))}"

            if run_task is not None:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(run_task),
                        timeout=TURN_INTERRUPT_GRACE_SECONDS,
                    )
                    return None
                except asyncio.TimeoutError:
                    fallback_reason = (
                        f"Codex turn 在 {TURN_INTERRUPT_GRACE_SECONDS:g}s 内未确认终止"
                    )
                except Exception as exc:
                    fallback_reason = (
                        "Codex turn 终态确认失败: "
                        f"{_redact_error_text(str(exc))}"
                    )

        close_error: Optional[str] = None
        try:
            await self._client.close()
        except Exception as exc:
            close_error = _redact_error_text(str(exc))

        if run_task is not None and not run_task.done():
            run_task.cancel()
        if run_task is not None:
            await asyncio.gather(run_task, return_exceptions=True)

        detail = fallback_reason or "Codex turn 未确认终止"
        if close_error:
            detail += f"；关闭 Codex SDK 失败: {close_error}"
        else:
            detail += "；已关闭 Codex SDK"
        return detail

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

        async with self._lock:
            turn = None
            run_task = None
            try:
                loop = asyncio.get_running_loop()
                deadline = (
                    loop.time() + timeout
                    if timeout is not None
                    else None
                )
                thread = (
                    await asyncio.wait_for(self._ensure_thread(), timeout=timeout)
                    if timeout is not None
                    else await self._ensure_thread()
                )
                turn_timeout = (
                    max(0.0, deadline - loop.time())
                    if deadline is not None
                    else None
                )
                turn = (
                    await asyncio.wait_for(
                        thread.turn(query), timeout=turn_timeout
                    )
                    if turn_timeout is not None
                    else await thread.turn(query)
                )
                run_task = asyncio.create_task(turn.run())
                if deadline is None:
                    result = await asyncio.shield(run_task)
                else:
                    remaining = max(0.0, deadline - loop.time())
                    result = await asyncio.wait_for(
                        asyncio.shield(run_task), timeout=remaining
                    )
            except asyncio.TimeoutError:
                cleanup_detail = await self._stop_active_turn(turn, run_task)
                error_message = f"Codex turn timed out after {timeout}s"
                if cleanup_detail:
                    error_message += f"; {cleanup_detail}"
                return ExecutionResult(
                    success=False,
                    stop_reason="timeout",
                    error_message=error_message,
                    session_id=getattr(self._thread, "id", None),
                    model_provider=self._defaults.model_provider,
                )
            except asyncio.CancelledError:
                await asyncio.shield(self._stop_active_turn(turn, run_task))
                raise
            except Exception as exc:  # SDK/app-server 错误统一收敛
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message=_redact_error_text(str(exc)),
                    session_id=getattr(self._thread, "id", None),
                    model_provider=self._defaults.model_provider,
                )

            content = (result.final_response or "").strip()
            error = getattr(result, "error", None)
            error_text = _redact_error_text(str(error)) if error else None
            status = getattr(result, "status", None)
            status_text = getattr(status, "value", None) or str(status or "complete")
            success = bool(content) and error is None
            return ExecutionResult(
                success=success,
                content=content,
                stop_reason="complete" if success else status_text,
                error_message=error_text or (None if success else "Codex 未返回最终文本"),
                usage=_usage_dict(getattr(result, "usage", None)),
                session_id=thread.id,
                model_provider=self._defaults.model_provider,
            )


class CodexClient:
    """管理一个隔离的 AsyncCodex app-server 和多个 Agent/thread。"""

    def __init__(self, codex_home: Path):
        self.codex_home = codex_home
        self._sdk: Optional[AsyncCodex] = None
        self._agents: Dict[tuple[str, str], CodexAgent] = {}
        self._agent_defaults: Dict[str, _AgentDefaults] = {}
        self.workspace_manager: Optional[CodexWorkspaceManager] = None

    @property
    def sdk(self) -> AsyncCodex:
        if self._sdk is None:
            raise CodexHarnessError("CodexClient 尚未启动")
        return self._sdk

    async def __aenter__(self) -> "CodexClient":
        config = CodexConfig(
            env={"CODEX_HOME": str(self.codex_home.resolve())},
            client_name="openclaw_task_harness",
            client_title="OpenClaw Task Harness",
        )
        sdk = AsyncCodex(config=config)
        try:
            await sdk.__aenter__()
        except Exception:
            await sdk.close()
            raise
        self._sdk = sdk
        logger.info("Codex SDK 已启动: CODEX_HOME=%s", self.codex_home)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def close(self) -> None:
        self._agents.clear()
        sdk = self._sdk
        self._sdk = None
        if sdk is not None:
            await sdk.close()

    def register_agent_defaults(
        self,
        agent_name: str,
        *,
        system_prompt: Optional[str],
        model: Optional[str],
        model_provider: Optional[str],
        cwd: Path,
    ) -> None:
        self._agent_defaults[agent_name] = _AgentDefaults(
            system_prompt=system_prompt,
            model=model,
            model_provider=model_provider,
            cwd=cwd,
        )

    def get_agent(self, agent_name: str, session_name: str) -> CodexAgent:
        key = (agent_name, session_name)
        if key not in self._agents:
            defaults = self._agent_defaults.get(agent_name)
            if defaults is None:
                raise CodexHarnessError(f"Codex agent 尚未注册: {agent_name}")
            self._agents[key] = CodexAgent(
                self, agent_name, session_name, defaults
            )
        return self._agents[key]


async def build_codex_client(codex_home: str) -> CodexClient:
    """使用部署阶段已经准备好的 CODEX_HOME 启动 Codex。"""
    return CodexClient(Path(codex_home).expanduser())


class CodexWorkspaceManager(BaseWorkspaceManager):
    """每个 Agent 独立 workspace，skill 使用 Codex 原生 .agents/skills。"""

    skills_subdir = Path(".agents/skills")

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).expanduser()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def get_agent_workspace(self, agent_name: str) -> Path:
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", agent_name).strip("-.")
        if not normalized:
            raise CodexHarnessError(f"无法生成 workspace 目录名: {agent_name!r}")
        workspace = self.base_dir / normalized
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / self.skills_subdir).mkdir(parents=True, exist_ok=True)
        return workspace

    def setup_agent_files(
        self,
        agent_name: str,
        config_files: List[str],
        skill_base_dir: Optional[str],
        agent_skills: List[str],
        agent_dir: Optional[str] = None,
        content_root: Optional[str] = None,
    ) -> None:
        """复用公共文件准备逻辑，但将 skill 放入 Codex 原生目录。"""
        super().setup_agent_files(
            agent_name=agent_name,
            config_files=config_files,
            skill_base_dir=None,
            agent_skills=[],
            agent_dir=agent_dir,
            content_root=content_root,
        )
        if not skill_base_dir or not agent_skills:
            return

        workspace = self.get_agent_workspace(agent_name)
        skills_dst = workspace / self.skills_subdir
        for skill_path in agent_skills:
            skill_name = Path(skill_path).name
            source = Path(skill_base_dir).expanduser() / skill_path
            if not source.is_dir():
                logger.warning("技能目录不存在: %s", source)
                continue
            target = skills_dst / skill_name
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(source, target)
            logger.info("复制 Codex 技能: %s -> %s", skill_path, target)

    def _copy_agent_configs(
        self,
        workspace: Path,
        config_files: List[str],
        agent_dir: str,
    ) -> None:
        source_dir = Path(agent_dir).expanduser()
        sections: List[str] = []
        for config_file in config_files:
            source = source_dir / config_file
            if not source.is_file():
                logger.warning("Agent 配置文件不存在: %s", source)
                continue
            sections.append(
                f"## {config_file}\n\n{source.read_text(encoding='utf-8').strip()}"
            )
        if sections:
            target = workspace / "AGENTS.md"
            target.write_text(
                "# Agent 配置\n\n" + "\n\n".join(sections) + "\n",
                encoding="utf-8",
            )
            logger.info("生成 Codex Agent 配置: %s", target)


class CodexAgentManager:
    def __init__(
        self,
        client: CodexClient,
        workspace_manager: CodexWorkspaceManager,
        agent_overrides: Optional[Dict[str, AgentModelConfig]] = None,
    ):
        self.client = client
        self.workspace_manager = workspace_manager
        self.agent_overrides = agent_overrides or {}
        self.client.workspace_manager = workspace_manager

    async def setup_agent(self, agent_config) -> None:
        agent_name = agent_config.name
        override = self.agent_overrides.get(agent_name)
        if override:
            warn_agent_model_conflict(agent_name, agent_config.model, override)

        model = override.model if override and override.model else agent_config.model
        model_provider = (
            override.provider
            if override and override.provider
            else agent_config.model_provider
        )
        workspace = self.workspace_manager.get_agent_workspace(agent_name)
        self.client.register_agent_defaults(
            agent_name,
            system_prompt=agent_config.system_prompt,
            model=model,
            model_provider=model_provider,
            cwd=workspace,
        )
        logger.info(
            "设置 Codex Agent: %s | provider=%s model=%s workspace=%s",
            agent_name,
            model_provider,
            model,
            workspace,
        )


def make_codex_get_agent(
    client: CodexClient,
    workspace_manager: Optional[CodexWorkspaceManager] = None,
):
    def get_agent(agent_name: str, session_name: str) -> CodexAgent:
        # workspace 在 setup_agent 时已经注册；此处保留参数以对齐其他 harness。
        return client.get_agent(agent_name, session_name)

    return get_agent


def make_codex_execute_with_retry(client: CodexClient):
    async def execute_with_retry(agent: CodexAgent, query_text: str, options):
        last_error: Optional[str] = None
        for attempt in range(1, EXECUTION_MAX_ATTEMPTS + 1):
            result = await agent.execute(query_text, options=options)
            if result.success and result.content:
                return result, False

            last_error = result.error_message or "Codex 返回空结果"
            # turn 超时后的副作用状态未知，不能在同一 thread 自动重发。
            if result.stop_reason == "timeout":
                raise CodexHarnessError(last_error)
            if attempt >= EXECUTION_MAX_ATTEMPTS:
                break
            logger.warning(
                "Codex 调用失败 (第 %d/%d 次): %s; %ds 后重试",
                attempt,
                EXECUTION_MAX_ATTEMPTS,
                last_error,
                EXECUTION_RETRY_WAIT_SECONDS,
            )
            await asyncio.sleep(EXECUTION_RETRY_WAIT_SECONDS)
        raise CodexHarnessError(last_error or "Codex 调用失败")

    return execute_with_retry
