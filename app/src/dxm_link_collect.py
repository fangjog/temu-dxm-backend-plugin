from __future__ import annotations

import os
import re
import sys
from typing import Any

from .utils import body_text, take_screenshot


LINK_COLLECT_URL_CANDIDATES = [
    os.getenv("DXM_LINK_COLLECT_URL", "").strip(),
    "https://www.dianxiaomi.com/web/productCrawl/dataAcquisition",
    "https://www.dianxiaomi.com/web/productCrawl/index",
    "https://www.dianxiaomi.com/web/productCrawl/collect",
]

COLLECT_BOX_URL_CANDIDATES = [
    os.getenv("DXM_COLLECT_BOX_URL", "").strip(),
    "https://www.dianxiaomi.com/web/temu/collectProduct/list",
    "https://www.dianxiaomi.com/web/temu/collectProduct",
    "https://www.dianxiaomi.com/web/temu/collect/list",
    "https://www.dianxiaomi.com/web/temu/collectBox",
]

SUCCESS_TOKENS = ["采集成功", "采集完成", "已采集", "进入采集箱", "前往采集箱"]
UNSUPPORTED_TOKENS = ["不支持", "无法识别", "采集数据为空", "需要页面打开后采集", "链接错误", "暂不支持", "解析失败"]


def open_dxm_link_collect(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "dxm_link_collect_open", "start", "Opening Dianxiaomi link collection page.", page=page)
    for url in [item for item in LINK_COLLECT_URL_CANDIDATES if item]:
        try:
            page.goto(url, wait_until="domcontentloaded")
            _wait_ready(page)
            _select_link_collect_mode(page)
            if _looks_like_link_collect(page):
                result = {"status": "ok", "url": page.url, "page": page}
                _log(logger, "dxm_link_collect_open", "ok", f"Link collect page ready: {page.url}", page=page)
                if state:
                    state.update(dxm_link_collect_open={k: v for k, v in result.items() if k != "page"})
                return result
        except Exception as exc:
            _log(logger, "dxm_link_collect_open", "warning", f"Candidate link collect URL failed: {url}; {exc}", page=page)

    screenshot_path = take_screenshot(page, "dxm_link_collect_open_missing")
    visible_text = body_text(page, timeout=2000)[:3000]
    message = "Could not locate Dianxiaomi link collection page. Please open it manually, then type continue."
    _log(logger, "dxm_link_collect_open", "manual_required", message, page=page, screenshot_path=screenshot_path, extra={"visible_text": visible_text})
    continued = _wait_for_continue(message)
    result = {"status": "manual_required", "continued": continued, "page": page, "url": page.url, "screenshot_path": screenshot_path, "visible_text": visible_text}
    if state:
        state.update(dxm_link_collect_open={k: v for k, v in result.items() if k != "page"})
    return result


def submit_product_link_collect(page: Any, product_url: str, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "dxm_link_collect_submit", "start", f"Submitting product link for collection: {product_url}", page=page)
    _close_result_dialog(page)
    _select_link_collect_mode(page)
    filled = _fill_link_input(page, product_url)
    if not filled:
        screenshot_path = take_screenshot(page, "dxm_link_collect_input_missing")
        result = {"status": "manual_required", "message": "Could not find link collection input.", "screenshot_path": screenshot_path, "url": page.url}
        _log(logger, "dxm_link_collect_submit", "manual_required", result["message"], page=page, screenshot_path=screenshot_path)
        if state:
            state.update(dxm_link_collect_submit=result)
        return result

    clicked = _click_any_text(page, ["开始采集", "立即采集", "采集", "确定", "提交"], timeout=2500, button_like=True)
    page.wait_for_timeout(2000)
    result = {"status": "ok" if clicked else "manual_required", "filled": True, "clicked": clicked, "product_url": product_url, "url": page.url}
    if not clicked:
        result["screenshot_path"] = take_screenshot(page, "dxm_link_collect_start_missing")
        _log(logger, "dxm_link_collect_submit", "manual_required", "Link was filled but start collect button was not found.", page=page, screenshot_path=result["screenshot_path"], extra=result)
    else:
        _log(logger, "dxm_link_collect_submit", "ok", "Product link submitted to Dianxiaomi link collection.", page=page, extra=result)
    if state:
        state.update(dxm_link_collect_submit=result)
    return result


