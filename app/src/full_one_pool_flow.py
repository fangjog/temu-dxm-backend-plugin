from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

from .browser_pool_manager import BrowserPoolManager, is_cdp_reachable
from .dxm_claim_pages import (
    claim_to_temu_store,
    find_recent_collected_product,
    open_claimed_edit_page,
    open_collect_box_or_use_current,
    wait_claim_finished,
)
from .dxm_plugin_collect import PLUGIN_CACHE_TOKENS, click_go_to_collect_box, trigger_dxm_plugin_collect, wait_collect_success
from .login_assistant import ensure_dxm_login, ensure_google_temu_login, ensure_yunqi_login
from .publish_pages import run_publish_current_edit_page
from .temu_front_pages import (
    ensure_temu_product_detail,
    handle_temu_security,
    recover_temu_shop_visibility_with_regions,
)
from .utils import ManualRequiredError, body_text, take_screenshot
from .windows_prompt import UserChoseSkip, UserChoseStop, show_manual_action_popup, wait_user_decision
from .yunqi_pages import click_first_store_or_product, open_yunqi, search_yunqi_results, set_yunqi_temu_filters


class BrowserAttemptFailed(RuntimeError):
    def __init__(self, step: str, message: str, screenshot_path: str = ""):
        self.step = step
        self.message = message
        self.screenshot_path = screenshot_path
        super().__init__(message)


