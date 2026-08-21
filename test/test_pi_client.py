"""Pi RPC harness 的离线协议、生命周期与通用配置回归测试。"""

from __future__ import annotations

import asyncio
import json
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
from src.pi_client import (
    ExecutionOptions,
    ExecutionResult,
    PiAgentManager,
    PiClient,
    PiHarnessError,
    PiWorkspaceManager,
    build_pi_client,
    make_pi_execute_with_retry,
)
from src.config import AgentModelConfig, ConfigLoader


def _line(value):
    return (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")


def _result_events(request_id: str, text: str, tool: bool = False):
    response = {
        "id": request_id,
        "type": "response",
        "command": "prompt",
        "success": True,
    }
    events = []
    if tool:
        events.extend(
            [
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "toolCall",
                                "id": "call-1",
                                "name": "write",
                                "arguments": {"path": "answer.txt"},
                            }
                        ],
                        "stopReason": "toolUse",
                    },
                },
                {"type": "agent_end", "messages": [], "willRetry": False},
                {
                    "type": "tool_execution_start",
                    "toolCallId": "call-1",
                    "toolName": "write",
                    "args": {"path": "answer.txt", "content": "ok"},
                },
                {
                    "type": "tool_execution_end",
                    "toolCallId": "call-1",
                    "toolName": "write",
                    "result": {
                        "content": [{"type": "text", "text": "wrote answer.txt"}]
                    },
                    "isError": False,
                },
            ]
        )
    events.extend(
        [
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": text}],
                    "provider": "test-provider",
                    "model": "test-model",
                    "usage": {"input": 10, "output": 2, "totalTokens": 12},
                    "stopReason": "stop",
                },
            },
            {"type": "agent_settled"},
            response,
        ]
    )
    return events


class _FakeReader:
    def __init__(self, values=(), block_when_empty=False):
        self._lines = [_line(value) for value in values]
        self._block_when_empty = block_when_empty
        self._never = asyncio.Event()

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._block_when_empty:
            await self._never.wait()
        return b""


class _FakeWriter:
    def __init__(self):
        self.payloads = []
        self.closed = False

    def write(self, data):
        self.payloads.append(json.loads(data.decode("utf-8")))

    async def drain(self):
        return None

    def close(self):
        self.closed = True


class _FakeProcess:
    _next_pid = 100

    def __init__(self, events=(), block_when_empty=False):
        self.stdin = _FakeWriter()
        self.stdout = _FakeReader(events, block_when_empty)
        self.stderr = _FakeReader()
        self.returncode = None
        self.terminate_calls = 0
        self.kill_calls = 0
        self.pid = self._next_pid
        type(self)._next_pid += 1

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _register(
    client: PiClient, workspace: Path, config_dir: Path | None = None
):
    client.register_agent_defaults(
        "agent",
        system_prompt="be brief",
        model="test-model",
        model_provider="test-provider",
        cwd=workspace,
        config_dir=config_dir,
    )


class PiRpcLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_client_does_not_install_or_write_pi(self):
        client = await build_pi_client()
        self.assertEqual(client.pi_command, "pi")

    async def test_client_requires_preinstalled_pi_on_enter(self):
        client = await build_pi_client()
        with patch("src.pi_client.shutil.which", return_value=None):
            with self.assertRaisesRegex(PiHarnessError, "预装"):
                await client.__aenter__()

    async def test_same_session_reuses_process_and_parses_rpc_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "agent"
            workspace.mkdir()
            (workspace / "INPUT.md").write_text("input", encoding="utf-8")
            first_process = _FakeProcess(
                _result_events("main-1", "first", tool=True)
                + _result_events("main-2", "second")
            )
            other_process = _FakeProcess(_result_events("other-1", "other"))
            processes = [first_process, other_process]
            calls = []

            async def fake_spawn(*args, **kwargs):
                calls.append((args, kwargs))
                return processes.pop(0)

            client = PiClient("pi-test")
            config_dir = workspace / ".pi-agent"
            _register(client, workspace, config_dir)
            main = client.get_agent("agent", "main")
            self.assertIs(main, client.get_agent("agent", "main"))
            other = client.get_agent("agent", "other")

            with patch(
                "src.pi_client.asyncio.create_subprocess_exec", fake_spawn
            ):
                first = await main.execute("first query")
                second = await main.execute("second query")
                third = await other.execute("other query")
                await client.close()

            self.assertEqual([first.content, second.content, third.content], ["first", "second", "other"])
            self.assertEqual(first.usage["totalTokens"], 12)
            self.assertEqual(first.model_provider, "test-provider")
            self.assertEqual(len(first.tool_calls), 1)
            self.assertEqual(first.tool_calls[0].tool, "write")
            self.assertEqual(first.tool_calls[0].input["path"], "answer.txt")
            self.assertEqual(first.tool_calls[0].output, "wrote answer.txt")
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]["cwd"], str(workspace / ".sessions" / "main"))
            self.assertEqual(
                calls[0][1]["env"]["PI_CODING_AGENT_DIR"], str(config_dir)
            )
            self.assertIn("--approve", calls[0][0])
            self.assertIn("--append-system-prompt", calls[0][0])
            self.assertTrue((workspace / ".sessions/main/INPUT.md").is_file())
            self.assertEqual(
                [item["message"] for item in first_process.stdin.payloads[:2]],
                ["first query", "second query"],
            )

    async def test_timeout_aborts_process_without_replaying_prompt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace = Path(temp_dir) / "agent"
            workspace.mkdir()
            process = _FakeProcess(
                [
                    {
                        "id": "main-1",
                        "type": "response",
                        "command": "prompt",
                        "success": True,
                    }
                ],
                block_when_empty=True,
            )

            async def fake_spawn(*args, **kwargs):
                return process

            client = PiClient("pi-test")
            _register(client, workspace)
            agent = client.get_agent("agent", "main")
            with patch(
                "src.pi_client.asyncio.create_subprocess_exec", fake_spawn
            ):
                result = await agent.execute(
                    "slow", ExecutionOptions(timeout_seconds=0.01)
                )

            self.assertFalse(result.success)
            self.assertEqual(result.stop_reason, "timeout")
            self.assertEqual(
                [payload["type"] for payload in process.stdin.payloads],
                ["prompt", "abort"],
            )
            self.assertEqual(process.terminate_calls, 1)

    async def test_execute_wrapper_does_not_replay_failed_prompt(self):
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
        execute_once = make_pi_execute_with_retry(SimpleNamespace())
        with self.assertRaisesRegex(PiHarnessError, "failed"):
            await execute_once(agent, "query", None)
        self.assertEqual(agent.calls, 1)