def wait_link_collect_success(page: Any, logger: Any | None = None, state: Any | None = None, timeout_ms: int = 120000) -> dict[str, Any]:
    _log(logger, "dxm_link_collect_wait", "start", "Waiting for Dianxiaomi link collection result.", page=page)
    deadline = page.evaluate("Date.now()") + timeout_ms
    while page.evaluate("Date.now()") < deadline:
        collect_state = _read_collect_result_state(page)
        if collect_state.get("status") == "failure":
            screenshot_path = take_screenshot(page, "dxm_link_collect_failed")
            result = {"status": "unsupported_link_collect", "message": collect_state.get("message", "Dianxiaomi link collection failed."), "screenshot_path": screenshot_path, "url": page.url}
            _log(logger, "dxm_link_collect_wait", "warning", result["message"], page=page, screenshot_path=screenshot_path, extra={**result, **collect_state})
            if state:
                state.update(dxm_link_collect_wait=result)
            return result
        if collect_state.get("status") == "success":
            screenshot_path = take_screenshot(page, "full_link_collect_success")
            result = {"status": "ok", "screenshot_path": screenshot_path, "url": page.url, "collect_state": collect_state}
            _log(logger, "dxm_link_collect_wait", "ok", "Dianxiaomi link collection reported at least 1 successful item.", page=page, screenshot_path=screenshot_path, extra=result)
            if state:
                state.update(dxm_link_collect_wait=result)
            return result

        text = body_text(page, timeout=1000)
        failure = _parse_collect_failure(text)
        if failure:
            screenshot_path = take_screenshot(page, "dxm_link_collect_failed")
            result = {"status": "unsupported_link_collect", "message": failure, "screenshot_path": screenshot_path, "url": page.url}
            _log(logger, "dxm_link_collect_wait", "warning", failure, page=page, screenshot_path=screenshot_path, extra=result)
            if state:
                state.update(dxm_link_collect_wait=result)
            return result
        if any(token in text for token in SUCCESS_TOKENS):
            screenshot_path = take_screenshot(page, "full_link_collect_success")
            result = {"status": "ok", "screenshot_path": screenshot_path, "url": page.url}
            _log(logger, "dxm_link_collect_wait", "ok", "Dianxiaomi link collection succeeded or collect box prompt is visible.", page=page, screenshot_path=screenshot_path, extra=result)
            if state:
                state.update(dxm_link_collect_wait=result)
            return result
        if any(token in text for token in UNSUPPORTED_TOKENS):
            screenshot_path = take_screenshot(page, "dxm_link_collect_unsupported")
            result = {"status": "unsupported_link_collect", "message": "Dianxiaomi reports this link cannot be collected by link collection.", "screenshot_path": screenshot_path, "url": page.url}
            _log(logger, "dxm_link_collect_wait", "warning", result["message"], page=page, screenshot_path=screenshot_path, extra=result)
            if state:
                state.update(dxm_link_collect_wait=result)
            return result
        page.wait_for_timeout(1500)

    screenshot_path = take_screenshot(page, "dxm_link_collect_timeout")
    result = {"status": "timeout", "message": "Timed out waiting for Dianxiaomi link collection success.", "screenshot_path": screenshot_path, "url": page.url}
    _log(logger, "dxm_link_collect_wait", "warning", result["message"], page=page, screenshot_path=screenshot_path)
    if state:
        state.update(dxm_link_collect_wait=result)
    return result


def go_to_collect_box_after_link_collect(page: Any, logger: Any | None = None, state: Any | None = None) -> Any:
    _log(logger, "dxm_link_collect_box", "start", "Opening collect box after link collection.", page=page)
    _close_result_dialog(page)
    if _looks_like_collect_box(page):
        _click_any_text(page, ["未认领", "全部"], timeout=1000)
        result = {"status": "ok", "url": page.url, "method": "current_page"}
        _log(logger, "dxm_link_collect_box", "ok", f"Current link collect page already contains collect list: {page.url}", page=page, extra=result)
        if state:
            state.update(dxm_link_collect_box=result)
        return page

    context = page.context
    target_page = page
    try:
        with context.expect_page(timeout=5000) as popup_info:
            if not _click_any_text(page, ["前往采集箱", "进入采集箱", "采集箱"], timeout=2500):
                raise RuntimeError("collect box button not found")
        target_page = popup_info.value
        _wait_ready(target_page)
    except Exception:
        if _click_any_text(page, ["前往采集箱", "进入采集箱", "采集箱"], timeout=2500):
            page.wait_for_timeout(2500)
            target_page = page
        else:
            _open_collect_box_url(page, logger)
            target_page = page

    result = {"status": "ok", "url": target_page.url}
    _log(logger, "dxm_link_collect_box", "ok", f"Collect box page opened: {target_page.url}", page=target_page, extra=result)
    if state:
        state.update(dxm_link_collect_box=result)
    return target_page


def _select_link_collect_mode(page: Any) -> None:
    _click_any_text(page, ["链接采集", "单品采集", "链接认领", "URL采集", "Temu", "通用采集"], timeout=1200)


def _looks_like_link_collect(page: Any) -> bool:
    text = body_text(page, timeout=2000)
    url = page.url.lower()
    return ("productcrawl" in url or "collect" in url) and any(token in text for token in ["链接", "采集", "单品", "URL", "http"])


def _looks_like_collect_box(page: Any) -> bool:
    text = body_text(page, timeout=2000)
    return any(token in text for token in ["未认领", "已认领", "批量认领", "认领", "数据采集", "采集箱"]) and any(token in text for token in ["标题", "图片", "负责人", "创建时间", "采集"])


