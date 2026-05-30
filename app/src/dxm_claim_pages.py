from __future__ import annotations

import os
import sys
from typing import Any

from .dianxiaomi_pages import open_draft_list, open_first_draft_edit
from .utils import body_text, take_screenshot
from .windows_prompt import show_manual_action_popup, wait_user_decision, UserChoseSkip, UserChoseStop


COLLECT_BOX_URL_CANDIDATES = [
    os.getenv("DXM_COLLECT_BOX_URL", "").strip(),
    "https://www.dianxiaomi.com/web/productCrawl/dataAcquisition",
]


def open_collect_box_or_use_current(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    if _looks_like_collect_box(page):
        result = {"status": "ok", "page": page, "url": page.url}
        _log(logger, "dxm_collect_box", "ok", "当前页面已是采集箱/未认领页面。", page=page)
        return result

    for url in [item for item in COLLECT_BOX_URL_CANDIDATES if item]:
        try:
            page.goto(url, wait_until="domcontentloaded")
            _wait_ready(page)
            if _looks_like_collect_box(page):
                result = {"status": "ok", "page": page, "url": page.url}
                _log(logger, "dxm_collect_box", "ok", f"已打开采集箱候选 URL: {url}", page=page)
                if state:
                    state.update(dxm_collect_box={k: v for k, v in result.items() if k != "page"})
                return result
        except Exception:
            continue

    screenshot_path = take_screenshot(page, "dxm_collect_box_manual")
    message = "无法自动打开店小秘采集箱，请人工打开采集箱/未认领页面后输入 continue。"
    _log(logger, "dxm_collect_box", "manual_required", message, page=page, screenshot_path=screenshot_path)
    continued = _wait_for_continue(message)
    result = {
        "status": "manual_required",
        "manual_intervention": True,
        "continued": continued,
        "page": page,
        "url": page.url,
        "screenshot_path": screenshot_path,
    }
    if state:
        state.update(dxm_collect_box={k: v for k, v in result.items() if k != "page"})
    return result


def find_recent_collected_product(
    page: Any,
    temu_product_url: str | None = None,
    logger: Any | None = None,
    state: Any | None = None,
    product_context: dict[str, Any] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    screenshot_path = take_screenshot(page, "dxm_recent_collected_before")
    selected = page.evaluate(
        """(ctx) => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const normalize = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9\\u4e00-\\u9fff]+/g, ' ').trim();
            const goodsId = String(ctx.goods_id || '').trim();
            const title = normalize(ctx.title || ctx.temu_title || ctx.yunqi_title || '');
            const titleTokens = title.split(/\\s+/).filter((item) => item.length >= 4).slice(0, 8);
            const scoreRow = (row) => {
                const text = textOf(row);
                const normalized = normalize(text);
                let score = 0;
                if (goodsId && normalized.includes(goodsId)) score += 100;
                for (const token of titleTokens) {
                    if (normalized.includes(token)) score += 12;
                }
                if (/Temu/i.test(text)) score += 5;
                if (/认领/.test(text)) score += 5;
                return score;
            };

            const unclaimed = Array.from(document.querySelectorAll('a, button, span, div'))
                .filter(visible)
                .find((el) => textOf(el) === '未认领' || /^未认领\\(\\d+\\)$/.test(textOf(el)));
            if (unclaimed) {
                try { unclaimed.click(); } catch (e) {}
            }

            const rowSelectors = [
                '.el-table__body-wrapper tbody tr',
                '.ant-table-tbody tr',
                'table tbody tr'
            ];
            let rows = [];
            for (const selector of rowSelectors) {
                rows = rows.concat(Array.from(document.querySelectorAll(selector)).filter(visible));
            }
            const seen = new Set();
            rows = rows.filter((row) => {
                if (seen.has(row)) return false;
                seen.add(row);
                const text = textOf(row);
                if (!text || text.length < 20) return false;
                if (/数据采集 采集箱|通用服务|首页 产品 订单/.test(text)) return false;
                return /认领|编辑|Temu|USD|\\d{4}-\\d{2}-\\d{2}/i.test(text);
            });

            const scored = rows.map((row) => ({row, score: scoreRow(row), text: textOf(row)}))
                .sort((a, b) => b.score - a.score);
            const rowItem = scored.find((item) => item.score > 0) || (!ctx.strict ? scored.find((item) => /认领/.test(item.text)) || scored[0] : null);
            const row = rowItem ? rowItem.row : null;
            if (!row) return {status: 'manual_required', message: 'no product row'};
            if (ctx.strict && (!rowItem || rowItem.score <= 0)) {
                return {status: 'manual_required', message: 'current collection product could not be confirmed', candidates: scored.slice(0, 5).map((item) => ({score: item.score, text: item.text.slice(0, 220)}))};
            }

            row.scrollIntoView({block: 'center', inline: 'nearest'});
            const checkbox = row.querySelector('input[type="checkbox"]');
            if (checkbox && !checkbox.checked) {
                const target = checkbox.closest('label') || checkbox;
                target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                target.click();
            }

            return {
                status: 'ok',
                text: textOf(row).slice(0, 500),
                checked: !!checkbox && checkbox.checked,
                match_score: rowItem ? rowItem.score : 0,
                strict: !!ctx.strict
            };
        }""",
        {
            "strict": strict,
            "goods_id": (product_context or {}).get("goods_id", "") if product_context else "",
            "title": (product_context or {}).get("title", "") if product_context else "",
            "temu_title": (product_context or {}).get("temu_title", "") if product_context else "",
            "yunqi_title": (product_context or {}).get("yunqi_title", "") if product_context else "",
        },
    )
    status = "ok" if selected.get("status") == "ok" else "manual_required"
    if status != "ok" and strict:
        show_manual_action_popup(
            "采集箱商品确认",
            "无法确认采集箱商品是否为本轮商品，请人工确认第一条是否正确。确认后回到终端输入 continue。",
            logger=logger,
        )
    _log(
        logger,
        "dxm_find_recent_collected",
        status,
        f"采集箱最近商品定位结果: {selected}",
        page=page,
        screenshot_path=screenshot_path,
        extra={"temu_product_url": temu_product_url or ""},
    )
    result = {"status": status, "selected": selected, "screenshot_path": screenshot_path, "temu_product_url": temu_product_url or ""}
    if state:
        state.update(dxm_find_recent_collected=result)
    return result


def claim_to_temu_store(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "dxm_claim", "start", "开始认领到 Temu 店铺。", page=page)
    if not _click_claim_button(page):
        screenshot_path = take_screenshot(page, "dxm_claim_button_missing")
        message = "未找到认领按钮，无法继续发布。"
        _log(logger, "dxm_claim", "manual_required", message, page=page, screenshot_path=screenshot_path)
        result = {"status": "manual_required", "message": message, "screenshot_path": screenshot_path}
        if state:
            state.update(dxm_claim=result)
        return result

    page.wait_for_timeout(1200)
    store_selected = _select_temu_store(page)
    confirmed = _click_confirm_in_dialog(page)
    page.wait_for_timeout(1800)
    status = "ok" if confirmed else "manual_required"
    screenshot_path = "" if status == "ok" else take_screenshot(page, "dxm_claim_confirm_missing")
    result = {"status": status, "store_selected": store_selected, "confirmed": confirmed, "screenshot_path": screenshot_path}
    _log(logger, "dxm_claim", status, f"认领弹窗处理结果: {result}", page=page, screenshot_path=screenshot_path, extra=result)
    if state:
        state.update(dxm_claim=result)
    return result


def wait_claim_finished(page: Any, logger: Any | None = None, state: Any | None = None, timeout_ms: int = 90000) -> dict[str, Any]:
    _log(logger, "dxm_claim_wait", "start", "等待认领完成。", page=page)
    tokens = ["认领成功", "完成", "100%", "Temu采集箱", "认领完成", "草稿", "编辑"]
    deadline = page.evaluate("Date.now()") + timeout_ms
    while page.evaluate("Date.now()") < deadline:
        text = body_text(page, timeout=1000)
        if any(token in text for token in tokens):
            screenshot_path = take_screenshot(page, "full_claim_result")
            result = {"status": "ok", "matched": True, "screenshot_path": screenshot_path, "url": page.url}
            _log(logger, "dxm_claim_wait", "ok", "认领完成状态已检测到。", page=page, screenshot_path=screenshot_path, extra=result)
            if state:
                state.update(dxm_claim_wait=result)
            return result
        page.wait_for_timeout(2000)

    screenshot_path = take_screenshot(page, "full_claim_result")
    message = "未自动确认认领完成，请人工确认认领完成后输入 continue。"
    _log(logger, "dxm_claim_wait", "manual_required", message, page=page, screenshot_path=screenshot_path)
    continued = _wait_for_continue(message)
    result = {"status": "manual_required", "manual_intervention": True, "continued": continued, "screenshot_path": screenshot_path, "url": page.url}
    if state:
        state.update(dxm_claim_wait=result)
    return result


def open_claimed_edit_page(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> Any:
    _log(logger, "dxm_open_edit", "start", "尝试打开刚认领商品的编辑页。", page=page)
    edit_page = _click_edit_on_current_page(page)
    if not edit_page:
        open_draft_list(page, config=config, logger=logger, state=state)
        edit_page = open_first_draft_edit(page, logger=logger, state=state)
    screenshot_path = take_screenshot(edit_page, "full_edit_page")
    _log(logger, "dxm_open_edit", "ok", f"已进入编辑页: {edit_page.url}", page=edit_page, screenshot_path=screenshot_path)
    if state:
        state.update(dxm_open_edit={"status": "ok", "url": edit_page.url, "screenshot_path": screenshot_path})
    return edit_page


def _click_edit_on_current_page(page: Any) -> Any | None:
    context = page.context
    try:
        with context.expect_page(timeout=5000) as popup_info:
            if not _click_any_text(page, ["编辑"], timeout=1500):
                return None
        new_page = popup_info.value
        _wait_ready(new_page)
        return new_page
    except Exception:
        if _click_any_text(page, ["编辑"], timeout=1500):
            page.wait_for_timeout(2500)
            return page
    return None


def _click_claim_button(page: Any) -> bool:
    return bool(page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const clickNode = (node) => {
                node.scrollIntoView({block: 'center', inline: 'nearest'});
                node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                node.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                node.click();
                return true;
            };

            const rows = Array.from(document.querySelectorAll('.el-table__body-wrapper tbody tr, .ant-table-tbody tr, table tbody tr')).filter(visible);
            for (const row of rows) {
                const actions = Array.from(row.querySelectorAll('button, a, span')).filter(visible);
                const action = actions.find((el) => textOf(el) === '认领');
                if (action) return clickNode(action);
            }

            const buttons = Array.from(document.querySelectorAll('button, a, span')).filter(visible);
            const batch = buttons.find((el) => ['批量认领', '认领'].includes(textOf(el)));
            if (batch) return clickNode(batch);
            return false;
        }"""
    ))


def _select_temu_store(page: Any) -> dict[str, Any]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const dialogs = Array.from(document.querySelectorAll('.ant-modal, .el-dialog, .layui-layer, [role="dialog"]')).filter(visible);
                const root = dialogs[dialogs.length - 1] || document.body;

                const inputs = Array.from(root.querySelectorAll('input[type="checkbox"], input[type="radio"]'))
                    .filter((input) => visible(input) && !input.disabled);
                const storeInputs = inputs.filter((input) => {
                    const box = input.closest('label, tr, li, .el-checkbox, .ant-checkbox-wrapper, div') || input;
                    const text = textOf(box);
                    if (/已认领至相同店铺/.test(text)) return false;
                    return /Temu|全托管|Kyiki|店铺/i.test(text) || inputs.length === 1;
                });

                const input = storeInputs[0] || inputs.find((item) => {
                    const text = textOf(item.closest('label, tr, li, div') || item);
                    return !/已认领至相同店铺/.test(text);
                });
                if (!input) return {status: 'manual_required', message: 'store option not found'};

                const target = input.closest('label, .el-checkbox, .ant-checkbox-wrapper, tr, li, div') || input;
                target.scrollIntoView({block: 'center', inline: 'nearest'});
                if (!input.checked) {
                    target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                    target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                    target.click();
                }
                return {status: 'ok', text: textOf(target).slice(0, 180), checked: input.checked};
            }"""
        )
    except Exception as exc:
        return {"status": "manual_required", "message": str(exc)}


def _click_confirm_in_dialog(page: Any) -> bool:
    return bool(page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            const dialogs = Array.from(document.querySelectorAll('.ant-modal, .el-dialog, .layui-layer, [role="dialog"]')).filter(visible);
            const root = dialogs[dialogs.length - 1] || document.body;
            const buttons = Array.from(root.querySelectorAll('button, a, span')).filter(visible);
            const button = buttons.find((el) => ['确定', '确认', '开始认领', '认领'].includes(textOf(el)));
            if (!button) return false;
            button.scrollIntoView({block: 'center', inline: 'nearest'});
            button.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
            button.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
            button.click();
            return true;
        }"""
    ))


def _looks_like_collect_box(page: Any) -> bool:
    text = body_text(page, timeout=2000)
    url = page.url.lower()
    return any(token in text for token in ["采集箱", "未认领", "认领", "采集商品", "数据采集"]) or "datacquisition" in url or "collect" in url


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
