from __future__ import annotations

import base64
import json
import os
import random
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .captcha_guard import check_and_wait_if_captcha
from .dianxiaomi_pages import (
    fill_product_title,
    open_draft_list,
    open_first_draft_edit,
    process_sku_fields,
    read_original_title,
    select_origin_country_and_province,
)
from .easyrouter_client import EasyRouterClient
from .sku_cleaner import contains_chinese
from .text_ai import optimize_product_title
from .utils import PROJECT_ROOT, now_ts, take_screenshot


STATUS_OK_TEXTS = ["发布成功", "提交成功", "已加入发布队列", "发布中", "刊登中", "任务已提交", "已提交", "发布任务中"]
REQUIRED_KEYWORDS = ["不能为空", "必填", "请选择", "请填写", "请上传", "尺寸", "重量", "外包装", "颜色"]
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
STATUS_OK_TEXTS.extend(["创建中", "发布任务已创建"])
FORBIDDEN_TITLE_TERMS = [
    "100% natural sand",
    "construction sand",
    "children toy",
    "toy for kids",
    "for children",
    "for kids",
    "natural sand",
    "crushed stone",
    "volcanic sand",
    "stone powder",
    "mineral sand",
    "quartz sand",
    "silica sand",
    "desert sand",
    "river sand",
    "beach sand",
    "real sand",
    "rock sand",
    "raw mineral",
    "children",
    "toddler",
    "infant",
    "mineral",
    "quartz",
    "silica",
    "volcanic",
    "child",
    "girls",
    "boys",
    "kids",
    "baby",
    "girl",
    "boy",
    "kid",
    "sand",
]


def _clean_forbidden_title_terms(title: str) -> dict[str, Any]:
    value = str(title or "").strip()
    hit_terms: list[str] = []
    cleaned = value
    for term in sorted(FORBIDDEN_TITLE_TERMS, key=len, reverse=True):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term).replace(r"\ ", r"\s+") + r"(?![A-Za-z0-9])"
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            hit_terms.append(term)
            cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*[-,/|]+\s*", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -_,./|")
    if not cleaned:
        cleaned = "Home Decor Accessory"
    return {"clean_title": cleaned[:180].strip(), "hit": bool(hit_terms), "terms": hit_terms}


def _titles_same_enough(left: str, right: str) -> bool:
    normalize = lambda value: re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9]+", " ", str(value or "").lower())).strip()
    return bool(normalize(left)) and normalize(left) == normalize(right)


def _looks_like_prompt_artifact(title: str) -> bool:
    value = str(title or "").lower()
    artifacts = [
        "rewrite this title",
        "different wording",
        "same product meaning",
        "only output",
        "do not explain",
        "生成",
        "请根据",
    ]
    return any(item in value for item in artifacts)


def _local_distinct_title_rewrite(original_title: str, candidate_title: str = "") -> str:
    base = str(candidate_title or original_title or "").strip()
    replacements = [
        (r"\bHolder\b", "Support"),
        (r"\bHolders\b", "Supports"),
        (r"\bStand\b", "Rack"),
        (r"\bStands\b", "Racks"),
        (r"\bClips\b", "Fasteners"),
        (r"\bClip\b", "Fastener"),
        (r"\bFixture\b", "Mount"),
        (r"\bFixtures\b", "Mounts"),
        (r"\bCute\b", "Decorative"),
        (r"\bStable\b", "Sturdy"),
        (r"\bSet\b", "Kit"),
        (r"\bTool\b", "Accessory"),
        (r"\bTools\b", "Accessories"),
    ]
    rewritten = base
    for pattern, repl in replacements:
        rewritten = re.sub(pattern, repl, rewritten, flags=re.IGNORECASE)

    rewritten = re.sub(r"\s+", " ", rewritten).strip(" -_,;:/")
    if rewritten and not _titles_same_enough(original_title, rewritten):
        return rewritten[:180].strip(" -_,;:/")

    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9+_-]*", base)
    if len(words) >= 3:
        rotated = words[1:] + words[:1]
        rewritten = " ".join(rotated)
    else:
        rewritten = f"Practical {base}".strip()
    rewritten = re.sub(r"\s+", " ", rewritten).strip(" -_,;:/")
    if _titles_same_enough(original_title, rewritten):
        rewritten = f"Updated {rewritten}".strip()
    return rewritten[:180].strip(" -_,;:/") or "Updated Everyday Product"


def _ensure_title_is_rewritten(original_title: str, candidate_title: str, logger: Any | None = None, page: Any | None = None) -> str:
    candidate = str(candidate_title or "").strip()
    if candidate and not _looks_like_prompt_artifact(candidate) and not _titles_same_enough(original_title, candidate):
        return candidate
    rewritten = _local_distinct_title_rewrite(original_title, candidate)
    _log(
        logger,
        "publish_title_local_forced_rewrite",
        "ok",
        "Title AI result still matched the original; applied local forced rewrite before publishing.",
        page=page,
        extra={"rewritten_length": len(rewritten)},
    )
    return rewritten


