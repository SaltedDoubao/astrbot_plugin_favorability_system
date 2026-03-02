import csv
import io
import json
import math
import os
import time
import zipfile
from typing import Any, Optional

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .config_parser import PluginConfigParser
from .db import FavorabilityDB, NicknameAmbiguousError, SchemaMismatchError, User
from .event_hooks import (
    handle_after_message_sent,
    handle_on_llm_request,
    handle_on_llm_response,
)
from .keywords import build_default_keyword_profile, load_keyword_profile
from .rule_engine import (
    DAILY_NEGATIVE_CAP_DEFAULT,
    DAILY_POSITIVE_CAP,
    AssessmentRecoverableError,
    AssessmentRuleEngine,
    AssessmentValidationError,
)
from .session_context import SessionContext, resolve_session_context, with_session_context

REQUIRED_MIN_LEVEL = -100
REQUIRED_MAX_LEVEL = 100

PENDING_CONTEXT_TTL_SEC = 900
MAX_PENDING_CONTEXT_SIZE = 2048
COMMAND_ALIASES = {
    "fav-init",
    "fav-rl",
    "好感度查询",
    "fav-reset",
    "fav-reset-all",
    "fav-export",
    "fav-stats",
}


@register(
    "astrbot_plugin_favorability_system",
    "SaltedDoubao",
    "角色扮演好感度记录系统，V2 自动注入风格并本地规则评分",
    "2.0.0",
)
class FavorabilityPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}
        self.db: Optional[FavorabilityDB] = None
        self.min_level: int = 0
        self.max_level: int = 0
        self.initial_level: int = 0
        self.tiers: list[dict] = []
        self.decay_enabled: bool = False
        self.idle_days_threshold: int = 14
        self.decay_per_day: int = 1
        self.auto_style_injection_enabled: bool = True
        self.auto_assess_enabled: bool = True
        self.auto_assess_skip_commands: bool = True
        self.negative_policy: str = "conservative"
        self.style_prompt_mode: str = "short_tier"
        self.rule_version: str = "v1"
        self.keyword_profile_path: str = ""
        self.daily_negative_cap: int = DAILY_NEGATIVE_CAP_DEFAULT
        self.data_dir: str = ""
        self.keyword_profile: dict[str, set[str]] = build_default_keyword_profile()
        self._pending_assessment: dict[str, dict[str, Any]] = {}
        self._recent_assessed_keys: dict[str, int] = {}
        self._pending_tier_notice: dict[str, dict[str, Any]] = {}
        self.pending_context_ttl_sec: int = PENDING_CONTEXT_TTL_SEC
        self._rule_engine = AssessmentRuleEngine(self)

    async def initialize(self):
        try:
            parser = PluginConfigParser(self._config)
            self.min_level = parser.parse_required_int("min_level")
            self.max_level = parser.parse_required_int("max_level")
            self._validate_level_bounds()
            self.initial_level = parser.parse_optional_int(
                "initial_level",
                0,
                min_value=self.min_level,
                max_value=self.max_level,
            )

            tiers = parser.parse_required_tiers("tiers")
            self.tiers = self._validate_and_normalize_tiers(tiers)

            self.decay_enabled = parser.parse_optional_bool("decay_enabled", False)
            self.idle_days_threshold = parser.parse_optional_int(
                "idle_days_threshold", 14, min_value=0
            )
            self.decay_per_day = parser.parse_optional_int(
                "decay_per_day", 1, min_value=1
            )
            self.auto_style_injection_enabled = parser.parse_optional_bool(
                "auto_style_injection_enabled", True
            )
            self.auto_assess_enabled = parser.parse_optional_bool(
                "auto_assess_enabled", True
            )
            self.auto_assess_skip_commands = parser.parse_optional_bool(
                "auto_assess_skip_commands", True
            )
            self.negative_policy = parser.parse_optional_choice(
                "negative_policy",
                "conservative",
                {"conservative", "balanced", "aggressive"},
            )
            self.style_prompt_mode = parser.parse_optional_choice(
                "style_prompt_mode",
                "short_tier",
                {"short_tier"},
            )
            self.rule_version = parser.parse_optional_choice(
                "rule_version",
                "v1",
                {"v1"},
            )
            self.keyword_profile_path = parser.parse_optional_str("keyword_profile_path", "")
            self.daily_negative_cap = parser.parse_optional_int(
                "daily_negative_cap",
                DAILY_NEGATIVE_CAP_DEFAULT,
                min_value=0,
            )

            try:
                from astrbot.core.utils.astrbot_path import get_astrbot_data_path

                data_dir = os.path.join(
                    get_astrbot_data_path(),
                    "data",
                    "plugin_data",
                    "astrbot_plugin_favorability_system",
                )
            except ImportError:
                data_dir = os.path.join(
                    os.path.dirname(__file__),
                    "data",
                    "plugin_data",
                    "astrbot_plugin_favorability_system",
                )

            self.data_dir = data_dir
            db_path = os.path.join(data_dir, "favorability.db")
            self.db = FavorabilityDB(db_path)
            self.keyword_profile = load_keyword_profile(
                self.keyword_profile_path,
                self.data_dir,
                logger,
            )
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

    def _parse_optional_int(
        self,
        key: str,
        default: int,
        min_value: int = 0,
        max_value: Optional[int] = None,
    ) -> int:
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
        if max_value is not None and value > max_value:
            raise ValueError(f"配置项 {key} 不能大于 {max_value}")
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

    def _parse_optional_choice(
        self,
        key: str,
        default: str,
        allowed: set[str],
    ) -> str:
        if key not in self._config:
            return default

        raw = self._config.get(key)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]

        value = str(raw or "").strip().lower()
        if not value:
            return default
        if value not in allowed:
            allow = ", ".join(sorted(allowed))
            raise ValueError(f"配置项 {key} 仅支持: {allow}")
        return value

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

    def _get_initial_level_for_new_user(self) -> int:
        return self._clamp_level(self.initial_level)

    def _resolve_session_context(self, event: AstrMessageEvent) -> tuple[str, str, str]:
        ctx = resolve_session_context(event)
        return ctx.session_type, ctx.session_id, ctx.sender_name

    def _format_session(self, session_type: str, session_id: str) -> str:
        return f"{session_type}:{session_id}"

    def _build_user_scope_key(
        self, session_type: str, session_id: str, user_id: str
    ) -> str:
        return f"{session_type}:{session_id}:{user_id}"

    def _is_admin_event(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_admin", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                pass

        role = str(getattr(event, "role", "") or "").strip().lower()
        return role in {"admin", "owner", "super_admin"}

    def _admin_only_message(self) -> str:
        return "权限不足：仅管理员可执行此操作。"

    def _stable_status_hint(self, user: User) -> Optional[str]:
        hints: list[str] = []
        if user.daily_pos_gain >= DAILY_POSITIVE_CAP:
            hints.append("今日正向增益已达上限")
        if user.level in {self.min_level, self.max_level}:
            hints.append("当前好感度已触达边界")
        if not hints:
            return None
        return "；".join(hints)

    def _build_tier_change_notice(
        self, session_type: str, session_id: str, user_id: str, now_ts: int
    ) -> Optional[str]:
        self._cleanup_cache(self._pending_tier_notice, now_ts)
        key = self._build_user_scope_key(session_type, session_id, user_id)
        payload = self._pending_tier_notice.pop(key, None)
        if not payload:
            return None
        from_tier = str(payload.get("from_tier", "未知"))
        to_tier = str(payload.get("to_tier", "未知"))
        return f"状态变化提示：该用户好感层级已从「{from_tier}」变为「{to_tier}」，请自然调整语气。"

    def _normalize_export_format(self, raw: str) -> str:
        value = str(raw or "").strip().lower() or "json"
        if value not in {"json", "csv"}:
            raise ValueError("导出格式仅支持 json 或 csv")
        return value

    def _normalize_export_scope(self, raw: str) -> str:
        value = str(raw or "").strip().lower() or "session"
        if value not in {"session", "global"}:
            raise ValueError("scope 仅支持 session 或 global")
        return value

    def _get_effective_data_dir(self) -> str:
        if self.data_dir:
            return self.data_dir
        if self.db:
            row = self.db.conn.execute("PRAGMA database_list").fetchone()
            if row and len(row) >= 3 and row[2]:
                return os.path.dirname(str(row[2]))
        return os.path.join(
            os.path.dirname(__file__),
            "data",
            "plugin_data",
            "astrbot_plugin_favorability_system",
        )

    def _reload_keyword_profile(self):
        self.keyword_profile = load_keyword_profile(
            self.keyword_profile_path,
            self._get_effective_data_dir(),
            logger,
        )

    def _write_json_export(
        self, payload: dict[str, Any], export_path: str, session_label: str
    ) -> str:
        with open(export_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)
        return (
            f"导出成功（JSON）\n"
            f"范围: {session_label}\n"
            f"users={len(payload['users'])} nicknames={len(payload['nicknames'])} events={len(payload['score_events'])}\n"
            f"文件: {export_path}"
        )

    def _write_csv_export_zip(
        self, payload: dict[str, Any], export_path: str, session_label: str
    ) -> str:
        table_headers = {
            "users": [
                "session_type",
                "session_id",
                "user_id",
                "level",
                "last_interaction_at",
                "daily_pos_gain",
                "daily_neg_gain",
                "daily_bucket",
            ],
            "nicknames": [
                "session_type",
                "session_id",
                "user_id",
                "nickname",
                "is_current",
                "created_at",
            ],
            "score_events": [
                "id",
                "session_type",
                "session_id",
                "user_id",
                "interaction_type",
                "intensity",
                "raw_delta",
                "final_delta",
                "anti_spam_mul",
                "created_at",
                "evidence",
            ],
        }
        with zipfile.ZipFile(export_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for table_name, headers in table_headers.items():
                buffer = io.StringIO()
                writer = csv.DictWriter(buffer, fieldnames=headers)
                writer.writeheader()
                for row in payload[table_name]:
                    writer.writerow({header: row.get(header) for header in headers})
                zf.writestr(f"{table_name}.csv", buffer.getvalue())
        return (
            f"导出成功（CSV ZIP）\n"
            f"范围: {session_label}\n"
            f"users={len(payload['users'])} nicknames={len(payload['nicknames'])} events={len(payload['score_events'])}\n"
            f"文件: {export_path}"
        )

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
        db_commit: bool = True,
    ) -> tuple[Optional[User], bool]:
        if not self.db:
            return None, False

        user = self.db.get_user(session_type, session_id, user_id)
        registered = False
        normalized_nickname = self._normalize_nickname(nickname, user_id)

        if not user:
            if not self.db.add_user(
                session_type,
                session_id,
                user_id,
                self._get_initial_level_for_new_user(),
                commit=db_commit,
            ):
                return None, False
            if normalized_nickname:
                self.db.upsert_current_nickname(
                    session_type,
                    session_id,
                    user_id,
                    normalized_nickname,
                    commit=db_commit,
                )
            user = self.db.get_user(session_type, session_id, user_id)
            registered = True
        elif (
            update_nickname
            and normalized_nickname
            and user.current_nickname != normalized_nickname
        ):
            self.db.upsert_current_nickname(
                session_type,
                session_id,
                user_id,
                normalized_nickname,
                commit=db_commit,
            )
            user = self.db.get_user(session_type, session_id, user_id)

        return user, registered

    def _refresh_daily_bucket(
        self,
        session_type: str,
        session_id: str,
        user: User,
        now_ts: Optional[int] = None,
        db_commit: bool = True,
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
            commit=db_commit,
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
        db_commit: bool = True,
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
            commit=db_commit,
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

    def _cleanup_cache(self, cache: dict[str, Any], now_ts: int):
        expired_keys = [
            key
            for key, payload in cache.items()
            if isinstance(payload, dict)
            and now_ts - int(payload.get("created_at", now_ts)) > PENDING_CONTEXT_TTL_SEC
        ]
        for key in expired_keys:
            cache.pop(key, None)
        if len(cache) <= MAX_PENDING_CONTEXT_SIZE:
            return
        # 按插入顺序删除最早项，避免长期累积占用内存。
        overflow = len(cache) - MAX_PENDING_CONTEXT_SIZE
        for key in list(cache.keys())[:overflow]:
            cache.pop(key, None)

    def _extract_message_id(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = getattr(message_obj, "message_id", None)
        if message_id is None:
            return f"evt_{id(event)}"
        return str(message_id)

    def _extract_unified_origin(self, event: AstrMessageEvent) -> str:
        origin = getattr(event, "unified_msg_origin", None)
        if origin is None:
            return "unknown"
        return str(origin)

    def _build_event_key(
        self,
        session_type: str,
        session_id: str,
        user_id: str,
        event: AstrMessageEvent,
    ) -> str:
        unified_origin = self._extract_unified_origin(event)
        message_id = self._extract_message_id(event)
        return (
            f"{session_type}:{session_id}:{user_id}:"
            f"{unified_origin}:{message_id}"
        )

    def _is_command_message(self, raw_text: str) -> bool:
        text = str(raw_text or "").strip()
        if not text:
            return False
        token = text.split()[0].lstrip("/!").lower()
        return token in COMMAND_ALIASES

    def _classify_interaction_rule_v1(
        self, text: str
    ) -> Optional[dict[str, str | int]]:
        return self._rule_engine.classify_interaction_rule_v1(text)

    def _build_short_style_prompt(self, level: int) -> str:
        tier = self._get_tier(level)
        style_hint = (
            str(tier["effect"]).strip() if tier and str(tier.get("effect", "")).strip() else ""
        )
        if not style_hint:
            style_hint = "自然客观，礼貌回应。"
        return f"当前用户交互风格：{style_hint}"

    def _apply_assessment_internal(
        self,
        *,
        session_type: str,
        session_id: str,
        user_id: str,
        interaction_type: str,
        intensity: int,
        evidence: str = "",
        source: str = "auto_hook",
    ) -> tuple[bool, dict[str, Any] | str]:
        if not self.db:
            return False, "好感度系统未初始化"
        try:
            result = self._rule_engine.apply_assessment(
                session_type=session_type,
                session_id=session_id,
                user_id=user_id,
                interaction_type=interaction_type,
                intensity=intensity,
                evidence=evidence,
                source=source,
            )
        except AssessmentValidationError as exc:
            return False, str(exc)
        except AssessmentRecoverableError:
            logger.exception("[FavorabilityPlugin] 评分事务失败（可恢复）")
            return False, "评分写入失败，请稍后重试"
        except Exception:
            logger.exception("[FavorabilityPlugin] 评分事务失败（未预期异常）")
            raise

        normalized_id = str(user_id or "").strip()
        now_ts = int(time.time())
        if result["tier_before"] != result["tier_after"]:
            self._pending_tier_notice[
                self._build_user_scope_key(session_type, session_id, normalized_id)
            ] = {
                "created_at": now_ts,
                "from_tier": result["tier_before"],
                "to_tier": result["tier_after"],
            }
        return True, result

    @filter.on_llm_request()
    @with_session_context(mode="silent")
    async def on_llm_request(
        self,
        event: AstrMessageEvent,
        req,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        if not session_ctx:
            return
        await handle_on_llm_request(self, event, req, session_ctx=session_ctx)

    @filter.on_llm_response()
    @with_session_context(mode="silent")
    async def on_llm_response(
        self,
        event: AstrMessageEvent,
        resp,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        if not session_ctx:
            return
        await handle_on_llm_response(self, event, resp, session_ctx=session_ctx)

    @filter.after_message_sent()
    @with_session_context(mode="silent")
    async def after_message_sent(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        if not session_ctx:
            return
        await handle_after_message_sent(self, event, session_ctx=session_ctx)

    def _export_data(
        self,
        *,
        fmt: str,
        scope: str,
        session_type: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        if not self.db:
            return "好感度系统未初始化"

        payload = self.db.fetch_export_rows(
            scope, session_type=session_type, session_id=session_id
        )
        now_ts = int(time.time())
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now_ts))
        export_dir = os.path.join(self._get_effective_data_dir(), "exports")
        os.makedirs(export_dir, exist_ok=True)
        session_label = (
            "global"
            if scope == "global"
            else self._format_session(str(session_type), str(session_id))
        )
        safe_label = session_label.replace(":", "_")
        if fmt == "json":
            export_path = os.path.join(export_dir, f"fav_export_{safe_label}_{timestamp}.json")
            payload_with_meta = {
                "scope": scope,
                "session": session_label,
                "exported_at": now_ts,
                **payload,
            }
            return self._write_json_export(payload_with_meta, export_path, session_label)
        export_path = os.path.join(export_dir, f"fav_export_{safe_label}_{timestamp}.zip")
        return self._write_csv_export_zip(payload, export_path, session_label)

    def _format_stats_message(
        self,
        *,
        stats: dict[str, Any],
        scope: str,
        session_type: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> str:
        scope_label = (
            "全局"
            if scope == "global"
            else f"会话 {self._format_session(str(session_type), str(session_id))}"
        )
        return (
            f"统计范围: {scope_label}\n"
            f"用户数: {stats['user_count']}\n"
            f"平均好感度: {stats['avg_level']}\n"
            f"最高/最低好感度: {stats['max_level']} / {stats['min_level']}\n"
            f"今日正向累计: {stats['daily_pos_total']}\n"
            f"今日负向累计: {stats['daily_neg_total']}\n"
            f"评分事件总数: {stats['score_event_count']}"
        )

    @llm_tool(name="fav_query")
    @with_session_context(mode="return_error")
    async def fav_query(
        self,
        event: AstrMessageEvent,
        identifier: str,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """查询当前会话内用户的好感度等级和层级效果。identifier 可以是用户 ID 或当前昵称。

        Args:
            identifier(string): 用户 ID 或当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"

        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

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

    @llm_tool(name="fav_update")
    @with_session_context(mode="return_error")
    async def fav_update(
        self,
        event: AstrMessageEvent,
        user_id: str,
        level: int,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """设置当前会话内用户的好感度等级（绝对值）。

        Args:
            user_id(string): 用户 ID
            level(number): 新的好感度等级
        """
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()

        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

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
    @with_session_context(mode="return_error")
    async def fav_add_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        nickname: str,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """在当前会话注册新用户并设置当前昵称。初始好感度由配置范围约束。

        Args:
            user_id(string): 用户唯一 ID
            nickname(string): 用户当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()

        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

        initial_level = self._get_initial_level_for_new_user()
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
    @with_session_context(mode="return_error")
    async def fav_remove_user(
        self,
        event: AstrMessageEvent,
        user_id: str,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """删除当前会话内用户及其所有昵称记录。

        Args:
            user_id(string): 用户 ID
        """
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()

        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

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
    @with_session_context(mode="return_error")
    async def fav_add_nickname(
        self,
        event: AstrMessageEvent,
        user_id: str,
        nickname: str,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """更新当前会话内用户的当前昵称，旧昵称会自动沉淀为曾用名。

        Args:
            user_id(string): 用户 ID
            nickname(string): 新的当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()

        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

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
    @with_session_context(mode="return_error")
    async def fav_remove_nickname(
        self,
        event: AstrMessageEvent,
        user_id: str,
        nickname: str,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """删除当前会话内用户的当前昵称。

        Args:
            user_id(string): 用户 ID
            nickname(string): 要删除的当前昵称
        """
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()

        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

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

    @llm_tool(name="fav_reset")
    @with_session_context(mode="return_error")
    async def fav_reset(
        self,
        event: AstrMessageEvent,
        user_id: str,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员重置当前会话内指定用户的好感度与日统计。"""
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()
        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        target_user = str(user_id or "").strip()
        if not target_user:
            return "user_id 不能为空"
        now_ts = int(time.time())
        initial_level = self._get_initial_level_for_new_user()
        ok = self.db.reset_user(
            session_type,
            session_id,
            target_user,
            initial_level,
            last_interaction_at=now_ts,
            daily_bucket=self._get_today_bucket(now_ts),
        )
        if not ok:
            return (
                f"当前会话（{self._format_session(session_type, session_id)}）中"
                f"用户「{target_user}」不存在"
            )
        return (
            f"已重置会话 {self._format_session(session_type, session_id)} 中"
            f"用户「{target_user}」的好感度为 {initial_level}"
        )

    @llm_tool(name="fav_reset_all")
    @with_session_context(mode="return_error")
    async def fav_reset_all(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员重置当前会话全部用户的好感度与日统计。"""
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()
        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        now_ts = int(time.time())
        initial_level = self._get_initial_level_for_new_user()
        count = self.db.reset_session_users(
            session_type,
            session_id,
            initial_level,
            last_interaction_at=now_ts,
            daily_bucket=self._get_today_bucket(now_ts),
        )
        return (
            f"已重置会话 {self._format_session(session_type, session_id)} 的 {count} 名用户，"
            f"好感度统一为 {initial_level}"
        )

    @llm_tool(name="fav_export")
    @with_session_context(mode="return_error")
    async def fav_export(
        self,
        event: AstrMessageEvent,
        format: str = "json",
        scope: str = "session",
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员导出当前会话或全局数据到文件。"""
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()
        try:
            normalized_format = self._normalize_export_format(format)
            normalized_scope = self._normalize_export_scope(scope)
        except ValueError as exc:
            return str(exc)
        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        if normalized_scope == "global":
            return self._export_data(fmt=normalized_format, scope=normalized_scope)
        return self._export_data(
            fmt=normalized_format,
            scope=normalized_scope,
            session_type=session_type,
            session_id=session_id,
        )

    @llm_tool(name="fav_stats")
    @with_session_context(mode="return_error")
    async def fav_stats(
        self,
        event: AstrMessageEvent,
        scope: str = "session",
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员查看当前会话或全局统计。"""
        if not self.db:
            return "好感度系统未初始化"
        if not self._is_admin_event(event):
            return self._admin_only_message()
        try:
            normalized_scope = self._normalize_export_scope(scope)
        except ValueError as exc:
            return str(exc)
        if not session_ctx:
            return "会话上下文异常: 缺少会话上下文"
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        if normalized_scope == "global":
            stats = self.db.get_stats(normalized_scope)
            return self._format_stats_message(stats=stats, scope=normalized_scope)
        stats = self.db.get_stats(
            normalized_scope, session_type=session_type, session_id=session_id
        )
        return self._format_stats_message(
            stats=stats,
            scope=normalized_scope,
            session_type=session_type,
            session_id=session_id,
        )

    @filter.command("好感度查询")
    @with_session_context(mode="yield_error")
    async def cmd_fav_query(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """查询自己在当前会话中的好感度等级和层级信息。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return

        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        sender_name = session_ctx.sender_name
        sender_id = session_ctx.sender_id

        user = self.db.get_user(session_type, session_id, sender_id)
        if not user:
            yield event.plain_result("你还没有被记录在当前会话中哦。")
            return

        now_ts = int(time.time())
        user = self._refresh_daily_bucket(session_type, session_id, user, now_ts)
        nickname = user.current_nickname or sender_name or "无"
        stable_hint = self._stable_status_hint(user)
        lines = [
            f"昵称: {nickname}（{sender_id}）",
            f"好感度: {user.level}",
            f"今日正向增益: {user.daily_pos_gain}/{DAILY_POSITIVE_CAP}",
        ]
        if user.daily_pos_gain >= DAILY_POSITIVE_CAP:
            lines.append("提示：今日正向增益已达上限。")
        if user.level in {self.min_level, self.max_level}:
            lines.append("提示：当前好感度已触达边界，处于稳定状态。")
        if stable_hint:
            lines.append(f"状态：{stable_hint}")

        yield event.plain_result("\n".join(lines))

    @filter.command("fav-init")
    @with_session_context(mode="yield_error")
    async def cmd_fav_init(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """在当前会话中注册自己的好感度记录。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return

        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        sender_name = session_ctx.sender_name
        sender_id = session_ctx.sender_id

        user = self.db.get_user(session_type, session_id, sender_id)
        if user:
            yield event.plain_result(f"你已经注册过了，当前好感度: {user.level}")
            return

        initial_level = self._get_initial_level_for_new_user()
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

    @filter.command("fav-reset")
    @with_session_context(mode="yield_error")
    async def cmd_fav_reset(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """重置自己在当前会话中的好感度与日统计。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return
        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        sender_id = session_ctx.sender_id
        user = self.db.get_user(session_type, session_id, sender_id)
        if not user:
            yield event.plain_result("你还没有被记录在当前会话中哦。")
            return
        now_ts = int(time.time())
        initial_level = self._get_initial_level_for_new_user()
        self.db.reset_user(
            session_type,
            session_id,
            sender_id,
            initial_level,
            last_interaction_at=now_ts,
            daily_bucket=self._get_today_bucket(now_ts),
        )
        yield event.plain_result(f"已将你的好感度重置为 {initial_level}。")

    @filter.command("fav-reset-all")
    @with_session_context(mode="yield_error")
    async def cmd_fav_reset_all(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员重置当前会话全部用户。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return
        if not self._is_admin_event(event):
            yield event.plain_result(self._admin_only_message())
            return
        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        now_ts = int(time.time())
        initial_level = self._get_initial_level_for_new_user()
        count = self.db.reset_session_users(
            session_type,
            session_id,
            initial_level,
            last_interaction_at=now_ts,
            daily_bucket=self._get_today_bucket(now_ts),
        )
        yield event.plain_result(f"已重置当前会话 {count} 名用户，好感度={initial_level}。")

    @filter.command("fav-export")
    @with_session_context(mode="yield_error")
    async def cmd_fav_export(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员导出数据到文件：fav-export [json|csv] [session|global]。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return
        if not self._is_admin_event(event):
            yield event.plain_result(self._admin_only_message())
            return
        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

        raw = (event.message_str or "").strip()
        parts = raw.split()
        if parts and parts[0].lstrip("/!").lower() == "fav-export":
            parts = parts[1:]
        if len(parts) > 2:
            yield event.plain_result("参数过多，用法：fav-export [json|csv] [session|global]")
            return

        export_format = "json"
        export_scope = "session"
        try:
            if len(parts) >= 1:
                token = parts[0].lower()
                if token in {"json", "csv"}:
                    export_format = self._normalize_export_format(token)
                elif token in {"session", "global"}:
                    export_scope = self._normalize_export_scope(token)
                else:
                    raise ValueError("第一个参数仅支持 json/csv 或 session/global")
            if len(parts) == 2:
                export_scope = self._normalize_export_scope(parts[1].lower())
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return

        if export_scope == "global":
            yield event.plain_result(self._export_data(fmt=export_format, scope=export_scope))
            return
        yield event.plain_result(
            self._export_data(
                fmt=export_format,
                scope=export_scope,
                session_type=session_type,
                session_id=session_id,
            )
        )

    @filter.command("fav-stats")
    @with_session_context(mode="yield_error")
    async def cmd_fav_stats(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """管理员查看统计：fav-stats [session|global]。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return
        if not self._is_admin_event(event):
            yield event.plain_result(self._admin_only_message())
            return
        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id
        raw = (event.message_str or "").strip()
        parts = raw.split()
        if parts and parts[0].lstrip("/!").lower() == "fav-stats":
            parts = parts[1:]
        if len(parts) > 1:
            yield event.plain_result("参数过多，用法：fav-stats [session|global]")
            return
        scope = "session"
        try:
            if parts:
                scope = self._normalize_export_scope(parts[0].lower())
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        if scope == "global":
            stats = self.db.get_stats(scope)
            yield event.plain_result(self._format_stats_message(stats=stats, scope=scope))
            return
        stats = self.db.get_stats(scope, session_type=session_type, session_id=session_id)
        yield event.plain_result(
            self._format_stats_message(
                stats=stats,
                scope=scope,
                session_type=session_type,
                session_id=session_id,
            )
        )

    @filter.command("fav-rl")
    @with_session_context(mode="yield_error")
    async def cmd_fav_ranking(
        self,
        event: AstrMessageEvent,
        *,
        session_ctx: Optional[SessionContext] = None,
    ):
        """查看当前会话的好感度排行榜。"""
        if not self.db:
            yield event.plain_result("好感度系统未初始化")
            return

        if not session_ctx:
            yield event.plain_result("会话上下文异常: 缺少会话上下文")
            return
        session_type = session_ctx.session_type
        session_id = session_ctx.session_id

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
