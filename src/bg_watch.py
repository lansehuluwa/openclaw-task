"""
后台监测机制

独立的后台子代理完成检测逻辑。当被测 agent 在 execute 中 spawn 了子代理
(sessions_spawn),父 agent 的 execute 会立即返回,但子代理仍在后台运行。
本模块负责轮询等待所有 spawn 的子代理都完成,才取出父 agent 的真实交付文本。

完成判定分两个信号,任一命中即算该子代理完成:
  信号1 (completion event): 父会话 chat_history 增量中出现 role=user 的
    "[Internal task completion event]",从中提取 session_key 匹配。
  信号2 (子代理 history 直查 stopReason): 直接调 gateway.chat_history(child_key)
    stopReason=="stop" → 子代理已产出最终回复、执行结束。

嵌套 spawn 追踪: 每轮轮询还扫描父会话增量里的新 spawn(childSessionKey),
加入 pending。这覆盖"child1 完成后父 agent 立即 spawn child2"的嵌套场景。
"""

import asyncio
import json as _json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("harness_automation")

# 后台检测轮询参数
BG_WATCH_INTERVAL = 60.0   # 轮询间隔(秒)
BG_WATCH_TIMEOUT = 3600    # 单轮兜底超时(秒);正常路径靠 stopReason 全收敛

# 后台观察返回状态
BG_NEW_CONTENT = "new_content"      # 拿到子代理全部完成后的父 agent 真实交付
BG_TIMEOUT = "timeout"              # 到达 timeout 或子代理进程失效,提前退出

# chat_history 拉取条数上限
_HISTORY_LIMIT = 50


def _gateway_of(agent):
    """取被测 agent 挂载的 openclaw 网关;非 openclaw(hirms/cc)返回 None。"""
    return getattr(getattr(agent, "_client", None), "gateway", None)