def run_publish_one_product(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    if not config.get("publish", {}).get("allow_publish", False):
        return _manual_result(page, "publish_disabled", "config.yaml 中 publish.allow_publish 不是 true。", logger, state)

    try:
        open_draft_list(page, config=config, logger=logger, state=state)
        edit_page = open_first_draft_edit(page, logger=logger, state=state)
        result = run_publish_current_edit_page(
            edit_page,
            config,
            logger=logger,
            state=state,
            product_context={"source": "draft_list"},
        )
        _log(logger, "run_publish_one_product", result.get("status", "unknown"), "单商品 publish-one 流程完成。", page=edit_page, extra={"product_id": result.get("product_id", "")})
        return result
    except Exception as exc:
        screenshot_path = ""
        try:
            screenshot_path = take_screenshot(page, "publish_one_error")
        except Exception:
            pass
        _log(logger, "run_publish_one_product", "error", f"publish-one 流程异常: {exc}", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="publish_error", error=str(exc), screenshot_path=screenshot_path)
        return {"status": "error", "manual_required": True, "message": str(exc), "screenshot_path": screenshot_path}


def run_publish_current_edit_page(
    page: Any,
    config: dict[str, Any],
    logger: Any | None = None,
    state: Any | None = None,
    product_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "running", "manual_required": False}
    if not config.get("publish", {}).get("allow_publish", False):
        return _manual_result(page, "publish_disabled", "config.yaml 中 publish.allow_publish 不是 true。", logger, state)

    product_context = product_context or {}
    try:
        _log(logger, "publish_flow_enter", "start", "Starting publish edit flow.", page=page)
        check_and_wait_if_captcha(page, logger=logger)
        _log(logger, "publish_captcha_checked", "ok", "Captcha/security check completed.", page=page)
        product_id = str(product_context.get("product_id") or _extract_product_id(page.url))

        original_title = str(product_context.get("original_title") or "").strip()
        if original_title:
            _log(logger, "read_original_title", "ok", f"Using pre-read original title, length={len(original_title)}.", page=page)
        else:
            _log(logger, "read_original_title_start", "start", "Reading product title before rewrite.", page=page)
            original_title = read_original_title(page, logger=logger, state=state)
        precomputed_title = str(product_context.get("precomputed_title") or "").strip()
        if precomputed_title:
            _log(logger, "publish_precomputed_title_used", "ok", f"Using precomputed rewritten title, length={len(precomputed_title)}.", page=page)
        new_title = precomputed_title or optimize_product_title(original_title)
        if _titles_same_enough(original_title, new_title):
            _log(
                logger,
                "publish_title_rewrite_retry",
                "warning",
                "AI title matched the original title; requesting a rewritten title before publishing.",
                page=page,
            )
            new_title = optimize_product_title(
                f"{original_title}\n\nRewrite this title with different wording while keeping the same product meaning."
            )
        new_title = _ensure_title_is_rewritten(original_title, new_title, logger=logger, page=page)
        forbidden_title = _clean_forbidden_title_terms(new_title)
        if forbidden_title["hit"]:
            _log(
                logger,
                "publish_title_forbidden_clean",
                "ok",
                f"Cleaned forbidden title terms: {forbidden_title['terms']}",
                page=page,
                extra={"terms": forbidden_title["terms"]},
            )
        new_title = forbidden_title["clean_title"]
        product_context["title_forbidden_hit"] = forbidden_title["hit"]
        product_context["cleaned_forbidden_terms"] = forbidden_title["terms"]
        new_title = _ensure_title_is_rewritten(original_title, new_title, logger=logger, page=page)
        product_context["cleaned_title"] = new_title
        _log(logger, "fill_product_title_start", "start", f"Filling rewritten title, length={len(new_title)}.", page=page)
        fill_product_title(page, new_title, logger=logger, state=state)
        if contains_chinese(new_title):
            return _manual_result(page, "publish_title_chinese", "标题仍包含中文，不允许发布。", logger, state)

        origin_result = select_origin_country_and_province(page, config, logger=logger, state=state)
        sku_result = process_sku_fields(page, config, logger=logger, state=state)
        if origin_result.get("status") != "ok" or sku_result.get("status") != "ok":
            return _manual_result(page, "publish_base_fields", "标题/产地/SKU 基础字段未全部处理成功，不允许发布。", logger, state)

        product_data = {
            **product_context,
            "product_id": product_id,
            "title": new_title,
            "original_title": original_title,
            "sku_items": sku_result.get("items", []),
            "url": page.url,
        }

        dimensions = fill_variant_dimensions_and_weight(page, config, logger=logger)
        package = fill_package_info_required(page, product_id, config, logger=logger)
        attributes = fill_required_product_attributes(page, product_data, config, logger=logger)
        size_chart = fill_size_chart_required(page, product_data, config, logger=logger)
        description_image = ensure_product_description_image_module(page, product_data, config, logger=logger)
        if description_image.get("status") != "ok":
            _log(
                logger,
                "description_image_module_retry",
                "start",
                "Description image module was not confirmed; retrying before publish.",
                page=page,
                extra={"description_image": description_image},
            )
            description_image = ensure_product_description_image_module(page, product_data, config, logger=logger)
        if description_image.get("status") != "ok":
            result.update(
                product_data,
                dimensions=dimensions,
                package=package,
                attributes=attributes,
                size_chart=size_chart,
                description_image=description_image,
                failure_reason="description_image_module_not_confirmed",
            )
            result["status"] = "manual_required"
            result["manual_required"] = True
            return result
        ensure = ensure_no_required_errors_before_publish(page, config, product_data, logger=logger, state=state)
        _log(
            logger,
            "publish_required_fill",
            "ok" if ensure.get("status") == "ok" else "manual_required",
            f"发布前必填项补齐结果: required_errors_count={ensure.get('required_errors_count')}",
            page=page,
            extra={"product_id": product_id, "required_errors_count": ensure.get("required_errors_count", -1)},
        )
        if ensure.get("status") != "ok":
            result.update(product_data, dimensions=dimensions, package=package, attributes=attributes, ensure=ensure)
            result["status"] = "manual_required"
            result["manual_required"] = True
            return result

        click_result = click_immediate_publish(page, config, logger=logger)
        _log(logger, "publish_click", click_result.get("status", "unknown"), "立即发布点击流程完成。", page=page, extra={"product_id": product_id, "clicked_immediate_publish": click_result.get("clicked_immediate_publish", False)})
        dialogs = handle_publish_dialogs(page, logger=logger)
        retry_after_dialog_errors: dict[str, Any] = {}
        if dialogs.get("status") == "manual_required" and dialogs.get("required_errors"):
            _log(
                logger,
                "publish_dialog_required_retry",
                "start",
                f"Publish dialog exposed {len(dialogs.get('required_errors', []))} required error(s); refilling attributes and retrying once.",
                page=page,
                extra={"required_errors": dialogs.get("required_errors", [])},
            )
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            retry_description_image = ensure_product_description_image_module(page, product_data, config, logger=logger)
            retry_attributes = fill_required_product_attributes(page, product_data, config, logger=logger)
            retry_ensure = ensure_no_required_errors_before_publish(page, config, product_data, logger=logger, state=state)
            retry_after_dialog_errors = {"description_image": retry_description_image, "attributes": retry_attributes, "ensure": retry_ensure}
            if retry_ensure.get("status") == "ok":
                click_result = click_immediate_publish(page, config, logger=logger)
                dialogs = handle_publish_dialogs(page, logger=logger)
                retry_after_dialog_errors.update({"click_publish": click_result, "dialogs": dialogs})
                _log(
                    logger,
                    "publish_dialog_required_retry",
                    "ok" if dialogs.get("status") != "manual_required" else "manual_required",
                    f"Publish retry completed with dialog status={dialogs.get('status')}.",
                    page=page,
                    extra=retry_after_dialog_errors,
                )
        publish_status = verify_publish_status(page, product_data, config, logger=logger)
        full_publish_screenshot = ""
        try:
            full_publish_screenshot = take_screenshot(page, "full_publish_result")
        except Exception:
            pass
        _log(
            logger,
            "publish_result",
            publish_status.get("status", "unknown"),
            f"发布状态验证完成: {publish_status}",
            page=page,
            screenshot_path=full_publish_screenshot or publish_status.get("screenshot_path", ""),
            extra={"product_id": product_id},
        )

        result.update(
            product_data,
            dimensions=dimensions,
            package=package,
            attributes=attributes,
            size_chart=size_chart,
            description_image=description_image,
            ensure=ensure,
            click_publish=click_result,
            dialogs=dialogs,
            retry_after_dialog_errors=retry_after_dialog_errors,
            publish_status=publish_status,
            full_publish_screenshot=full_publish_screenshot,
        )
        result["status"] = publish_status.get("status", "unknown")
        result["manual_required"] = dialogs.get("status") == "manual_required"
        if state:
            state.update(status=result["status"], publish_result=result)
        return result
    except Exception as exc:
        screenshot_path = ""
        try:
            screenshot_path = take_screenshot(page, "publish_current_edit_error")
        except Exception:
            pass
        _log(logger, "run_publish_current_edit_page", "error", f"当前编辑页发布流程异常: {exc}", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="publish_error", error=str(exc), screenshot_path=screenshot_path)
        return {"status": "error", "manual_required": True, "message": str(exc), "screenshot_path": screenshot_path}


def fill_variant_dimensions_and_weight(page: Any, config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    defaults = config.get("product_defaults", {})
    length = str(defaults.get("default_length_cm", 10))
    width = str(defaults.get("default_width_cm", 10))
    height = str(defaults.get("default_height_cm", 3))
    weight_min = int(defaults.get("weight_min_g", 30))
    weight_max = int(defaults.get("weight_max_g", 99))

    filled = page.evaluate(
        """({length, width, height, weightMin, weightMax}) => {
            const visibleEnabled = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled;
            const setValue = (el, value) => {
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            };
            const fillByName = (name, valueFactory) => {
                const inputs = Array.from(document.querySelectorAll(`input[name="${name}"]`)).filter(visibleEnabled);
                inputs.forEach((el, index) => setValue(el, String(valueFactory(index))));
                return inputs.length;
            };
            const weightCount = fillByName('weight', () => Math.floor(Math.random() * (weightMax - weightMin + 1)) + weightMin);
            return {
                length: fillByName('skuLength', () => length),
                width: fillByName('skuWidth', () => width),
                height: fillByName('skuHeight', () => height),
                weight: weightCount
            };
        }""",
        {"length": length, "width": width, "height": height, "weightMin": weight_min, "weightMax": weight_max},
    )
    page.wait_for_timeout(800)
    validation = _validate_dimensions_and_weight(page)
    status = "ok" if validation["ok"] else "manual_required"
    screenshot_path = "" if validation["ok"] else take_screenshot(page, "variant_dimensions_weight")
    _log(
        logger,
        "fill_variant_dimensions_and_weight",
        status,
        f"尺寸/重量填充结果: {filled}, 校验: {validation}",
        page=page,
        screenshot_path=screenshot_path,
        extra={"required_errors_count": 0 if validation["ok"] else len(validation["errors"])},
    )
    return {"status": status, "filled": filled, "validation": validation, "screenshot_path": screenshot_path}


def extract_dimensions_from_product_images(page: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Read dimension-like text from product carousel image DOM/nearby text.

    This intentionally starts with DOM signals (text near images, alt/title, file
    names). If OCR is added later, it can append to ``raw_dimension_text`` before
    the parser below runs.
    """
    context = context or {}
    candidates = page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const dimensionRe = /(\\d+(?:\\.\\d+)?\\s*(?:x|X|×|\\*)\\s*\\d+(?:\\.\\d+)?\\s*(?:mm|cm|毫米|厘米)\\b|800\\s*(?:x|X|×|\\*)\\s*800|\\b\\d+(?:\\.\\d+)?\\s*(?:mm|cm|毫米|厘米|g|kg|克|千克|cc)\\b|depth|height|size|dimension|尺寸|规格|参数|宽|高|厚|深)/i;
            const headers = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                const text = textOf(el);
                return /产品轮播图|产品素材图|主图|轮播图|图片/.test(text) && text.length < 80;
            });
            const headerY = headers.length ? Math.min(...headers.map((el) => el.getBoundingClientRect().y)) : 0;
            const nextHeaders = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                const text = textOf(el);
                const y = el.getBoundingClientRect().y;
                return y > headerY + 40 && /变种信息|包装信息|产品属性|产品描述|物流/.test(text) && text.length < 80;
            });
            const nextY = nextHeaders.length ? Math.min(...nextHeaders.map((el) => el.getBoundingClientRect().y)) : Number.MAX_SAFE_INTEGER;
            let imgs = Array.from(document.querySelectorAll('img')).filter(visible).filter((img) => {
                if (img.closest('.ant-modal,.ant-dropdown,.ant-popover')) return false;
                const r = img.getBoundingClientRect();
                const src = img.currentSrc || img.src || '';
                if (!src || src.startsWith('data:') || src.includes('loading')) return false;
                return r.y >= Math.max(0, headerY - 120) && r.y <= nextY;
            });
            if (!imgs.length) {
                imgs = Array.from(document.querySelectorAll('img')).filter(visible).filter((img) => {
                    if (img.closest('.ant-modal,.ant-dropdown,.ant-popover')) return false;
                    const r = img.getBoundingClientRect();
                    const src = img.currentSrc || img.src || '';
                    if (!src || src.startsWith('data:') || src.includes('loading')) return false;
                    if (r.width < 45 || r.height < 45) return false;
                    if (/logo|avatar|icon|loading|placeholder/i.test(src)) return false;
                    return true;
                }).slice(0, 20);
            }
            return imgs.map((img, index) => {
                const r = img.getBoundingClientRect();
                const src = img.currentSrc || img.src || '';
                const parent = img.closest('li,div[class*="image"],div[class*="img"],div[class*="pic"],div[class*="upload"],div') || img.parentElement;
                const grand = parent ? (parent.parentElement || parent) : img.parentElement;
                const attrs = [
                    img.alt || '',
                    img.title || '',
                    img.getAttribute('aria-label') || '',
                    img.getAttribute('data-original') || '',
                    img.getAttribute('data-src') || '',
                    src.split('/').pop() || ''
                ].join(' ');
                const nearby = [parent, grand].filter(Boolean).map((el) => textOf(el).slice(0, 500)).join(' ');
                const text = [attrs, nearby].join(' ').replace(/\\s+/g, ' ').trim();
                return {
                    index: index + 1,
                    src,
                    natural_width: img.naturalWidth || 0,
                    natural_height: img.naturalHeight || 0,
                    text,
                    has_dimension_mark: dimensionRe.test(text),
                    x: r.x,
                    y: r.y,
                    width: r.width,
                    height: r.height
                };
            });
        }"""
    )
    candidates = candidates if isinstance(candidates, list) else []
    raw_text = " ".join(str(item.get("text", "")) for item in candidates if isinstance(item, dict))
    if context.get("dimension_hint_text"):
        raw_text = f"{raw_text} {context.get('dimension_hint_text')}"
    ocr_result = _ocr_product_image_candidates(candidates, context)
    ocr_text = " ".join(str(item.get("combined_text", "")) for item in ocr_result.get("items", []) if isinstance(item, dict))
    parsed = _parse_dimension_text(f"{raw_text} {ocr_text}")
    marked = []
    physical_mark_re = re.compile(r"(\d+(?:\.\d+)?\s*(?:x|X|×|\*)\s*\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)\b|\b\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米|g|kg|克|千克|cc)\b|depth|height|caliber|diameter|volume|upper caliber|bottom diameter|single volume|尺寸|规格|参数|宽|高|厚|深)", re.IGNORECASE)
    for item in candidates:
        if not isinstance(item, dict):
            continue
        item_index = int(item.get("index", 0) or 0)
        ocr_item = next((entry for entry in ocr_result.get("items", []) if isinstance(entry, dict) and int(entry.get("index", 0) or 0) == item_index), {})
        if ocr_item:
            item["ocr_text"] = ocr_item.get("combined_text", "")
            item["ocr_has_dimension_mark"] = bool(physical_mark_re.search(str(ocr_item.get("combined_text") or "")))
        item["physical_dimension_mark"] = bool(physical_mark_re.search(" ".join([str(item.get("text") or ""), str(item.get("ocr_text") or "")])))
        if item.get("physical_dimension_mark"):
            marked.append(item)
    parsed["image_candidates"] = candidates[:20]
    parsed["image_ocr_debug_path"] = ocr_result.get("debug_path", "")
    parsed["image_ocr_items"] = ocr_result.get("items", [])[:20]
    best_mark = _pick_best_dimension_image(marked, parsed.get("raw_dimension_text", ""))
    parsed["dimension_candidate_src"] = str((best_mark if best_mark else {}).get("src", ""))
    parsed["dimension_candidate_index"] = int((best_mark if best_mark else {}).get("index", 0) or 0)
    return parsed


def fill_variant_dimensions_and_weight_from_images(page: Any, dimension_info: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    length = str(dimension_info.get("length_cm") or 12)
    width = str(dimension_info.get("width_cm") or 8)
    height = str(dimension_info.get("height_cm") or 4)
    weight = str(dimension_info.get("weight_g") or 71)
    filled = page.evaluate(
        """({length, width, height, weight}) => {
            const visibleEnabled = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled;
            const setValue = (el, value) => {
                const proto = Object.getPrototypeOf(el);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                el.dispatchEvent(new Event('blur', {bubbles: true}));
            };
            const fillByName = (name, value) => {
                const inputs = Array.from(document.querySelectorAll(`input[name="${name}"]`)).filter(visibleEnabled);
                inputs.forEach((el) => setValue(el, String(value)));
                return inputs.length;
            };
            return {
                skuLength: fillByName('skuLength', length),
                skuWidth: fillByName('skuWidth', width),
                skuHeight: fillByName('skuHeight', height),
                weight: fillByName('weight', weight)
            };
        }""",
        {"length": length, "width": width, "height": height, "weight": weight},
    )
    page.wait_for_timeout(500)
    validation = _validate_dimensions_and_weight(page)
    status = "ok" if validation.get("ok") else "manual_required"
    screenshot_path = "" if status == "ok" else take_screenshot(page, "field_fill_dimensions_from_images")
    _log(
        logger,
        "field_fill_dimensions_from_images",
        status,
        f"Dimension fill from image text: {dimension_info}; filled={filled}; validation={validation}",
        page=page,
        screenshot_path=screenshot_path,
    )
    return {"status": status, "filled": filled, "validation": validation, "screenshot_path": screenshot_path, **dimension_info}


def choose_package_image_with_dimension_priority(page: Any, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    open_result = _open_package_collect_image_modal(page)
    if open_result.get("status") != "ok":
        return {**open_result, "selected_index": 0, "has_dimension_mark": False, "package_image_source": "fallback_random", "selected_image_text": ""}

    preferred_src = str(context.get("dimension_candidate_src") or "")
    selected = page.evaluate(
        """({preferredSrc}) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);
            const modal = modals.find((el) => textOf(el).includes('引用采集图片')) || modals[modals.length - 1];
            if (!modal) return {status: 'manual_required', message: '引用采集图片弹窗未打开'};
            const dimensionRe = /(\\d+(?:\\.\\d+)?\\s*(?:x|X|×|\\*)\\s*\\d+(?:\\.\\d+)?\\s*(?:mm|cm|毫米|厘米)\\b|800\\s*(?:x|X|×|\\*)\\s*800|\\b\\d+(?:\\.\\d+)?\\s*(?:mm|cm|毫米|厘米|g|kg|克|千克|cc)\\b|detail display|size|dimension|caliber|diameter|volume|depth|upper caliber|bottom diameter|single volume|尺寸|规格|参数|参数表|尺寸表|宽|高|厚|深)/i;
            const boxes = Array.from(modal.querySelectorAll('input[type="checkbox"]')).filter((box) => box.value && box.value !== 'on' && !box.disabled);
            const items = boxes.map((box, index) => {
                const root = box.closest('label,li,div[class*="item"],div[class*="image"],div[class*="img"],tr,div') || box.parentElement || box;
                const img = root.querySelector('img');
                const src = img ? (img.currentSrc || img.src || img.getAttribute('data-src') || '') : '';
                const text = [
                    textOf(root),
                    img ? (img.alt || '') : '',
                    img ? (img.title || '') : '',
                    src.split('/').pop() || '',
                    box.value || ''
                ].join(' ').replace(/\\s+/g, ' ').trim();
                let score = 0;
                const preferredMatch = !!(preferredSrc && src && (src === preferredSrc || src.includes(preferredSrc.split('/').pop() || '__never__')));
                const hasDimension = dimensionRe.test(text) || preferredMatch;
                if (hasDimension) score += 100;
                if (preferredMatch) score += 80;
                if (!hasDimension && /800\\s*(?:x|X|×|\\*)\\s*800/.test(text)) score += 20;
                if (img && img.naturalWidth >= 500 && img.naturalHeight >= 500) score += 5;
                return {box, root, img, src, text, index: index + 1, score, hasDimension};
            });
            if (!items.length) return {status: 'manual_required', message: '弹窗中没有可选图片'};
            items.sort((a, b) => b.score - a.score || a.index - b.index);
            const picked = items[0];
            if (!picked.box.checked) {
                const label = picked.box.closest('label') || picked.root || picked.box;
                label.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                label.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                label.click();
            }
            return {
                status: 'selected',
                selected_index: picked.index,
                has_dimension_mark: !!picked.hasDimension,
                selected_image_text: picked.text.slice(0, 800),
                selected_src: picked.src,
                score: picked.score,
                package_image_source: picked.hasDimension ? 'dimension_marked_image' : 'fallback_random'
            };
        }""",
        {"preferredSrc": preferred_src},
    )
    if selected.get("status") != "selected":
        return {**selected, "selected_index": 0, "has_dimension_mark": False, "package_image_source": "fallback_random", "selected_image_text": ""}
    page.wait_for_timeout(500)
    try:
        confirm = page.locator('.ant-modal:has-text("引用采集图片") button:has-text("选择")').last
        confirm.click(timeout=5000)
    except Exception:
        confirm = page.locator('.ant-modal:visible button.ant-btn-primary').last
        confirm.click(timeout=5000)
    page.wait_for_timeout(2000)
    image_ok = _has_package_image(page)
    return {**selected, "status": "ok" if image_ok else "manual_required", "image_ok": image_ok}


def _parse_dimension_text(raw_text: str) -> dict[str, Any]:
    text = re.sub(r"\s+", " ", str(raw_text or "")).strip()
    dimension_pairs: list[tuple[float, float, str]] = []
    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:x|X|×|\*)\s*(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米|px)?", text):
        unit = (match.group(3) or "").lower()
        first = float(match.group(1))
        second = float(match.group(2))
        if unit == "px" or (not unit and first >= 700 and second >= 700):
            continue
        dimension_pairs.append((first, second, unit))

    singles: list[tuple[float, str, str]] = []
    for match in re.finditer(r"(?:(depth|height|thick|thickness|高|厚|深|宽|长)\\s*[:：]?\s*)?(\d+(?:\.\d+)?)\s*(mm|cm|毫米|厘米|g|kg|克|千克|cc)\b", text, flags=re.IGNORECASE):
        label = (match.group(1) or "").lower()
        value = float(match.group(2))
        unit = match.group(3).lower()
        singles.append((value, unit, label))

    def to_cm(value: float, unit: str) -> float:
        if unit in {"mm", "毫米"}:
            return value / 10
        if unit in {"cm", "厘米"}:
            return value
        return value / 10 if value > 100 else value

    length_cm = width_cm = height_cm = None
    raw_dimension_text = ""
    if dimension_pairs:
        first, second, unit = max(dimension_pairs, key=lambda item: item[0] * item[1])
        length_cm = to_cm(first, unit)
        width_cm = to_cm(second, unit)
        raw_dimension_text = f"{first:g}*{second:g}{unit or ''}"

    for value, unit, label in singles:
        if unit in {"g", "克", "kg", "千克", "cc"}:
            continue
        if label in {"depth", "height", "thick", "thickness", "高", "厚", "深"}:
            height_cm = to_cm(value, unit)
            if raw_dimension_text:
                raw_dimension_text += f"; {label or 'height'} {value:g}{unit}"
            else:
                raw_dimension_text = f"{label or 'height'} {value:g}{unit}"
            break

    weight_g = None
    for value, unit, label in singles:
        if unit in {"g", "克"}:
            weight_g = int(round(value))
            break
        if unit in {"kg", "千克"}:
            weight_g = int(round(value * 1000))
            break

    if length_cm and width_cm:
        source = "image_dimension_detected" if weight_g else "image_dimension_detected_weight_fallback"
        if not height_cm:
            height_cm = 4
        if not weight_g:
            weight_g = 71
    else:
        source = "fallback_random"
        length_cm, width_cm, height_cm, weight_g = 12, 8, 4, 71

    return {
        "raw_dimension_text": raw_dimension_text,
        "length_cm": _format_number(length_cm),
        "width_cm": _format_number(width_cm),
        "height_cm": _format_number(height_cm),
        "weight_g": str(int(round(float(weight_g)))),
        "source": source,
    }


def _ocr_product_image_candidates(candidates: list[dict[str, Any]], context: dict[str, Any]) -> dict[str, Any]:
    stamp = now_ts()
    debug_dir = PROJECT_ROOT / "data" / "debug" / f"dimension_images_{stamp}"
    debug_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    vision_disabled = False
    for item in candidates[:12]:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "")
        if not src.startswith("http"):
            continue
        image_path = _download_debug_image(src, debug_dir, int(item.get("index", 0) or len(items) + 1))
        windows_text = ""
        windows_error = ""
        vision_text = ""
        vision_error = ""
        if image_path:
            windows_text, windows_error = _windows_ocr_image(image_path)
            if not vision_disabled:
                vision_text, vision_error = _easyrouter_image_ocr(image_path)
                if vision_error and not vision_text:
                    vision_disabled = True
        combined = " ".join([str(item.get("text", "")), windows_text, vision_text]).strip()
        has_mark = bool(re.search(r"(\d+(?:\.\d+)?\s*(?:x|X|×|\*)\s*\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米)\b|\b\d+(?:\.\d+)?\s*(?:mm|cm|毫米|厘米|g|kg|克|千克|cc)\b|depth|height|caliber|diameter|volume|upper caliber|bottom diameter|single volume|尺寸|规格|参数|宽|高|厚|深)", combined, flags=re.IGNORECASE))
        items.append({
            "index": item.get("index", 0),
            "src": src,
            "local_path": str(image_path or ""),
            "dom_text": item.get("text", ""),
            "windows_ocr_text": windows_text,
            "windows_ocr_error": windows_error,
            "easyrouter_vision_text": vision_text,
            "easyrouter_vision_error": vision_error,
            "combined_text": combined,
            "has_dimension_mark": has_mark,
        })
    debug_payload = {
        "timestamp": stamp,
        "edit_url": context.get("edit_url") or context.get("url") or "",
        "title": context.get("title") or context.get("source_list_title") or "",
        "items": items,
    }
    debug_path = PROJECT_ROOT / "data" / "debug" / f"dimension_image_ocr_{stamp}.json"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    debug_path.write_text(json.dumps(debug_payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return {"debug_path": str(debug_path), "items": items}


def _pick_best_dimension_image(marked: list[dict[str, Any]], raw_dimension_text: str) -> dict[str, Any]:
    if not marked:
        return {}
    raw_tokens = [token.lower() for token in re.findall(r"\d+(?:\.\d+)?\s*(?:mm|cm|cc)|\d+(?:\.\d+)?\s*(?:x|X|×|\*)\s*\d+(?:\.\d+)?", str(raw_dimension_text or ""))]
    scored: list[tuple[int, dict[str, Any]]] = []
    for item in marked:
        text = " ".join([str(item.get("text") or ""), str(item.get("ocr_text") or "")]).lower()
        score = 0
        if re.search(r"\d+(?:\.\d+)?\s*(?:x|×|\*)\s*\d+(?:\.\d+)?\s*(?:mm|cm)", text):
            score += 100
        if re.search(r"\bdepth\b|\bheight\b|高|厚|深", text):
            score += 40
        if re.search(r"caliber|diameter|volume|cc|规格|参数|尺寸", text):
            score += 30
        for token in raw_tokens:
            if token and token in text:
                score += 10
        scored.append((score, item))
    scored.sort(key=lambda pair: (-pair[0], int(pair[1].get("index", 9999) or 9999)))
    return scored[0][1] if scored else marked[0]


def _download_debug_image(src: str, debug_dir: Path, index: int) -> Path | None:
    try:
        suffix = Path(src.split("?", 1)[0]).suffix.lower()
        if suffix not in IMAGE_EXTENSIONS:
            suffix = ".jpg"
        path = debug_dir / f"image_{index:02d}{suffix}"
        request = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=20) as response:
            path.write_bytes(response.read())
        return path
    except Exception:
        return None


def _windows_ocr_image(image_path: Path) -> tuple[str, str]:
    script = r"""
param([string]$Path)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType = WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType = WindowsRuntime]
$methods = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
}
$asTaskGeneric = $methods[0]
function Await($AsyncOp, [Type]$ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $task = $asTask.Invoke($null, @($AsyncOp))
    $task.Wait()
    return $task.Result
}
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($Path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { throw 'OcrEngine unavailable' }
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
$result.Text
"""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as handle:
            handle.write(script)
            script_path = handle.name
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script_path, str(image_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
        )
        if completed.returncode == 0:
            return completed.stdout.strip(), ""
        return "", (completed.stderr or completed.stdout).strip()
    except Exception as exc:
        return "", str(exc)
    finally:
        try:
            Path(script_path).unlink(missing_ok=True)  # type: ignore[name-defined]
        except Exception:
            pass


def _easyrouter_image_ocr(image_path: Path) -> tuple[str, str]:
    model_candidates = [
        os.getenv("EASYROUTER_VISION_MODEL", "").strip(),
        os.getenv("EASYROUTER_PRO_MODEL", "").strip(),
        os.getenv("EASYROUTER_TEXT_MODEL", "").strip(),
        os.getenv("EASYROUTER_BACKUP_MODEL", "").strip(),
    ]
    models: list[str] = []
    for model in model_candidates:
        if model and model not in models:
            models.append(model)
    if not models:
        return "", "no_easyrouter_model_configured"
    try:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except Exception as exc:
        return "", f"image_base64_failed: {exc}"
    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    prompt = (
        "OCR this product image. Extract all visible text and product physical dimensions only. "
        "Focus on parameter/size tables: values like 540*280mm, depth 40mm, 34mm, 15mm, 25cc, caliber, diameter, volume. "
        "Do not treat image pixel size like 800x800 or 1340x1785 as product physical size. Return concise plain text."
    )
    errors: list[str] = []
    for model in models:
        try:
            client = EasyRouterClient(model=model, max_tokens=500, temperature=0.0)
            response = client.client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
                        ],
                    }
                ],
                temperature=0.0,
                max_tokens=500,
            )
            text = (response.choices[0].message.content or "").strip()
            if text:
                return text, ""
        except Exception as exc:
            errors.append(f"{model}: {exc}")
    return "", " | ".join(errors)[:1200]


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except Exception:
        return str(value or "")
    if abs(number - round(number)) < 0.001:
        return str(int(round(number)))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def fill_size_chart_required(page: Any, product_data: dict[str, Any], config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    """Fill Temu size chart when the edit page marks it as required.

    Some categories require a size chart even when SKU dimensions are complete.
    The modal currently exposes one template-name input and a grid of numeric
    inputs named ``price`` for the selected size rows/columns.
    """
    try:
        state = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const box = Array.from(document.querySelectorAll('.skuAttrSizeChart')).find(visible);
                if (!box) return {present: false, ok: true, text: ''};
                const text = (box.innerText || box.textContent || '').trim().replace(/\\s+/g, ' ');
                return {present: true, ok: text && !text.includes('添加尺码表'), text};
            }"""
        )
        if not state.get("present"):
            return {"status": "skipped", "message": "size chart section not present", "state": state}
        if state.get("ok"):
            _log(logger, "fill_size_chart_required", "ok", f"尺码表已存在: {state.get('text')}", page=page)
            return {"status": "ok", "already_filled": True, "state": state}

        page.locator(".skuAttrSizeChart .link").first.scroll_into_view_if_needed(timeout=3000)
        page.locator(".skuAttrSizeChart .link").first.click(timeout=5000)
        page.locator(".ant-modal:visible").last.wait_for(state="visible", timeout=8000)
        modal = page.locator(".ant-modal:visible").last

        title = str(product_data.get("title") or product_data.get("original_title") or "Pet Bed").strip()
        template_name = _build_size_chart_name(title)
        text_inputs = modal.locator('input[type="text"]')
        if text_inputs.count() > 0:
            text_inputs.first.fill(template_name, timeout=5000)

        values = _size_chart_values_from_skus(product_data)
        numeric_inputs = modal.locator('input[name="price"]')
        input_count = numeric_inputs.count()
        if input_count == 0:
            screenshot_path = take_screenshot(page, "size_chart_no_inputs")
            _log(logger, "fill_size_chart_required", "manual_required", "Size chart modal opened but no numeric inputs were found.", page=page, screenshot_path=screenshot_path)
            return {"status": "manual_required", "message": "size chart inputs not found", "screenshot_path": screenshot_path}
        for index in range(input_count):
            numeric_inputs.nth(index).fill(values[index % len(values)], timeout=5000)

        modal.locator("button.ant-btn-primary").last.click(timeout=5000)
        page.wait_for_timeout(1200)
        final_state = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const box = Array.from(document.querySelectorAll('.skuAttrSizeChart')).find(visible);
                const text = box ? (box.innerText || box.textContent || '').trim().replace(/\\s+/g, ' ') : '';
                return {present: !!box, ok: !!text && !text.includes('添加尺码表'), text};
            }"""
        )
        status = "ok" if final_state.get("ok") else "manual_required"
        screenshot_path = "" if status == "ok" else take_screenshot(page, "size_chart_required")
        _log(
            logger,
            "fill_size_chart_required",
            status,
            f"Size chart fill result: {final_state}",
            page=page,
            screenshot_path=screenshot_path,
            extra={"input_count": input_count, "template_name": template_name},
        )
        return {"status": status, "template_name": template_name, "input_count": input_count, "state": final_state, "screenshot_path": screenshot_path}
    except Exception as exc:
        screenshot_path = take_screenshot(page, "size_chart_required_error")
        _log(logger, "fill_size_chart_required", "manual_required", f"Size chart fill failed: {exc}", page=page, screenshot_path=screenshot_path)
        return {"status": "manual_required", "message": str(exc), "screenshot_path": screenshot_path}