def prepare_browser_pool(config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    """Validate browser pool assets without opening all browsers."""
    pool = BrowserPoolManager(config, logger=logger)
    result = pool.validate_pool_assets()
    result["message"] = "浏览器池已准备；不会同时打开 5 个浏览器。full-one-pool 会按需逐个启动。"
    if state:
        state.update(prepare_browser_pool=result)
    return result


def prepare_browser_by_index(index: int, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    """Start and prepare one browser only."""
    pool = BrowserPoolManager(config, logger=logger)
    pool.close_all_pool_browsers()
    if index < 1 or index > pool.browser_count():
        return {"status": "error", "message": f"Browser index out of range: {index}"}

    browser_cfg = pool.browser_config(index)
    manager = None
    report: dict[str, Any] = {"status": "running", "browser_name": browser_cfg["name"], "index": index}
    try:
        report["start"] = pool.start_browser_by_index(index)
        _wait_cdp(browser_cfg["cdp_url"])
        report["diagnose"] = pool.diagnose_browser_by_index(index)
        manager, _browser, context, page = pool.connect_browser_by_index(index)
        report["opened_pages"] = _open_single_browser_prepare_pages(page, context, browser_cfg["name"], logger=logger, state=state)
        report["status"] = "ok"
        report["message"] = "单个浏览器已打开。登录/验证完成后可运行 full-one-pool。"
    except (UserChoseSkip, UserChoseStop) as exc:
        report.update({"status": "manual_required", "message": str(exc)})
    except Exception as exc:
        report.update({"status": "manual_required", "message": str(exc)})
    finally:
        if manager:
            manager.close()
    if state:
        state.update(prepare_browser=report)
    return report


def run_full_one_pool(config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    pool = BrowserPoolManager(config, logger=logger)
    result: dict[str, Any] = {
        "status": "running",
        "mode": "single_active_browser_pool",
        "full_one_pool_success": False,
        "publish_status": "not_run",
        "browser_results": [],
        "failures": pool.failures,
    }

    pool.close_all_pool_browsers()

    for index in range(1, pool.browser_count() + 1):
        browser_cfg = pool.browser_config(index)
        browser_name = browser_cfg["name"]
        attempt: dict[str, Any] = {"browser_name": browser_name, "index": index, "status": "running"}
        manager = None
        close_current_browser = True
        try:
            attempt["start"] = pool.start_browser_by_index(index)
            _wait_cdp(browser_cfg["cdp_url"])
            attempt["diagnose"] = pool.diagnose_browser_by_index(index)
            manager, _browser, _context, page = pool.connect_browser_by_index(index)

            publish = _run_one_browser_attempt(browser_name, page, config, logger=logger, state=state)
            attempt["publish"] = publish
            attempt["status"] = publish.get("status", "unknown")
            result["browser_results"].append(attempt)

            if publish.get("status") == "success":
                close_current_browser = False
                result.update(
                    {
                        "status": "success",
                        "full_one_pool_success": True,
                        "publish_status": "success",
                        "used_browser": browser_name,
                        "publish": publish,
                        "failures": pool.failures,
                    }
                )
                if state:
                    state.update(full_one_pool=result)
                return result

            if publish.get("status") == "manual_required":
                decision = _manual_decision_for_attempt(browser_name, publish, logger=logger)
                if decision == "continue":
                    publish["manual_confirmed"] = True
                    claim_publish = _continue_after_manual_collect_confirmation(browser_name, page, config, publish, logger=logger, state=state)
                    attempt["publish_after_manual_confirm"] = claim_publish
                    if claim_publish.get("status") == "success":
                        close_current_browser = False
                        result.update(
                            {
                                "status": "success",
                                "full_one_pool_success": True,
                                "publish_status": "success",
                                "used_browser": browser_name,
                                "publish": claim_publish,
                                "failures": pool.failures,
                            }
                        )
                        if state:
                            state.update(full_one_pool=result)
                        return result
                elif decision == "skip":
                    raise UserChoseSkip(f"Operator skipped {browser_name}.")
                else:
                    raise UserChoseStop(f"Operator stopped at {browser_name}.")

            pool.record_failure(index, f"publish status={publish.get('status')}")
        except UserChoseSkip as exc:
            screenshot_path = _best_effort_screenshot(manager, f"{browser_name}_user_skip")
            attempt.update({"status": "skipped", "failed_step": "operator_skip", "message": str(exc), "screenshot_path": screenshot_path})
            pool.record_failure(index, f"operator_skip: {exc}", screenshot_path)
            result["browser_results"].append(attempt)
            continue
        except UserChoseStop as exc:
            screenshot_path = _best_effort_screenshot(manager, f"{browser_name}_user_stop")
            attempt.update({"status": "stopped", "failed_step": "operator_stop", "message": str(exc), "screenshot_path": screenshot_path})
            pool.record_failure(index, f"operator_stop: {exc}", screenshot_path)
            result["browser_results"].append(attempt)
            result.update({"status": "stopped", "failures": pool.failures})
            if state:
                state.update(full_one_pool=result)
            return result
        except BrowserAttemptFailed as exc:
            attempt.update({"status": "failed", "failed_step": exc.step, "message": exc.message, "screenshot_path": exc.screenshot_path})
            pool.record_failure(index, f"{exc.step}: {exc.message}", exc.screenshot_path)
            result["browser_results"].append(attempt)
            continue
        except ManualRequiredError as exc:
            decision = _manual_decision_for_attempt(
                browser_name,
                {"failed_step": exc.step, "message": exc.message, "screenshot_path": exc.screenshot_path},
                logger=logger,
            )
            attempt.update({"status": "manual_required", "failed_step": exc.step, "message": exc.message, "screenshot_path": exc.screenshot_path, "decision": decision})
            pool.record_failure(index, f"{exc.step}: {exc.message}", exc.screenshot_path)
            result["browser_results"].append(attempt)
            if decision == "continue":
                continue
            if decision == "skip":
                continue
            result.update({"status": "stopped", "failures": pool.failures})
            if state:
                state.update(full_one_pool=result)
            return result
        except Exception as exc:
            screenshot_path = _best_effort_screenshot(manager, f"{browser_name}_failed")
            attempt.update({"status": "failed", "failed_step": "unexpected", "message": str(exc), "screenshot_path": screenshot_path})
            pool.record_failure(index, f"unexpected: {exc}", screenshot_path)
            result["browser_results"].append(attempt)
            continue
        finally:
            if manager:
                manager.close()
            if close_current_browser:
                pool.close_browser_by_index(index)

    result["status"] = "failed"
    result["failures"] = pool.failures
    if state:
        state.update(full_one_pool=result)
    return result


def _run_one_browser_attempt(browser_name: str, page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    context: dict[str, Any] = {"browser_name": browser_name, "collect_time": _now_iso()}

    page.goto("https://www.yunqishuju.com/", wait_until="domcontentloaded")
    _wait_ready(page)
    ensure_yunqi_login(page, browser_name, logger=logger, state=state)
    yunqi_open = open_yunqi(page, logger=logger, state=state)
    if yunqi_open.get("status") != "ok":
        raise BrowserAttemptFailed("yunqi_open", yunqi_open.get("message", "Yunqi open failed."), yunqi_open.get("screenshot_path", ""))

    set_yunqi_temu_filters(page, logger=logger, state=state)
    search = search_yunqi_results(page, logger=logger, state=state)

    clicked = click_first_store_or_product(page, logger=logger, state=state)
    if clicked.get("status") != "ok":
        raise BrowserAttemptFailed("yunqi_click_first", clicked.get("message", "Yunqi first store click failed."), clicked.get("screenshot_path", ""))
    context.update(
        {
            "yunqi_title": clicked.get("title", ""),
            "temu_shop_name": clicked.get("shop_name", ""),
            "yunqi_url": page.url,
            "yunqi_result_screenshot": search.get("screenshot_path", ""),
        }
    )

    temu_page = clicked.get("page") or page
    handle_temu_security(temu_page, logger=logger, state=state)
    ensure_google_temu_login(temu_page, browser_name, logger=logger, state=state)

    recovery = recover_temu_shop_visibility_with_regions(temu_page, config, logger=logger, state=state)
    if recovery.get("status") != "ok":
        raise BrowserAttemptFailed("temu_region_recovery", "Temu shop products not visible after region switching.", recovery.get("screenshot_path", ""))
    context["tried_regions"] = recovery.get("tried_regions", [])

    detail = ensure_temu_product_detail(temu_page, logger=logger, state=state)
    product_url = detail.get("product_url", temu_page.url)
    product_context = _extract_temu_product_context(temu_page, product_url)
    product_context.update(context)

    collect = trigger_dxm_plugin_collect(temu_page, logger=logger, state=state)
    collect_text = body_text(temu_page, timeout=1500)
    if any(token in collect_text for token in PLUGIN_CACHE_TOKENS) or any("cache warning" in item for item in collect.get("warnings", [])):
        screenshot_path = take_screenshot(temu_page, f"{browser_name}_dxm_plugin_cache_block")
        raise BrowserAttemptFailed("dxm_plugin_collect", "Dianxiaomi plugin reports Temu cannot browse this product normally.", screenshot_path)
    if collect.get("status") == "manual_required":
        decision = wait_user_decision("店小秘插件采集需要人工确认。完成后输入 continue；跳过当前浏览器输入 skip；停止输入 stop。", logger=logger)
        if decision == "skip":
            raise UserChoseSkip("Operator skipped plugin collection.")
        if decision == "stop":
            raise UserChoseStop("Operator stopped at plugin collection.")
    elif collect.get("status") != "ok":
        raise BrowserAttemptFailed("dxm_plugin_collect", "Dianxiaomi plugin collection did not start successfully.", collect.get("screenshot_path", ""))

    collect_success = wait_collect_success(temu_page, logger=logger, state=state)
    if collect_success.get("status") != "ok":
        decision = wait_user_decision("店小秘采集成功状态未能自动确认。确认已采集成功请输入 continue；跳过输入 skip；停止输入 stop。", logger=logger)
        if decision == "skip":
            raise UserChoseSkip("Operator skipped after collect success check.")
        if decision == "stop":
            raise UserChoseStop("Operator stopped after collect success check.")

    collect_page = click_go_to_collect_box(temu_page, logger=logger, state=state)
    collect_box = open_collect_box_or_use_current(collect_page, logger=logger, state=state)
    collect_page = collect_box.get("page") or collect_page
    if collect_box.get("status") != "ok":
        raise BrowserAttemptFailed("dxm_collect_box", "Dianxiaomi collect box did not open.", collect_box.get("screenshot_path", ""))

    recent = find_recent_collected_product(
        collect_page,
        temu_product_url=product_url,
        logger=logger,
        state=state,
        product_context=product_context,
        strict=True,
    )
    if recent.get("status") != "ok":
        payload = {
            "failed_step": "dxm_recent_collected",
            "message": "无法自动确认采集箱商品是否为本轮采集商品，继续前必须人工确认。",
            "screenshot_path": recent.get("screenshot_path", ""),
        }
        decision = _manual_decision_for_attempt(browser_name, payload, logger=logger)
        if decision == "skip":
            raise UserChoseSkip("Operator skipped because collected product was not confirmed.")
        if decision == "stop":
            raise UserChoseStop("Operator stopped because collected product was not confirmed.")
        recent["status"] = "manual_confirmed"
        recent["manual_confirmed"] = True

    return _claim_and_publish_current_collect_page(browser_name, collect_page, config, product_context, recent, logger=logger, state=state)


def _continue_after_manual_collect_confirmation(
    browser_name: str,
    page: Any,
    config: dict[str, Any],
    publish: dict[str, Any],
    logger: Any | None = None,
    state: Any | None = None,
) -> dict[str, Any]:
    product_context = dict(publish.get("product_context") or {})
    recent = dict(publish.get("recent") or {"status": "manual_confirmed"})
    return _claim_and_publish_current_collect_page(browser_name, page, config, product_context, recent, logger=logger, state=state)


def _claim_and_publish_current_collect_page(
    browser_name: str,
    collect_page: Any,
    config: dict[str, Any],
    product_context: dict[str, Any],
    recent: dict[str, Any],
    logger: Any | None = None,
    state: Any | None = None,
) -> dict[str, Any]:
    claim = claim_to_temu_store(collect_page, logger=logger, state=state)
    if claim.get("status") != "ok":
        raise BrowserAttemptFailed("dxm_claim", "Claim to Temu store failed.", claim.get("screenshot_path", ""))
    claim_wait = wait_claim_finished(collect_page, logger=logger, state=state)
    if claim_wait.get("status") == "manual_required" and not claim_wait.get("continued", False):
        decision = wait_user_decision("认领完成状态未能自动确认。确认完成请输入 continue；跳过输入 skip；停止输入 stop。", logger=logger)
        if decision == "skip":
            raise UserChoseSkip("Operator skipped after claim wait.")
        if decision == "stop":
            raise UserChoseStop("Operator stopped after claim wait.")

    edit_page = open_claimed_edit_page(collect_page, config, logger=logger, state=state)
    publish = run_publish_current_edit_page(edit_page, config, logger=logger, state=state, product_context=product_context)
    publish["browser_name"] = browser_name
    publish["product_context"] = product_context
    publish["collect_confirmed"] = recent
    return publish


def _open_single_browser_prepare_pages(page: Any, context: Any, browser_name: str, logger: Any | None = None, state: Any | None = None) -> list[dict[str, Any]]:
    pages = [
        ("yunqi", "https://www.yunqishuju.com/", ensure_yunqi_login),
        ("temu", "https://www.temu.com/", ensure_google_temu_login),
        ("dianxiaomi", "https://www.dianxiaomi.com/", ensure_dxm_login),
        ("dxm_draft", "https://www.dianxiaomi.com/web/temu/choiceTemuList/draft", ensure_dxm_login),
    ]
    results: list[dict[str, Any]] = []
    target = page
    for name, url, login_fn in pages:
        try:
            target.goto(url, wait_until="domcontentloaded")
            _wait_ready(target)
            login_result = login_fn(target, browser_name, logger=logger, state=state)
            results.append({"name": name, "status": "ok", "url": target.url, "login": login_result})
        except (UserChoseSkip, UserChoseStop):
            raise
        except Exception as exc:
            screenshot_path = take_screenshot(target, f"{browser_name}_prepare_{name}")
            results.append({"name": name, "status": "manual_required", "url": target.url, "message": str(exc), "screenshot_path": screenshot_path})
            message = f"{browser_name} 打开或登录 {name} 失败。请人工处理后输入 continue；跳过输入 skip；停止输入 stop。"
            show_manual_action_popup(f"{browser_name} {name} 登录准备", message, logger=logger)
            decision = wait_user_decision(message, logger=logger)
            if decision == "skip":
                raise UserChoseSkip(message)
            if decision == "stop":
                raise UserChoseStop(message)
    return results


def _manual_decision_for_attempt(browser_name: str, payload: dict[str, Any], logger: Any | None = None) -> str:
    message = (
        f"{browser_name} 需要人工确认：{payload.get('failed_step', '')} {payload.get('message', '')}\n"
        f"截图: {payload.get('screenshot_path', '')}\n"
        "确认后输入 continue；跳过当前浏览器输入 skip；停止全部流程输入 stop。"
    )
    show_manual_action_popup(f"{browser_name} 人工确认", message, logger=logger)
    return wait_user_decision(message, logger=logger)


def _extract_temu_product_context(page: Any, product_url: str) -> dict[str, Any]:
    text = body_text(page, timeout=1500)
    title = ""
    try:
        title = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const node = Array.from(document.querySelectorAll('h1, h2, [data-testid*=title], [class*=title]'))
                    .find((el) => visible(el) && (el.innerText || el.textContent || '').trim().length > 8);
                return node ? (node.innerText || node.textContent || '').trim() : '';
            }"""
        )
    except Exception:
        title = ""
    if not title:
        title = text.splitlines()[0].strip() if text.splitlines() else ""
    goods_id = _extract_goods_id(product_url) or _extract_goods_id(text)
    return {"temu_product_url": product_url, "temu_title": title[:240], "title": title[:240], "goods_id": goods_id}


def _extract_goods_id(value: str) -> str:
    if not value:
        return ""
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    goods_id = (query.get("goods_id") or [""])[0]
    if re.fullmatch(r"\d{8,}", goods_id or ""):
        return goods_id
    match = re.search(r"(?:goods_id=|goodsId=|goods ID:|Item ID:|item id[:=]?|[-_]g-)(\d{8,})", value, re.I)
    return match.group(1) if match else ""


def _wait_cdp(cdp_url: str, timeout_seconds: int = 20) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if is_cdp_reachable(cdp_url):
            return
        time.sleep(0.5)
    raise RuntimeError(f"CDP not reachable after starting browser: {cdp_url}")


def _wait_ready(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _best_effort_screenshot(manager: Any | None, step: str) -> str:
    try:
        if manager and manager.context and manager.context.pages:
            return take_screenshot(manager.context.pages[-1], step)
    except Exception:
        return ""
    return ""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
