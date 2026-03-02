import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Optional

from astrbot.api import logger


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
DAILY_NEGATIVE_CAP_DEFAULT = 50
PER_ROUND_MIN_DELTA = -12
PER_ROUND_MAX_DELTA = 12
MAX_EVIDENCE_LENGTH = 120


class AssessmentValidationError(ValueError):
    pass


class AssessmentRecoverableError(RuntimeError):
    pass


@dataclass
class AssessmentInput:
    user_id: str
    interaction_key: str
    intensity: int


class AssessmentRuleEngine:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    def _normalize_text(self, text: str) -> str:
        import re

        lowered = str(text or "").lower()
        return re.sub(r"\s+", " ", lowered).strip()

    def _keyword_hit(self, text: str, keywords: set[str]) -> Optional[str]:
        for keyword in keywords:
            if keyword in text:
                return keyword
        return None

    def classify_interaction_rule_v1(self, text: str) -> Optional[dict[str, str | int]]:
        normalized = self._normalize_text(text)
        if not normalized:
            return None

        keywords = self.plugin.keyword_profile
        abuse_keyword = self._keyword_hit(normalized, keywords["abuse"])
        if abuse_keyword:
            strong_hit = any(hint in normalized for hint in keywords["abuse_strong_hints"])
            intensity = 3 if strong_hit else 2
            return {
                "interaction_type": "abuse",
                "intensity": intensity,
                "evidence": "KW_ABUSE_STRONG" if intensity == 3 else "KW_ABUSE",
            }

        rude_keyword = self._keyword_hit(normalized, keywords["rude"])
        if rude_keyword:
            return {"interaction_type": "rude", "intensity": 2, "evidence": "KW_RUDE"}

        celebration_keyword = self._keyword_hit(normalized, keywords["celebration"])
        if celebration_keyword:
            return {
                "interaction_type": "celebration",
                "intensity": 2,
                "evidence": "KW_CELEBRATION",
            }

        thanks_keyword = self._keyword_hit(normalized, keywords["thanks"])
        if thanks_keyword:
            return {"interaction_type": "thanks", "intensity": 1, "evidence": "KW_THANKS"}

        deep_keyword = self._keyword_hit(normalized, keywords["deep_talk"])
        if deep_keyword:
            intensity = 2 if len(normalized) >= 30 else 1
            return {
                "interaction_type": "deep_talk",
                "intensity": intensity,
                "evidence": "KW_DEEP_TALK",
            }

        helpful_keyword = self._keyword_hit(normalized, keywords["helpful_dialogue"])
        if helpful_keyword:
            return {
                "interaction_type": "helpful_dialogue",
                "intensity": 1,
                "evidence": "KW_HELPFUL",
            }

        small_talk_keyword = self._keyword_hit(normalized, keywords["small_talk"])
        if small_talk_keyword:
            return {
                "interaction_type": "small_talk",
                "intensity": 1,
                "evidence": "KW_SMALL_TALK",
            }

        if self.plugin.negative_policy != "conservative":
            if "冷淡" in normalized or "敷衍" in normalized:
                return {"interaction_type": "cold", "intensity": 1, "evidence": "KW_COLD"}
        return None

    def _validate_assessment_input(
        self,
        *,
        user_id: str,
        interaction_type: str,
        intensity: int,
    ) -> AssessmentInput:
        normalized_id = str(user_id or "").strip()
        if not normalized_id:
            raise AssessmentValidationError("user_id 不能为空")

        interaction_key = str(interaction_type or "").strip().lower()
        if interaction_key not in INTERACTION_BASE_DELTA:
            allow = ", ".join(INTERACTION_BASE_DELTA.keys())
            raise AssessmentValidationError(f"interaction_type 非法，允许值: {allow}")

        try:
            normalized_intensity = int(intensity)
        except (TypeError, ValueError) as exc:
            raise AssessmentValidationError(f"intensity {intensity} 不是有效整数") from exc
        if normalized_intensity not in INTENSITY_MULTIPLIER:
            raise AssessmentValidationError("intensity 必须是 1、2、3")
        return AssessmentInput(
            user_id=normalized_id,
            interaction_key=interaction_key,
            intensity=normalized_intensity,
        )

    def _compute_raw_delta(self, interaction_key: str, intensity: int) -> tuple[int, float]:
        base_delta = INTERACTION_BASE_DELTA[interaction_key]
        intensity_mul = INTENSITY_MULTIPLIER[intensity]
        raw_delta = round(base_delta * intensity_mul)
        positive_bias = 1.0
        if raw_delta > 0:
            positive_bias = POSITIVE_BIAS_FACTOR
            raw_delta = round(raw_delta * positive_bias)
        return raw_delta, positive_bias

    def _compute_anti_spam_multiplier(
        self,
        *,
        session_type: str,
        session_id: str,
        user_id: str,
        interaction_key: str,
        now_ts: int,
        raw_delta: int,
    ) -> float:
        if raw_delta == 0:
            return 1.0
        if raw_delta > 0:
            count = self.plugin.db.count_positive_events_by_type_since(
                session_type,
                session_id,
                user_id,
                interaction_key,
                now_ts - ANTI_SPAM_WINDOW_SEC,
            )
        else:
            count = self.plugin.db.count_negative_events_by_type_since(
                session_type,
                session_id,
                user_id,
                interaction_key,
                now_ts - ANTI_SPAM_WINDOW_SEC,
            )
        occurrence = count + 1
        if occurrence == 1:
            return 1.0
        if occurrence == 2:
            return 0.75
        if occurrence == 3:
            return 0.5
        return 0.3

    def _apply_caps(
        self,
        *,
        session_type: str,
        session_id: str,
        user_id: str,
        user: Any,
        now_ts: int,
        final_delta: int,
    ) -> tuple[int, int, int, int, dict[str, bool]]:
        cap_clip = {
            "per_round": False,
            "ten_min_positive": False,
            "daily_positive": False,
            "daily_negative": False,
            "global_level": False,
        }
        clipped = max(PER_ROUND_MIN_DELTA, min(PER_ROUND_MAX_DELTA, final_delta))
        if clipped != final_delta:
            cap_clip["per_round"] = True
        final_delta = clipped

        if final_delta > 0:
            ten_min_gain = self.plugin.db.sum_positive_delta_since(
                session_type,
                session_id,
                user_id,
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
        elif final_delta < 0:
            remaining_daily_negative = max(
                0,
                int(self.plugin.daily_negative_cap) - int(user.daily_neg_gain),
            )
            allowed_min_delta = -remaining_daily_negative
            if final_delta < allowed_min_delta:
                final_delta = allowed_min_delta
                cap_clip["daily_negative"] = True

        proposed_level = user.level + final_delta
        new_level = self.plugin._clamp_level(proposed_level)
        if new_level != proposed_level:
            cap_clip["global_level"] = True
        effective_delta = new_level - user.level

        daily_pos_gain = int(user.daily_pos_gain)
        daily_neg_gain = int(user.daily_neg_gain)
        if effective_delta > 0:
            daily_pos_gain += effective_delta
        elif effective_delta < 0:
            daily_neg_gain += abs(effective_delta)
        return effective_delta, new_level, daily_pos_gain, daily_neg_gain, cap_clip

    def _persist_assessment(
        self,
        *,
        session_type: str,
        session_id: str,
        user_id: str,
        new_level: int,
        now_ts: int,
        daily_pos_gain: int,
        daily_neg_gain: int,
        interaction_key: str,
        intensity: int,
        raw_delta: int,
        effective_delta: int,
        anti_spam_mul: float,
        evidence: str,
    ) -> str:
        evidence_text = str(evidence or "").strip()[:MAX_EVIDENCE_LENGTH]
        self.plugin.db.update_level(
            session_type,
            session_id,
            user_id,
            new_level,
            last_interaction_at=now_ts,
            daily_pos_gain=daily_pos_gain,
            daily_neg_gain=daily_neg_gain,
            daily_bucket=self.plugin._get_today_bucket(now_ts),
            commit=False,
        )
        self.plugin.db.log_score_event(
            session_type,
            session_id,
            user_id,
            interaction_key,
            intensity,
            raw_delta,
            effective_delta,
            anti_spam_mul,
            now_ts,
            evidence_text,
            commit=False,
        )
        return evidence_text

    def _build_assessment_result(
        self,
        *,
        old_level: int,
        new_level: int,
        raw_delta: int,
        effective_delta: int,
        intensity_mul: float,
        positive_bias: float,
        anti_spam_mul: float,
        cap_clip: dict[str, bool],
        tier_before_name: str,
        tier_after_name: str,
        interaction_key: str,
        intensity: int,
        evidence_text: str,
    ) -> dict[str, Any]:
        return {
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
            "tier_before": tier_before_name,
            "tier_after": tier_after_name,
            "tier_changed": tier_before_name != tier_after_name,
            "interaction_key": interaction_key,
            "intensity": intensity,
            "evidence_text": evidence_text,
        }

    def apply_assessment(
        self,
        *,
        session_type: str,
        session_id: str,
        user_id: str,
        interaction_type: str,
        intensity: int,
        evidence: str = "",
        source: str = "auto_hook",
    ) -> dict[str, Any]:
        validated = self._validate_assessment_input(
            user_id=user_id,
            interaction_type=interaction_type,
            intensity=intensity,
        )
        interaction_key = validated.interaction_key
        normalized_intensity = validated.intensity
        normalized_id = validated.user_id
        intensity_mul = INTENSITY_MULTIPLIER[normalized_intensity]

        try:
            with self.plugin.db.immediate_transaction():
                user, _ = self.plugin._coerce_user(
                    session_type,
                    session_id,
                    normalized_id,
                    "",
                    update_nickname=False,
                    db_commit=False,
                )
                if not user:
                    raise AssessmentRecoverableError("无法初始化用户资料")

                now_ts = int(time.time())
                user = self.plugin._refresh_daily_bucket(
                    session_type, session_id, user, now_ts, db_commit=False
                )
                user = self.plugin._apply_decay_if_needed(
                    session_type, session_id, user, now_ts, db_commit=False
                )

                old_level = user.level
                tier_before = self.plugin._get_tier(old_level)
                tier_before_name = tier_before["name"] if tier_before else "未知"
                raw_delta, positive_bias = self._compute_raw_delta(
                    interaction_key, normalized_intensity
                )
                anti_spam_mul = self._compute_anti_spam_multiplier(
                    session_type=session_type,
                    session_id=session_id,
                    user_id=normalized_id,
                    interaction_key=interaction_key,
                    now_ts=now_ts,
                    raw_delta=raw_delta,
                )
                final_delta = round(raw_delta * anti_spam_mul)
                effective_delta, new_level, daily_pos_gain, daily_neg_gain, cap_clip = (
                    self._apply_caps(
                        session_type=session_type,
                        session_id=session_id,
                        user_id=normalized_id,
                        user=user,
                        now_ts=now_ts,
                        final_delta=final_delta,
                    )
                )
                tier_after = self.plugin._get_tier(new_level)
                tier_after_name = tier_after["name"] if tier_after else "未知"
                evidence_text = self._persist_assessment(
                    session_type=session_type,
                    session_id=session_id,
                    user_id=normalized_id,
                    new_level=new_level,
                    now_ts=now_ts,
                    daily_pos_gain=daily_pos_gain,
                    daily_neg_gain=daily_neg_gain,
                    interaction_key=interaction_key,
                    intensity=normalized_intensity,
                    raw_delta=raw_delta,
                    effective_delta=effective_delta,
                    anti_spam_mul=anti_spam_mul,
                    evidence=evidence,
                )
        except sqlite3.Error as exc:
            raise AssessmentRecoverableError("数据库写入异常") from exc

        result = self._build_assessment_result(
            old_level=old_level,
            new_level=new_level,
            raw_delta=raw_delta,
            effective_delta=effective_delta,
            intensity_mul=intensity_mul,
            positive_bias=positive_bias,
            anti_spam_mul=anti_spam_mul,
            cap_clip=cap_clip,
            tier_before_name=tier_before_name,
            tier_after_name=tier_after_name,
            interaction_key=interaction_key,
            intensity=normalized_intensity,
            evidence_text=evidence_text,
        )
        logger.info(
            "[FavorabilityPlugin] assess"
            f" source={source}"
            f" session={self.plugin._format_session(session_type, session_id)}"
            f" user={normalized_id}"
            f" type={interaction_key}"
            f" intensity={normalized_intensity}"
            f" old={old_level} new={new_level}"
            f" raw={raw_delta} final={effective_delta}"
            f" anti_spam_mul={anti_spam_mul}"
            f" cap={cap_clip}"
            f" evidence={evidence_text}"
        )
        return result