def fill_package_info_required(page: Any, product_id: str, config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    defaults = config.get("product_defaults", {})
    _close_blocking_image_modals(page)
    shape = _select_ant_by_control_text(page, ["请选择外包装形状", "外包装形状"], [defaults.get("package_shape", "规则")], allow_first=True)
    package_type_values = [
        defaults.get("package_type", "软包装+软物"),
        "软包装 + 软物",
        "软包装+软物",
        "软包装 / 软物",
        "软包装",
    ]
    package_type = _select_ant_by_control_text(page, ["请选择外包装类型", "外包装类型"], package_type_values, allow_first=True)
    upload_result = _choose_existing_package_image(page)
    image_path = None
    if upload_result.get("status") != "ok":
        image_path = _find_or_download_package_image(page, product_id)
        upload_result = _upload_package_image(page, image_path) if image_path else {"status": "manual_required", "message": "没有可用商品图片"}
    image_ok = _has_package_image(page)
    status = "ok" if shape.get("status") == "ok" and package_type.get("status") == "ok" and image_ok else "manual_required"
    screenshot_path = "" if status == "ok" else take_screenshot(page, "package_info_required")
    _log(
        logger,
        "fill_package_info_required",
        status,
        f"包装信息处理: shape={shape}, type={package_type}, image_ok={image_ok}, image_path={image_path}",
        page=page,
        screenshot_path=screenshot_path,
        extra={"product_id": product_id},
    )
    return {"status": status, "shape": shape, "type": package_type, "image_path": str(image_path or ""), "upload": upload_result, "image_ok": image_ok, "screenshot_path": screenshot_path}


def ensure_product_description_image_module(
    page: Any,
    product_data: dict[str, Any],
    config: dict[str, Any],
    logger: Any | None = None,
) -> dict[str, Any]:
    """Ensure the Temu product description editor contains at least one image module."""
    try:
        before = _read_description_image_state(page)
        if before.get("has_image_module"):
            _log(logger, "description_image_module", "ok", "Product description already contains an image module.", page=page, extra={"state": before})
            return {"status": "ok", "already_present": True, "before": before, "after": before}

        opened = _open_product_description_editor(page)
        if not opened.get("ok"):
            screenshot_path = take_screenshot(page, "description_image_open_failed")
            _log(logger, "description_image_module", "manual_required", f"Could not open product description editor: {opened}", page=page, screenshot_path=screenshot_path)
            return {"status": "manual_required", "message": "description_editor_not_opened", "open": opened, "screenshot_path": screenshot_path}

        add_result = _add_description_image_module(page)
        upload_result = _fill_description_image_module(page)
        save_result = _save_product_description_editor(page)
        after = _read_description_image_state(page)
        status = "ok" if after.get("has_image_module") or upload_result.get("status") == "ok" else "manual_required"
        screenshot_path = "" if status == "ok" else take_screenshot(page, "description_image_module_required")
        _log(
            logger,
            "description_image_module",
            status,
            f"Description image module result: add={add_result}, upload={upload_result}, save={save_result}, after={after}",
            page=page,
            screenshot_path=screenshot_path,
            extra={"before": before, "after": after, "add": add_result, "upload": upload_result, "save": save_result},
        )
        return {"status": status, "before": before, "after": after, "add": add_result, "upload": upload_result, "save": save_result, "screenshot_path": screenshot_path}
    except Exception as exc:
        screenshot_path = take_screenshot(page, "description_image_module_error")
        _log(logger, "description_image_module", "manual_required", f"Description image module handling failed: {exc}", page=page, screenshot_path=screenshot_path)
        return {"status": "manual_required", "message": str(exc), "screenshot_path": screenshot_path}


def fill_required_product_attributes(page: Any, product_data: dict[str, Any], config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    defaults = config.get("product_defaults", {})
    title = str(product_data.get("title", ""))
    sku_text = " ".join(str(item.get("new") or item.get("old") or "") for item in product_data.get("sku_items", []))
    product_basic = _fill_product_basic_required_inputs(page)
    dynamic_attrs = _fill_basic_dynamic_product_attributes(page)
    color = _infer_color(title + " " + sku_text, defaults)
    selected_color = _select_checkbox_by_label(page, [color, defaults.get("default_color", "白色"), defaults.get("fallback_color", "其他色"), "其他"])
    material = _select_ant_by_control_text(page, ["材质"], [defaults.get("default_material", "其他")], allow_first=False)
    flower = _select_ant_by_control_text(page, ["印花类型"], [defaults.get("default_flower_type", "无印花")], allow_first=False)
    filling = _fill_percentage_attribute(
        page,
        ["填充物成分"],
        ["pp棉", "海绵", "聚氨酯泡沫（海绵）", "涤纶", "棉"],
        "100",
    )
    status = "ok" if selected_color.get("status") == "ok" else "manual_required"
    screenshot_path = "" if status == "ok" else take_screenshot(page, "required_product_attributes")
    _log(logger, "fill_required_product_attributes", status, f"产品属性处理: product_basic={product_basic}, color={selected_color}, material={material}, flower={flower}, filling={filling}, dynamic_attrs={dynamic_attrs}", page=page, screenshot_path=screenshot_path)
    return {"status": status, "product_basic": product_basic, "color": selected_color, "material": material, "flower_type": flower, "filling": filling, "dynamic_attrs": dynamic_attrs, "screenshot_path": screenshot_path}


def _fill_product_basic_required_inputs(page: Any) -> dict[str, Any]:
    """Use real keyboard input for top product-info fields that React validation tracks."""
    results: list[dict[str, Any]] = []
    try:
        targets = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll('#productBasicInfo .ant-form-item, #productBasicInfo .attr-form-item, .ant-form-item-has-error')).filter(visible);
                const wanted = roots.filter((row) => {
                    const text = textOf(row);
                    return text.includes('重量') && (text.includes('磅') || text.includes('请输入产品属性'));
                }).map((row) => {
                    const input = Array.from(row.querySelectorAll('input')).find((el) => {
                        const cls = String(el.className || '');
                        return visible(el) && !el.disabled && el.type !== 'hidden' && el.type !== 'file' &&
                            !cls.includes('ant-select-selection-search-input');
                    });
                    if (!input) return null;
                    const rect = input.getBoundingClientRect();
                    return {
                        label: textOf(row).slice(0, 120),
                        value: input.value || '',
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                        width: rect.width,
                        height: rect.height
                    };
                }).filter(Boolean);
                return wanted.slice(0, 3);
            }"""
        )
        for target in targets:
            old_value = str(target.get("value") or "").strip()
            if old_value and old_value not in {"0", "0.0", "0.00"}:
                results.append({"status": "already_filled", **target})
                continue
            page.mouse.click(float(target["x"]), float(target["y"]))
            page.keyboard.press("Control+A")
            page.keyboard.type("1")
            page.keyboard.press("Tab")
            page.wait_for_timeout(250)
            results.append({"status": "ok", "old_value": old_value, "new_value": "1", "label": target.get("label", "")})
        return {"status": "ok", "items": results}
    except Exception as exc:
        return {"status": "failed", "message": str(exc), "items": results}


def _fill_basic_dynamic_product_attributes(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """async () => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const setValue = (input, value) => {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                    if (setter) setter.call(input, value);
                    else input.value = value;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                };
                const normalize = (value) => String(value || '').replace(/\\s+/g, '').toLowerCase();
                const rowHas = (rowText, values) => values.some((value) => rowText.includes(value));
                const optionAllowed = (text) => text && !/请选择|选择产品属性|Select|Choose/i.test(text);
                const clickSelectOption = async (row, preferred, reason) => {
                    const select = Array.from(row.querySelectorAll('.ant-select')).find(visible);
                    if (!select) return null;
                    const currentText = textOf(select);
                    if (currentText && optionAllowed(currentText) && !/请选择/.test(currentText)) {
                        return {status: 'already_selected', value: currentText, reason};
                    }
                    const selector = select.querySelector('.ant-select-selector') || select;
                    selector.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mousedown', 'mouseup', 'click']) {
                        selector.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                    await sleep(450);
                    const options = Array.from(document.querySelectorAll(
                        '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content'
                    )).filter(visible).map((el) => ({el, text: textOf(el)})).filter((item) => optionAllowed(item.text));
                    let target = null;
                    for (const value of preferred) {
                        const needle = normalize(value);
                        target = options.find((item) => normalize(item.text) === needle) ||
                            options.find((item) => normalize(item.text).includes(needle));
                        if (target) break;
                    }
                    if (!target) target = options[0] || null;
                    if (!target) return null;
                    target.el.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mousedown', 'mouseup', 'click']) {
                        target.el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                    await sleep(350);
                    return {status: 'ok', value: target.text, reason};
                };
                const clickCheckboxOption = (row, preferred, reason) => {
                    const labels = Array.from(row.querySelectorAll('label')).filter(visible).map((label) => {
                        const input = label.querySelector('input[type="checkbox"],input[type="radio"]');
                        return {label, input, text: textOf(label)};
                    }).filter((item) => item.input && !item.input.disabled && optionAllowed(item.text));
                    if (labels.some((item) => item.input.checked)) {
                        const checked = labels.find((item) => item.input.checked);
                        return {status: 'already_checked', value: checked ? checked.text : '', reason};
                    }
                    let target = null;
                    for (const value of preferred) {
                        const needle = normalize(value);
                        target = labels.find((item) => normalize(item.text) === needle) ||
                            labels.find((item) => normalize(item.text).includes(needle));
                        if (target) break;
                    }
                    if (!target) target = labels[0] || null;
                    if (!target) return null;
                    target.label.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mousedown', 'mouseup', 'click']) {
                        target.label.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                    return {status: 'ok', value: target.text, reason};
                };
                const root = document.querySelector('#productBasicInfo') || document;
                const rows = Array.from(root.querySelectorAll('.attr-form-item, .ant-form-item')).filter(visible);
                const changes = [];
                for (const row of rows) {
                    const text = textOf(row);
                    const input = Array.from(row.querySelectorAll('input')).find((el) =>
                        visible(el) && !el.disabled && el.type !== 'file' && el.type !== 'checkbox' && el.type !== 'radio' &&
                        !String(el.className || '').includes('ant-select-selection-search-input')
                    );
                    if (!input) continue;
                    const current = String(input.value || '').trim();
                    let value = '';
                    let reason = '';
                    if (text.includes('续航时间')) {
                        value = '1';
                        reason = 'runtime_hours';
                    } else if (text.includes('吸力')) {
                        const parsed = parseFloat(current || '0');
                        if (!current || Number.isNaN(parsed) || parsed > 64 || parsed <= 0) {
                            value = '10';
                            reason = 'suction_kpa_limit';
                        }
                    } else if (text.includes('电池容量')) {
                        value = current || '500';
                        reason = 'battery_capacity';
                    } else if ((text.includes('功率') || text.includes('电压')) && !current) {
                        value = '5';
                        reason = 'generic_power';
                    } else if (text.includes('请输入产品属性') && !current) {
                        value = '1';
                        reason = 'required_numeric_attr';
                    }
                    if (value && value !== current) {
                        input.scrollIntoView({block: 'center', inline: 'nearest'});
                        setValue(input, value);
                        changes.push({label: text.slice(0, 80), old_value: current, new_value: value, reason});
                    }
                }
                for (const row of rows) {
                    const text = textOf(row);
                    let selected = null;
                    if (rowHas(text, ['\\u7535\\u6e90\\u65b9\\u5f0f'])) {
                        selected = await clickSelectOption(
                            row,
                            ['\\u65e0\\u7535\\u6e90', '\\u624b\\u52a8', '\\u7535\\u6c60', 'USB', 'Battery', 'Manual', 'Other', '\\u5176\\u4ed6'],
                            'power_mode'
                        );
                    } else if (rowHas(text, ['\\u5de5\\u4f5c\\u7535\\u538b'])) {
                        selected = await clickSelectOption(
                            row,
                            ['5V', '3.7V', '12V', '\\u5176\\u4ed6', 'Other'],
                            'working_voltage'
                        );
                    } else if (rowHas(text, ['\\u7535\\u6c60\\u5c5e\\u6027'])) {
                        selected = await clickSelectOption(
                            row,
                            ['\\u65e0\\u7535\\u6c60', '\\u4e0d\\u542b\\u7535\\u6c60', '\\u5176\\u4ed6', 'No Battery', 'Other'],
                            'battery_property'
                        );
                    } else if (rowHas(text, ['\\u5305\\u542b\\u7535\\u6c60'])) {
                        selected = await clickSelectOption(
                            row,
                            ['\\u5426', '\\u4e0d\\u542b\\u7535\\u6c60', '\\u4e0d\\u5305\\u542b', 'No', 'No Battery', '\\u5176\\u4ed6', 'Other'],
                            'battery_included'
                        );
                    } else if (rowHas(text, ['\\u7535\\u6c60\\u7c7b\\u578b'])) {
                        selected = await clickSelectOption(
                            row,
                            ['\\u65e0\\u7535\\u6c60', '\\u5e72\\u7535\\u6c60', 'AA', 'AAA', '\\u7ebd\\u6263\\u7535\\u6c60', '\\u9502\\u7535\\u6c60', '\\u5176\\u4ed6', 'Other'],
                            'battery_type'
                        );
                    } else if (rowHas(text, ['\\u7279\\u6b8a\\u529f\\u80fd'])) {
                        selected = clickCheckboxOption(
                            row,
                            ['rfid', 'RFID', '\\u9632\\u76d7', '\\u8f7b\\u4fbf', '\\u53ef\\u6298\\u53e0', '\\u9501\\u5b9a', '\\u5176\\u4ed6'],
                            'special_feature'
                        );
                    } else if (row.className && String(row.className).includes('has-error') && text.includes('\\u8bf7\\u9009\\u62e9\\u4ea7\\u54c1\\u5c5e\\u6027')) {
                        selected = await clickSelectOption(row, ['\\u5176\\u4ed6', 'Other'], 'generic_required_select') ||
                            clickCheckboxOption(row, ['\\u5176\\u4ed6', 'Other'], 'generic_required_checkbox');
                    }
                    if (selected && (selected.status === 'ok' || selected.status === 'already_selected' || selected.status === 'already_checked')) {
                        changes.push({label: text.slice(0, 80), new_value: selected.value, reason: selected.reason, status: selected.status});
                    }
                }
                return {status: 'ok', changes};
            }"""
        )
    except Exception as exc:
        return {"status": "failed", "message": str(exc), "changes": []}


def scan_required_errors(page: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    errors.extend(page.evaluate(
        """(keywords) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const isRed = (el) => {
                const style = getComputedStyle(el);
                return /error|danger|invalid/.test(String(el.className).toLowerCase()) ||
                    style.color.includes('255, 0, 0') ||
                    style.color.includes('245, 34, 45') ||
                    style.borderColor.includes('255, 0, 0') ||
                    style.borderColor.includes('245, 34, 45');
            };
            return Array.from(document.querySelectorAll('body *')).filter(visible).map((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                return {el, text};
            }).filter(({el, text}) => text && text.length <= 80 && keywords.some((k) => text.includes(k)) && isRed(el))
              .slice(0, 50).map(({el, text}) => ({
                field: text,
                message: text,
                section: '',
                selector_hint: el.tagName.toLowerCase() + '.' + String(el.className).split(' ').slice(0, 3).join('.')
              }));
        }""",
        REQUIRED_KEYWORDS,
    ))
    errors.extend(page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            return Array.from(document.querySelectorAll('.ant-form-item-has-error')).filter(visible).slice(0, 50).map((el) => {
                const label = textOf(el.querySelector('.ant-form-item-label, label')) || textOf(el).slice(0, 80) || 'form_error';
                const message = textOf(el.querySelector('.ant-form-item-explain-error')) || textOf(el).slice(0, 120) || 'field error';
                return {
                    field: label,
                    message,
                    section: 'form',
                    selector_hint: '.' + String(el.className || '').split(' ').filter(Boolean).slice(0, 3).join('.')
                };
            });
        }"""
    ))

    title = _read_title_value(page)
    if not title or contains_chinese(title):
        errors.append({"field": "产品标题", "message": "标题为空或仍包含中文", "section": "产品信息", "selector_hint": "title input"})

    for item in _read_sku_values(page):
        if not item["value"] or contains_chinese(item["value"]):
            errors.append({"field": "SKU货号", "message": f"SKU为空或仍包含中文: {item['value']}", "section": "变种信息", "selector_hint": item["selector_hint"]})

    dimension_state = _validate_dimensions_and_weight(page)
    errors.extend(dimension_state["errors"])

    package_state = _read_package_state(page)
    if not package_state.get("shape_ok"):
        errors.append({"field": "外包装形状", "message": "请选择外包装形状", "section": "包装信息", "selector_hint": "外包装形状 AntD select"})
    if not package_state.get("type_ok"):
        errors.append({"field": "外包装类型", "message": "请选择外包装类型", "section": "包装信息", "selector_hint": "外包装类型 AntD select"})
    if not package_state.get("image_ok"):
        errors.append({"field": "外包装图片", "message": "请上传外包装图片", "section": "包装信息", "selector_hint": "包装信息 image/file input"})

    if not _has_selected_color(page):
        errors.append({"field": "颜色", "message": "请选择颜色", "section": "产品属性", "selector_hint": "颜色 checkbox"})

    size_chart_state = _read_size_chart_state(page)
    if size_chart_state.get("present") and not size_chart_state.get("ok"):
        errors.append({"field": "size_chart", "message": "Size chart is required", "section": "sku_attributes", "selector_hint": ".skuAttrSizeChart .link"})

    return _dedupe_errors(errors)


