"""Codex harness Linux 端到端测试。

覆盖：
1. 使用部署前已配置的真实 Responses provider；
2. workspace skill 使用 Codex 原生目录加载；
3. 同一 agent/session 的真实 Codex thread 多轮复用；
4. 测试只读取指定 CODEX_HOME，不生成或修改 Codex 配置。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.codex_client import (
    CodexAgentManager,
    CodexWorkspaceManager,
    ExecutionOptions,
    build_codex_client,
)


def _require_environment() -> tuple[Path, str, str]:
    missing = [
        name
        for name in ("CODEX_E2E_HOME", "CODEX_E2E_MODEL", "CODEX_E2E_PROVIDER")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"Linux 环境变量未配置: {', '.join(missing)}")

    codex_home = Path(os.environ["CODEX_E2E_HOME"]).expanduser()
    if not (codex_home / "config.toml").is_file():
        raise RuntimeError(f"CODEX_E2E_HOME 缺少 config.toml: {codex_home}")
    return (
        codex_home,
        os.environ["CODEX_E2E_MODEL"],
        os.environ["CODEX_E2E_PROVIDER"],
    )


async def _run() -> None:
    codex_home, model, model_provider = _require_environment()
    with tempfile.TemporaryDirectory(prefix="openclaw-task-codex-") as temp_dir:
        isolated_root = Path(temp_dir)
        workspace_manager = CodexWorkspaceManager(
            str(isolated_root / "workspace")
        )
        client = await build_codex_client(codex_home=str(codex_home))

        async with client:
            manager = CodexAgentManager(client, workspace_manager)
            agent_specs = [
                SimpleNamespace(
                    name="provider-main",
                    model=model,
                    model_provider=model_provider,
                    system_prompt="严格按用户要求简洁回答。",
                ),
                SimpleNamespace(
                    name="skill-agent",
                    model=model,
                    model_provider=model_provider,
                    system_prompt="需要技能时必须读取对应 SKILL.md。",
                ),
                SimpleNamespace(
                    name="conversation",
                    model=model,
                    model_provider=model_provider,
                    system_prompt="记住同一会话前文并严格回答。",
                ),
            ]
            for spec in agent_specs:
                await manager.setup_agent(spec)

            workspace_manager.setup_agent_files(
                agent_name="skill-agent",
                config_files=[],
                skill_base_dir=str(PROJECT_ROOT / "skills"),
                agent_skills=["agent-browser"],
            )

            options = ExecutionOptions(timeout_seconds=180)
            main_result = await client.get_agent(
                "provider-main", "provider-main"
            ).execute(
                "只回复 CODEX_RESPONSES_PROVIDER_OK，不要添加其他内容。",
                options,
            )
            skill_result = await client.get_agent(
                "skill-agent", "workspace-skill"
            ).execute(
                "请显式使用 $agent-browser，但不要执行浏览器操作。只回复该技能核心"
                "工作流的四个英文步骤，并使用 > 分隔。",
                options,
            )
            conversation = client.get_agent("conversation", "memory")
            first_turn = await conversation.execute(
                "请记住会话标记 CODEX-MULTI-TURN-9A31，只回复『已记住』。",
                options,
            )
            assert first_turn.success, first_turn.error_message
            conversation_result = await conversation.execute(
                "只回复上一轮让我记住的会话标记，不要添加其他内容。",
                options,
            )

    assert main_result.success, main_result.error_message
    assert main_result.model_provider == model_provider
    assert "CODEX_RESPONSES_PROVIDER_OK" in main_result.content
    assert skill_result.success, skill_result.error_message
    assert "snapshot" in skill_result.content.lower()
    assert "re-snapshot" in skill_result.content.lower()
    assert conversation_result.success, conversation_result.error_message
    assert "CODEX-MULTI-TURN-9A31" in conversation_result.content

    print("Codex 真实 Responses provider 测试通过")
    print("Codex workspace skill 测试通过")
    print("Codex 同 thread 多轮对话测试通过")
    print("临时 workspace 已清理；指定 CODEX_HOME 配置未修改")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    asyncio.run(_run())
