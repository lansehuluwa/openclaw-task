"""Codex harness 的离线生命周期与通用配置回归测试。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from harness_automation import HarnessAutomation
from src import codex_client
from src.codex_client import (
    CodexAgent,
    CodexClient,
    CodexHarnessError,
    CodexWorkspaceManager,
    ExecutionOptions,
    ExecutionResult,
    _AgentDefaults,
    build_codex_client,
    make_codex_execute_with_retry,
)
from src.config import ConfigLoader


def _turn_result(text: str = "ok"):
    return SimpleNamespace(
        final_response=text,
        error=None,
        status=SimpleNamespace(value="completed"),
        usage=None,
    )


class _FakeTurn:
    def __init__(self, *, immediate: bool = True):
        self.interrupt_calls = 0
        self.run_started = asyncio.Event()
        self._release = asyncio.Event()
        self.run_cancelled = False
        if immediate:
            self._release.set()

    async def run(self):
        self.run_started.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            self.run_cancelled = True
            raise
        return _turn_result()

    async def interrupt(self) -> None:
        self.interrupt_calls += 1
        self._release.set()


class _FakeThread:
    def __init__(self, thread_id: str, turns):
        self.id = thread_id
        self._turns = list(turns)
        self.queries = []

    async def turn(self, query: str):
        self.queries.append(query)
        return self._turns.pop(0)


class _FakeSdk:
    def __init__(self, threads):
        self._threads = list(threads)
        self.thread_start_calls = []

    async def thread_start(self, **kwargs):
        self.thread_start_calls.append(kwargs)
        return self._threads.pop(0)


def _defaults(cwd: Path = PROJECT_ROOT) -> _AgentDefaults:
    return _AgentDefaults(
        system_prompt=None,
        model="test-model",
        model_provider="test-provider",
        cwd=cwd,
    )


def _agent(thread: _FakeThread) -> CodexAgent:
    client = SimpleNamespace(sdk=None)
    agent = CodexAgent(client, "agent", "session", _defaults())
    agent._thread = thread
    return agent


class CodexClientLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_client_uses_preconfigured_home_without_writing_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "preconfigured-home"
            client = await build_codex_client(str(codex_home))

            self.assertEqual(client.codex_home, codex_home)
            self.assertFalse(codex_home.exists())

    async def test_sdk_is_started_once_and_reused_until_client_exit(self):
        instances = []

        class FakeAsyncCodex:
            def __init__(self, config):
                self.config = config
                self.enter_calls = 0
                self.close_calls = 0
                instances.append(self)

            async def __aenter__(self):
                self.enter_calls += 1
                return self

            async def close(self):
                self.close_calls += 1

        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(codex_client, "AsyncCodex", FakeAsyncCodex):
                client = await build_codex_client(temp_dir)
                async with client:
                    self.assertIs(client.sdk, instances[0])
                    self.assertIs(client.sdk, instances[0])

        self.assertEqual(len(instances), 1)
        self.assertEqual(instances[0].enter_calls, 1)
        self.assertEqual(instances[0].close_calls, 1)

    async def test_same_session_reuses_thread_and_other_session_isolated(self):
        first_thread = _FakeThread(
            "thread-main", [_FakeTurn(), _FakeTurn()]
        )
        second_thread = _FakeThread("thread-other", [_FakeTurn()])
        sdk = _FakeSdk([first_thread, second_thread])
        client = CodexClient(PROJECT_ROOT)
        client._sdk = sdk
        client.register_agent_defaults(
            "agent",
            system_prompt=None,
            model="test-model",
            model_provider="test-provider",
            cwd=PROJECT_ROOT,
        )

        session = client.get_agent("agent", "main")
        self.assertIs(session, client.get_agent("agent", "main"))
        other_session = client.get_agent("agent", "other")
        self.assertIsNot(session, other_session)

        first = await session.execute("first")
        second = await session.execute("second")
        other = await other_session.execute("other")

        self.assertTrue(first.success and second.success and other.success)
        self.assertEqual(first_thread.queries, ["first", "second"])
        self.assertEqual(second_thread.queries, ["other"])
        self.assertEqual(first.session_id, "thread-main")
        self.assertEqual(other.session_id, "thread-other")
        self.assertEqual(len(sdk.thread_start_calls), 2)

    async def test_timeout_interrupts_active_turn(self):
        turn = _FakeTurn(immediate=False)
        agent = _agent(_FakeThread("thread-timeout", [turn]))

        result = await agent.execute(
            "slow", ExecutionOptions(timeout_seconds=0.01)
        )

        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(turn.interrupt_calls, 1)
        self.assertTrue(turn.run_cancelled)

    async def test_cancellation_interrupts_active_turn(self):
        turn = _FakeTurn(immediate=False)
        agent = _agent(_FakeThread("thread-cancel", [turn]))
        task = asyncio.create_task(agent.execute("cancel me"))
        await turn.run_started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(turn.interrupt_calls, 1)

    async def test_execute_wrapper_does_not_replay_failed_turn(self):
        class ErrorAgent:
            def __init__(self):
                self.calls = 0

            async def execute(self, query, options=None):
                self.calls += 1
                return ExecutionResult(
                    success=False,
                    stop_reason="error",
                    error_message="failed",
                )

        agent = ErrorAgent()
        execute_once = make_codex_execute_with_retry(SimpleNamespace())

        with self.assertRaisesRegex(CodexHarnessError, "failed"):
            await execute_once(agent, "query", None)
        self.assertEqual(agent.calls, 1)


class CodexWorkspaceManagerTests(unittest.TestCase):
    def test_agent_configs_are_copied_without_merging_agents_md(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "agent"
            source.mkdir()
            (source / "USER.md").write_text("user", encoding="utf-8")
            (source / "SOUL.md").write_text("soul", encoding="utf-8")
            manager = CodexWorkspaceManager(str(root / "workspaces"))

            manager.setup_agent_files(
                agent_name="demo-agent",
                config_files=["USER.md", "SOUL.md"],
                skill_base_dir=None,
                agent_skills=[],
                agent_dir=str(source),
            )

            workspace = manager.get_agent_workspace("demo-agent")
            self.assertEqual(
                (workspace / "USER.md").read_text(encoding="utf-8"), "user"
            )
            self.assertEqual(
                (workspace / "SOUL.md").read_text(encoding="utf-8"), "soul"
            )
            self.assertFalse((workspace / "AGENTS.md").exists())

    def test_skills_are_copied_to_codex_project_skill_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill_source = root / "source" / "demo-skill"
            skill_source.mkdir(parents=True)
            (skill_source / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: test\n---\n",
                encoding="utf-8",
            )
            manager = CodexWorkspaceManager(str(root / "workspaces"))

            manager.setup_agent_files(
                agent_name="demo-agent",
                config_files=[],
                skill_base_dir=str(root / "source"),
                agent_skills=["demo-skill"],
            )

            workspace = manager.get_agent_workspace("demo-agent")
            self.assertTrue(
                (workspace / ".agents/skills/demo-skill/SKILL.md").is_file()
            )
            self.assertFalse((workspace / "skills/demo-skill").exists())


class CodexGenericConfigTests(unittest.TestCase):
    def test_three_shared_configs_can_select_codex_without_schema_changes(self):
        config_names = (
            "config_simple.json",
            "config_user.json",
            "config_simple_eval.json",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            for config_name in config_names:
                with self.subTest(config=config_name):
                    config = ConfigLoader.load_from_file(
                        str(PROJECT_ROOT / "configs" / config_name)
                    )
                    config.harness_type = "codex"
                    config.workspace_base = str(Path(temp_dir) / config_name)

                    automation = HarnessAutomation(config)

                    self.assertIsInstance(
                        automation.workspace_manager, CodexWorkspaceManager
                    )


if __name__ == "__main__":
    unittest.main()