def ensure_no_required_errors_before_publish(page: Any, config: dict[str, Any], product_data: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    errors = scan_required_errors(page)
    if errors:
        fill_variant_dimensions_and_weight(page, config, logger=logger)
        fill_package_info_required(page, product_data.get("product_id", "DXM"), config, logger=logger)
        fill_required_product_attributes(page, product_data, config, logger=logger)
        fill_size_chart_required(page, product_data, config, logger=logger)
        errors = scan_required_errors(page)

    if errors:
        screenshot_path = take_screenshot(page, "required_errors")
        _log(logger, "ensure_no_required_errors_before_publish", "manual_required", f"发布前仍有必填错误 {len(errors)} 个，不允许发布。", page=page, screenshot_path=screenshot_path, extra={"required_errors_count": len(errors), "required_errors": errors})
        if state:
            state.update(status="manual_required", manual_step="required_errors_before_publish", required_errors=errors, screenshot_path=screenshot_path)
        return {"status": "manual_required", "errors": errors, "required_errors_count": len(errors), "screenshot_path": screenshot_path}

    _log(logger, "ensure_no_required_errors_before_publish", "ok", "发布前必填错误扫描为 0，允许进入发布点击。", page=page, extra={"required_errors_count": 0})
    return {"status": "ok", "errors": [], "required_errors_count": 0}


def click_immediate_publish(page: Any, config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    before = take_screenshot(page, "publish_before_click")
    if not config.get("publish", {}).get("click_immediate_publish", False):
        return {"status": "manual_required", "message": "click_immediate_publish 未开启", "before_screenshot": before}

    clicked_menu = False
    try:
        page.keyboard.press("Escape")
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(300)
        publish_button = page.locator("button.btn-green").first
        publish_button.wait_for(state="visible", timeout=5000)
        publish_button.click(timeout=5000)
        page.wait_for_timeout(800)
        clicked_menu = _click_publish_dropdown_item(page)
        if not clicked_menu:
            clicked_menu = _click_visible_text(page, ["立即发布"], exact=True)
        if not clicked_menu:
            clicked_menu = _click_visible_text(page, ["直接发布", "马上发布"], exact=True)
    except Exception as exc:
        after = take_screenshot(page, "publish_after_click")
        _log(logger, "click_immediate_publish", "manual_required", f"点击发布按钮失败: {exc}", page=page, screenshot_path=after)
        return {"status": "manual_required", "message": str(exc), "before_screenshot": before, "after_screenshot": after}

    after = take_screenshot(page, "publish_after_click")
    status = "ok" if clicked_menu else "unknown"
    message = "已点击立即发布。" if clicked_menu else "已点击发布按钮，但未观察到“立即发布”菜单项，等待后续弹窗/状态确认。"
    _log(logger, "click_immediate_publish", status, message, page=page, screenshot_path=after)
    return {"status": status, "clicked_immediate_publish": clicked_menu, "before_screenshot": before, "after_screenshot": after}


def handle_publish_dialogs(page: Any, logger: Any | None = None) -> dict[str, Any]:
    actions: list[str] = []
    for _ in range(10):
        check_and_wait_if_captcha(page, logger=logger)
        errors = scan_required_errors(page)
        red_errors = [err for err in errors if err["field"] not in {"外包装图片"}]
        if red_errors and any(keyword in _body_text(page) for keyword in ["请填写", "请选择", "不能为空", "必填"]):
            screenshot_path = take_screenshot(page, "publish_dialog_required_error")
            _log(logger, "handle_publish_dialogs", "manual_required", f"发布弹窗/页面出现字段错误: {red_errors}", page=page, screenshot_path=screenshot_path, extra={"required_errors": red_errors})
            return {"status": "manual_required", "actions": actions, "required_errors": red_errors, "screenshot_path": screenshot_path}

        clicked = False
        for texts in (
            ["产品分类确认", "确认分类"],
            ["确定", "确认", "继续", "知道了"],
        ):
            if _click_visible_text(page, texts, exact=False):
                actions.append(texts[0])
                clicked = True
                page.wait_for_timeout(1200)
                break
        if not clicked:
            break

    _log(logger, "handle_publish_dialogs", "ok", f"发布弹窗处理完成，actions={actions}", page=page)
    return {"status": "ok", "actions": actions}


def verify_publish_status(page: Any, product_data: dict[str, Any], config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:
    wait_seconds = int(config.get("publish", {}).get("wait_after_publish_seconds", 10))
    page.wait_for_timeout(wait_seconds * 1000)
    text = _body_text(page)
    screenshot_path = take_screenshot(page, "publish_result")
    for token in STATUS_OK_TEXTS:
        if token in text:
            _log(logger, "verify_publish_status", "success", f"发布后状态命中: {token}", page=page, screenshot_path=screenshot_path, extra={"product_id": product_data.get("product_id", "")})
            return {"status": "success", "matched_text": token, "url": page.url, "screenshot_path": screenshot_path}

    _log(logger, "verify_publish_status", "unknown", "无法自动确认发布状态，请人工确认是否进入发布中/刊登中/已提交。", page=page, screenshot_path=screenshot_path, extra={"product_id": product_data.get("product_id", "")})
    return {"status": "unknown", "url": page.url, "screenshot_path": screenshot_path}


def _read_size_chart_state(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const box = Array.from(document.querySelectorAll('.skuAttrSizeChart')).find(visible);
                const text = box ? (box.innerText || box.textContent || '').trim().replace(/\\s+/g, ' ') : '';
                return {present: !!box, ok: !!text && !text.includes('添加尺码表'), text};
            }"""
        )
    except Exception:
        return {"present": False, "ok": True, "text": ""}


def _build_size_chart_name(title: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", title)
    base = " ".join(words[:4]).strip() or "Product"
    return (base[:20].strip() + " Size").strip()[:30]


def _size_chart_values_from_skus(product_data: dict[str, Any]) -> list[str]:
    sku_text = " ".join(str(item.get("new") or item.get("old") or "") for item in product_data.get("sku_items", []))
    if re.search(r"\\bL\\b|LARGE", sku_text, re.I):
        return ["40", "0.5", "50", "50", "0.8", "60"]
    return ["35", "0.4", "45", "45", "0.7", "55"]


def _click_publish_dropdown_item(page: Any) -> bool:
    try:
        menu_items = page.locator(".ant-dropdown-menu-item:visible")
        count = menu_items.count()
        if count <= 0:
            return False
        for index in range(count):
            item = menu_items.nth(index)
            try:
                text = item.inner_text(timeout=1000).strip()
            except Exception:
                text = ""
            if "立即发布" in text or "直接发布" in text or "马上发布" in text:
                item.click(timeout=5000)
                return True
        menu_items.first.click(timeout=5000)
        return True
    except Exception:
        return False


def _select_ant_by_control_text(page: Any, control_texts: list[str], values: list[str], allow_first: bool) -> dict[str, Any]:
    result = page.evaluate(
        """({controlTexts, values, allowFirst}) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const normalize = (value) => String(value || '').replace(/[\\s/+／]+/g, '').trim();
            const controls = Array.from(document.querySelectorAll('.ant-select')).filter(visible);
            const control = controls.find((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                return controlTexts.some((needle) => text.includes(needle));
            });
            if (!control) return {status: 'manual_required', message: 'control not found'};
            const currentText = (control.innerText || control.textContent || '').trim();
            if (values.some((value) => currentText.includes(value))) {
                return {status: 'ok', value: currentText, already_selected: true};
            }
            control.scrollIntoView({block: 'center', inline: 'nearest'});
            const selector = control.querySelector('.ant-select-selector') || control;
            selector.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            selector.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            selector.click();
            return {status: 'opened'};
        }""",
        {"controlTexts": control_texts, "values": values, "allowFirst": allow_first},
    )
    if result.get("status") == "ok":
        return result
    if result.get("status") != "opened":
        return result

    page.wait_for_timeout(600)
    selected = page.evaluate(
        """({values, allowFirst}) => {
            const visibleOptions = Array.from(document.querySelectorAll(
                '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content'
            ));
            const normalize = (value) => String(value || '').replace(/[\\s/+／]+/g, '').trim();
            let option = null;
            for (const value of values) {
                option = visibleOptions.find((el) => normalize(el.innerText || el.textContent) === normalize(value));
                if (option) break;
                option = visibleOptions.find((el) => normalize(el.innerText || el.textContent).includes(normalize(value)));
                if (option) break;
            }
            if (!option && allowFirst) {
                option = visibleOptions.find((el) => {
                    const text = (el.innerText || el.textContent || '').trim();
                    return text && !text.includes('请选择');
                });
            }
            if (!option) return {status: 'manual_required', message: 'option not found'};
            const value = (option.innerText || option.textContent || '').trim();
            option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            option.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            option.click();
            return {status: 'ok', value};
        }""",
        {"values": values, "allowFirst": allow_first},
    )
    page.wait_for_timeout(600)
    return selected


def _fill_percentage_attribute(page: Any, labels: list[str], values: list[str], percent: str) -> dict[str, Any]:
    opened = page.evaluate(
        """({labels}) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            let roots = Array.from(document.querySelectorAll('.ant-form-item.attr-form-item')).filter(visible);
            if (!roots.length) roots = Array.from(document.querySelectorAll('.ant-form-item')).filter(visible);
            const root = roots.find((el) => {
                const labelNodes = Array.from(el.querySelectorAll('label, .attr-label, .label-wrapper'));
                return labelNodes.some((node) => labels.some((label) => textOf(node).includes(label)));
            });
            if (!root) return {status: 'manual_required', message: 'attribute field not found'};
            const input = Array.from(root.querySelectorAll('input')).find((el) => {
                return !el.readOnly && !el.disabled && el.type !== 'search' && !el.closest('.ant-select');
            });
            const select = root.querySelector('.ant-select');
            if (!select) return {status: 'manual_required', message: 'attribute select not found', text: textOf(root)};
            const currentText = textOf(select);
            if (currentText && !currentText.includes('请选择')) {
                if (input && !(input.value || '').trim()) {
                    const proto = Object.getPrototypeOf(input);
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(input, '100');
                    else input.value = '100';
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    input.dispatchEvent(new Event('blur', {bubbles: true}));
                }
                return {status: 'ok', already_selected: true, value: currentText};
            }
            const selector = select.querySelector('.ant-select-selector') || select;
            selector.scrollIntoView({block: 'center', inline: 'nearest'});
            selector.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            selector.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            selector.click();
            return {status: 'opened', text: textOf(root)};
        }""",
        {"labels": labels},
    )
    if opened.get("status") == "ok":
        return opened
    if opened.get("status") != "opened":
        return opened

    page.wait_for_timeout(700)
    selected = page.evaluate(
        """({labels, values, percent}) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
            const dropdowns = Array.from(document.querySelectorAll('.ant-select-dropdown:not(.ant-select-dropdown-hidden)')).filter(visible);
            const options = dropdowns.flatMap((el) => Array.from(el.querySelectorAll('.ant-select-item-option-content')));
            let option = null;
            for (const value of values) {
                option = options.find((el) => normalize(textOf(el)) === normalize(value));
                if (option) break;
                option = options.find((el) => normalize(textOf(el)).includes(normalize(value)));
                if (option) break;
            }
            if (!option) {
                option = options.find((el) => {
                    const text = textOf(el);
                    return text && !text.includes('请选择') && !text.endsWith('省') && !text.includes('包装');
                });
            }
            if (!option) return {status: 'manual_required', message: 'attribute option not found'};
            const chosen = textOf(option);
            option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            option.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            option.click();
            let roots = Array.from(document.querySelectorAll('.ant-form-item.attr-form-item')).filter(visible);
            if (!roots.length) roots = Array.from(document.querySelectorAll('.ant-form-item')).filter(visible);
            const root = roots.find((el) => {
                const labelNodes = Array.from(el.querySelectorAll('label, .attr-label, .label-wrapper'));
                return labelNodes.some((node) => labels.some((label) => textOf(node).includes(label)));
            });
            if (!root) return {status: 'ok', value: chosen, percent_filled: false};
            const input = Array.from(root.querySelectorAll('input')).find((el) => {
                return !el.readOnly && !el.disabled && el.type !== 'search' && !el.closest('.ant-select');
            });
            if (input) {
                const proto = Object.getPrototypeOf(input);
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                if (desc && desc.set) desc.set.call(input, percent);
                else input.value = percent;
                input.dispatchEvent(new Event('input', {bubbles: true}));
                input.dispatchEvent(new Event('change', {bubbles: true}));
                input.dispatchEvent(new Event('blur', {bubbles: true}));
            }
            return {status: 'ok', value: chosen, percent_filled: !!input, percent};
        }""",
        {"labels": labels, "values": values, "percent": percent},
    )
    page.wait_for_timeout(600)
    return selected


def _select_checkbox_by_label(page: Any, labels: list[str]) -> dict[str, Any]:
    for label in labels:
        if not label:
            continue
        selected = page.evaluate(
            """(labelText) => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const labels = Array.from(document.querySelectorAll('label')).filter(visible);
                const label = labels.find((el) => (el.innerText || el.textContent || '').trim() === labelText);
                if (!label) return {status: 'not_found'};
                const input = label.querySelector('input[type="checkbox"]') || label.closest('label')?.querySelector('input[type="checkbox"]');
                if (input && input.checked) return {status: 'ok', value: labelText, already_checked: true};
                label.scrollIntoView({block: 'center', inline: 'nearest'});
                label.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                label.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                label.click();
                return {status: 'ok', value: labelText};
            }""",
            label,
        )
        if selected.get("status") == "ok":
            page.wait_for_timeout(300)
            return selected
    fallback = _select_first_variant_color_checkbox(page)
    if fallback.get("status") == "ok":
        page.wait_for_timeout(300)
        return fallback
    return {"status": "manual_required", "message": "color checkbox not found", "tried": labels}


def _validate_dimensions_and_weight(page: Any) -> dict[str, Any]:
    values = page.evaluate(
        """() => {
            const visibleEnabled = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled;
            const read = (name) => Array.from(document.querySelectorAll(`input[name="${name}"]`)).filter(visibleEnabled).map((el) => el.value || '');
            return {
                skuLength: read('skuLength'),
                skuWidth: read('skuWidth'),
                skuHeight: read('skuHeight'),
                weight: read('weight')
            };
        }"""
    )
    errors: list[dict[str, str]] = []
    labels = {"skuLength": "长", "skuWidth": "宽", "skuHeight": "高", "weight": "重量(g)"}
    for key, label in labels.items():
        items = values.get(key, [])
        if not items:
            errors.append({"field": label, "message": f"{label} 输入框不存在", "section": "变种信息", "selector_hint": f'input[name="{key}"]'})
            continue
        for value in items:
            try:
                if float(value) <= 0:
                    errors.append({"field": label, "message": f"{label} 不能为空或0", "section": "变种信息", "selector_hint": f'input[name="{key}"]'})
            except ValueError:
                errors.append({"field": label, "message": f"{label} 不是有效数字: {value}", "section": "变种信息", "selector_hint": f'input[name="{key}"]'})
    return {"ok": not errors, "values": values, "errors": errors}


def _read_package_state(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const root = document.querySelector('#packageInfo');
            const selects = Array.from(document.querySelectorAll('.ant-select')).filter(visible).map((el) => ({
                text: (el.innerText || el.textContent || '').trim(),
                y: el.getBoundingClientRect().y
            }));
            const shape = selects.find((item) => item.text.includes('外包装形状') || item.text.includes('规则') || item.text.includes('不规则') || item.text.includes('请选择外包装形状'));
            const type = selects.find((item) => item.text.includes('外包装类型') || item.text.includes('软包装') || item.text.includes('硬包装') || item.text.includes('请选择外包装类型'));
            const packageHeaders = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => (el.innerText || el.textContent || '').trim() === '包装信息');
            const headerY = packageHeaders.length ? Math.max(...packageHeaders.map((el) => el.getBoundingClientRect().y)) : 0;
            const nextHeaders = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                const y = el.getBoundingClientRect().y;
                return y > headerY && (text === '产品描述' || text === '产品信息');
            });
            const nextY = nextHeaders.length ? Math.min(...nextHeaders.map((el) => el.getBoundingClientRect().y)) : Number.MAX_SAFE_INTEGER;
            const imageRoot = root || document;
            const images = Array.from(imageRoot.querySelectorAll('img')).filter(visible).filter((img) => {
                if (img.closest('.ant-modal, .ant-dropdown, .ant-popover')) return false;
                const y = img.getBoundingClientRect().y;
                const src = img.currentSrc || img.src || '';
                return y > headerY && y < nextY && src && !src.startsWith('data:') && !src.includes('loading');
            });
            const backgroundImages = Array.from(imageRoot.querySelectorAll('*')).filter(visible).filter((el) => {
                if (el.closest('.ant-modal, .ant-dropdown, .ant-popover')) return false;
                const y = el.getBoundingClientRect().y;
                const bg = getComputedStyle(el).backgroundImage || '';
                return y > headerY && y < nextY && bg.includes('url(') && !bg.includes('data:') && !bg.includes('loading');
            });
            return {
                shape_text: shape ? shape.text : '',
                type_text: type ? type.text : '',
                shape_ok: !!shape && !shape.text.includes('请选择'),
                type_ok: !!type && !type.text.includes('请选择'),
                image_ok: images.length > 0 || backgroundImages.length > 0,
                image_count: images.length,
                background_image_count: backgroundImages.length,
                header_y: headerY,
                next_y: nextY
            };
        }"""
    )


