import json
import os
from typing import Any


KEYWORD_KEYS = {
    "abuse",
    "abuse_strong_hints",
    "rude",
    "thanks",
    "celebration",
    "deep_talk",
    "helpful_dialogue",
    "small_talk",
}

DEFAULT_KEYWORD_PROFILE: dict[str, set[str]] = {
    "abuse": {
        "傻逼",
        "煞笔",
        "傻x",
        "傻叉",
        "脑残",
        "废物",
        "垃圾",
        "去死",
        "妈的",
        "操你",
        "fuck you",
    },
    "abuse_strong_hints": {"你妈", "全家", "滚出", "sb", "cnm", "nmsl"},
    "rude": {"闭嘴", "滚", "别烦", "弱智", "蠢", "有病", "讨厌你", "烦死了", "无语"},
    "thanks": {
        "谢谢",
        "感谢",
        "辛苦了",
        "多谢",
        "感激",
        "thanks",
        "thank you",
        "thx",
    },
    "celebration": {
        "生日快乐",
        "恭喜",
        "庆祝",
        "好耶",
        "太棒了",
        "厉害",
        "牛逼",
        "congrats",
        "congratulations",
        "666",
        "yyds",
        "xswl",
        "awsl",
    },
    "deep_talk": {
        "深入",
        "详细",
        "原理",
        "推导",
        "证明",
        "分析",
        "为什么",
        "how",
        "why",
    },
    "helpful_dialogue": {
        "请",
        "麻烦",
        "帮我",
        "可以",
        "一起",
        "协助",
        "建议",
        "步骤",
    },
    "small_talk": {
        "你好",
        "在吗",
        "早上好",
        "晚安",
        "哈哈",
        "嗨",
        "hello",
        "hi",
    },
}


def build_default_keyword_profile() -> dict[str, set[str]]:
    return {key: set(values) for key, values in DEFAULT_KEYWORD_PROFILE.items()}


def _normalize_keyword_values(values: Any, key: str) -> set[str]:
    if not isinstance(values, (list, tuple, set)):
        raise ValueError(f"关键词配置 {key} 必须是数组")
    normalized: set[str] = set()
    for item in values:
        token = str(item or "").strip().lower()
        if token:
            normalized.add(token)
    return normalized


def load_keyword_profile(
    keyword_profile_path: str, data_dir: str, logger: Any
) -> dict[str, set[str]]:
    profile = build_default_keyword_profile()
    raw_path = str(keyword_profile_path or "").strip()
    if not raw_path:
        return profile

    resolved_path = raw_path if os.path.isabs(raw_path) else os.path.join(data_dir, raw_path)
    try:
        with open(resolved_path, "r", encoding="utf-8") as fp:
            payload = json.load(fp)
        if not isinstance(payload, dict):
            raise ValueError("关键词配置文件根节点必须是对象")
        for key, values in payload.items():
            if key not in KEYWORD_KEYS:
                raise ValueError(f"未知关键词类别: {key}")
            profile[key] = _normalize_keyword_values(values, key)
        logger.info(f"[FavorabilityPlugin] 已加载关键词配置: {resolved_path}")
    except Exception as exc:
        logger.warning(
            f"[FavorabilityPlugin] 关键词配置加载失败，将回退内置词库: {resolved_path}, err={exc}"
        )
    return profile
