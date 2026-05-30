from __future__ import annotations

import os
import sys
from typing import Any

from .temu_front_pages import ensure_temu_region, is_temu_product_page_healthy
from .utils import body_text, take_screenshot
from .windows_prompt import wait_user_decision, UserChoseSkip, UserChoseStop


COLLECT_BOX_URL_CANDIDATES = [
    os.getenv("DXM_COLLECT_BOX_URL", "").strip(),
    "https://www.dianxiaomi.com/web/temu/collectProduct/list",
    "https://www.dianxiaomi.com/web/temu/collectProduct",
    "https://www.dianxiaomi.com/web/temu/collect/list",
    "https://www.dianxiaomi.com/web/temu/collectBox",
]

COLLECT_SUCCESS_TOKENS = ["采集成功", "成功", "前往采集箱", "采集箱", "查看已采集数据", "已采集"]
PLUGIN_CACHE_TOKENS = ["无法正常浏览商品", "建议清理缓存", "清理缓存后继续", "清除缓存", "清理缓存"]


def trigger_dxm_plugin_collect(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "dxm_plugin_collect", "start", "Triggering Dianxiaomi plugin collection.", page=page)

    warnings: list[str] = []
    for attempt in range(1, 4):
        if not is_temu_product_page_healthy(page):
            screenshot_path = take_screenshot(page, f"dxm_collect_unhealthy_product_attempt_{attempt}")
            warnings.append(f"attempt {attempt}: product page not healthy")
            _log(logger, "dxm_plugin_collect", "warning", "Temu product page is not healthy; refreshing before collection.", page=page, screenshot_path=screenshot_path, extra={"attempt": attempt})
            _refresh_and_recheck_product(page, logger)
            if not is_temu_product_page_healthy(page):
                continue

        clicked = _click_dxm_collect_bar(page)
        if not clicked:
            clicked = _click_any_text(page, ["开始采集", "采集商品", "采集到店小秘"], timeout=1800)
        page.wait_for_timeout(2500)

        text = body_text(page, timeout=1500)
        if any(token in text for token in COLLECT_SUCCESS_TOKENS):
            result = {"status": "ok", "clicked": clicked, "attempt": attempt, "url": page.url, "warnings": warnings}
            _log(logger, "dxm_plugin_collect", "ok", f"Dianxiaomi collection triggered, clicked={clicked}.", page=page, extra=result)
            if state:
                state.update(dxm_plugin_collect=result)
            return result

        if any(token in text for token in PLUGIN_CACHE_TOKENS):
            screenshot_path = take_screenshot(page, f"dxm_plugin_cache_warning_attempt_{attempt}")
            warnings.append(f"attempt {attempt}: plugin cache warning")
            _log(
                logger,
                "dxm_plugin_collect",
                "warning",
                "Dianxiaomi/Temu says product cannot be browsed normally. Only refreshing/region switching is attempted automatically.",
                page=page,
                screenshot_path=screenshot_path,
                extra={"attempt": attempt},
            )
            _refresh_and_recheck_product(page, logger)
            continue

        if clicked:
            result = {"status": "ok", "clicked": True, "attempt": attempt, "url": page.url, "warnings": warnings}
            _log(logger, "dxm_plugin_collect", "ok", "Dianxiaomi plugin collect button clicked; waiting for success state.", page=page, extra=result)
            if state:
                state.update(dxm_plugin_collect=result)
            return result

    screenshot_path = take_screenshot(page, "dxm_plugin_collect_manual")
    message = (
        "Dianxiaomi collection could not be confirmed. If the isolated browser shows a cache warning, "
        "please handle it manually inside the isolated browser, then type continue."
    )
    _log(logger, "dxm_plugin_collect", "manual_required", message, page=page, screenshot_path=screenshot_path, extra={"warnings": warnings})
    continued = _wait_for_continue(message)
    result = {
        "status": "manual_required",
        "manual_intervention": True,
        "continued": continued,
        "screenshot_path": screenshot_path,
        "url": page.url,
        "warnings": warnings,
    }
    if state:
        state.update(dxm_plugin_collect=result)
    return result


