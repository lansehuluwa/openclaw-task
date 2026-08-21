"""
统一查询执行器

共享的查询循环逻辑(变量替换、simulator 多轮对话、turn 管理),
harness 差异通过 get_agent_fn / execute_with_retry_fn 回调注入。
"""

import asyncio
import logging
import re
from typing import Any, Callable, Awaitable, Dict, List, Optional
from pathlib import Path

from user_simulator import User_simulator
from src.config import QueryItem
from src.evaluator.evaluator import (
    Evaluator, 
    EvaluateConfig,
    create_evaluator,
    _restore_eval_files, 
    _isolate_eval_files
)
from src.evaluator.trajectory import (
    Trajectory, 
    ToolCallEvidence,
    build_turn_record, 
    capture_file_evidence,
    extract_tool_calls,
    extract_tool_calls_openai,
)

logger = logging.getLogger("harness_automation")

EXECUTION_MAX_ATTEMPTS = 5
EXECUTION_RETRY_WAIT_SECONDS = 60
EXECUTION_HISTORY_FALLBACK_LIMIT = 50
EXECUTION_HISTORY_FALLBACK_MAX_POLLS = 40
EXECUTION_HISTORY_FALLBACK_POLL_INTERVAL_SECONDS = 30.0

# 后台检测轮询参数
BG_WATCH_INTERVAL = 60.0   # 轮询间隔(秒)
BG_WATCH_TIMEOUT = 600  # 单轮兜底超时(秒);正常路径靠 stopReason 全收敛

def _replace_variables(text: str, results: Dict[str, Any]) -> str:
    pattern = r'\{result_(\w+)\}'

    def replacer(match):
        result_key = match.group(0)[1:-1]
        result = results.get(result_key)
        if result is None:
            return f"[Error: {result_key} not found]"
        elif hasattr(result, 'content'):
            return result.content
        return str(result)

    return re.sub(pattern, replacer, text)

def _gateway_of(agent):
    """取被测 agent 挂载的 openclaw 网关;非 openclaw(hirms/cc)返回 None。"""
    return getattr(getattr(agent, "_client", None), "gateway", None)