def _has_package_image(page: Any) -> bool:
    return bool(_read_package_state(page).get("image_ok"))


def _find_or_download_package_image(page: Any, product_id: str) -> Path | None:
    for root_name in ("downloads", "generated_images"):
        root = PROJECT_ROOT / root_name / product_id
        if root.exists():
            for path in root.iterdir():
                if path.suffix.lower() in IMAGE_EXTENSIONS and path.is_file():
                    return path

    src = page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const imgs = Array.from(document.querySelectorAll('img')).filter(visible).filter((img) => {
                const src = img.currentSrc || img.src || '';
                return src.startsWith('http') && img.naturalWidth >= 300 && img.naturalHeight >= 300;
            });
            return imgs.length ? (imgs[0].currentSrc || imgs[0].src) : '';
        }"""
    )
    if not src:
        return None

    target_dir = PROJECT_ROOT / "downloads" / product_id
    target_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(src.split("?", 1)[0]).suffix.lower()
    if suffix not in IMAGE_EXTENSIONS:
        suffix = ".jpg"
    target = target_dir / f"package_source{suffix}"
    request = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        target.write_bytes(response.read())
    return target


def _upload_package_image(page: Any, image_path: Path) -> dict[str, Any]:
    if not image_path.exists():
        return {"status": "manual_required", "message": f"图片不存在: {image_path}"}
    try:
        package_button = page.locator('#packageInfo button:has-text("选择图片")').first
        try:
            package_button.scroll_into_view_if_needed(timeout=3000)
            with page.expect_file_chooser(timeout=3000) as file_chooser_info:
                package_button.click(timeout=3000)
            file_chooser_info.value.set_files(str(image_path))
            page.wait_for_timeout(5000)
            image_ok = _has_package_image(page)
            if image_ok:
                return {"status": "ok", "path": str(image_path), "image_ok": image_ok, "method": "file_chooser"}
        except Exception:
            pass
        page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = document.querySelector('#packageInfo') ||
                    Array.from(document.querySelectorAll('body *')).find((el) => {
                        const text = textOf(el);
                        return visible(el) && text.includes('包装信息') && text.includes('外包装图片');
                    });
                if (!root) return false;
                const button = Array.from(root.querySelectorAll('button')).filter(visible)
                    .find((el) => textOf(el).includes('选择图片')) || null;
                if (!button) return false;
                button.scrollIntoView({block: 'center', inline: 'nearest'});
                for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
                    button.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                }
                return true;
            }"""
        )
        page.wait_for_timeout(500)
        if page.locator("#localFileUploadInp").count() > 0:
            file_input = page.locator("#localFileUploadInp").first
        else:
            package_inputs = page.locator('#packageInfo input[type="file"]')
            file_input = package_inputs.first if package_inputs.count() > 0 else page.locator('input[type="file"][accept*=".jpg"], input[type="file"][accept*=".png"], input[type="file"][accept*="image"]').first
        file_input.set_input_files(str(image_path), timeout=5000)
        page.wait_for_timeout(5000)
        return {"status": "ok", "path": str(image_path), "image_ok": _has_package_image(page)}
    except Exception as exc:
        return {"status": "manual_required", "message": str(exc), "path": str(image_path)}


