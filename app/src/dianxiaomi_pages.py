from __future__ import annotations

import os
import random
import re
from typing import Any

from .captcha_guard import check_and_wait_if_captcha
from .sku_cleaner import contains_chinese, sanitize_sku
from .text_ai import optimize_product_title
from .utils import ManualRequiredError, body_text, take_screenshot


EDIT_BUTTON_SELECTORS = [
    'a:has-text("编辑")',
    'button:has-text("编辑")',
    'span:has-text("编辑")',
    'text=编辑',
]

TITLE_INPUT_SELECTORS = [
    'input[name*="title" i]',
    'textarea[name*="title" i]',
    'input[name*="name" i]',
    'textarea[name*="name" i]',
    'input[placeholder*="标题"]',
    'textarea[placeholder*="标题"]',
    'input[placeholder*="商品名"]',
    'textarea[placeholder*="商品名"]',
    'xpath=//*[contains(normalize-space(.), "产品标题") or contains(normalize-space(.), "商品标题") or contains(normalize-space(.), "标题")]/following::input[1]',
    'xpath=//*[contains(normalize-space(.), "产品标题") or contains(normalize-space(.), "商品标题") or contains(normalize-space(.), "标题")]/following::textarea[1]',
]

SKU_INPUT_SELECTORS = [
    'input[name="variationSku"]',
    'textarea[name="variationSku"]',
    'xpath=//*[contains(normalize-space(.), "SKU货号")]/following::input[1]',
    'xpath=//*[contains(normalize-space(.), "SKU") and contains(normalize-space(.), "货号")]/following::input[1]',
    'xpath=//*[contains(normalize-space(.), "货号")]/following::input[1]',
]

SAVE_BUTTON_SELECTORS = [
    'button:has-text("保存草稿")',
    'a:has-text("保存草稿")',
    'text=保存草稿',
    'button:has-text("保存")',
    'a:has-text("保存")',
]

PUBLISH_KEYWORDS = ["发布", "上架", "提交审核", "立即刊登"]


def open_draft_list(page: Any, config: dict[str, Any] | None = None, logger: Any | None = None, state: Any | None = None) -> Any:
    url = os.getenv("DXM_DRAFT_URL", "https://www.dianxiaomi.com/w-temu/choiceTemuList/draft").strip()
    _log(logger, "open_draft_list", "start", f"打开店小秘 Temu 草稿列表: {url}", page=page)
    page.goto(url, wait_until="domcontentloaded")
    _wait_page_ready(page)
    check_and_wait_if_captcha(page, logger=logger)
    _wait_for_manual_login_if_needed(page, logger, "open_draft_list")
    if state:
        state.update(last_draft_url=page.url)
    _log(logger, "open_draft_list", "ok", "店小秘草稿列表已打开。", page=page)
    return page


def open_first_draft_edit(page: Any, logger: Any | None = None, state: Any | None = None) -> Any:
    check_and_wait_if_captcha(page, logger=logger)
    button, selector = _first_visible(page, EDIT_BUTTON_SELECTORS, timeout=5000)
    if not button:
        _manual_required("open_first_draft_edit", "找不到第一条草稿商品的“编辑”按钮，需要补充 selector。", page, logger, state)

    context = page.context
    before_pages = list(context.pages)
    _log(logger, "open_first_draft_edit", "start", f"点击第一条草稿商品编辑按钮，selector={selector}", page=page)
    button.click()
    page.wait_for_timeout(2000)
    after_pages = list(context.pages)
    new_pages = [p for p in after_pages if p not in before_pages]
    edit_page = new_pages[-1] if new_pages else page
    _wait_page_ready(edit_page)
    check_and_wait_if_captcha(edit_page, logger=logger)
    _wait_for_manual_login_if_needed(edit_page, logger, "open_first_draft_edit")
    if state:
        state.update(last_edit_url=edit_page.url)
    _log(logger, "open_first_draft_edit", "ok", f"已进入商品编辑页: {edit_page.url}", page=edit_page)
    return edit_page


def read_original_title(page: Any, logger: Any | None = None, state: Any | None = None) -> str:
    locator, selector = _first_visible(page, TITLE_INPUT_SELECTORS, timeout=3000)
    if not locator:
        _manual_required("read_original_title", "找不到产品标题输入框，需要补充 selector。", page, logger, state)
    title = _input_value(locator).strip()
    if not title:
        _manual_required("read_original_title", f"标题输入框为空或无法读取，selector={selector}", page, logger, state)
    _log(logger, "read_original_title", "ok", f"读取原标题成功，长度={len(title)}，selector={selector}", page=page)
    return title


