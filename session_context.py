import inspect
from dataclasses import dataclass
from functools import wraps
from typing import Any, AsyncGenerator, Callable, Literal, Optional


Mode = Literal["silent", "return_error", "yield_error"]


@dataclass(frozen=True)
class SessionContext:
    session_type: str
    session_id: str
    sender_name: str
    sender_id: str


def resolve_session_context(event: Any) -> SessionContext:
    sender_id = str(event.get_sender_id() or "").strip()
    if not sender_id:
        raise ValueError("无法解析发送者 ID")
    sender_name = str(event.get_sender_name() or "").strip() or sender_id

    if event.is_private_chat():
        return SessionContext(
            session_type="private",
            session_id=sender_id,
            sender_name=sender_name,
            sender_id=sender_id,
        )

    group_id = str(event.get_group_id() or "").strip()
    if not group_id:
        raise ValueError("群聊事件缺少 group_id，无法定位会话")
    return SessionContext(
        session_type="group",
        session_id=group_id,
        sender_name=sender_name,
        sender_id=sender_id,
    )


def with_session_context(mode: Mode = "silent") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        sig = inspect.signature(func)
        params = list(sig.parameters.values())
        has_extra_positional = len(params) > 2 and params[2].kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        )
        public_sig = sig.replace(
            parameters=[
                param
                for name, param in sig.parameters.items()
                if name != "session_ctx"
            ]
        )

        def _resolve_injected_session_ctx(
            event: Any,
            kwargs: dict[str, Any],
        ) -> tuple[bool, SessionContext | str]:
            session_ctx = kwargs.get("session_ctx")
            if isinstance(session_ctx, SessionContext):
                return True, session_ctx
            try:
                return True, resolve_session_context(event)
            except ValueError as exc:
                return False, str(exc)

        if inspect.isasyncgenfunction(func):

            @wraps(func)
            async def asyncgen_wrapper(
                self, event: Any, *args: Any, **kwargs: Any
            ) -> AsyncGenerator[Any, None]:
                ok, result = _resolve_injected_session_ctx(event, kwargs)
                if not ok:
                    if mode == "yield_error":
                        yield event.plain_result(f"会话上下文异常: {result}")
                    return
                call_kwargs = dict(kwargs)
                call_kwargs["session_ctx"] = result
                if has_extra_positional:
                    async for item in func(self, event, *args, **call_kwargs):
                        yield item
                else:
                    async for item in func(self, event, **call_kwargs):
                        yield item

            asyncgen_wrapper.__signature__ = public_sig
            return asyncgen_wrapper

        @wraps(func)
        async def async_wrapper(self, event: Any, *args: Any, **kwargs: Any) -> Any:
            ok, result = _resolve_injected_session_ctx(event, kwargs)
            if not ok:
                if mode == "return_error":
                    return f"会话上下文异常: {result}"
                return None
            call_kwargs = dict(kwargs)
            call_kwargs["session_ctx"] = result
            if has_extra_positional:
                return await func(self, event, *args, **call_kwargs)
            else:
                return await func(self, event, **call_kwargs)

        async_wrapper.__signature__ = public_sig
        return async_wrapper

    return decorator
