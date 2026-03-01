import json
import math
import os
import time
from typing import Optional

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .db import FavorabilityDB, NicknameAmbiguousError, SchemaMismatchError, User

REQUIRED_MIN_LEVEL = -100
REQUIRED_MAX_LEVEL = 100

INTERACTION_BASE_DELTA = {
    "small_talk": 2,
    "thanks": 4,
    "helpful_dialogue": 5,
    "deep_talk": 6,
    "celebration": 9,
    "cold": -2,
    "rude": -6,
    "abuse": -10,
}

INTENSITY_MULTIPLIER = {1: 0.8, 2: 1.0, 3: 1.25}
POSITIVE_BIAS_FACTOR = 1.15
ANTI_SPAM_WINDOW_SEC = 120
TEN_MIN_WINDOW_SEC = 600
TEN_MIN_POSITIVE_CAP = 20
DAILY_POSITIVE_CAP = 50
PER_ROUND_MIN_DELTA = -12
PER_ROUND_MAX_DELTA = 12
MAX_EVIDENCE_LENGTH = 120


@register(
    "astrbot_plugin_favorability_system",
    "SaltedDoubao",
    "角色扮演好感度记录系统，提供好感度的增删查改工具供 LLM 调用",
    "0.2.0",
)
class FavorabilityPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}
        self.db: Optional[FavorabilityDB] = None
        self.min_level: int = 0
        self.max_level: int = 0
        self.tiers: list[dict] = []
        self.decay_enabled: bool = False
        self.idle_days_threshold: int = 14
        self.decay_per_day: int = 1

    async def initialize(self):
        try:
            self.min_level = self._parse_required_int("min_level")
            self.max_level = self._parse_required_int("max_level")
            self._validate_level_bounds()

            tiers = self._parse_required_tiers("tiers")
            self.tiers = self._validate_and_normalize_tiers(tiers)

            self.decay_enabled = self._parse_optional_bool("decay_enabled", False)
            self.idle_days_threshold = self._parse_optional_int(
                "idle_days_threshold", 14, min_value=0
            )
            self.decay_per_day = self._parse_optional_int(
                "decay_per_day", 1, min_value=1
            )

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

    def _parse_optional_int(self, key: str, default: int, min_value: int = 0) -> int:
        if key not in self._config:
            return default
        raw = self._config.get(key)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 不是合法整数: {raw}") from exc
        if value < min_value:
            raise ValueError(f"配置项 {key} 不能小于 {min_value}")
        return value

    def _parse_optional_bool(self, key: str, default: bool) -> bool:
        if key not in self._config:
            return default

        raw = self._config.get(key)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]

        if isinstance(raw, bool):
            return raw
        if isinstance(raw, int):
            return raw != 0
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False

        raise ValueError(f"配置项 {key} 不是合法布尔值: {raw}")

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

    def _get_today_bucket(self, now_ts: Optional[int] = None) -> str:
        ts = now_ts or int(time.time())
        return time.strftime("%Y-%m-%d", time.localtime(ts))

    def _normalize_nickname(self, nickname: str, user_id: str) -> Optional[str]:
        normalized = str(nickname or "").strip()
        if not normalized:
            return None
        if normalized == user_id:
            return None
        return normalized

    def _coerce_user(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        nickname: str,
        update_nickname: bool = True,
    ) -> tuple[Optional[User], bool]:
        if not self.db:
            return None, False

        user = self.db.get_user(session_type, session_id, user_id)
        registered = False
        normalized_nickname = self._normalize_nickname(nickname, user_id)

        if not user:
            if not self.db.add_user(session_type, session_id, user_id, self._clamp_level(0)):
                return None, False
            if normalized_nickname:
                self.db.upsert_current_nickname(
                    session_type, session_id, user_id, normalized_nickname
                )
            user = self.db.get_user(session_type, session_id, user_id)
            registered = True
        elif (
            update_nickname
            and normalized_nickname
            and user.current_nickname != normalized_nickname
        ):
            self.db.upsert_current_nickname(
                session_type, session_id, user_id, normalized_nickname
            )
            user = self.db.get_user(session_type, session_id, user_id)

        return user, registered

    def _refresh_daily_bucket(
        self,
        session_type: str,
        session_id: str,
        user: User,
        now_ts: Optional[int] = None,
    ) -> User:
        if not self.db:
            return user

        today = self._get_today_bucket(now_ts)
        if user.daily_bucket == today:
            return user

        self.db.update_level(
            session_type,
            session_id,
            user.user_id,
            user.level,
            daily_pos_gain=0,
            daily_neg_gain=0,
            daily_bucket=today,
        )
        user.daily_pos_gain = 0
        user.daily_neg_gain = 0
        user.daily_bucket = today
        return user

    def _apply_decay_if_needed(
        self,
        session_type: str,
        session_id: str,
        user: User,
        now_ts: Optional[int] = None,
    ) -> User:
        if not self.db:
            return user
        if not self.decay_enabled:
            return user
        if not user.last_interaction_at:
            return user

        now = now_ts or int(time.time())
        idle_days = max(0, (now - user.last_interaction_at) // 86400)
        if idle_days <= self.idle_days_threshold:
            return user

        decay_days = idle_days - self.idle_days_threshold
        decay_amount = decay_days * self.decay_per_day

        if user.level > 0:
            new_level = max(0, user.level - decay_amount)
        elif user.level < 0:
            new_level = min(0, user.level + decay_amount)
        else:
            new_level = user.level

        if new_level == user.level:
            return user

        settled_last_interaction = now - self.idle_days_threshold * 86400
        self.db.update_level(
            session_type,
            session_id,
            user.user_id,
            new_level,
            last_interaction_at=settled_last_interaction,
        )
        user.level = new_level
        user.last_interaction_at = settled_last_interaction
        return user

    def _interpolate(
        self,
        value: int,
        min_level: int,
        max_level: int,
        start: float,
        end: float,
    ) -> float:
        if max_level <= min_level:
            return start
        ratio = (value - min_level) / float(max_level - min_level)
        ratio = max(0.0, min(1.0, ratio))
        return start + (end - start) * ratio

    def _build_style_payload(self, level: int) -> tuple[float, dict]:
        if level <= -51:
            style_weight = self._interpolate(level, -100, -51, 0.32, 0.38)
        elif level <= -11:
            style_weight = self._interpolate(level, -50, -11, 0.38, 0.44)
        elif level <= 9:
            style_weight = self._interpolate(level, -10, 9, 0.44, 0.50)
        elif level <= 39:
            style_weight = self._interpolate(level, 10, 39, 0.60, 0.69)
        else:
            style_weight = self._interpolate(level, 40, 100, 0.70, 0.78)

        warmth = int(max(0, min(100, round(50 + level * 0.45))))
        initiative = int(max(0, min(100, round(45 + level * 0.40))))
        boundary = int(max(0, min(100, round(55 - level * 0.35))))
        playfulness = int(max(0, min(100, round(35 + level * 0.50))))

        return round(style_weight, 2), {
            "warmth": warmth,
            "initiative": initiative,
            "boundary": boundary,
            "playfulness": playfulness,
        }

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

    @llm_tool(name="fav_profile")
    async def fav_profile(self, event: AstrMessageEvent, user_id: str, nickname: str):
        """回复前拉取用户好感度画像，必要时自动注册用户。

        Args:
            user_id(string): 用户 ID
            nickname(string): 用户当前昵称
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

        user, registered = self._coerce_user(
            session_type,
            session_id,
            normalized_id,
            str(nickname or "").strip() or normalized_id,
        )
        if not user:
            return "无法初始化用户画像"

        now_ts = int(time.time())
        user = self._refresh_daily_bucket(session_type, session_id, user, now_ts)
        user = self._apply_decay_if_needed(session_type, session_id, user, now_ts)

        tier = self._get_tier(user.level)
        tier_name = tier["name"] if tier else "未知"
        effect_brief = tier["effect"] if tier else ""
        style_weight, style_axes = self._build_style_payload(user.level)

        payload = {
            "level": user.level,
            "tier": tier_name,
            "style_weight": style_weight,
            "style_axes": style_axes,
            "effect_brief": effect_brief,
            "registered": registered,
        }
        return json.dumps(payload, ensure_ascii=False)

    @llm_tool(name="fav_assess")
    async def fav_assess(
        self,
        event: AstrMessageEvent,
        user_id: str,
        interaction_type: str,
        intensity: int,
        evidence: str = "",
    ):
        """回复后评估交互质量，应用反刷和限幅策略后更新好感度。

        Args:
            user_id(string): 用户 ID
            interaction_type(string): 交互类型
            intensity(number): 强度 1~3
            evidence(string): 评分依据简述
        """
        if not self.db:
            return "好感度系统未初始化"

        try:
            session_type, session_id, sender_name = self._resolve_session_context(event)
        except ValueError as exc:
            return f"会话上下文异常: {exc}"

        normalized_id = str(user_id or "").strip()
        if not normalized_id:
            return "user_id 不能为空"

        interaction_key = str(interaction_type or "").strip().lower()
        if interaction_key not in INTERACTION_BASE_DELTA:
            allow = ", ".join(INTERACTION_BASE_DELTA.keys())
            return f"interaction_type 非法，允许值: {allow}"

        try:
            normalized_intensity = int(intensity)
        except (TypeError, ValueError):
            return f"intensity {intensity} 不是有效整数"
        if normalized_intensity not in INTENSITY_MULTIPLIER:
            return "intensity 必须是 1、2、3"

        user, _ = self._coerce_user(
            session_type,
            session_id,
            normalized_id,
            "",
            update_nickname=False,
        )
        if not user:
            return "无法初始化用户资料"

        now_ts = int(time.time())
        user = self._refresh_daily_bucket(session_type, session_id, user, now_ts)
        user = self._apply_decay_if_needed(session_type, session_id, user, now_ts)

        old_level = user.level
        base_delta = INTERACTION_BASE_DELTA[interaction_key]
        intensity_mul = INTENSITY_MULTIPLIER[normalized_intensity]
        raw_delta = round(base_delta * intensity_mul)

        positive_bias = 1.0
        if raw_delta > 0:
            positive_bias = POSITIVE_BIAS_FACTOR
            raw_delta = round(raw_delta * positive_bias)

        anti_spam_mul = 1.0
        if raw_delta > 0:
            positive_count = self.db.count_positive_events_by_type_since(
                session_type,
                session_id,
                normalized_id,
                interaction_key,
                now_ts - ANTI_SPAM_WINDOW_SEC,
            )
            occurrence = positive_count + 1
            if occurrence == 1:
                anti_spam_mul = 1.0
            elif occurrence == 2:
                anti_spam_mul = 0.75
            elif occurrence == 3:
                anti_spam_mul = 0.5
            else:
                anti_spam_mul = 0.3

        final_delta = round(raw_delta * anti_spam_mul)

        cap_clip = {
            "per_round": False,
            "ten_min_positive": False,
            "daily_positive": False,
            "global_level": False,
        }

        clipped = max(PER_ROUND_MIN_DELTA, min(PER_ROUND_MAX_DELTA, final_delta))
        if clipped != final_delta:
            cap_clip["per_round"] = True
        final_delta = clipped

        if final_delta > 0:
            ten_min_gain = self.db.sum_positive_delta_since(
                session_type,
                session_id,
                normalized_id,
                now_ts - TEN_MIN_WINDOW_SEC,
            )
            remaining_10m = max(0, TEN_MIN_POSITIVE_CAP - ten_min_gain)
            if final_delta > remaining_10m:
                final_delta = remaining_10m
                cap_clip["ten_min_positive"] = True

            remaining_daily = max(0, DAILY_POSITIVE_CAP - user.daily_pos_gain)
            if final_delta > remaining_daily:
                final_delta = remaining_daily
                cap_clip["daily_positive"] = True

        proposed_level = user.level + final_delta
        new_level = self._clamp_level(proposed_level)
        if new_level != proposed_level:
            cap_clip["global_level"] = True

        effective_delta = new_level - user.level

        daily_pos_gain = user.daily_pos_gain
        daily_neg_gain = user.daily_neg_gain
        if effective_delta > 0:
            daily_pos_gain += effective_delta
        elif effective_delta < 0:
            daily_neg_gain += abs(effective_delta)

        user.daily_bucket = user.daily_bucket or self._get_today_bucket(now_ts)
        self.db.update_level(
            session_type,
            session_id,
            normalized_id,
            new_level,
            last_interaction_at=now_ts,
            daily_pos_gain=daily_pos_gain,
            daily_neg_gain=daily_neg_gain,
            daily_bucket=user.daily_bucket,
        )

        evidence_text = str(evidence or "").strip()[:MAX_EVIDENCE_LENGTH]
        self.db.log_score_event(
            session_type,
            session_id,
            normalized_id,
            interaction_key,
            normalized_intensity,
            raw_delta,
            effective_delta,
            anti_spam_mul,
            now_ts,
            evidence_text,
        )

        tier_after = self._get_tier(new_level)
        tier_name = tier_after["name"] if tier_after else "未知"

        logger.info(
            "[FavorabilityPlugin] assess"
            f" session={self._format_session(session_type, session_id)}"
            f" user={normalized_id}"
            f" type={interaction_key}"
            f" intensity={normalized_intensity}"
            f" old={old_level} new={new_level}"
            f" raw={raw_delta} final={effective_delta}"
            f" anti_spam_mul={anti_spam_mul}"
            f" cap={cap_clip}"
            f" evidence={evidence_text}"
        )

        result = {
            "old_level": old_level,
            "new_level": new_level,
            "raw_delta": raw_delta,
            "final_delta": effective_delta,
            "factors": {
                "intensity_mul": intensity_mul,
                "positive_bias": positive_bias,
                "anti_spam_mul": anti_spam_mul,
                "cap_clip": cap_clip,
            },
            "tier_after": tier_name,
        }
        return json.dumps(result, ensure_ascii=False)

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

        nickname = user.current_nickname or sender_name or "无"

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
        nickname = self._normalize_nickname(sender_name, sender_id)
        display_nickname = nickname or sender_id

        if not self.db.add_user(session_type, session_id, sender_id, initial_level):
            yield event.plain_result("注册失败，请稍后重试。")
            return

        if nickname:
            self.db.upsert_current_nickname(session_type, session_id, sender_id, nickname)

        yield event.plain_result(
            f"注册成功！\n昵称: {display_nickname}\n好感度: {initial_level}"
        )

    @filter.command("fav-rl")
    async def cmd_fav_ranking(self, event: AstrMessageEvent):
        """查看当前会话的好感度排行榜。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return

        try:
            session_type, session_id, _ = self._resolve_session_context(event)
        except ValueError as exc:
            yield event.plain_result(f"会话上下文异常: {exc}")
            return

        raw = (event.message_str or "").strip()
        page = 1
        if raw:
            parts = raw.split()
            if parts and parts[0].lstrip("/!").lower() == "fav-rl":
                parts = parts[1:]
            if len(parts) > 1:
                yield event.plain_result("参数过多，仅支持可选页码：fav-rl [页码]")
                return
            page_str = parts[0] if parts else ""
            if page_str and (not page_str.isdigit() or int(page_str) < 1):
                yield event.plain_result("页码必须是正整数")
                return
            if page_str:
                page = int(page_str)

        per_page = 10
        users, total = self.db.get_ranking(
            session_type, session_id, per_page, (page - 1) * per_page
        )

        if total == 0:
            yield event.plain_result("当前会话还没有好感度记录")
            return

        total_pages = math.ceil(total / per_page)
        if page > total_pages:
            yield event.plain_result(f"页码超出范围，共 {total_pages} 页")
            return

        lines = ["好感度排行"]
        for u in users:
            name = u.current_nickname or u.user_id
            lines.append(f"{name}（{u.user_id}）：{u.level}")
        lines.append("---")
        lines.append(f"{page}/{total_pages}")

        yield event.plain_result("\n".join(lines))

    async def terminate(self):
        if self.db:
            self.db.close()
            logger.info("[FavorabilityPlugin] 数据库连接已关闭")