def fill_product_title(page: Any, new_title: str, logger: Any | None = None, state: Any | None = None) -> bool:
    if not new_title or contains_chinese(new_title):
        _manual_required("fill_product_title", f"AI 标题为空或仍包含中文: {new_title}", page, logger, state)
    locator, selector = _first_visible(page, TITLE_INPUT_SELECTORS, timeout=3000)
    if not locator:
        _manual_required("fill_product_title", "找不到产品标题输入框，无法填写新标题。", page, logger, state)
    _fill(locator, new_title)
    page.wait_for_timeout(300)
    current = _input_value(locator)
    if contains_chinese(current):
        _manual_required("fill_product_title", f"标题填入后仍包含中文: {current}", page, logger, state)
    _log(logger, "fill_product_title", "ok", f"已填写英文标题，长度={len(current)}，selector={selector}", page=page)
    if state:
        state.update(last_title=new_title)
    return True


def select_origin_country_and_province(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, str]:
    defaults = config.get("product_defaults", {})
    country = defaults.get("origin_country", "中国大陆")
    provinces = list(defaults.get("origin_provinces", ["广东省", "浙江省", "江苏省", "福建省"]))
    random.shuffle(provinces)

    country_ok = _select_ant_select_option(page, ["请选择国家", "请选择国家/地区", "中国大陆"], [country], logger, "select_origin_country")
    if not country_ok:
        country_ok = _select_first_available(page, ["产地", "国家", "地区"], [country], logger, "select_origin_country")

    province_ok, province = _select_ant_select_first_available(page, ["请选择省份", "省份"], provinces, logger, "select_origin_province")
    if not province_ok:
        province_ok, province = _select_first_available_with_value(page, ["产地", "省份", "省", "地区"], provinces, logger, "select_origin_province")

    if country_ok and province_ok:
        _log(logger, "select_origin_country_and_province", "ok", f"产地已选择: {country} / {province}", page=page)
        if state:
            state.update(origin_country=country, origin_province=province)
        return {"status": "ok", "country": country, "province": province}

    screenshot_path = take_screenshot(page, "select_origin_country_and_province")
    message = f"产地或省份下拉框定位失败，需要人工处理。country_ok={country_ok}, province_ok={province_ok}"
    _log(logger, "select_origin_country_and_province", "manual_required", message, page=page, screenshot_path=screenshot_path)
    if state:
        state.update(status="manual_required", manual_step="select_origin_country_and_province", screenshot_path=screenshot_path)
    return {"status": "manual_required", "country": country, "province": province or ""}


