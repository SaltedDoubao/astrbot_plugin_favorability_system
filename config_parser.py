import json
from typing import Any, Optional


class PluginConfigParser:
    def __init__(self, config: Optional[dict[str, Any]] = None):
        self._config = config or {}

    def _unwrap_config_value(self, key: str) -> Any:
        if key not in self._config:
            raise KeyError(key)
        raw = self._config.get(key)
        if isinstance(raw, dict) and "value" in raw:
            raw = raw["value"]
        return raw

    def parse_required_int(self, key: str) -> int:
        try:
            raw = self._unwrap_config_value(key)
        except KeyError as exc:
            raise ValueError(f"缺少必填配置项: {key}") from exc
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 不是合法整数: {raw}") from exc

    def parse_optional_int(
        self,
        key: str,
        default: int,
        *,
        min_value: int = 0,
        max_value: Optional[int] = None,
    ) -> int:
        if key not in self._config:
            return default
        raw = self._unwrap_config_value(key)
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"配置项 {key} 不是合法整数: {raw}") from exc
        if value < min_value:
            raise ValueError(f"配置项 {key} 不能小于 {min_value}")
        if max_value is not None and value > max_value:
            raise ValueError(f"配置项 {key} 不能大于 {max_value}")
        return value

    def parse_optional_bool(self, key: str, default: bool) -> bool:
        if key not in self._config:
            return default
        raw = self._unwrap_config_value(key)
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

    def parse_optional_choice(
        self, key: str, default: str, allowed: set[str]
    ) -> str:
        if key not in self._config:
            return default
        raw = self._unwrap_config_value(key)
        value = str(raw or "").strip().lower()
        if not value:
            return default
        if value not in allowed:
            allow = ", ".join(sorted(allowed))
            raise ValueError(f"配置项 {key} 仅支持: {allow}")
        return value

    def parse_optional_str(self, key: str, default: str = "") -> str:
        if key not in self._config:
            return default
        raw = self._unwrap_config_value(key)
        return str(raw or "").strip()

    def parse_required_tiers(self, key: str) -> list[dict[str, Any]]:
        if key not in self._config:
            raise ValueError(f"缺少必填配置项: {key}")
        raw = self._unwrap_config_value(key)
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
