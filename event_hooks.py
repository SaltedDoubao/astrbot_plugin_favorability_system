import time
from typing import Any

from .session_context import SessionContext


async def handle_on_llm_request(
    plugin: Any,
    event: Any,
    req: Any,
    *,
    session_ctx: SessionContext,
) -> None:
    if not plugin.auto_style_injection_enabled or not plugin.db:
        return
    sender_id = session_ctx.sender_id
    user, _ = plugin._coerce_user(
        session_ctx.session_type,
        session_ctx.session_id,
        sender_id,
        session_ctx.sender_name,
    )
    if not user:
        return
    now_ts = int(time.time())
    user = plugin._refresh_daily_bucket(
        session_ctx.session_type, session_ctx.session_id, user, now_ts
    )
    user = plugin._apply_decay_if_needed(
        session_ctx.session_type, session_ctx.session_id, user, now_ts
    )
    if plugin.style_prompt_mode != "short_tier":
        return
    prompt_lines = [plugin._build_short_style_prompt(user.level)]
    tier_notice = plugin._build_tier_change_notice(
        session_ctx.session_type, session_ctx.session_id, sender_id, now_ts
    )
    if tier_notice:
        prompt_lines.append(tier_notice)
    stable_hint = plugin._stable_status_hint(user)
    if stable_hint:
        prompt_lines.append(f"状态提示：{stable_hint}。请保持自然与分寸。")
    current_prompt = str(getattr(req, "system_prompt", "") or "")
    setattr(req, "system_prompt", f"{current_prompt}\n" + "\n".join(prompt_lines))
    setattr(req, "system_prompt", str(getattr(req, "system_prompt", "")).strip())


async def handle_on_llm_response(
    plugin: Any,
    event: Any,
    resp: Any,
    *,
    session_ctx: SessionContext,
) -> None:
    if not plugin.auto_assess_enabled or not plugin.db:
        return
    user_text = str(getattr(event, "message_str", "") or "").strip()
    if not user_text:
        return
    if plugin.auto_assess_skip_commands and plugin._is_command_message(user_text):
        return
    classification = plugin._classify_interaction_rule_v1(user_text)
    if not classification:
        return
    completion_text = str(getattr(resp, "completion_text", "") or "").strip()
    event_key = plugin._build_event_key(
        session_ctx.session_type, session_ctx.session_id, session_ctx.sender_id, event
    )
    now_ts = int(time.time())
    plugin._cleanup_cache(plugin._pending_assessment, now_ts)
    plugin._pending_assessment[event_key] = {
        "created_at": now_ts,
        "session_type": session_ctx.session_type,
        "session_id": session_ctx.session_id,
        "user_id": session_ctx.sender_id,
        "user_text": user_text,
        "bot_text": completion_text[:200],
        "interaction_type": classification["interaction_type"],
        "intensity": classification["intensity"],
        "evidence": classification["evidence"],
    }


async def handle_after_message_sent(
    plugin: Any,
    event: Any,
    *,
    session_ctx: SessionContext,
) -> None:
    if not plugin.auto_assess_enabled or not plugin.db:
        return
    event_key = plugin._build_event_key(
        session_ctx.session_type, session_ctx.session_id, session_ctx.sender_id, event
    )
    pending = plugin._pending_assessment.pop(event_key, None)
    if not pending:
        return
    now_ts = int(time.time())
    expired = [
        key
        for key, ts in plugin._recent_assessed_keys.items()
        if now_ts - ts > plugin.pending_context_ttl_sec
    ]
    for key in expired:
        plugin._recent_assessed_keys.pop(key, None)
    if event_key in plugin._recent_assessed_keys:
        return
    plugin._recent_assessed_keys[event_key] = now_ts
    ok, result = plugin._apply_assessment_internal(
        session_type=pending["session_type"],
        session_id=pending["session_id"],
        user_id=pending["user_id"],
        interaction_type=pending["interaction_type"],
        intensity=pending["intensity"],
        evidence=pending["evidence"],
        source="auto_hook",
    )
    if not ok:
        from astrbot.api import logger

        logger.warning(
            "[FavorabilityPlugin] auto_assess skipped"
            f" reason={result}"
            f" session={plugin._format_session(session_ctx.session_type, session_ctx.session_id)}"
            f" user={session_ctx.sender_id}"
        )