def process_sku_fields(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    sku_config = config.get("sku", {})
    product_id = _extract_product_id(page.url)
    fields = _find_sku_field_locators(page)
    if not fields:
        screenshot_path = take_screenshot(page, "process_sku_fields")
        input_debug = _collect_input_debug(page)
        _log(
            logger,
            "process_sku_fields",
            "manual_required",
            "找不到 SKU 货号输入框，需要补充 selector。",
            page=page,
            screenshot_path=screenshot_path,
            extra={"input_debug": input_debug},
        )
        if input_debug:
            print("当前页面 input/textarea 调试信息:")
            for item in input_debug[:25]:
                print(item)
        if state:
            state.update(status="manual_required", manual_step="process_sku_fields", screenshot_path=screenshot_path, input_debug=input_debug)
        return {"status": "manual_required", "processed": 0, "items": []}

    items: list[dict[str, str]] = []
    manual_required = False
    processed = 0
    for field, selector in fields:
        try:
            old_value = _input_value(field)
            new_value = sanitize_sku(old_value, product_id=product_id, index=processed + 1, config=sku_config)
            _fill(field, new_value)
            page.wait_for_timeout(100)
            current = _input_value(field)
            if contains_chinese(current):
                manual_required = True
                items.append({"index": str(processed + 1), "old": old_value, "new": current, "status": "manual_required"})
            else:
                items.append({"index": str(processed + 1), "old": old_value, "new": current, "status": "ok"})
            processed += 1
        except Exception as exc:
            manual_required = True
            items.append({"index": str(processed + 1), "old": "", "new": "", "status": f"error: {exc}"})

    if processed == 0:
        screenshot_path = take_screenshot(page, "process_sku_fields")
        _log(logger, "process_sku_fields", "manual_required", "SKU selector 命中了元素，但没有可见输入框。", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="manual_required", manual_step="process_sku_fields", screenshot_path=screenshot_path)
        return {"status": "manual_required", "processed": 0, "items": items}

    if manual_required:
        screenshot_path = take_screenshot(page, "process_sku_fields_manual")
        _log(logger, "process_sku_fields", "manual_required", "部分 SKU 清洗后仍需人工处理，不会提交或发布。", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="manual_required", manual_step="process_sku_fields", sku_items=items, screenshot_path=screenshot_path)
        return {"status": "manual_required", "processed": processed, "items": items}

    _log(logger, "process_sku_fields", "ok", f"SKU 处理完成，共 {processed} 个输入框。", page=page, extra={"sku_items": items})
    if state:
        state.update(sku_items=items)
    return {"status": "ok", "processed": processed, "items": items}


def _find_sku_field_locators(page: Any) -> list[tuple[Any, str]]:
    for selector in SKU_INPUT_SELECTORS:
        fields: list[tuple[Any, str]] = []
        try:
            locator = page.locator(selector)
            count = min(locator.count(), 20)
            for index in range(count):
                field = locator.nth(index)
                if field.is_visible() and field.is_enabled():
                    fields.append((field, selector))
        except Exception:
            continue
        if fields:
            return fields
    return []


def save_draft(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, str]:
    button = _find_safe_save_button(page)
    if not button:
        screenshot_path = take_screenshot(page, "save_draft_missing")
        message = "没有找到安全的“保存草稿”按钮。已完成字段处理，等待人工保存；不会点击发布。"
        _log(logger, "save_draft", "manual_required", message, page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="manual_required", manual_step="save_draft", screenshot_path=screenshot_path)
        return {"status": "manual_required", "message": message}

    button_text = _text(button)
    _log(logger, "save_draft", "start", f"点击保存草稿按钮: {button_text}", page=page)
    button.click()
    page.wait_for_timeout(1500)
    check_and_wait_if_captcha(page, logger=logger)
    _log(logger, "save_draft", "ok", "已点击保存草稿按钮。", page=page)
    if state:
        state.update(status="saved_draft", last_saved_url=page.url)
    return {"status": "ok", "message": "saved_draft"}


def run_edit_one_product(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "running", "manual_required": False}
    try:
        open_draft_list(page, config=config, logger=logger, state=state)
        edit_page = open_first_draft_edit(page, logger=logger, state=state)
        check_and_wait_if_captcha(edit_page, logger=logger)

        original_title = read_original_title(edit_page, logger=logger, state=state)
        new_title = optimize_product_title(original_title)
        fill_product_title(edit_page, new_title, logger=logger, state=state)

        origin_result = select_origin_country_and_province(edit_page, config, logger=logger, state=state)
        sku_result = process_sku_fields(edit_page, config, logger=logger, state=state)
        result.update({"title": new_title, "origin": origin_result, "sku": sku_result, "url": edit_page.url})

        if origin_result.get("status") != "ok" or sku_result.get("status") != "ok":
            result["status"] = "manual_required"
            result["manual_required"] = True
            _log(logger, "run_edit_one_product", "manual_required", "字段处理存在 manual_required，本次不会自动保存或发布。", page=edit_page)
            if state:
                state.update(status="manual_required", last_edit_url=edit_page.url)
            return result

        submit_mode = os.getenv("DEFAULT_SUBMIT_MODE", config.get("product_defaults", {}).get("submit_mode", "draft"))
        if submit_mode != "draft":
            result["status"] = "manual_required"
            result["manual_required"] = True
            _log(logger, "run_edit_one_product", "manual_required", f"DEFAULT_SUBMIT_MODE={submit_mode}，MVP 只允许 draft，等待人工保存。", page=edit_page)
            return result

        save_result = save_draft(edit_page, logger=logger, state=state)
        result["save"] = save_result
        result["status"] = "saved_draft" if save_result.get("status") == "ok" else "manual_required"
        result["manual_required"] = save_result.get("status") != "ok"
        _log(logger, "run_edit_one_product", result["status"], "单个商品编辑流程完成。", page=edit_page)
        return result
    except ManualRequiredError as exc:
        result.update({"status": "manual_required", "manual_required": True, "step": exc.step, "message": exc.message, "screenshot_path": exc.screenshot_path})
        _log(logger, exc.step, "manual_required", exc.message, page=page, screenshot_path=exc.screenshot_path)
        if state:
            state.update(status="manual_required", manual_step=exc.step, screenshot_path=exc.screenshot_path)
        return result
    except Exception as exc:
        screenshot_path = ""
        try:
            screenshot_path = take_screenshot(page, "run_edit_one_product_error")
        except Exception:
            pass
        result.update({"status": "error", "manual_required": True, "message": str(exc), "screenshot_path": screenshot_path})
        _log(logger, "run_edit_one_product", "error", f"流程异常: {exc}", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="error", error=str(exc), screenshot_path=screenshot_path)
        return result


def _first_visible(page: Any, selectors: list[str], timeout: int = 2000) -> tuple[Any | None, str]:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout)
            return locator, selector
        except Exception:
            continue
    return None, ""