def _close_result_dialog(page: Any) -> None:
    _click_any_text(page, ["关闭", "确定", "取消"], timeout=800)


def _read_collect_result_state(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, '').trim();
                const dialogText = Array.from(document.querySelectorAll('.ant-modal, .el-dialog, .layui-layer, [role="dialog"]'))
                    .filter(visible)
                    .map(textOf)
                    .join('');
                const visibleText = Array.from(document.querySelectorAll('body *'))
                    .filter(visible)
                    .map(textOf)
                    .filter(Boolean)
                    .join('');
                const text = `${dialogText}${visibleText}`;
                const normalized = text.replace(/[，,。:：\\s]/g, '');
                const successMatch = text.match(/(?:成功采集|采集成功|成功)(\\d+)(?:条|个)?/);
                const failureMatch = text.match(/失败(\\d+)(?:条|个)?/);
                const successCount = successMatch ? Number(successMatch[1]) : null;
                const failureCount = failureMatch ? Number(failureMatch[1]) : null;
                if (/(?:已)?成功采集0(?:条|个)?/.test(normalized) && /失败[1-9]\\d*/.test(normalized)) {
                    return {status: 'failure', success_count: 0, failure_count: failureCount || 1, message: 'Dianxiaomi link collection completed with 0 success and failures.'};
                }
                if (successCount === 0 && failureCount && failureCount > 0) {
                    return {status: 'failure', success_count: successCount, failure_count: failureCount, message: 'Dianxiaomi link collection completed with 0 success and failures.'};
                }
                if ((successCount === null || successCount === 0) && failureCount && failureCount > 0) {
                    return {status: 'failure', success_count: successCount, failure_count: failureCount, message: 'Dianxiaomi link collection reported failures and no successful item.'};
                }
                if (successCount && successCount > 0) {
                    return {status: 'success', success_count: successCount, failure_count: failureCount || 0};
                }
                if (/不支持|无法识别|采集数据为空|需要页面打开后采集|链接错误|暂不支持|解析失败/.test(text)) {
                    return {status: 'failure', success_count: successCount, failure_count: failureCount, message: 'Dianxiaomi reports this link cannot be collected by link collection.'};
                }
                return {status: 'pending', success_count: successCount, failure_count: failureCount};
            }"""
        )
    except Exception as exc:
        return {"status": "pending", "message": str(exc)}


def _parse_collect_failure(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if re.search(r"(?:已)?成功采集0(?:条|个)?", compact) and re.search(r"失败[1-9]\d*", compact):
        return "Dianxiaomi link collection completed with 0 success and at least 1 failure."
    if re.search(r"(成功采集|采集成功|成功)0(?:条|个)?.{0,30}(失败|失败采集)[1-9]", compact):
        return "Dianxiaomi link collection completed with 0 success and at least 1 failure."
    if re.search(r"失败[1-9]\d*(?:条|个)?", compact) and not re.search(r"(成功采集|采集成功|成功)[1-9]\d*", compact):
        return "Dianxiaomi link collection reported failures and no successful collection."
    if any(token in text for token in UNSUPPORTED_TOKENS):
        return "Dianxiaomi reports this link cannot be collected by link collection."
    return ""


def _fill_link_input(page: Any, product_url: str) -> bool:
    selectors = [
        "textarea:visible",
        'input[placeholder*="链接"]:visible',
        'input[placeholder*="URL"]:visible',
        'input[placeholder*="http"]:visible',
        'input[type="text"]:visible',
        '[contenteditable="true"]:visible',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=2000)
            locator.scroll_into_view_if_needed(timeout=2000)
            if "contenteditable" in selector:
                locator.evaluate("(el, value) => { el.innerText = value; el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: value})); }", product_url)
            else:
                locator.fill(product_url, timeout=3000)
            return True
        except Exception:
            continue
    return False


def _open_collect_box_url(page: Any, logger: Any | None = None) -> bool:
    for url in [item for item in COLLECT_BOX_URL_CANDIDATES if item]:
        try:
            page.goto(url, wait_until="domcontentloaded")
            _wait_ready(page)
            text = body_text(page, timeout=2000)
            if "采集" in text or "认领" in text or "collect" in page.url.lower():
                _log(logger, "dxm_link_collect_box", "ok", f"Opened collect box candidate URL: {url}", page=page)
                return True
        except Exception:
            continue
    return False


def _click_any_text(page: Any, texts: list[str], timeout: int = 1200, button_like: bool = False) -> bool:
    selectors: list[str] = []
    if button_like:
        selectors.extend([f'button:has-text("{text}")' for text in texts])
        selectors.extend([f'a:has-text("{text}")' for text in texts])
    selectors.extend([f'text={text}' for text in texts])
    for selector in selectors:
        try:
            locator = page.locator(selector).first
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
    print(message)
    if not sys.stdin.isatty():
        print("Current terminal is noninteractive; returning manual_required.")
        return False
    while True:
        try:
            if input("> ").strip().lower() == "continue":
                return True
        except EOFError:
            return False
        print("Please type continue to proceed.")


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
