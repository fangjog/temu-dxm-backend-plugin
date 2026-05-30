from __future__ import annotations

import re
from typing import Any

from .easyrouter_client import EasyRouterClient


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
ALLOWED_RE = re.compile(r"[^A-Za-z0-9_-]")

LOCAL_SKU_WORDS = {
    "黑色": "BLACK",
    "白色": "WHITE",
    "红色": "RED",
    "蓝色": "BLUE",
    "绿色": "GREEN",
    "黄色": "YELLOW",
    "粉色": "PINK",
    "紫色": "PURPLE",
    "灰色": "GRAY",
    "透明色": "TRANSPARENT",
    "透明": "TRANSPARENT",
    "金色": "GOLD",
    "银色": "SILVER",
    "手机": "PHONE",
    "适用于": "FOR",
    "通用": "UNIVERSAL",
    "套装": "SET",
    "加厚": "THICK",
    "大号": "LARGE",
    "小号": "SMALL",
    "中号": "MEDIUM",
    "款": "STYLE",
    "件": "PCS",
}


def contains_chinese(text: str) -> bool:
    return bool(CHINESE_RE.search(text or ""))


def translate_sku_to_english(text: str) -> str:
    if not contains_chinese(text):
        return text

    prompt = f"""你是跨境电商 SKU 货号翻译助手。
请把下面 SKU 中的中文翻译为简短英文关键词，保留数字、规格、英文和符号。
只输出翻译后的 SKU 文本，不要解释。

SKU:
{text}
"""
    try:
        client = EasyRouterClient(max_tokens=120, temperature=0.1, model_tier="fast")
        translated = client.chat_text(
            [
                {"role": "system", "content": "Only return a concise English SKU string. No Chinese. No explanation."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=120,
            temperature=0.1,
        )
        if translated and not contains_chinese(translated):
            return translated
    except Exception as exc:
        print(f"[sku_translate] EasyRouter 翻译失败，使用本地兜底: {exc}")

    return _local_translate_sku(text)


def sanitize_sku(text: str, product_id: str = "", index: int = 1, config: dict[str, Any] | None = None) -> str:
    cfg = config or {}
    max_length = int(cfg.get("max_length", 80))
    uppercase = bool(cfg.get("uppercase", True))
    allow_pattern = cfg.get("allow_chars_regex", r"[^A-Za-z0-9_-]")
    allow_re = re.compile(allow_pattern)

    value = str(text or "").strip()
    if contains_chinese(value):
        value = translate_sku_to_english(value)

    value = CHINESE_RE.sub("", value)
    value = re.sub(r"\s+", "-", value)
    value = value.replace("–", "-").replace("—", "-").replace("－", "-")
    value = allow_re.sub("", value)
    value = re.sub(r"-{2,}", "-", value)
    value = value.strip("-_")
    if uppercase:
        value = value.upper()
    value = value[:max_length].strip("-_")

    if not value:
        fallback_product_id = sanitize_sku(product_id, "", index, config) if product_id else "SKU"
        value = f"{fallback_product_id}-{index}"
        value = value[:max_length].strip("-_")
        if uppercase:
            value = value.upper()

    if contains_chinese(value):
        raise ValueError(f"SKU 清洗后仍包含中文，需要人工处理: {value}")
    return value


def _local_translate_sku(text: str) -> str:
    value = text
    for cn, en in sorted(LOCAL_SKU_WORDS.items(), key=lambda item: len(item[0]), reverse=True):
        value = value.replace(cn, f" {en} ")
    value = CHINESE_RE.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()