def _find_safe_save_button(page: Any) -> Any | None:
    for selector in SAVE_BUTTON_SELECTORS:
        try:
            locator = page.locator(selector)
            count = locator.count()
            for index in range(count):
                button = locator.nth(index)
                if not button.is_visible():
                    continue
                text = _text(button)
                if "保存" in text and not any(keyword in text for keyword in PUBLISH_KEYWORDS):
                    return button
        except Exception:
            continue
    return None


def _select_first_available_with_value(page: Any, label_keywords: list[str], candidates: list[str], logger: Any | None, step: str) -> tuple[bool, str]:
    for candidate in candidates:
        if _select_value(page, label_keywords, candidate, logger, step):
            return True, candidate
    return False, ""


def _select_first_available(page: Any, label_keywords: list[str], candidates: list[str], logger: Any | None, step: str) -> bool:
    return _select_first_available_with_value(page, label_keywords, candidates, logger, step)[0]


def _select_value(page: Any, label_keywords: list[str], value: str, logger: Any | None, step: str) -> bool:
    if _select_native_option(page, label_keywords, value):
        _log(logger, step, "ok", f"通过原生 select 选择: {value}", page=page)
        return True
    if _select_custom_option(page, label_keywords, value):
        _log(logger, step, "ok", f"通过自定义下拉选择: {value}", page=page)
        return True
    return False


def _select_ant_select_first_available(page: Any, control_texts: list[str], values: list[str], logger: Any | None, step: str) -> tuple[bool, str]:
    for value in values:
        if _select_ant_select_option(page, control_texts, [value], logger, step):
            return True, value
    return False, ""


def _select_ant_select_option(page: Any, control_texts: list[str], values: list[str], logger: Any | None, step: str) -> bool:
    for value in values:
        already_selected = page.evaluate(
            """(value) => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return Array.from(document.querySelectorAll('.ant-select'))
                    .filter(visible)
                    .some((el) => (el.innerText || el.textContent || '').trim().includes(value));
            }""",
            value,
        )
        if already_selected:
            _log(logger, step, "ok", f"AntD 下拉已是目标值: {value}", page=page)
            return True

    control_found = page.evaluate(
        """(controlTexts) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const controls = Array.from(document.querySelectorAll('.ant-select')).filter(visible);
            const control = controls.find((el) => {
                const text = (el.innerText || el.textContent || '').trim();
                return controlTexts.some((needle) => text.includes(needle));
            });
            if (!control) return false;
            control.scrollIntoView({block: 'center', inline: 'nearest'});
            const selector = control.querySelector('.ant-select-selector') || control;
            selector.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            selector.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            selector.click();
            return true;
        }""",
        control_texts,
    )
    if not control_found:
        return False

    page.wait_for_timeout(600)
    for value in values:
        selected = page.evaluate(
            """(value) => {
                const visibleOptions = Array.from(document.querySelectorAll(
                    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content'
                ));
                const option = visibleOptions.find((el) => (el.innerText || el.textContent || '').trim() === value);
                if (!option) return false;
                option.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                option.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                option.click();
                return true;
            }""",
            value,
        )
        if selected:
            page.wait_for_timeout(500)
            _log(logger, step, "ok", f"通过 AntD 下拉选择: {value}", page=page)
            return True
    return False