async def _safe_chat_history(
    agent, session_key: Optional[str] = None
) -> List[Dict[str, Any]]:
    """安全拉取被测 agent 会话历史(失败降级为空,绝不中断主流程)。"""
    gateway = _gateway_of(agent)
    if gateway is None:
        return []
    try:
        return await gateway.chat_history(
            session_key or agent.session_key, limit=_HISTORY_LIMIT
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("chat_history 采集失败: %s", e)
        return []


def _new_messages_since(
    before: List[Dict[str, Any]], after: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """从 after 取出相对 before 新增的消息(按 timestamp 界,稳健于 limit 截断)。"""
    if not after:
        return []
    if not before:
        return list(after)
    before_max_ts = max(
        (m.get("timestamp", 0) for m in before if isinstance(m, dict)), default=0
    )
    return [
        m for m in after
        if isinstance(m, dict) and m.get("timestamp", 0) > before_max_ts
    ]


async def _check_child_session_done(
    gateway: Any, child_key: str
) -> Tuple[bool, str]:
    """直接查子代理自身的 session history,判断其是否已结束执行。

    判定逻辑:取子会话 history 的最后一条 assistant 消息,检查其 stopReason 字段。
    stopReason=="stop" 表示子代理已产出最终回复、执行结束。
    stopReason=="toolUse" 表示还在调工具、尚未结束。

    Returns:
        (is_done, reason) — is_done=True 时 reason 描述判定依据。
    """
    if gateway is None:
        return False, "no gateway"
    try:
        child_history = await gateway.chat_history(child_key, limit=10)
    except Exception as e:  # noqa: BLE001
        logger.debug("[子代理直查] %s history 获取失败: %s", child_key, e)
        return False, f"history error: {e}"

    if not child_history:
        return False, "empty history"

    # 找最后一条 assistant 消息
    last_asst = None
    for msg in reversed(child_history):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() != "assistant":
            continue
        last_asst = msg
        break

    if last_asst is None:
        return False, "no assistant message"

    stop_reason = str(last_asst.get("stopReason", "")).lower()
    if stop_reason == "stop":
        return True, "stopReason=stop"
    if stop_reason in ("tooluse", "tool_use", "tool_use"):
        return False, f"stopReason={stop_reason} (still using tools)"
    if stop_reason:
        return False, f"stopReason={stop_reason} (not conclusive)"

    # 没有 stopReason 字段:降级为旧逻辑——有文本就算完成
    content = last_asst.get("content")
    text = ""
    if isinstance(content, str):
        text = content.strip()
    elif isinstance(content, list):
        parts: List[str] = []
        for b in content:
            if isinstance(b, dict):
                v = b.get("text") or b.get("content")
                if isinstance(v, str):
                    parts.append(v)
        text = "".join(parts).strip()
    if text:
        return True, "no stopReason but last assistant msg has text"
    return False, "no stopReason and no text"


def _latest_assistant_text(messages: List[Dict[str, Any]]) -> str:
    """从消息列表取最后一条 assistant 的纯文本(content 可能是 str 或 block 列表)。"""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            parts: List[str] = []
            for b in content:
                if not isinstance(b, dict):
                    continue
                v = b.get("text") or b.get("content")
                if isinstance(v, str):
                    parts.append(v)
            text = "".join(parts).strip()
        else:
            t = msg.get("text")
            text = t.strip() if isinstance(t, str) else ""
        if text:
            return text
    return ""


def _extract_spawned_child_keys(new_msgs: List[Dict[str, Any]]) -> List[str]:
    """从本轮 execute 新增消息中,提取 sessions_spawn 工具返回的 childSessionKey 集合。

    JSONL 结构:role=toolResult, toolName=sessions_spawn, content 是 JSON 文本或
    block 列表,里面含 "childSessionKey": "agent:main:subagent:<uuid>"。
    """
    keys: List[str] = []
    for msg in new_msgs:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() != "toolresult":
            continue
        tn = str(msg.get("toolName") or msg.get("tool_name") or "").lower()
        if tn != "sessions_spawn":
            continue
        # content 可能是 str 或 block 列表,块里 text 是 JSON 文本
        content = msg.get("content")
        raw_texts: List[str] = []
        if isinstance(content, str):
            raw_texts.append(content)
        elif isinstance(content, list):
            for b in content:
                if isinstance(b, dict):
                    v = b.get("text") or b.get("content")
                    if isinstance(v, str):
                        raw_texts.append(v)
        # 也兼容 details.childSessionKey 直接字段
        details = msg.get("details")
        if isinstance(details, dict):
            k = details.get("childSessionKey")
            if isinstance(k, str) and k:
                keys.append(k)
        for txt in raw_texts:
            try:
                d = _json.loads(txt)
            except Exception:
                continue
            if isinstance(d, dict):
                k = d.get("childSessionKey")
                if isinstance(k, str) and k:
                    keys.append(k)
    # 去重保序
    seen = set()
    uniq = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            uniq.append(k)
    return uniq


def _extract_completed_child_keys(new_msgs: List[Dict[str, Any]]) -> List[str]:
    """从消息中提取"子代理完成事件"里对应的 session_key。

    Openclaw 把子代理完成通知作为 role=user 消息注入父会话,内容含
    `<<<BEGIN_OPENCLAW_INTERNAL_CONTEXT>>>` 和 `session_key: <key>`。
    """
    keys: List[str] = []
    for msg in new_msgs:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() != "user":
            continue
        c = msg.get("content")
        txt = c if isinstance(c, str) else ""
        if not txt and isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and isinstance(b.get("text"), str):
                    txt += b["text"]
        if "Internal task completion event" not in txt:
            continue
        # 抓 "session_key: agent:main:subagent:<uuid>"
        m = re.search(r"session_key\s*:\s*(agent:[^\s]+)", txt)
        if m:
            keys.append(m.group(1).strip())
    return keys


def _is_placeholder_message(msg: Dict[str, Any]) -> bool:
    """判断一条消息是否属于"不该视为父 agent 真实交付"的占位/内部事件。

    覆盖(全部跳过):
      1. cli echo (api=cli 的 assistant 重放)
      2. sessions_yield 回执 (customType=openclaw.sessions_yield / content 含标记)
      3. 子代理 completion event (role=user + Internal task completion event)
      4. heartbeat poll (role=user + 内容 "[OpenClaw heartbeat poll]")
      5. NO_REPLY 占位
      6. 只含 tool_use/toolCall 块、无文本的 assistant 消息(非交付)
    """
    if not isinstance(msg, dict):
        return False
    role = str(msg.get("role", "")).lower()
    api = str(msg.get("api", "")).lower()
    ctype = str(msg.get("customType") or msg.get("custom_type") or "").lower()
    c = msg.get("content")
    txt = c if isinstance(c, str) else ""
    if not txt and isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and isinstance(b.get("text"), str):
                txt += b["text"]
    if txt.strip() == "NO_REPLY":
        return True
    if role == "assistant" and api == "cli":
        return True
    if "sessions_yield" in ctype:
        return True
    if "previous turn ended intentionally via sessions_yield" in txt:
        return True
    if role == "user" and "Internal task completion event" in txt:
        return True
    if role == "user" and "OpenClaw heartbeat poll" in txt:
        return True
    # assistant 只带 toolCall/tool_use 块、无任何文本 → 非交付
    if role == "assistant" and isinstance(c, list):
        has_text = any(
            isinstance(b, dict) and isinstance(b.get("text"), str) and b["text"].strip()
            for b in c
        )
        only_tools = all(
            isinstance(b, dict) and b.get("type") in ("toolCall", "tool_use", "thinking")
            for b in c
        )
        if not has_text and only_tools:
            return True
    return False


def _latest_deliverable_text(messages: List[Dict[str, Any]]) -> str:
    """取最后一条真实"父 agent 交付"文本:跳过 _is_placeholder_message 命中的所有占位。"""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")).lower() != "assistant":
            continue
        if _is_placeholder_message(msg):
            continue
        c = msg.get("content")
        if isinstance(c, str):
            text = c.strip()
        elif isinstance(c, list):
            parts: List[str] = []
            for b in c:
                if not isinstance(b, dict):
                    continue
                v = b.get("text") or b.get("content")
                if isinstance(v, str):
                    parts.append(v)
            text = "".join(parts).strip()
        else:
            t = msg.get("text")
            text = t.strip() if isinstance(t, str) else ""
        if text:
            return text
    return ""


async def _collect_spawned_children(
    agent, before_history: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """本轮 execute 新增的子代理 spawn childSessionKey 集合。

    只看本轮 execute 相对 before_history 的增量消息里,role=toolResult 且
    toolName=sessions_spawn 返回的 childSessionKey。无 gateway 返回空。
    """
    gateway = _gateway_of(agent)
    if gateway is None:
        return []
    after_history = await _safe_chat_history(agent)
    new_msgs = _new_messages_since(before_history or [], after_history)
    keys = _extract_spawned_child_keys(new_msgs)
    if keys:
        logger.info("[后台检测] 本轮 spawn %d 个子代理: %s", len(keys), keys)
    return keys


async def _background_watch(
    agent,
    before_history: List[Dict[str, Any]],
    pending_children: List[str],
) -> Tuple[str, str]:
    """后台监测:轮询等待 spawn 的所有子代理都完成,才返回父 agent 的真实交付。

    完成判定分两个信号,任一命中即算该子代理完成:

      **信号1 (completion event)**: 父会话 chat_history 增量中出现 role=user 的
      "[Internal task completion event]",从中提取 session_key 匹配。

      **信号2 (子代理 history 直查 stopReason)**: 直接调 gateway.chat_history(child_key)
      stopReason=="stop" → 子代理已产出最终回复、执行结束。

    **嵌套 spawn 追踪**: 每轮轮询还扫描父会话增量里的新 spawn(childSessionKey),
      加入 pending。这覆盖"child1 完成后父 agent 立即 spawn child2"的嵌套场景——
      bg-watch 不会因 pending 暂时清空就提前返回中间产物。

    每轮轮询顺序:
      1. 父 chat_history 增量:completion event → 移除;新 spawn → 加入 pending
      2. 子代理 history 直查 stopReason:最后一条 assistant 的 stopReason=="stop" → 移除
      3. pending 全部清空且本轮无新 spawn → 取父 agent 真实交付
      4. 到 max_polls(BG_WATCH_TIMEOUT)兜底 → BG_TIMEOUT

    期间所有"中间产物"(cli echo / yield 回执 / heartbeat / 部分子代理完成)都不算
    交付、不触发下一轮交互,因此不占用 max_turn。
    """
    parent_key = agent.session_key
    gateway = _gateway_of(agent)

    # 基线:execute 之后的当前 history,后续轮询用 _new_messages_since 取增量
    base_history = await _safe_chat_history(agent, parent_key)

    pending = set(pending_children)
    max_polls = max(1, int(BG_WATCH_TIMEOUT // BG_WATCH_INTERVAL))

    for poll in range(1, max_polls + 1):
        await asyncio.sleep(BG_WATCH_INTERVAL)

        # 1. 扫描父会话增量:completion event 移除 + 新 spawn 加入 pending
        after_history = await _safe_chat_history(agent, parent_key)
        new_msgs = _new_messages_since(base_history, after_history)
        if new_msgs:
            # 1a. 辅助信号:completion event 中的 session_key → 移除
            completed = _extract_completed_child_keys(new_msgs)
            for k in completed:
                if k in pending:
                    pending.discard(k)
                    logger.info(
                        "[后台检测] 子代理 %s 完成 (via completion event),剩余 %d 个",
                        k, len(pending),
                    )
            # 1b. 嵌套 spawn:增量里新出现的 childSessionKey → 加入 pending
            new_spawns = _extract_spawned_child_keys(new_msgs)
            for k in new_spawns:
                if k not in pending:
                    pending.add(k)
                    logger.info(
                        "[后台检测] 嵌套 spawn 检测到新子代理 %s,加入 pending"
                        "(剩余 %d 个)",
                        k, len(pending),
                    )
            # 增量已处理,推进基线
            base_history = after_history

        # 2. 主信号:直查子代理 session history 的 stopReason
        #    取最后一条 assistant 消息,stopReason=="stop" → 已产出最终回复
        if gateway and pending:
            for k in list(pending):
                is_done, reason = await _check_child_session_done(gateway, k)
                if is_done:
                    pending.discard(k)
                    logger.info(
                        "[后台检测] 子代理 %s 完成 (via child history: %s),剩余 %d 个",
                        k, reason, len(pending),
                    )

        # 3. 全部子代理完成且本轮无新 spawn → 取父 agent 真实交付
        if not pending:
            full_history = await _safe_chat_history(agent, parent_key)
            text = _latest_deliverable_text(full_history)
            if text:
                logger.info(
                    "[后台检测] 全部 %d 个子代理完成,取到父 agent 交付"
                    "(等待约 %.0fs)",
                    len(pending_children), poll * BG_WATCH_INTERVAL,
                )
                return BG_NEW_CONTENT, text
            # 全部完成但父 agent 交付尚未落盘,继续等下一轮(父可能在综合)

        logger.debug(
            "[后台检测] 第 %d/%d 次轮询,未完成子代理 %d 个: %s",
            poll, max_polls, len(pending), sorted(pending),
        )

    logger.info(
        "[后台检测] 等待约 %.0fs 仍未等到全部子代理完成(剩余 %d 个),超时兜底",
        max_polls * BG_WATCH_INTERVAL, len(pending),
    )
    return BG_TIMEOUT, ""