def _close_blocking_image_modals(page: Any) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const clickLikeUser = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mouseover', 'mouseenter', 'mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                };
                const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible)
                    .filter((modal) => {
                        const text = textOf(modal);
                        return !text.includes('引用采集图片') && (
                            text.includes('批量改图片尺寸') ||
                            text.includes('生成JPG图片') ||
                            text.includes('生成PNG图片') ||
                            text.includes('变化至')
                        );
                    });
                const closed = [];
                for (const modal of modals) {
                    const buttons = Array.from(modal.querySelectorAll('.ant-modal-close, button, a, span')).filter(visible);
                    const closeButton = buttons.find((el) => String(el.className || '').includes('ant-modal-close'))
                        || buttons.find((el) => ['关闭', '取消'].some((text) => textOf(el) === text || textOf(el).includes(text)));
                    if (!closeButton) continue;
                    clickLikeUser(closeButton);
                    closed.push(textOf(closeButton) || String(closeButton.className || 'close'));
                }
                return {status: 'ok', closed};
            }"""
        )
        page.wait_for_timeout(500)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        page.wait_for_timeout(500)
        return result if isinstance(result, dict) else {"status": "ok"}
    except Exception as exc:
        return {"status": "warning", "message": str(exc)}


def _choose_existing_package_image(page: Any) -> dict[str, Any]:
    try:
        open_result = _open_package_collect_image_modal(page)
        if open_result.get("status") != "ok":
            return open_result

        selected = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);
                const modal = modals.find((el) => (el.innerText || el.textContent || '').includes('引用采集图片'));
                if (!modal) return {status: 'manual_required', message: '引用采集图片弹窗未打开'};
                const boxes = Array.from(modal.querySelectorAll('input[type="checkbox"]'))
                    .filter((box) => box.value && box.value !== 'on');
                const imageBox = boxes[0];
                if (!imageBox) return {status: 'manual_required', message: '弹窗中没有可选图片'};
                if (!imageBox.checked) {
                    const label = imageBox.closest('label') || imageBox;
                    label.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                    label.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                    label.click();
                }
                return {status: 'selected', value: imageBox.value || ''};
            }"""
        )
        if selected.get("status") != "selected":
            return selected
        page.wait_for_timeout(500)
        confirm = page.locator('.ant-modal:has-text("引用采集图片") button:has-text("选择")').last
        confirm.click(timeout=5000)
        page.wait_for_timeout(2500)
        image_ok = _has_package_image(page)
        if not image_ok:
            return {"status": "manual_required", "method": "引用采集图片", "selected": selected, "message": "引用采集图片后未检测到外包装图片"}
        return {"status": "ok", "method": "引用采集图片", "selected": selected, "image_ok": image_ok}
    except Exception as exc:
        return {"status": "manual_required", "method": "引用采集图片", "message": str(exc)}