def _select_native_option(page: Any, label_keywords: list[str], value: str) -> bool:
    selectors: list[str] = []
    for keyword in label_keywords:
        selectors.extend(
            [
                f'xpath=//*[contains(normalize-space(.), "{keyword}")]/following::select[1]',
                f'select[name*="{keyword}"]',
                f'select[id*="{keyword}"]',
            ]
        )
    selectors.append("select")
    for selector in selectors:
        try:
            selects = page.locator(selector)
            for index in range(min(selects.count(), 20)):
                select = selects.nth(index)
                if not select.is_visible():
                    continue
                option_text = select.inner_text(timeout=1000)
                if value in option_text:
                    select.select_option(label=value, timeout=1500)
                    return True
        except Exception:
            continue
    return False


def _select_custom_option(page: Any, label_keywords: list[str], value: str) -> bool:
    control_selectors: list[str] = []
    for keyword in label_keywords:
        control_selectors.extend(
            [
                f'input[placeholder*="{keyword}"]',
                f'xpath=//*[contains(normalize-space(.), "{keyword}")]/following::*[self::input or self::div[contains(@class, "select")] or self::span][1]',
            ]
        )
    for selector in control_selectors:
        try:
            control = page.locator(selector).first
            control.wait_for(state="visible", timeout=1200)
            control.click(timeout=1500)
            page.wait_for_timeout(300)
            option = page.get_by_text(value, exact=True).last
            option.wait_for(state="visible", timeout=2000)
            option.click(timeout=1500)
            return True
        except Exception:
            continue
    return False


def _wait_for_manual_login_if_needed(page: Any, logger: Any | None, step: str) -> None:
    while _looks_like_login_page(page):
        screenshot_path = take_screenshot(page, f"{step}_login_required")
        message = "检测到登录态可能失效，请在浏览器中人工登录，完成后回到终端输入 continue。"
        _log(logger, step, "manual_required", message, page=page, screenshot_path=screenshot_path)
        print(message)
        while input("> ").strip().lower() != "continue":
            print("请输入 continue 继续检测。")
        page.reload(wait_until="domcontentloaded")
        _wait_page_ready(page)


def _looks_like_login_page(page: Any) -> bool:
    url = page.url.lower()
    if "login" in url or "signin" in url:
        return True
    text = body_text(page).lower()
    login_markers = ["登录店小秘", "账号登录", "扫码登录", "请输入密码", "sign in", "login"]
    return any(marker.lower() in text for marker in login_markers)


def _input_value(locator: Any) -> str:
    try:
        return locator.input_value(timeout=1500)
    except Exception:
        try:
            return locator.evaluate("(el) => el.value || el.textContent || ''")
        except Exception:
            return ""


def _fill(locator: Any, value: str) -> None:
    try:
        locator.fill(value, timeout=3000)
    except Exception:
        locator.click(timeout=3000)
        locator.press("Control+A", timeout=3000)
        locator.type(value, timeout=3000)


def _text(locator: Any) -> str:
    try:
        return locator.inner_text(timeout=1000).strip()
    except Exception:
        try:
            return locator.text_content(timeout=1000).strip()
        except Exception:
            return ""


def _wait_page_ready(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _extract_product_id(url: str) -> str:
    match = re.search(r"(?:id|productId|goodsId|itemId)=([A-Za-z0-9_-]+)", url)
    if match:
        return match.group(1)
    return "DXM"


def _manual_required(step: str, message: str, page: Any, logger: Any | None, state: Any | None) -> None:
    screenshot_path = take_screenshot(page, step)
    input_debug = _collect_input_debug(page)
    _log(logger, step, "manual_required", message, page=page, screenshot_path=screenshot_path, extra={"input_debug": input_debug})
    if input_debug:
        print("当前页面 input/textarea 调试信息:")
        for item in input_debug[:25]:
            print(item)
    if state:
        state.update(status="manual_required", manual_step=step, screenshot_path=screenshot_path, input_debug=input_debug)
    raise ManualRequiredError(step, message, screenshot_path)


def _collect_input_debug(page: Any) -> list[dict[str, Any]]:
    try:
        return page.evaluate(
            """() => Array.from(document.querySelectorAll('input, textarea')).slice(0, 120).map((el, index) => ({
                index,
                tag: el.tagName,
                type: el.getAttribute('type') || '',
                name: el.getAttribute('name') || '',
                id: el.id || '',
                placeholder: el.getAttribute('placeholder') || '',
                value: (el.value || '').slice(0, 120),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            }))"""
        )
    except Exception:
        return []


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