async def _safe_chat_history(agent, session_key: Optional[str] = None) -> List[dict[str, Any]]:
    """安全拉取被测 agent 会话历史(失败降级为空,绝不中断主流程)。"""
    gateway = _gateway_of(agent)
    if gateway is None:
        return []
    try:
        return await gateway.chat_history(
            session_key or agent.session_key, limit=EXECUTION_HISTORY_FALLBACK_LIMIT
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("chat_history 采集失败: %s", e)
        return []


async def _check_child_session_done(
    gateway: Any, child_key: str,
) -> tuple[bool, str]:
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


def _new_messages_since(
    before: List[dict[str, Any]], after: List[dict[str, Any]]
) -> List[dict[str, Any]]:
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


def _latest_assistant_text(messages: List[dict[str, Any]]) -> str:
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


import json as _json


def _extract_spawned_child_keys(new_msgs: List[dict[str, Any]]) -> List[str]:
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


def _extract_completed_child_keys(new_msgs: List[dict[str, Any]]) -> List[str]:
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


def _is_placeholder_message(msg: dict[str, Any]) -> bool:
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


def _latest_deliverable_text(messages: List[dict[str, Any]]) -> str:
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
    agent, before_history: List[dict[str, Any]] = None,
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


# 后台观察返回状态
BG_NEW_CONTENT = "new_content"      # 拿到子代理全部完成后的父 agent 真实交付
BG_TIMEOUT = "timeout"              # 到达 timeout 或子代理进程失效,提前退出


async def _background_watch(
    agent,
    before_history: List[dict[str, Any]],
    pending_children: List[str],
) -> tuple[str, str]:
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


async def process_turn(
    client: Any,
    query: QueryItem,
    turn: int,
    current_query: str,
    result: Any,
    evidence_incomplete: bool,
    trajectory: Trajectory,
    evaluator: Optional[Evaluator],
    agent: Any = None,
    before_history: Optional[List[dict[str, Any]]] = None,
) -> Optional[str]:
    """逐轮处理(仅多轮 simulator 路径):能力1 每轮捕获带证据轨迹 + 能力2 按 eval_step 节流评估。
    
    单轮对话不进入本函数(不采集轨迹)。每个 turn 都捕获轨迹(供评审窗口取数),
    但仅在评审点(`turn % eval_step == 0`)触发 evaluator;被跳过的轮给 simulator 喂空。

    Returns:
        evaluator_feedback: 本轮喂回 simulator 的反馈;跳过轮/未回流/评估失败均为 None。
    """
    # evaluator 未启用:轨迹无人消费(evaluator 是其唯一 reader),既不评估也不采集
    if evaluator is None:
        return None

    # 能力1:从 OC chat_history 解析本轮新增工具调用(SDK 的 ExecutionResult.tool_calls
    # 对服务端自主 agent 恒空),再逐轮捕获带证据的轨迹(文件证据升级为磁盘真相 D5)。
    # 即便本轮不评审也要捕获,否则评审点窗口取不到中间轮数据。
    # before_history 须由调用方在 execute 之前采集(本轮基线),after 在此处取以截取增量。
    turn_tool_calls: Optional[List[ToolCallEvidence]] = None
    if agent is not None:
        try:
            gateway = getattr(getattr(agent, "_client", None), "gateway", None)
            if gateway is None:
                # Hermes/ClaudeCode:无 gateway,chat_history 恒空。工具证据只能从
                # ExecutionResult.messages(run_conversation 返回的原生 OpenAI 消息)解析。
                # _history 只存纯文本 user/assistant 对(无 tool_calls),故整份解析
                # 天然只命中本轮工具调用,历史轮不贡献。
                native_msgs = getattr(result, "messages", None) or []
                turn_tool_calls = extract_tool_calls_openai(native_msgs)
            else:
                # OpenClaw:走网关 chat_history,按 timestamp 增量截取本轮新增消息。
                after_history = await _safe_chat_history(agent)
                new_msgs = _new_messages_since(before_history or [], after_history)
                turn_tool_calls = extract_tool_calls(new_msgs)
            # 兜底但从证据里救回了工具调用 → 不再算"证据不完整"
            if evidence_incomplete and turn_tool_calls:
                evidence_incomplete = False
        except Exception as e:  # noqa: BLE001
            logger.debug("解析本轮 tool_calls 失败,降级为空: %s", e)

    turn_record = build_turn_record(
        turn, current_query, result, evidence_incomplete, tool_calls=turn_tool_calls
    )
    try:
        await capture_file_evidence(client, query.agent_name, turn_record)
    except Exception as e:  # noqa: BLE001
        logger.debug("文件证据捕获失败: %s", e)
    trajectory.turns.append(turn_record)

    # 能力2:eval_step 节流——仅在评审点触发评估;跳过轮喂空(simulator 仍拍板)
    step = evaluator.config.eval_step
    if turn % step != 0:
        logger.debug("[Evaluator] turn=%d 未达评审点(eval_step=%d),跳过并喂空", turn, step)
        return None

    window = step  # 最近 X 轮 = eval_step,窗口正好覆盖两次评审之间的全部 turn
    logger.info("[Evaluator] 调用 agent=%s turn=%d window=%d", evaluator.config.agent_name, turn, window)
    rubric_items = evaluator.config.rubric_items()  # 结构化 rubric(旧式字符串自动归一)
    try:
        ev = await evaluator.evaluate_turn(
            trajectory, turn_record, rubric=rubric_items, window=window
        )
    except Exception as e:
        logger.warning("evaluator 调用异常: %s", e)
        ev = None

    if ev is not None:
        # 能力3:评分累积进轨迹(供落盘);终局评审点的 completion(0~1) 为该 query 最终成绩
        trajectory.evaluations.append({
            "turn": turn,
            "completion": ev.completion,
            "gate_status": ev.gate_status,
            "bucket_scores": ev.bucket_scores,
            "inclination": ev.inclination,
            "rubric_checks": [rc.model_dump() for rc in ev.rubric_checks],
        })
    # 前置检测门控:执行中(task_declared_complete=false)本轮不回流反馈给 simulator
    # (走已有空反馈降级路径);评估自身仍照常执行并已落盘,仅门控"是否回流"这一步。
    if evaluator.to_simulator and ev is not None and ev.task_declared_complete:
        return evaluator.format_feedback(ev)
    return None


async def execute_queries(
    queries: List[QueryItem],
    client: Any,
    get_agent_fn: Callable[[str, str], Any],
    execute_with_retry_fn: Callable[[Any, str, Any], Awaitable[Any]],
    simulator_factory: Optional[Callable[[], Optional[User_simulator]]] = None,
    max_turn: int = 5,
    agent_system_prompts: Optional[Dict[str, str]] = None,
    run_id: str = "",
    pre_query_hook: Optional[Callable[[], Awaitable[None]]] = None,
) -> Dict[str, Any]:
    """统一查询执行循环

    Args:
        queries: 查询任务列表 (QueryItem)
        get_agent_fn: (agent_name, session_name) -> evaluator-agent 对象
        execute_with_retry_fn: (agent, query_text, options) -> result 对象
        simulator_factory: 构造 User_simulator 的工厂(每个 session 调用一次);
            返回 None 表示未启用 simulator → 仅单轮
        max_turn: 多轮对话最大轮次
        evaluator: 第三方 Evaluator,None 或 disabled 则退回 simulator 自判
        run_id: 本次 run 的唯一 id
        pre_query_hook: 每个 query 执行前的钩子 (如 openclaw 的 check_readyz)
    """
    logger.info("=" * 60)
    logger.info("开始执行查询任务")
    logger.info("=" * 60)

    results: Dict[str, Any] = {}
    simulators: Dict[str, User_simulator] = {}

    for idx, query in enumerate(queries, 1):
        logger.info("任务 %d/%d: [%s|%s]", idx, len(queries), query.agent_name, query.session_name)
        logger.info("[Q] %s", query.text)

        query_text = _replace_variables(query.text, results)

        options = None
        if query.timeout:
            options = _make_options(query.timeout)

        base_session = query.session_name or "main"
        session_name = f"{base_session}_{run_id}"

        if pre_query_hook is not None:
            await pre_query_hook()

        # 首见即建、再见复用 → 同会话续聊保留记忆,跨会话隔离不泄露。
        query_simulator: Optional[User_simulator] = None
        if query.use_simulator and simulator_factory is not None:
            query_simulator = simulators.get(base_session)
            if query_simulator is None:
                query_simulator = simulator_factory()
                if query_simulator is not None:
                    simulators[base_session] = query_simulator

        if query_simulator is not None:
            query_simulator.update_origin_query(query_text)

        current_query = query_text
        last_result = None
        success = False
        retry = 0

        # 能力1:逐轮累积带证据的轨迹
        trajectory = Trajectory(query=query_text, agent_name=query.agent_name)
        # 能力2:per-query 构建持久 evaluator(无 evaluate 块则为 None);rubric/eval_step 取自该块。
        eval_sys_prompt = None
        if query.evaluate is not None:
            eval_sys_prompt = (agent_system_prompts or {}).get(query.evaluate.agent_name)
        evaluator = create_evaluator(query.evaluate, client, run_id, base_session, eval_sys_prompt, get_agent_fn)

        # 文件隔离:被测 agent 执行前,把本 query 的 oracle/rubrics 从磁盘删除(内容已在内存)。
        if evaluator is not None:
            _isolate_eval_files(query.evaluate)

        for turn in range(1, max_turn + 1 if query_simulator else 2):
            logger.debug("[Q%d] %s", turn, current_query)
            agent = get_agent_fn(query.agent_name, session_name)

            # 能力1:采集本轮工具证据基线(发送前的会话历史),供本轮结束后做增量解析
            before_history = await _safe_chat_history(agent)

            try:
                result, evidence_incomplete = await execute_with_retry_fn(
                    agent, current_query, options)
                last_result = result
                agent_reply = result.content
                logger.info("[A%d] %s", turn, agent_reply)
                if not agent_reply:
                    logger.debug(result)
            except Exception as e:
                import traceback
                logger.error("Agent 执行失败: %s", e)
                logger.debug(traceback.format_exc())
                break

            if not agent_reply:
                retry += 1
                if retry >= 3:
                    logger.error("连续3次未收到回复,任务失败")
                    break
                current_query = "没有看到你的回复,请重新执行。"
                continue

            retry = 0

            if query_simulator is None:
                success = True
                break
            # 后台监测: 扫描本轮增量消息，从 sessions_spawn 里提取 childSessionKey
            # 等全部子代理都完成(completion event 收齐)后才进入下一轮真实对话。
            #   - 全部完成 → 取父 agent 最终交付给 S,清空待完成集合,正常走本轮
            #   - 超时/子代理进程失效 → 兜底,注入系统提示促 simulator 追问
            bg_notice = ""
            newly_spawned = await _collect_spawned_children(agent, before_history)
            # pending_children 挂在 agent 上跨轮持久,覆盖"上一轮未完成的子代理超时未完成"的情况
            pending_children: set = getattr(agent, "_bg_pending_children", None) or set()
            pending_children.update(newly_spawned)

            if pending_children:
                logger.info(
                    "[后台检测] 待完成子代理 %d 个(turn=%d): %s",
                    len(pending_children), turn, sorted(pending_children),
                )
                bg_status, bg_text = await _background_watch(
                    agent, before_history, list(pending_children),
                )
                if bg_status == BG_NEW_CONTENT and bg_text:
                    # 全部子代理完成,父 agent 已综合交付 → 本轮真实交付
                    agent_reply = bg_text
                    result = result.model_copy(update={"content": bg_text})
                    last_result = result
                    logger.info("[A%d·后台交付] %s", turn, agent_reply)
                    pending_children.clear()
                else:
                    # 超时兜底:未完成子代理保留在 pending,带入下一轮继续追踪
                    wait_min = int(BG_WATCH_TIMEOUT // 60) or 1
                    bg_notice = (
                        f"\n\n【系统提示】后台子代理长时间未全部完成,"
                        f"已等待约 {wait_min} 分钟。请向对方确认后台任务实际进展,"
                        "或要求其给出当前已完成的结果。"
                    )
            agent._bg_pending_children = pending_children  # 空集也回写,确保状态一致

            evaluator_feedback = await process_turn(
                client, query, turn, current_query, result, evidence_incomplete,
                trajectory, evaluator, agent=agent, before_history=before_history,
            )

            user_reply = query_simulator.chat(agent_reply + bg_notice, evaluator_feedback=evaluator_feedback)
            logger.debug("[S%d] %s", turn, user_reply)

            # 空 user_reply 绝不能原样作为下一轮 current_query 下发给网关
            # (网关拒空消息 → message or attachment required → 误判连接异常空转)。
            # simulator 层已保证重试仍空时返回【Task_Failed】,此处为纵深防御:
            # 任何来源的空回复都判失败(simulator 未正常产出=故障),不下发、保留轨迹落盘。
            if not (user_reply or "").strip():
                logger.error("simulator 回复为空(Turn %d);判为失败,不下发空消息", turn)
                trajectory.outcome = "failed"
                break

            if "【Task_Done】" in user_reply:
                logger.info("任务完成(Turn %d)", turn)
                trajectory.outcome = "done"
                success = True
                break
            elif "【Task_Failed】" in user_reply:
                logger.error("任务失败(Turn %d):%s", turn, user_reply)
                trajectory.outcome = "failed"
                break

            current_query = user_reply
        else:
            if query_simulator is not None:
                trajectory.outcome = "max_turn"
                logger.warning("达到最大轮次 %d,任务未完成", max_turn)

        # 文件隔离收尾:任务结束后把 oracle/rubrics 原始字节写回(best-effort,纯调试便利)。
        if evaluator is not None:
            _restore_eval_files(query.evaluate)

        results[f"result_{query.agent_name}"] = last_result

        # 能力3:轨迹 + 评分落盘(RL 样本)。evaluator 启用时才采集了轨迹,故仅此时落盘。
        if evaluator is not None and trajectory.turns:
            try:
                out_path = Path("logs") / "trajectories" / run_id / f"{base_session}.json"
                trajectory.save(out_path)
                logger.info("轨迹已落盘: %s (turns=%d, evals=%d, outcome=%s)",
                            out_path, len(trajectory.turns), len(trajectory.evaluations), trajectory.outcome)
            except Exception as e:
                logger.warning("轨迹落盘失败: %s", e)

        if not success:
            logger.error("任务 %d 失败,终止后续 %d 个任务", idx, len(queries) - idx)
            break

    return results


def _make_options(timeout: int):
    """构造 ExecutionOptions — 延迟导入避免循环依赖。"""
    # (module, attr) 优先级序列;openclaw_sdk 在前,带 3600 上限绕过
    candidates = [
        ("openclaw_sdk", "ExecutionOptions"),
        ("src.hermes_client", "ExecutionOptions"),
        ("src.claudecode_client", "ExecutionOptions"),
        ("src.openjiuwen_client", "ExecutionOptions"),
        ("src.opencode_client", "ExecutionOptions"),
        ("src.codex_client", "ExecutionOptions"),
    ]
    for mod_path, cls_name in candidates:
        try:
            mod = __import__(mod_path, fromlist=[cls_name])
            Cls = getattr(mod, cls_name)
        except ImportError:
            continue

        # openclaw_sdk: pydantic 约束 le=3600,需绕过
        if mod_path == "openclaw_sdk":
            opts = Cls()
            try:
                object.__setattr__(opts, "timeout_seconds", int(timeout))
            except Exception:
                from pydantic import Field
                class _Unbounded(Cls):
                    timeout_seconds: int = Field(default=300, ge=1)
                opts = _Unbounded(timeout_seconds=int(timeout))
            if timeout > 3600:
                logger.info("openclaw: 已绕过 SDK 3600 上限,向网关下发单次超时 %ds", timeout)
            return opts

        # 其余 harness:简单 dataclass,直接构造
        return Cls(timeout_seconds=timeout)

    return None