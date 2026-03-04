import inspect
from dataclasses import dataclass
from functools import wraps
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal, Optional


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
        if inspect.isasyncgenfunction(func):

            @wraps(func)
            async def asyncgen_wrapper(
                self, event: Any, *args: Any, **kwargs: Any
            ) -> AsyncGenerator[Any, None]:
                try:
                    session_ctx = resolve_session_context(event)
                except ValueError as exc:
                    if mode == "yield_error":
                        yield event.plain_result(f"会话上下文异常: {exc}")
                    return
                call_kwargs = dict(kwargs)
                if call_kwargs.get("session_ctx") is None:
                    call_kwargs["session_ctx"] = session_ctx
                async for item in func(self, event, *args, **call_kwargs):
                    yield item

            return asyncgen_wrapper

        @wraps(func)
        async def async_wrapper(self, event: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                session_ctx = resolve_session_context(event)
            except ValueError as exc:
                if mode == "return_error":
                    return f"会话上下文异常: {exc}"
                return None
            call_kwargs = dict(kwargs)
            if call_kwargs.get("session_ctx") is None:
                call_kwargs["session_ctx"] = session_ctx
            return await func(self, event, *args, **call_kwargs)

        return async_wrapper

    return decorator
