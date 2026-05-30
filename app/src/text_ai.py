from __future__ import annotations

import re

from .easyrouter_client import EasyRouterClient


CHINESE_RE = re.compile(r"[\u4e00-\u9fff]")
BANNED_PHRASES = [
    "best",
    "no.1",
    "no 1",
    "100% guaranteed",
    "guaranteed",
    "miracle",
]
BANNED_BRANDS = [
    "nike",
    "adidas",
    "disney",
    "apple",
    "iphone",
    "samsung",
    "huawei",
    "xiaomi",
    "lego",
    "barbie",
    "pokemon",
]


def optimize_product_title(original_title: str, keyword: str = "") -> str:
    original_title = str(original_title or "").strip()
    prompt = f"""你是跨境电商 Temu 商品标题优化助手。
请根据原始商品标题生成一个合规的英文商品标题。
要求：
1. 只输出英文标题，不要解释。
2. 不要包含中文。
3. 不要包含侵权品牌词。
4. 不要使用绝对化宣传词。
5. 保留商品核心用途、材质、数量、适用场景。
6. 长度不超过180个英文字符。
7. 即使原始标题已经是英文，也必须重新改写，不要原样返回。

原始标题：
{original_title}
"""
    if keyword:
        prompt += f"\n核心关键词：\n{keyword}\n"

    try:
        client = EasyRouterClient(max_tokens=220, temperature=0.3, model_tier="fast")
        title = client.chat_text(
            [
                {"role": "system", "content": "Return one compliant English Temu product title only. No Chinese. No explanation."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=220,
            temperature=0.3,
        )
        cleaned = _clean_title(title)
        if cleaned and not CHINESE_RE.search(cleaned):
            return cleaned
    except Exception as exc:
        print(f"[title_ai] EasyRouter 标题优化失败，使用本地兜底: {exc}")

    return _fallback_title(original_title, keyword)


def _clean_title(title: str) -> str:
    value = str(title or "").strip().strip('"').strip("'")
    value = CHINESE_RE.sub("", value)
    value = re.sub(r"[\r\n\t]+", " ", value)
    value = re.sub(r"\s+", " ", value)
    value = _remove_banned_words(value)
    value = re.sub(r"\s+([,;:/])", r"\1", value).strip(" -_,;:/")
    return value[:180].strip(" -_,;:/")


def _remove_banned_words(value: str) -> str:
    for word in BANNED_PHRASES + BANNED_BRANDS:
        value = re.sub(rf"\b{re.escape(word)}\b", "", value, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", value).strip()


def _fallback_title(original_title: str, keyword: str = "") -> str:
    ascii_part = re.sub(r"[^\x00-\x7F]+", " ", original_title)
    ascii_part = re.sub(r"[^A-Za-z0-9 ,;:/+&()_-]+", " ", ascii_part)
    ascii_part = _clean_title(ascii_part)
    keyword_part = _clean_title(keyword)
    fallback = " ".join(part for part in (keyword_part, ascii_part) if part).strip()
    if not fallback:
        fallback = "Practical Everyday Product"
    return fallback[:180].strip()
