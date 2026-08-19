"""Codex harness 的离线生命周期回归测试。"""

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

from src import codex_client
from src.codex_client import (
    CodexAgent,
    CodexHarnessError,
    CodexWorkspaceManager,
    ExecutionOptions,
    ExecutionResult,
    _AgentDefaults,
    build_codex_client,
    make_codex_execute_with_retry,
)


def _turn_result(text: str = "ok"):
    return SimpleNamespace(
        final_response=text,
        error=None,
        status=SimpleNamespace(value="completed"),
        usage=None,
    )


class _FakeClient:
    def __init__(self):
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1


class _FakeTurn:
    def __init__(self, *, complete_on_interrupt: bool = True, immediate: bool = False):
        self.complete_on_interrupt = complete_on_interrupt
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
        if self.complete_on_interrupt:
            self._release.set()


class _FakeThread:
    id = "thread-123"

    def __init__(self, turns):
        self._turns = list(turns)
        self.queries = []

    async def turn(self, query: str):
        self.queries.append(query)
        return self._turns.pop(0)


def _agent(client: _FakeClient, thread: _FakeThread) -> CodexAgent:
    defaults = _AgentDefaults(
        system_prompt=None,
        model="test-model",
        model_provider="test-provider",
        cwd=PROJECT_ROOT,
    )
    agent = CodexAgent(client, "agent", "session", defaults)
    agent._thread = thread
    return agent


class CodexAgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_client_does_not_create_or_copy_codex_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            codex_home = Path(temp_dir) / "preconfigured-home"
            client = await build_codex_client(str(codex_home))

            self.assertEqual(client.codex_home, codex_home)
            self.assertFalse(codex_home.exists())

    async def test_timeout_while_starting_thread_closes_sdk(self):
        class SlowSdk:
            async def thread_start(self, **kwargs):
                await asyncio.Event().wait()

        client = _FakeClient()
        client.sdk = SlowSdk()
        defaults = _AgentDefaults(
            system_prompt=None,
            model="test-model",
            model_provider="test-provider",
            cwd=PROJECT_ROOT,
        )
        agent = CodexAgent(client, "agent", "session", defaults)

        result = await agent.execute(
            "slow start", ExecutionOptions(timeout_seconds=0.01)
        )

        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(client.close_calls, 1)
        self.assertIn("turn 句柄尚未返回", result.error_message or "")

    async def test_successive_queries_reuse_thread(self):
        client = _FakeClient()
        thread = _FakeThread([
            _FakeTurn(immediate=True),
            _FakeTurn(immediate=True),
        ])
        agent = _agent(client, thread)

        first = await agent.execute("first")
        second = await agent.execute("second")

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(thread.queries, ["first", "second"])
        self.assertEqual(first.session_id, "thread-123")
        self.assertEqual(client.close_calls, 0)

    async def test_timeout_interrupts_and_waits_for_turn_completion(self):
        client = _FakeClient()
        turn = _FakeTurn(complete_on_interrupt=True)
        agent = _agent(client, _FakeThread([turn]))

        with patch.object(codex_client, "TURN_INTERRUPT_GRACE_SECONDS", 0.05):
            result = await agent.execute(
                "slow", ExecutionOptions(timeout_seconds=0.1)
            )

        self.assertFalse(result.success)
        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(turn.interrupt_calls, 1)
        self.assertEqual(client.close_calls, 0)

    async def test_timeout_closes_sdk_if_turn_does_not_stop(self):
        client = _FakeClient()
        turn = _FakeTurn(complete_on_interrupt=False)
        agent = _agent(client, _FakeThread([turn]))

        with patch.object(codex_client, "TURN_INTERRUPT_GRACE_SECONDS", 0.01):
            result = await agent.execute(
                "stuck", ExecutionOptions(timeout_seconds=0.1)
            )

        self.assertEqual(result.stop_reason, "timeout")
        self.assertEqual(turn.interrupt_calls, 1)
        self.assertEqual(client.close_calls, 1)
        self.assertTrue(turn.run_cancelled)
        self.assertIn("已关闭 Codex SDK", result.error_message or "")

    async def test_cancellation_interrupts_active_turn(self):
        client = _FakeClient()
        turn = _FakeTurn(complete_on_interrupt=True)
        agent = _agent(client, _FakeThread([turn]))
        task = asyncio.create_task(agent.execute("cancel me"))
        await turn.run_started.wait()

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(turn.interrupt_calls, 1)
        self.assertEqual(client.close_calls, 0)

    async def test_timeout_is_not_retried(self):
        class TimeoutAgent:
            def __init__(self):
                self.calls = 0

            async def execute(self, query, options=None):
                self.calls += 1
                return ExecutionResult(
                    success=False,
                    stop_reason="timeout",
                    error_message="timed out",
                )

        agent = TimeoutAgent()
        execute_with_retry = make_codex_execute_with_retry(_FakeClient())

        with self.assertRaisesRegex(CodexHarnessError, "timed out"):
            await execute_with_retry(agent, "query", None)
        self.assertEqual(agent.calls, 1)


class CodexWorkspaceManagerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