def _read_description_image_state(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const headers = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                    const text = textOf(el);
                    return text === '产品描述' || text === '产品说明' || text === 'Temu产品描述';
                });
                const header = headers.length ? headers.sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y)[0] : null;
                const headerY = header ? header.getBoundingClientRect().y : 0;
                const nextHeaders = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                    const text = textOf(el);
                    const y = el.getBoundingClientRect().y;
                    return y > headerY + 20 && /包装信息|变种信息|产品属性|物流|服务模板|发布/.test(text) && text.length < 30;
                });
                const nextY = nextHeaders.length ? Math.min(...nextHeaders.map((el) => el.getBoundingClientRect().y)) : Number.MAX_SAFE_INTEGER;
                const imgs = Array.from(document.querySelectorAll('img')).filter(visible).filter((img) => {
                    if (img.closest('.ant-modal, .ant-dropdown, .ant-popover')) return false;
                    const y = img.getBoundingClientRect().y;
                    const src = img.currentSrc || img.src || '';
                    return y >= headerY && y <= nextY && src && !src.startsWith('data:') && !src.includes('loading');
                });
                const imageModules = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                    const text = textOf(el);
                    const y = el.getBoundingClientRect().y;
                    return y >= headerY && y <= nextY && /图片模块|上传图片/.test(text);
                });
                return {
                    header_found: !!header,
                    image_count: imgs.length,
                    image_module_count: imageModules.length,
                    has_image_module: imgs.length > 0 || imageModules.some((el) => /图片模块/.test(textOf(el)) && !/上传图片/.test(textOf(el))),
                    header_y: headerY,
                    next_y: nextY
                };
            }"""
        )
    except Exception as exc:
        return {"has_image_module": False, "message": str(exc)}


def _open_product_description_editor(page: Any) -> dict[str, Any]:
    try:
        try:
            box = page.locator("#wirelessDescBox, .wirelessDescBox, .wireless-description-box").first
            box.scroll_into_view_if_needed(timeout=2500)
            rect = box.bounding_box(timeout=2500)
            if rect:
                page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + rect["height"] / 2)
                page.wait_for_timeout(500)
                page.evaluate(
                    """() => {
                        const button = document.querySelector(
                            '#wirelessDescBox .wireless-description-shadow button, ' +
                            '.wirelessDescBox .wireless-description-shadow button, ' +
                            '.wireless-description-box .wireless-description-shadow button, ' +
                            '#baiduStatisticsSmtNewEditorEditClickNum button'
                        );
                        if (!button) return false;
                        button.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, cancelable:true, view:window}));
                        button.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                        button.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                        button.click();
                        return true;
                    }"""
                )
                page.wait_for_function(
                    """() => {
                        const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                        const editor = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述'));
                        return !!editor;
                    }""",
                    timeout=6000,
                )
                return {"ok": True, "method": "wireless_description_hover_button"}
        except Exception:
            pass
        page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const candidates = Array.from(document.querySelectorAll('body *')).filter(visible).filter((el) => {
                    const text = textOf(el);
                    return text === '产品描述' || text === '产品说明' || text === 'Temu产品描述';
                });
                if (candidates.length) candidates.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y)[0].scrollIntoView({block:'center'});
            }"""
        )
        page.wait_for_timeout(300)
        clicked = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const directButton = document.querySelector('#wirelessDescBox .wireless-description-shadow button, .wirelessDescBox .wireless-description-shadow button, .wireless-description-box .wireless-description-shadow button, #baiduStatisticsSmtNewEditorEditClickNum button');
                if (directButton && visible(directButton)) {
                    directButton.scrollIntoView({block:'center'});
                    directButton.dispatchEvent(new MouseEvent('mouseover', {bubbles:true, cancelable:true, view:window}));
                    directButton.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                    directButton.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                    directButton.click();
                    return {ok:true, text:textOf(directButton), clicked:textOf(directButton), tag:directButton.tagName, className:String(directButton.className || ''), method:'direct_description_button'};
                }
                const all = Array.from(document.querySelectorAll('button,a,[role="button"],.ant-btn,div,span')).filter(visible);
                const clickableCandidates = all.filter((el) => ['BUTTON', 'A'].includes(el.tagName) || el.getAttribute('role') === 'button' || String(el.className || '').includes('btn'));
                let item = clickableCandidates.find((el) => textOf(el) === '编辑描述');
                if (!item) item = clickableCandidates.find((el) => textOf(el).includes('编辑描述'));
                if (!item) item = all.find((el) => textOf(el) === '编辑描述');
                if (!item) item = all.find((el) => textOf(el).includes('编辑描述'));
                if (!item) return {ok:false, message:'edit_description_button_not_found'};
                const clickable = item.closest('button,a,[role="button"],.ant-btn') || item;
                clickable.scrollIntoView({block:'center'});
                clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                clickable.click();
                return {ok:true, text:textOf(item), clicked:textOf(clickable), tag:clickable.tagName, className:String(clickable.className || '')};
            }"""
        )
        if not clicked.get("ok"):
            fallback = page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const boxes = Array.from(document.querySelectorAll('#wirelessDescBox, .wireless-description-box, [class*="wireless-description"], [class*="description-box"]'))
                        .filter(visible)
                        .filter((el) => {
                            const rect = el.getBoundingClientRect();
                            return rect.width > 250 && rect.height > 120;
                        })
                        .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height);
                    const box = boxes[0];
                    if (!box) return {ok:false, message:'description_box_not_found_after_edit_button_miss'};
                    box.scrollIntoView({block:'center'});
                    const rect = box.getBoundingClientRect();
                    return {ok:true, x:rect.left + rect.width / 2, y:rect.top + rect.height / 2, text:textOf(box).slice(0, 200)};
                }"""
            )
            if not fallback.get("ok"):
                return clicked
            page.mouse.move(float(fallback["x"]), float(fallback["y"]))
            page.wait_for_timeout(700)
            hover_clicked = page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const all = Array.from(document.querySelectorAll('button,a,[role="button"],.ant-btn,div,span')).filter(visible);
                    const clickableCandidates = all.filter((el) => ['BUTTON', 'A'].includes(el.tagName) || el.getAttribute('role') === 'button' || String(el.className || '').includes('btn'));
                    let item = clickableCandidates.find((el) => textOf(el) === '编辑描述');
                    if (!item) item = clickableCandidates.find((el) => textOf(el).includes('编辑描述'));
                    if (!item) item = all.find((el) => textOf(el) === '编辑描述');
                    if (!item) item = all.find((el) => textOf(el).includes('编辑描述'));
                    if (!item) {
                        const boxes = Array.from(document.querySelectorAll('#wirelessDescBox, .wireless-description-box, [class*="wireless-description"], [class*="description-box"]'))
                            .filter(visible)
                            .filter((el) => {
                                const rect = el.getBoundingClientRect();
                                return rect.width > 250 && rect.height > 120;
                            })
                            .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height);
                        const box = boxes[0];
                        if (box) {
                            const candidates = Array.from(box.querySelectorAll('button,a,[role="button"],.ant-btn,div,span')).filter(visible);
                            const boxClickable = candidates.filter((el) => ['BUTTON', 'A'].includes(el.tagName) || el.getAttribute('role') === 'button' || String(el.className || '').includes('btn'));
                            item = boxClickable.find((el) => /编辑|描述|Edit/i.test(textOf(el))) || candidates.find((el) => /编辑|描述|Edit/i.test(textOf(el))) || candidates.find((el) => {
                                const rect = el.getBoundingClientRect();
                                return rect.width >= 60 && rect.width <= 220 && rect.height >= 24 && rect.height <= 80;
                            });
                        }
                    }
                    if (!item) return {ok:false, message:'hover_edit_description_button_not_found'};
                    const clickable = item.closest('button,a,[role="button"],.ant-btn') || item;
                    clickable.scrollIntoView({block:'center'});
                    clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                    clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                    clickable.click();
                    return {ok:true, text:textOf(item), clicked:textOf(clickable), tag:clickable.tagName, className:String(clickable.className || '')};
                }"""
            )
            if hover_clicked.get("ok"):
                clicked = {"ok": True, "fallback": fallback, "hover_clicked": hover_clicked}
            else:
                page.mouse.click(float(fallback["x"]), float(fallback["y"]))
                page.wait_for_timeout(400)
                page.mouse.click(float(fallback["x"]), float(fallback["y"]))
                clicked = {"ok": True, "fallback": fallback, "hover_clicked": hover_clicked, "double_center_click": True}
        try:
            page.wait_for_function(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const editor = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述'));
                    if (!editor) return false;
                    const moduleButtons = Array.from(editor.querySelectorAll('.smt-add-module, [class*="add-module"]')).filter(visible);
                    const content = Array.from(editor.querySelectorAll('.smt-content-center, [class*="content-center"]')).filter(visible);
                    return moduleButtons.length > 0 && content.length > 0;
                }""",
                timeout=8000,
            )
            opened = True
        except Exception:
            opened = page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const editor = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述'));
                    if (!editor) return false;
                    const moduleButtons = Array.from(editor.querySelectorAll('.smt-add-module, [class*="add-module"]')).filter(visible);
                    const content = Array.from(editor.querySelectorAll('.smt-content-center, [class*="content-center"]')).filter(visible);
                    return moduleButtons.length > 0 && content.length > 0;
                }"""
            )
        return {"ok": bool(opened), "clicked": clicked}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def _add_description_image_module(page: Any) -> dict[str, Any]:
    try:
        state = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述')) || null;
                if (!root) return {ok:false, message:'description_editor_root_not_found'};
                const rootText = textOf(root);
                if (!/添加模块|使用中模块|Temu产品描述|图片模块/.test(rootText)) return {ok:false, message:'description_editor_not_active'};
                const existing = Array.from(root.querySelectorAll('.using-modules-content .using-item, .smt-content-center .smt-desc-content')).filter(visible).filter((el) => {
                    const text = textOf(el);
                    const hasImageLabel = text === '图片' || text.includes('图片模块');
                    const hasImageBox = !!el.querySelector('.desc-img-box, .item-image-content, img[src^="http"]');
                    return hasImageLabel || hasImageBox;
                });
                if (existing.length) return {ok:true, already_present:true, image_module_count:existing.length};
                const items = Array.from(root.querySelectorAll('.smt-add-module, [class*="add-module"], div, span')).filter(visible);
                let item = root.querySelector('.smt-add-module.m-left20') || Array.from(root.querySelectorAll('.smt-add-module')).filter(visible)[1] || null;
                if (!item) item = items.find((el) => textOf(el) === '图片');
                if (!item) item = items.find((el) => textOf(el).includes('图片') && !textOf(el).includes('上传图片') && !textOf(el).includes('图片模块'));
                let content = Array.from(root.querySelectorAll('.smt-content-center, [class*="content-center"]')).filter(visible)[0] || null;
                if (!content) content = Array.from(root.querySelectorAll('[class*="description-box"], [class*="wireless-description-box"], div')).filter(visible)
                    .filter((el) => /Product Description|产品描述|NOTE|Description/.test(textOf(el)))
                    .sort((a, b) => b.getBoundingClientRect().width * b.getBoundingClientRect().height - a.getBoundingClientRect().width * a.getBoundingClientRect().height)[0] || null;
                if (!item) return {ok:false, message:'image_module_button_not_found'};
                const ir = item.getBoundingClientRect();
                const tr = content ? content.getBoundingClientRect() : null;
                return {
                    ok:true,
                    needs_drag:true,
                    item_text:textOf(item),
                    item:{x:ir.left + ir.width / 2, y:ir.top + ir.height / 2},
                    target: tr ? {x:tr.left + tr.width / 2, y:Math.min(window.innerHeight - 80, tr.bottom + 45)} : null
                };
            }"""
        )
        if state.get("already_present"):
            return state
        if not state.get("ok"):
            return state
        # The Temu description editor creates temporary `.smt-empty` drop zones
        # during dragstart. Dropping on the center container only opens the right
        # panel; dropping on the placeholder actually inserts the module.
        event_result = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述')) || null;
                if (!root) return {ok:false, message:'description_editor_root_not_found'};
                const item = root.querySelector('.smt-add-module.m-left20') || Array.from(root.querySelectorAll('.smt-add-module')).filter(visible)[1] || null;
                if (!item) return {ok:false, message:'drag_source_not_found'};
                const dataTransfer = new DataTransfer();
                dataTransfer.setData('text/plain', 'image');
                item.dispatchEvent(new DragEvent('dragstart', {bubbles:true, cancelable:true, dataTransfer}));
                return {ok:true, phase:'dragstart', empty_count:root.querySelectorAll('.smt-empty').length};
            }"""
        )
        if event_result.get("ok"):
            try:
                page.wait_for_function(
                    """() => {
                        const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                        return Array.from(document.querySelectorAll('.smt-new-editor .smt-empty')).filter(visible).length > 0;
                    }""",
                    timeout=2500,
                )
            except Exception:
                pass
            drop_result = page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const root = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述')) || null;
                    if (!root) return {ok:false, message:'description_editor_root_not_found_after_dragstart'};
                    const item = root.querySelector('.smt-add-module.m-left20') || Array.from(root.querySelectorAll('.smt-add-module')).filter(visible)[1] || null;
                    const empties = Array.from(root.querySelectorAll('.smt-empty')).filter(visible);
                    const target = empties[empties.length - 1] || null;
                    if (!item || !target) return {ok:false, message:'empty_drop_target_not_found', item:!!item, empty_count:empties.length, text:textOf(root).slice(0, 500)};
                    const dataTransfer = new DataTransfer();
                    dataTransfer.setData('text/plain', 'image');
                    for (const type of ['dragenter', 'dragover', 'drop']) {
                        target.dispatchEvent(new DragEvent(type, {bubbles:true, cancelable:true, dataTransfer}));
                    }
                    item.dispatchEvent(new DragEvent('dragend', {bubbles:true, cancelable:true, dataTransfer}));
                    const usingText = textOf(root.querySelector('.using-modules-content') || root);
                    const rightText = textOf(root.querySelector('.smt-content-right, .detail-info') || root);
                    return {
                        ok: usingText.includes('图片') && rightText.includes('图片模块') && rightText.includes('上传图片'),
                        phase:'drop',
                        target_idx: target.closest('.smt-desc-content')?.dataset.idx || '',
                        using_text: usingText.slice(0, 300),
                        right_text: rightText.slice(0, 500)
                    };
                }"""
            )
            if drop_result.get("ok"):
                return {"ok": True, "method": "empty_placeholder_drop", "event": event_result, "drop": drop_result}
        item = state.get("item") or {}
        target = state.get("target") or {}
        if item and target:
            page.mouse.move(float(item["x"]), float(item["y"]))
            page.mouse.down()
            page.mouse.move(float(target["x"]), float(target["y"]), steps=12)
            page.mouse.up()
            page.wait_for_timeout(1000)
        else:
            page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const item = Array.from(document.querySelectorAll('.smt-add-module, [class*="add-module"], div, span')).filter(visible).find((el) => textOf(el) === '图片');
                    if (item) item.click();
                }"""
            )
            page.wait_for_timeout(800)
        after = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const text = Array.from(document.querySelectorAll('.ant-modal, body')).filter(visible).map((el) => el.innerText || '').join('\\n');
                return {ok:/图片模块|上传图片/.test(text), text:text.slice(0, 500)};
            }"""
        )
        return {"ok": bool(after.get("ok")), "drag": state, "after": after}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def _fill_description_image_module(page: Any) -> dict[str, Any]:
    try:
        page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述')) || null;
                if (!root) return false;
                const rightText = textOf(root.querySelector('.smt-content-right, .detail-info') || root);
                if (rightText.includes('图片模块') && rightText.includes('上传图片')) return true;
                const usingImage = Array.from(root.querySelectorAll('.using-modules-content .using-item')).filter(visible)
                    .find((el) => textOf(el) === '图片' || textOf(el).includes('图片'));
                const centerImage = Array.from(root.querySelectorAll('.smt-content-center .smt-desc-content')).filter(visible)
                    .find((el) => el.querySelector('.desc-img-box, .emptyContentBox img'));
                const target = usingImage || centerImage;
                if (!target) return false;
                target.scrollIntoView({block:'center', inline:'nearest'});
                target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                target.click();
                return true;
            }"""
        )
        page.wait_for_timeout(500)
        upload_target = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述')) || null;
                if (!root) return {ok:false, message:'description_editor_root_not_found'};
                const moduleRoots = Array.from(root.querySelectorAll('div,section,li')).filter(visible).filter((el) => {
                    const text = textOf(el);
                    return text.includes('图片模块') || text.includes('上传图片');
                });
                const imgScope = moduleRoots.length ? moduleRoots[moduleRoots.length - 1] : root;
                const imgs = Array.from(imgScope.querySelectorAll('img')).filter(visible).filter((img) => (img.currentSrc || img.src || '').startsWith('http'));
                if (imgs.length) return {ok:true, already_has_image:true, image_count:imgs.length};
                const items = Array.from(root.querySelectorAll('a,button,span,div')).filter(visible);
                let item = Array.from(root.querySelectorAll('.smt-content-right .item-image-content a, .smt-content-right a, .detail-info a')).filter(visible)[0] || null;
                if (!item) item = items.find((el) => textOf(el) === '上传图片');
                if (!item) item = items.find((el) => textOf(el).includes('上传图片'));
                if (!item) return {ok:false, message:'upload_link_not_found'};
                item.scrollIntoView({block:'center'});
                const r = item.getBoundingClientRect();
                return {ok:true, text:textOf(item), x:r.left + r.width / 2, y:r.top + r.height / 2};
            }"""
        )
        if upload_target.get("already_has_image"):
            return {"status": "ok", "method": "already_has_image", "details": upload_target}
        if not upload_target.get("ok"):
            return {"status": "manual_required", "message": upload_target.get("message", "upload link not found")}

        # This Ant dropdown is hover-triggered. A normal DOM click often does nothing.
        page.mouse.move(float(upload_target["x"]), float(upload_target["y"]))
        try:
            page.wait_for_function(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return Array.from(document.querySelectorAll('.ant-dropdown, .ant-dropdown-menu'))
                        .filter(visible)
                        .some((el) => textOf(el).includes('空间上传'));
                }""",
                timeout=1800,
            )
        except Exception:
            page.mouse.click(float(upload_target["x"]), float(upload_target["y"]))
            page.wait_for_timeout(500)

        menu_click = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const dropdown = Array.from(document.querySelectorAll('.ant-dropdown, .ant-dropdown-menu')).filter(visible).find((el) => textOf(el).includes('空间上传'));
                const items = Array.from(document.querySelectorAll('.ant-dropdown-menu-item, .ant-dropdown-menu-title-content, li, a, span')).filter(visible);
                const prefs = ['空间上传', '引用产品轮播图', '引用采集图片', '网络上传'];
                for (let index = 0; index < prefs.length; index += 1) {
                    const pref = prefs[index];
                    const item = items.find((el) => textOf(el).includes(pref));
                    if (item) {
                        const clickable = item.closest('li,[role="menuitem"],a,button') || item;
                        const r = clickable.getBoundingClientRect();
                        if ((r.width < 5 || r.height < 5) && dropdown) {
                            const dr = dropdown.getBoundingClientRect();
                            const menuTexts = ['本地上传', '空间上传', '网络上传', '引用产品轮播图', '引用采集图片'];
                            const rowIndex = Math.max(0, menuTexts.indexOf(pref));
                            return {ok:true, value:pref, text:textOf(dropdown), x:dr.left + Math.max(dr.width / 2, 60), y:dr.top + 16 + rowIndex * 32, by_rect:true};
                        }
                        return {ok:true, value:pref, text:textOf(clickable), x:r.left + r.width / 2, y:r.top + r.height / 2};
                    }
                }
                if (dropdown) {
                    const r = dropdown.getBoundingClientRect();
                    return {ok:true, value:'空间上传', text:textOf(dropdown), x:r.left + Math.max(r.width / 2, 60), y:r.top + 48, by_rect:true};
                }
                return {ok:false, message:'upload_menu_item_not_found', visible_items:items.map(textOf).filter(Boolean).slice(0, 20)};
            }"""
        )
        if not menu_click.get("ok"):
            return {"status": "manual_required", "message": menu_click.get("message", "upload menu not found"), "menu": menu_click}
        page.mouse.click(float(menu_click["x"]), float(menu_click["y"]))
        page.wait_for_timeout(1800)

        chosen = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll('.exchange-btn-position, .ant-modal, .ant-drawer')).filter(visible);
                const root = roots.find((el) => String(el.className || '').includes('exchange-btn-position'))
                    || roots.find((el) => /选择|图片空间|引用产品轮播图|引用采集图片|空间上传/.test(textOf(el)));
                if (!root) return {ok:false, message:'image_space_modal_not_found'};
                const checkboxes = Array.from(root.querySelectorAll('input[type="checkbox"]')).filter((el) => !el.disabled);
                if (checkboxes.length) {
                    const box = checkboxes.find((el) => !el.checked) || checkboxes.find((el) => el.checked) || checkboxes[0];
                    const label = box.closest('label') || box;
                    label.click();
                } else {
                    const img = Array.from(root.querySelectorAll('img')).filter(visible).find((el) => (el.currentSrc || el.src || '').startsWith('http'));
                    if (img) (img.closest('label, li, div') || img).click();
                }
                const primaryButtons = Array.from(root.querySelectorAll('button')).filter(visible).filter((el) => String(el.className || '').includes('ant-btn-primary'));
                const button = primaryButtons
                        .map((el) => ({el, rect:el.getBoundingClientRect(), text:textOf(el)}))
                        .sort((a, b) => b.rect.top - a.rect.top)[0]?.el
                    || Array.from(root.querySelectorAll('button,a')).filter(visible).find((el) => ['选择', '确定', '确认', '保存'].some((text) => textOf(el).includes(text)));
                if (button) {
                    const r = button.getBoundingClientRect();
                    return {ok:true, button:textOf(button), x:r.left + r.width / 2, y:r.top + r.height / 2};
                }
                return {ok:false, message:'confirm_button_not_found', root_text:textOf(root).slice(0, 500)};
            }"""
        )
        if not chosen.get("ok"):
            return {"status": "manual_required", "method": menu_click.get("value"), "menu": menu_click, "chosen": chosen}
        page.mouse.click(float(chosen["x"]), float(chosen["y"]))
        try:
            page.wait_for_function(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    return Array.from(document.querySelectorAll('.exchange-btn-position')).filter(visible).length === 0;
                }""",
                timeout=5000,
            )
        except Exception:
            page.wait_for_timeout(1500)
        verify = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const right = Array.from(document.querySelectorAll('.smt-new-editor .smt-content-right')).filter(visible)[0];
                const content = Array.from(document.querySelectorAll('.smt-new-editor .smt-content-center')).filter(visible)[0];
                const rightImgs = right ? Array.from(right.querySelectorAll('img')).filter(visible).map((img) => img.currentSrc || img.src).filter(Boolean) : [];
                const contentImgs = content ? Array.from(content.querySelectorAll('img')).filter(visible).map((img) => img.currentSrc || img.src).filter(Boolean) : [];
                return {ok:rightImgs.length > 0 || contentImgs.some((src) => src.startsWith('http')), right_images:rightImgs.length, content_images:contentImgs.length};
            }"""
        )
        return {"status": "ok" if verify.get("ok") else "manual_required", "method": menu_click.get("value"), "menu": menu_click, "chosen": chosen, "verify": verify}
    except Exception as exc:
        return {"status": "manual_required", "message": str(exc)}


def _save_product_description_editor(page: Any) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const modal = Array.from(document.querySelectorAll('.smt-new-editor, .ant-modal.smt-new-editor')).filter(visible).find((el) => textOf(el).includes('Temu产品描述'));
                if (!modal) return {ok:true, skipped:true, message:'description editor modal not open'};
                const item = Array.from(modal.querySelectorAll('.top-header button.btn-orange, .top-header .btn-orange, .title-right button.btn-orange')).filter(visible)[0]
                    || Array.from(modal.querySelectorAll('.top-header button, .top-header a, .top-header span, .ant-modal-header button, .ant-modal-header a, .ant-modal-header span, button, a, span')).filter(visible).find((el) => textOf(el) === '保存');
                if (!item) return {ok:false, message:'description editor save button not found'};
                const clickable = item.closest('button,a,[role="button"],.ant-btn') || item;
                clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
                clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
                clickable.click();
                return {ok:true, clicked:textOf(item), tag:clickable.tagName, className:String(clickable.className || '')};
            }"""
        )
        page.wait_for_timeout(2500)
        return result if isinstance(result, dict) else {"ok": False, "message": "unknown save result"}
    except Exception as exc:
        return {"ok": False, "message": str(exc)}