def wait_collect_success(page: Any, logger: Any | None = None, state: Any | None = None, timeout_ms: int = 60000) -> dict[str, Any]:
    _log(logger, "dxm_collect_success", "start", "Waiting for Dianxiaomi collection success.", page=page)
    deadline = page.evaluate("Date.now()") + timeout_ms
    while page.evaluate("Date.now()") < deadline:
        text = body_text(page, timeout=1000)
        if any(token in text for token in COLLECT_SUCCESS_TOKENS):
            screenshot_path = take_screenshot(page, "full_collect_success")
            result = {"status": "ok", "matched": True, "screenshot_path": screenshot_path, "url": page.url}
            _log(logger, "dxm_collect_success", "ok", "Collection success / collect box prompt detected.", page=page, screenshot_path=screenshot_path, extra=result)
            if state:
                state.update(dxm_collect_success=result)
            return result
        if any(token in text for token in PLUGIN_CACHE_TOKENS):
            screenshot_path = take_screenshot(page, "dxm_collect_cache_warning")
            message = "Dianxiaomi plugin reports Temu cannot browse the product normally."
            _log(logger, "dxm_collect_success", "manual_required", message, page=page, screenshot_path=screenshot_path)
            continued = _wait_for_continue("Please fix the plugin/cache warning in the isolated browser, then type continue.")
            result = {"status": "manual_required", "manual_intervention": True, "continued": continued, "screenshot_path": screenshot_path, "url": page.url}
            if state:
                state.update(dxm_collect_success=result)
            return result
        page.wait_for_timeout(1500)

    screenshot_path = take_screenshot(page, "dxm_collect_success_timeout")
    message = "Collection success was not detected automatically. Please confirm manually, then type continue."
    _log(logger, "dxm_collect_success", "manual_required", message, page=page, screenshot_path=screenshot_path)
    continued = _wait_for_continue(message)
    result = {"status": "manual_required", "manual_intervention": True, "continued": continued, "screenshot_path": screenshot_path, "url": page.url}
    if state:
        state.update(dxm_collect_success=result)
    return result


def click_go_to_collect_box(page: Any, logger: Any | None = None, state: Any | None = None) -> Any:
    _log(logger, "dxm_go_collect_box", "start", "Opening Dianxiaomi collect box.", page=page)
    context = page.context
    target_page = page
    try:
        with context.expect_page(timeout=5000) as popup_info:
            if not _click_any_text(page, ["前往采集箱", "采集箱"], timeout=1800):
                raise RuntimeError("collect box button not found")
        target_page = popup_info.value
        _wait_ready(target_page)
    except Exception:
        if _click_any_text(page, ["前往采集箱", "采集箱"], timeout=1800):
            page.wait_for_timeout(2500)
            target_page = page
        else:
            _try_open_collect_box_url(page, logger)
            target_page = page

    if not _looks_like_collect_box(target_page):
        screenshot_path = take_screenshot(target_page, "dxm_go_collect_box_manual")
        message = "Could not confirm Dianxiaomi collect box. Please open collect box manually, then type continue."
        _log(logger, "dxm_go_collect_box", "manual_required", message, page=target_page, screenshot_path=screenshot_path)
        _wait_for_continue(message)

    result = {"status": "ok", "url": target_page.url}
    _log(logger, "dxm_go_collect_box", "ok", f"Collect box page ready: {target_page.url}", page=target_page, extra=result)
    if state:
        state.update(dxm_go_collect_box=result)
    return target_page


def _refresh_and_recheck_product(page: Any, logger: Any | None = None) -> None:
    try:
        ensure_temu_region(page, logger=logger)
    except Exception:
        pass
    try:
        page.reload(wait_until="domcontentloaded")
        _wait_ready(page)
    except Exception:
        pass


def _try_open_collect_box_url(page: Any, logger: Any | None = None) -> bool:
    for url in [item for item in COLLECT_BOX_URL_CANDIDATES if item]:
        try:
            page.goto(url, wait_until="domcontentloaded")
            _wait_ready(page)
            if _looks_like_collect_box(page):
                _log(logger, "dxm_go_collect_box", "ok", f"Opened collect box candidate URL: {url}", page=page)
                return True
        except Exception:
            continue
    return False


def _click_dxm_collect_bar(page: Any) -> bool:
    try:
        return bool(page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const roots = Array.from(document.querySelectorAll('body *')).filter((el) => {
                    const text = textOf(el);
                    const rect = el.getBoundingClientRect();
                    return visible(el) && text.includes('采集到店小秘') && rect.y > window.innerHeight * 0.45;
                }).sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
                const root = roots[0];
                const scope = root || document.body;
                const candidates = Array.from(scope.querySelectorAll('button, a, span, div')).filter((el) => {
                    const text = textOf(el);
                    return visible(el) && (text === '开始采集' || text.includes('开始采集'));
                }).sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);
                const node = candidates[0];
                if (!node) return false;
                node.scrollIntoView({block: 'center', inline: 'nearest'});
                node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                node.click();
                return true;
            }"""
        ))
    except Exception:
        return False


def _looks_like_collect_box(page: Any) -> bool:
    text = body_text(page, timeout=2000)
    url = page.url.lower()
    return any(token in text for token in ["采集箱", "未认领", "认领", "采集商品"]) or "collect" in url


def _click_any_text(page: Any, texts: list[str], timeout: int = 1200) -> bool:
    for text in texts:
        for exact in (True, False):
            try:
                locator = page.get_by_text(text, exact=exact).first
                locator.wait_for(state="visible", timeout=timeout)
                locator.scroll_into_view_if_needed(timeout=timeout)
                locator.click(timeout=timeout)
                return True
            except Exception:
                continue
    return False


def _wait_ready(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _wait_for_continue(message: str) -> bool:
    decision = wait_user_decision(message, default_noninteractive="stop")
    if decision == "continue":
        return True
    if decision == "skip":
        raise UserChoseSkip(message)
    if decision == "stop":
        raise UserChoseStop(message)
    return False


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
