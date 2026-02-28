import json
import os
from typing import Optional

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .db import FavorabilityDB, NicknameAmbiguousError, SchemaMismatchError

REQUIRED_MIN_LEVEL = -100
REQUIRED_MAX_LEVEL = 100


@register(
    "astrbot_plugin_favorability_system",
    "SaltedDoubao",
    "角色扮演好感度记录系统，提供好感度的增删查改工具供 LLM 调用",
    "0.1.0",
)
class FavorabilityPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}
        self.db: Optional[FavorabilityDB] = None
        self.min_level: int = 0
        self.max_level: int = 0
        self.tiers: list[dict] = []

    async def initialize(self):
        try:
            self.min_level = self._parse_required_int("min_level")
            self.max_level = self._parse_required_int("max_level")
            self._validate_level_bounds()

            tiers = self._parse_required_tiers("tiers")
            self.tiers = self._validate_and_normalize_tiers(tiers)

            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                data_dir = os.path.join(get_astrbot_data_path(), "favorability")
            except ImportError:
                data_dir = os.path.join(os.path.dirname(__file__), "data")

            db_path = os.path.join(data_dir, "favorability.db")
            self.db = FavorabilityDB(db_path)
            logger.info(f"[FavorabilityPlugin] 数据库已初始化: {db_path}")
        except (ValueError, SchemaMismatchError) as exc:
            logger.error(f"[FavorabilityPlugin] 初始化失败: {exc}")
            raise

    def _parse_required_int(self, key: str) -> int:
        if key not in self._config:
            raise ValueError(f"缺少必填配置项: {key}")
        raw = self._config.get(key)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 不是合法整数: {raw}") from exc

    def _parse_required_tiers(self, key: str) -> list[dict]:
        if key not in self._config:
            raise ValueError(f"缺少必填配置项: {key}")

        raw = self._config.get(key)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        if isinstance(raw, str):
            if not raw.strip():
                raise ValueError("tiers 配置为空")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("tiers 不是合法 JSON") from exc
        else:
            parsed = raw

        if not isinstance(parsed, list) or not parsed:
            raise ValueError("tiers 必须是非空数组")

        return parsed

    def _validate_level_bounds(self):
        if self.min_level != REQUIRED_MIN_LEVEL or self.max_level != REQUIRED_MAX_LEVEL:
            raise ValueError(
                f"当前版本要求 min_level={REQUIRED_MIN_LEVEL}, max_level={REQUIRED_MAX_LEVEL}"
            )

    def _validate_and_normalize_tiers(self, tiers: list[dict]) -> list[dict]:
        normalized: list[dict] = []
        for index, tier in enumerate(tiers):
            if not isinstance(tier, dict):
                raise ValueError(f"tiers[{index}] 必须是对象")

            for field_name in ("name", "min", "max", "effect"):
                if field_name not in tier:
                    raise ValueError(f"tiers[{index}] 缺少字段: {field_name}")

            name = str(tier["name"]).strip()
            effect = str(tier["effect"]).strip()
            if not name:
                raise ValueError(f"tiers[{index}].name 不能为空")
            if not effect:
                raise ValueError(f"tiers[{index}].effect 不能为空")

            try:
                min_value = int(tier["min"])
                max_value = int(tier["max"])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"tiers[{index}] 的 min/max 必须是整数") from exc

            if min_value > max_value:
                raise ValueError(f"tiers[{index}] 区间非法: min > max")

            normalized.append(
                {
                    "name": name,
                    "min": min_value,
                    "max": max_value,
                    "effect": effect,
                }
            )

        normalized.sort(key=lambda x: x["min"])

        expected_min = self.min_level
        for index, tier in enumerate(normalized):
            min_value = tier["min"]
            max_value = tier["max"]

            if min_value != expected_min:
                raise ValueError(
                    f"tiers[{index}] 起始值应为 {expected_min}，实际为 {min_value}"
                )

            expected_min = max_value + 1

        if expected_min - 1 != self.max_level:
            raise ValueError(
                f"tiers 未覆盖到最大值 {self.max_level}，当前结束于 {expected_min - 1}"
            )

        return normalized

    def _get_tier(self, level: int) -> Optional[dict]:
        for tier in self.tiers:
            if tier["min"] <= level <= tier["max"]:
                return tier
        return None

    def _clamp_level(self, level: int) -> int:
        return max(self.min_level, min(self.max_level, level))

    def _resolve_session_context(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            raise ValueError("无法解析发送者 ID")

        sender_name = str(event.get_sender_name() or "").strip() or sender_id

        if event.is_private_chat():
            return "private", sender_id, sender_name

        group_id = str(event.get_group_id() or "").strip()
        if not group_id:
            raise ValueError("群聊事件缺少 group_id，无法定位会话")

        return "group", group_id, sender_name

    def _format_session(self, session_type: str, session_id: str) -> str:
        return f"{session_type}:{session_id}"

    @llm_tool(name="fav_query")
    async def fav_query(self, event: AstrMessageEvent, identifier: str):
        """查询当前会话内用户的好感度等级和层级效果。identifier 可以是用户 ID 或当前昵称。

        Args:
            identifier(string): 用户 ID 或当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        normalized_identifier = str(identifier or "").strip()
        if not normalized_identifier:
            return "identifier 不能为空"

        user = self.db.get_user(session_type, session_id, normalized_identifier)
        if not user:
            try:
                user = self.db.find_user_by_current_nickname(
                    session_type, session_id, normalized_identifier
                )
            except NicknameAmbiguousError as exc:
                candidates = "、".join(exc.user_ids)
                return (
                    f"当前会话内昵称「{normalized_identifier}」匹配到多个用户（{candidates}）。"
                    "请改用用户 ID 查询。"
                )

        if not user:
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）未找到用户「{normalized_identifier}」，"
                "请先使用 fav_add_user 注册"
            )

        tier = self._get_tier(user.level)
        tier_info = f"【{tier['name']}】{tier['effect']}" if tier else "未知层级"

        current_nickname = user.current_nickname or "无"
        historical_nicknames = (
            "、".join(user.historical_nicknames) if user.historical_nicknames else "无"
        )

        return (
            f"会话: {self._format_session(session_type, session_id)}\n"
            f"用户 ID: {user.user_id}\n"
            f"当前昵称: {current_nickname}\n"
            f"曾用名: {historical_nicknames}\n"
            f"好感度: {user.level}\n"
            f"当前层级: {tier_info}"
        )

    @llm_tool(name="fav_ensure")
    async def fav_ensure(self, event: AstrMessageEvent, user_id: str, nickname: str):
        """查询当前会话内用户的好感度与层级效果；若用户不存在则自动注册。每轮对话开始时调用。

        Args:
            user_id(string): 用户 ID
            nickname(string): 用户当前昵称（仅在自动注册时使用）
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        normalized_id = str(user_id or "").strip()
        if not normalized_id:
            return "user_id 不能为空"

        user = self.db.get_user(session_type, session_id, normalized_id)
        registered = False

        if not user:
            normalized_nickname = str(nickname or "").strip() or normalized_id
            initial_level = self._clamp_level(0)
            if not self.db.add_user(
                session_type, session_id, normalized_id, initial_level
            ):
                return "自动注册失败"
            self.db.upsert_current_nickname(
                session_type, session_id, normalized_id, normalized_nickname
            )
            user = self.db.get_user(session_type, session_id, normalized_id)
            if not user:
                return "自动注册后查询失败"
            registered = True

        tier = self._get_tier(user.level)
        tier_info = f"【{tier['name']}】{tier['effect']}" if tier else "未知层级"

        msg = ""
        if registered:
            msg += "[新用户已注册]\n"
        msg += f"好感度: {user.level}\n当前层级: {tier_info}"
        return msg

    @llm_tool(name="fav_update")
    async def fav_update(self, event: AstrMessageEvent, user_id: str, level: int):
        """设置当前会话内用户的好感度等级（绝对值）。

        Args:
            user_id(string): 用户 ID
            level(number): 新的好感度等级
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        try:
            target_level = int(level)
        except (TypeError, ValueError):
            return f"等级 {level} 不是有效整数"

        clamped = self._clamp_level(target_level)
        if not self.db.update_level(session_type, session_id, user_id, clamped):
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{user_id}」不存在"
            )

        tier = self._get_tier(clamped)
        tier_info = f"【{tier['name']}】{tier['effect']}" if tier else ""
        msg = (
            f"已将会话 {self._format_session(session_type, session_id)} 中"
            f"「{user_id}」的好感度更新为 {clamped}"
        )
        if clamped != target_level:
            msg += f"（已限制在 {self.min_level}~{self.max_level} 范围内）"
        if tier_info:
            msg += f"\n当前层级: {tier_info}"
        return msg

    @llm_tool(name="fav_add_user")
    async def fav_add_user(self, event: AstrMessageEvent, user_id: str, nickname: str):
        """在当前会话注册新用户并设置当前昵称。初始好感度由配置范围约束。

        Args:
            user_id(string): 用户唯一 ID
            nickname(string): 用户当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        initial_level = self._clamp_level(0)
        normalized_nickname = str(nickname or "").strip() or user_id

        if not self.db.add_user(session_type, session_id, user_id, initial_level):
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{user_id}」已存在"
            )

        if not self.db.upsert_current_nickname(
            session_type, session_id, user_id, normalized_nickname
        ):
            self.db.remove_user(session_type, session_id, user_id)
            return "注册失败：无法设置当前昵称"

        return (
            f"已在会话 {self._format_session(session_type, session_id)} 注册用户"
            f"「{user_id}」，当前昵称「{normalized_nickname}」，初始好感度: {initial_level}"
        )

    @llm_tool(name="fav_delta")
    async def fav_delta(self, event: AstrMessageEvent, user_id: str, delta: int):
        """对当前会话内用户的好感度施加相对变化量（正数增加，负数减少），无需先查询当前值。

        Args:
            user_id(string): 用户 ID
            delta(number): 好感度变化量，正数增加，负数减少
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        try:
            delta_value = int(delta)
        except (TypeError, ValueError):
            return f"delta {delta} 不是有效整数"

        user = self.db.get_user(session_type, session_id, user_id)
        if not user:
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{user_id}」不存在"
            )

        new_level = self._clamp_level(user.level + delta_value)
        self.db.update_level(session_type, session_id, user_id, new_level)

        tier = self._get_tier(new_level)
        tier_info = f"【{tier['name']}】{tier['effect']}" if tier else ""
        msg = f"「{user_id}」好感度 {user.level:+d} → {new_level}"
        if tier_info:
            msg += f"\n当前层级: {tier_info}"
        return msg

    @llm_tool(name="fav_remove_user")
    async def fav_remove_user(self, event: AstrMessageEvent, user_id: str):
        """删除当前会话内用户及其所有昵称记录。

        Args:
            user_id(string): 用户 ID
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        if not self.db.remove_user(session_type, session_id, user_id):
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{user_id}」不存在"
            )

        return (
            f"已删除会话 {self._format_session(session_type, session_id)} 中"
            f"用户「{user_id}」及其所有昵称"
        )

    @llm_tool(name="fav_add_nickname")
    async def fav_add_nickname(
        self, event: AstrMessageEvent, user_id: str, nickname: str
    ):
        """更新当前会话内用户的当前昵称，旧昵称会自动沉淀为曾用名。

        Args:
            user_id(string): 用户 ID
            nickname(string): 新的当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        user = self.db.get_user(session_type, session_id, user_id)
        if not user:
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{user_id}」不存在"
            )

        new_nickname = str(nickname or "").strip()
        if not new_nickname:
            return "nickname 不能为空"
        old_nickname = user.current_nickname

        if old_nickname == new_nickname:
            return f"用户「{user_id}」当前昵称已是「{new_nickname}」"

        if not self.db.upsert_current_nickname(
            session_type, session_id, user_id, new_nickname
        ):
            return f"为用户「{user_id}」设置当前昵称失败"

        if old_nickname:
            return (
                f"已将用户「{user_id}」当前昵称从「{old_nickname}」更新为「{new_nickname}」，"
                "旧昵称已计入曾用名"
            )

        return f"已为用户「{user_id}」设置当前昵称「{new_nickname}」"

    @llm_tool(name="fav_remove_nickname")
    async def fav_remove_nickname(
        self, event: AstrMessageEvent, user_id: str, nickname: str
    ):
        """删除当前会话内用户的当前昵称。

        Args:
            user_id(string): 用户 ID
            nickname(string): 要删除的当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        user = self.db.get_user(session_type, session_id, user_id)
        if not user:
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{user_id}」不存在"
            )

        if not self.db.remove_current_nickname(
            session_type, session_id, user_id, nickname
        ):
            return f"未找到用户「{user_id}」的当前昵称「{nickname}」"

        return (
            f"已删除用户「{user_id}」的当前昵称「{nickname}」。"
            "该用户当前无昵称，请使用 fav_add_nickname 设置新的当前昵称。"
        )

    @llm_tool(name="fav_get_effect")
    async def fav_get_effect(self, event: AstrMessageEvent, level: int):
        """查询指定好感度等级对应的层级名称和效果描述。

        Args:
            level(number): 好感度等级数值
        """
        try:
            normalized_level = int(level)
        except (TypeError, ValueError):
            return f"等级 {level} 不是有效整数"

        tier = self._get_tier(normalized_level)
        if not tier:
            return f"等级 {normalized_level} 不在任何已定义的层级范围内"
        return f"等级 {normalized_level} 对应层级【{tier['name']}】：{tier['effect']}"

    @filter.command("好感度查询")
    async def cmd_fav_query(self, event: AstrMessageEvent):
        """查询自己在当前会话中的好感度等级和层级信息。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return

        try:
            session_type, session_id, sender_name = self._resolve_session_context(event)
        except ValueError as exc:
            yield event.plain_result(f"会话上下文异常: {exc}")
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            yield event.plain_result("无法获取你的用户 ID")
            return

        user = self.db.get_user(session_type, session_id, sender_id)
        if not user:
            yield event.plain_result("你还没有被记录在当前会话中哦。")
            return

        nickname = user.current_nickname or "无"

        yield event.plain_result(
            f"昵称: {nickname}（{sender_id}）\n好感度: {user.level}"
        )

    @filter.command("fav-init")
    async def cmd_fav_init(self, event: AstrMessageEvent):
        """在当前会话中注册自己的好感度记录。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return

        try:
            session_type, session_id, sender_name = self._resolve_session_context(event)
        except ValueError as exc:
            yield event.plain_result(f"会话上下文异常: {exc}")
            return

        sender_id = str(event.get_sender_id() or "").strip()
        if not sender_id:
            yield event.plain_result("无法获取你的用户 ID")
            return

        user = self.db.get_user(session_type, session_id, sender_id)
        if user:
            yield event.plain_result(f"你已经注册过了，当前好感度: {user.level}")
            return

        initial_level = self._clamp_level(0)
        nickname = sender_name or sender_id

        if not self.db.add_user(session_type, session_id, sender_id, initial_level):
            yield event.plain_result("注册失败，请稍后重试。")
            return

        self.db.upsert_current_nickname(session_type, session_id, sender_id, nickname)

        yield event.plain_result(
            f"注册成功！\n昵称: {nickname}\n好感度: {initial_level}"
        )

    async def terminate(self):
        if self.db:
            self.db.close()
            logger.info("[FavorabilityPlugin] 数据库连接已关闭")
