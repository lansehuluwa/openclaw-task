"""Codex harness Linux 端到端测试。

覆盖：
1. 多 agent 使用不同 model_provider；
2. 单 agent 同时加载两个 workspace skill；
3. 同一 agent/session 的真实 Codex thread 多轮复用；
4. 测试在临时 CODEX_HOME 中准备配置，不修改服务器已有配置。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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


def _require_environment() -> None:
    missing = [
        name
        for name in ("DEEPSEEK_MAIN_API_KEY", "EVAL_PROXY_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"Linux 环境变量未配置: {', '.join(missing)}")

    catalog = Path.home() / ".codex" / "models.json"
    if not catalog.is_file():
        raise RuntimeError(f"缺少 DeepSeek models.json: {catalog}")


async def _run() -> None:
    _require_environment()
    # 测试自行模拟部署前置步骤：把已写好的配置和模型目录放入临时 CODEX_HOME。
    with tempfile.TemporaryDirectory(prefix="openclaw-task-codex-") as temp_dir:
        isolated_root = Path(temp_dir)
        codex_home = isolated_root / "home"
        codex_home.mkdir()
        model_catalog = codex_home / "models.json"
        shutil.copy2(Path.home() / ".codex" / "models.json", model_catalog)
        config_text = (
            PROJECT_ROOT / "configs" / "codex_config_deepseek.toml"
        ).read_text(encoding="utf-8")
        (codex_home / "config.toml").write_text(
            f"model_catalog_json = {json.dumps(str(model_catalog.resolve()))}\n"
            f"{config_text}",
            encoding="utf-8",
        )
        workspace_manager = CodexWorkspaceManager(
            str(isolated_root / "workspace")
        )
        client = await build_codex_client(codex_home=str(codex_home))

        async with client:
            manager = CodexAgentManager(client, workspace_manager)
            agent_specs = [
                SimpleNamespace(
                    name="deepseek-main",
                    model="deepseek-v4-flash",
                    model_provider="deepseek_main",
                    system_prompt="严格按用户要求简洁回答。",
                ),
                SimpleNamespace(
                    name="eval-proxy",
                    model="deepseek-v4-flash",
                    model_provider="eval_proxy",
                    system_prompt="严格按用户要求简洁回答。",
                ),
                SimpleNamespace(
                    name="multi-skill",
                    model="deepseek-v4-flash",
                    model_provider="deepseek_main",
                    system_prompt="需要技能时必须读取对应 SKILL.md。",
                ),
                SimpleNamespace(
                    name="conversation",
                    model="deepseek-v4-flash",
                    model_provider="deepseek_main",
                    system_prompt="记住同一会话前文并严格回答。",
                ),
            ]
            for spec in agent_specs:
                await manager.setup_agent(spec)

            workspace_manager.setup_agent_files(
                agent_name="multi-skill",
                config_files=[],
                skill_base_dir=str(PROJECT_ROOT / "skills"),
                agent_skills=["secret-checker", "agent-browser"],
            )

            options = ExecutionOptions(timeout_seconds=180)
            main_result = await client.get_agent(
                "deepseek-main", "provider-main"
            ).execute(
                "只回复 PROVIDER_DEEPSEEK_MAIN_OK，不要添加其他内容。",
                options,
            )
            eval_result = await client.get_agent(
                "eval-proxy", "provider-eval"
            ).execute(
                "只回复 PROVIDER_EVAL_PROXY_OK，不要添加其他内容。",
                options,
            )
            skill_result = await client.get_agent(
                "multi-skill", "multi-skill"
            ).execute(
                "请显式使用 $secret-checker 和 $agent-browser。第一行只输出 "
                "secret-checker 验证码；第二行用一句中文说明 agent-browser 的核心"
                "用途。不要执行浏览器操作。",
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
    assert main_result.model_provider == "deepseek_main"
    assert "PROVIDER_DEEPSEEK_MAIN_OK" in main_result.content
    assert eval_result.success, eval_result.error_message
    assert eval_result.model_provider == "eval_proxy"
    assert "PROVIDER_EVAL_PROXY_OK" in eval_result.content
    assert skill_result.success, skill_result.error_message
    assert "HARNESS-SKILL-OK-7F3A" in skill_result.content
    assert "agent-browser" in skill_result.content.lower()
    assert conversation_result.success, conversation_result.error_message
    assert "CODEX-MULTI-TURN-9A31" in conversation_result.content

    print("Codex 多 agent/provider 测试通过")
    print("Codex 单 agent 多 skill 测试通过")
    print("Codex 同 thread 多轮对话测试通过")
    print("临时 CODEX_HOME 已清理；服务器已有 Codex 配置未修改")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    asyncio.run(_run())