def _open_package_collect_image_modal(page: Any) -> dict[str, Any]:
    if page.locator('.ant-modal:has-text("引用采集图片")').count() > 0:
        return {"status": "ok", "already_open": True}

    try:
        button = page.locator("#packageInfo button").first
        button.scroll_into_view_if_needed(timeout=5000)
        button.click(timeout=5000)
        page.wait_for_timeout(500)
        clicked = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const dropdowns = Array.from(document.querySelectorAll('.ant-dropdown')).filter(visible);
                const items = dropdowns.flatMap((el) => Array.from(el.querySelectorAll('.ant-dropdown-menu-item')).filter(visible));
                let item = items.find((el) => textOf(el).includes('引用采集图片'));
                if (!item) item = items.find((el) => el.getAttribute('data-menu-id') === 'crawl');
                if (!item && items.length) item = items[items.length - 1];
                if (!item) return {status: 'manual_required', message: '引用采集图片菜单项未出现'};
                item.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                item.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                item.click();
                return {status: 'ok', value: textOf(item)};
            }"""
        )
        if clicked.get("status") != "ok":
            return clicked

        page.wait_for_timeout(1500)
        if page.locator('.ant-modal:has-text("引用采集图片")').count() == 0:
            return {"status": "manual_required", "message": "引用采集图片弹窗打开失败", "menu": clicked}
        return {"status": "ok", "menu": clicked}
    except Exception as exc:
        return {"status": "manual_required", "message": str(exc)}


def _read_title_value(page: Any) -> str:
    try:
        locator = page.locator('xpath=//*[contains(normalize-space(.), "产品标题") or contains(normalize-space(.), "商品标题") or contains(normalize-space(.), "标题")]/following::input[1]').first
        return locator.input_value(timeout=1500).strip()
    except Exception:
        return ""


def _read_sku_values(page: Any) -> list[dict[str, str]]:
    try:
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('input[name="variationSku"], textarea[name="variationSku"]'))
                .filter((el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled)
                .map((el) => ({value: el.value || '', selector_hint: el.tagName.toLowerCase() + '[name="variationSku"]'}))"""
        )
    except Exception:
        return []


def _has_selected_color(page: Any) -> bool:
    return bool(page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const colorLabels = ['白色', '黑色', '红色', '蓝色', '绿色', '透明', '其他色', '其他'];
            const knownColorChecked = Array.from(document.querySelectorAll('label')).filter(visible).some((label) => {
                const text = (label.innerText || label.textContent || '').trim();
                const input = label.querySelector('input[type="checkbox"]');
                return input && input.checked && colorLabels.some((item) => text.includes(item));
            });
            if (knownColorChecked) return true;

            const variantRoot = Array.from(document.querySelectorAll('.skuAttrModule, .form-card')).filter(visible).find((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                return text.includes('变种属性') && text.includes('颜色');
            });
            if (!variantRoot) return false;
            return Array.from(variantRoot.querySelectorAll('label')).filter(visible).some((label) => {
                const text = (label.innerText || label.textContent || '').trim();
                const input = label.querySelector('input[type="checkbox"]');
                return input && input.checked && text && !text.includes('尺码');
            });
        }"""
    ))


def _select_first_variant_color_checkbox(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const roots = Array.from(document.querySelectorAll('.skuAttrModule, .form-card')).filter(visible);
            const root = roots.find((el) => {
                const text = textOf(el);
                return text.includes('变种属性') && text.includes('颜色');
            });
            if (!root) return {status: 'manual_required', message: 'variant color section not found'};

            const labels = Array.from(root.querySelectorAll('label')).filter(visible).filter((label) => {
                const input = label.querySelector('input[type="checkbox"]');
                const text = textOf(label);
                return input && text && !text.includes('尺码') && !text.includes('模板');
            });
            const label = labels.find((item) => !item.querySelector('input[type="checkbox"]').checked) || labels[0];
            if (!label) return {status: 'manual_required', message: 'variant color checkbox not found'};

            const input = label.querySelector('input[type="checkbox"]');
            label.scrollIntoView({block: 'center', inline: 'nearest'});
            if (!input.checked) {
                label.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                label.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                label.click();
            }
            return {status: 'ok', value: textOf(label), method: 'variant_color_checkbox', checked: input.checked};
        }"""
    )


def _infer_color(text: str, defaults: dict[str, Any]) -> str:
    value = text.lower()
    mapping = [
        (("black", "黑色"), "黑色"),
        (("white", "白色"), "白色"),
        (("red", "红色"), "红色"),
        (("blue", "蓝色"), "蓝色"),
        (("green", "绿色"), "绿色"),
        (("transparent", "透明"), "透明"),
    ]
    for needles, color in mapping:
        if any(needle in value or needle in text for needle in needles):
            return color
    return defaults.get("default_color", "白色")


def _click_visible_text(page: Any, texts: list[str], exact: bool) -> bool:
    for text in texts:
        try:
            locator = page.get_by_text(text, exact=exact).last
            locator.wait_for(state="visible", timeout=1500)
            locator.click(timeout=3000)
            return True
        except Exception:
            continue
    return False


def _body_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _dedupe_errors(errors: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    unique = []
    for err in errors:
        key = (err.get("field", ""), err.get("message", ""), err.get("section", ""))
        if key in seen:
            continue
        seen.add(key)
        unique.append(err)
    return unique


def _extract_product_id(url: str) -> str:
    match = re.search(r"(?:id|productId|goodsId|itemId)=([A-Za-z0-9_-]+)", url)
    return match.group(1) if match else "DXM"


def _manual_result(page: Any, step: str, message: str, logger: Any | None, state: Any | None) -> dict[str, Any]:
    screenshot_path = take_screenshot(page, step)
    _log(logger, step, "manual_required", message, page=page, screenshot_path=screenshot_path)
    if state:
        state.update(status="manual_required", manual_step=step, screenshot_path=screenshot_path)
    return {"status": "manual_required", "manual_required": True, "step": step, "message": message, "screenshot_path": screenshot_path}


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