class PiWorkspaceAndConfigTests(unittest.TestCase):
    def test_agent_files_and_skills_use_pi_project_layout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            agent_source = root / "agent-source"
            skill_source = root / "skills" / "demo-skill"
            agent_source.mkdir()
            skill_source.mkdir(parents=True)
            (agent_source / "AGENTS.md").write_text("agent", encoding="utf-8")
            (skill_source / "SKILL.md").write_text(
                "---\nname: demo-skill\ndescription: test\n---\n",
                encoding="utf-8",
            )
            manager = PiWorkspaceManager(str(root / "workspaces"))

            manager.setup_agent_files(
                agent_name="demo",
                config_files=["AGENTS.md"],
                skill_base_dir=str(root / "skills"),
                agent_skills=["demo-skill"],
                agent_dir=str(agent_source),
            )

            workspace = manager.get_agent_workspace("demo")
            self.assertTrue((workspace / "AGENTS.md").is_file())
            self.assertTrue(
                (workspace / ".agents/skills/demo-skill/SKILL.md").is_file()
            )
            self.assertFalse((workspace / "skills/demo-skill").exists())

    def test_agent_manager_uses_simulator_model_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            client = PiClient()
            workspace_manager = PiWorkspaceManager(temp_dir)
            override = AgentModelConfig(
                model="model-b",
                provider="provider-b",
                base_url="https://provider-b.example/v1",
                api_key="plain-text-key",
                api="openai-completions",
            )
            manager = PiAgentManager(
                client, workspace_manager, {"main": override}
            )
            config = SimpleNamespace(
                name="main", model="provider-a/model-a", system_prompt=None
            )

            asyncio.run(manager.setup_agent(config))

            defaults = client._agent_defaults["main"]
            self.assertEqual(defaults.model, "model-b")
            self.assertEqual(defaults.model_provider, "provider-b")
            self.assertEqual(defaults.config_dir, Path(temp_dir) / "main/.pi-agent")
            models_config = json.loads(
                (defaults.config_dir / "models.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                models_config,
                {
                    "providers": {
                        "provider-b": {
                            "baseUrl": "https://provider-b.example/v1",
                            "api": "openai-completions",
                            "models": [{"id": "model-b"}],
                            "apiKey": "plain-text-key",
                        }
                    }
                },
            )

    def test_three_shared_configs_use_fixed_pi_workspace(self):
        config_names = (
            "config_simple.json",
            "config_user.json",
            "config_simple_eval.json",
        )
        with patch("src.pi_client.PiWorkspaceManager") as manager_class:
            workspace_manager = manager_class.return_value
            for config_name in config_names:
                with self.subTest(config=config_name):
                    config = ConfigLoader.load_from_file(
                        str(PROJECT_ROOT / "configs" / config_name)
                    )
                    config.harness_type = "pi"

                    automation = HarnessAutomation(config)

                    manager_class.assert_called_once_with("~/.pi/workspace")
                    self.assertIs(automation.workspace_manager, workspace_manager)
                    manager_class.reset_mock()


if __name__ == "__main__":
    unittest.main()
