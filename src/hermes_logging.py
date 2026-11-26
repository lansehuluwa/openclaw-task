"""Hermes 日志兼容入口；各 Agent 共用 src.agent_logging。"""

from pathlib import Path

from src.agent_logging import AgentLogBridge


class HermesLogBridge(AgentLogBridge):
    def __init__(self, agent: str, session: str, log_dir: Path | None = None):
        super().__init__("hermes", agent, session, log_dir or Path("logs") / "hermes")
