from __future__ import annotations

from typing import Any

from .dxm_claim_pages import (
    claim_to_temu_store,
    find_recent_collected_product,
    open_claimed_edit_page,
    open_collect_box_or_use_current,
    wait_claim_finished,
)
from .dxm_link_collect import (
    go_to_collect_box_after_link_collect,
    open_dxm_link_collect,
    submit_product_link_collect,
    wait_link_collect_success,
)
from .full_one_flow import run_full_one_product
from .publish_pages import run_publish_current_edit_page
from .utils import ManualRequiredError, take_screenshot
from .yunqi_pages import extract_first_temu_product_link, open_yunqi, search_yunqi_results, set_yunqi_temu_filters


def run_full_one_link_product(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "running",
        "full_one_link_success": False,
        "publish_status": "not_run",
        "used_plugin_fallback": False,
        "screenshots": {},
    }
    try:
        result["yunqi_open"] = open_yunqi(page, logger=logger, state=state)
        if result["yunqi_open"].get("status") != "ok":
            return _finish_manual(result, "yunqi_open", "Yunqi login/open requires manual handling.", result["yunqi_open"].get("screenshot_path", ""), state, logger, page)

        result["yunqi_filter"] = set_yunqi_temu_filters(page, logger=logger, state=state)
        result["yunqi_search"] = search_yunqi_results(page, logger=logger, state=state)
        if result["yunqi_search"].get("screenshot_path"):
            result["screenshots"]["full_yunqi_result"] = result["yunqi_search"]["screenshot_path"]

        product_info = extract_first_temu_product_link(page, logger=logger, state=state)
        result["yunqi_product_link"] = product_info
        if product_info.get("screenshot_path"):
            result["screenshots"]["full_yunqi_result_extract"] = product_info["screenshot_path"]
        product_url = str(product_info.get("product_url") or "")
        if not product_url:
            result["used_plugin_fallback"] = True
            _log(logger, "full_one_link", "warning", "Yunqi product link extraction failed; falling back to old plugin collection flow.", page=page)
            fallback = run_full_one_product(page, config, logger=logger, state=state)
            result["fallback"] = fallback
            result["status"] = fallback.get("status", "unknown")
            result["publish_status"] = fallback.get("publish_status", "not_run")
            result["full_one_link_success"] = fallback.get("status") == "success"
            return result

        link_page_result = open_dxm_link_collect(page, logger=logger, state=state)
        result["dxm_link_collect_open"] = _strip_page(link_page_result)
        link_page = link_page_result.get("page") or page
        if link_page_result.get("status") == "manual_required" and not link_page_result.get("continued", False):
            return _finish_manual(result, "dxm_link_collect_open", "Dianxiaomi link collect page requires manual navigation.", link_page_result.get("screenshot_path", ""), state, logger, link_page)

        submit = submit_product_link_collect(link_page, product_url, logger=logger, state=state)
        result["dxm_link_collect_submit"] = submit
        if submit.get("status") != "ok":
            return _fallback_or_manual(result, page, config, logger, state, "dxm_link_collect_submit", submit.get("message", "Link collect submit failed."), submit.get("screenshot_path", ""))

        collect_wait = wait_link_collect_success(link_page, logger=logger, state=state)
        result["dxm_link_collect_wait"] = collect_wait
        if collect_wait.get("status") != "ok":
            retry_url = _alternate_temu_url(product_url)
            if retry_url and retry_url != product_url:
                _log(logger, "dxm_link_collect_retry", "start", f"Retrying link collection with alternate URL: {retry_url}", page=link_page)
                retry_submit = submit_product_link_collect(link_page, retry_url, logger=logger, state=state)
                result["dxm_link_collect_retry_submit"] = retry_submit
                if retry_submit.get("status") == "ok":
                    retry_wait = wait_link_collect_success(link_page, logger=logger, state=state)
                    result["dxm_link_collect_retry_wait"] = retry_wait
                    if retry_wait.get("status") == "ok":
                        product_url = retry_url
                        collect_wait = retry_wait
                        result["dxm_link_collect_wait"] = retry_wait
        if collect_wait.get("screenshot_path"):
            result["screenshots"]["full_link_collect_success"] = collect_wait["screenshot_path"]
        if collect_wait.get("status") != "ok":
            return _fallback_or_manual(result, page, config, logger, state, "dxm_link_collect_wait", collect_wait.get("message", "Link collection failed."), collect_wait.get("screenshot_path", ""))

        collect_page = go_to_collect_box_after_link_collect(link_page, logger=logger, state=state)
        collect_box = open_collect_box_or_use_current(collect_page, logger=logger, state=state)
        collect_page = collect_box.get("page") or collect_page
        result["dxm_collect_box"] = _strip_page(collect_box)
        if collect_box.get("status") == "manual_required" and not collect_box.get("continued", False):
            return _finish_manual(result, "dxm_collect_box", "Collect box requires manual handling.", collect_box.get("screenshot_path", ""), state, logger, collect_page)

        recent = find_recent_collected_product(collect_page, temu_product_url=product_url, logger=logger, state=state)
        result["dxm_recent_collected"] = recent
        if recent.get("status") != "ok":
            return _finish_manual(result, "dxm_recent_collected", "No recent collected product found in collect box.", recent.get("screenshot_path", ""), state, logger, collect_page)

        claim = claim_to_temu_store(collect_page, logger=logger, state=state)
        result["dxm_claim"] = claim
        if claim.get("status") != "ok":
            return _finish_manual(result, "dxm_claim", claim.get("message", "Claim to Temu store failed."), claim.get("screenshot_path", ""), state, logger, collect_page)

        claim_wait = wait_claim_finished(collect_page, logger=logger, state=state)
        result["dxm_claim_wait"] = claim_wait
        if claim_wait.get("screenshot_path"):
            result["screenshots"]["full_claim_result"] = claim_wait["screenshot_path"]
        if claim_wait.get("status") == "manual_required" and not claim_wait.get("continued", False):
            return _finish_manual(result, "dxm_claim_wait", "Claim completion requires manual confirmation.", claim_wait.get("screenshot_path", ""), state, logger, collect_page)

        edit_page = open_claimed_edit_page(collect_page, config, logger=logger, state=state)
        result["edit_url"] = edit_page.url
        result["dxm_open_edit"] = {"status": "ok", "url": edit_page.url}

        publish = run_publish_current_edit_page(
            edit_page,
            config,
            logger=logger,
            state=state,
            product_context={
                "source": "full_one_link",
                "temu_product_url": product_url,
                "goods_id": product_info.get("goods_id", ""),
                "title": product_info.get("title", ""),
                "shop_name": product_info.get("shop_name", ""),
            },
        )
        result["publish"] = publish
        result["publish_status"] = publish.get("status", "unknown")
        result["status"] = publish.get("status", "unknown")
        result["full_one_link_success"] = publish.get("status") == "success"
        if publish.get("full_publish_screenshot"):
            result["screenshots"]["full_publish_result"] = publish["full_publish_screenshot"]

        _log(logger, "full_one_link", result["status"], f"full-one-link completed, publish_status={result['publish_status']}", page=edit_page, screenshot_path=result["screenshots"].get("full_publish_result", ""), extra={"product_url": product_url})
        if state:
            state.update(status=result["status"], full_one_link_result=result)
        return result
    except ManualRequiredError as exc:
        return _finish_manual(result, exc.step, exc.message, exc.screenshot_path, state, logger, page)
    except Exception as exc:
        screenshot_path = ""
        try:
            screenshot_path = take_screenshot(page, "full_one_link_error")
        except Exception:
            pass
        result.update({"status": "error", "failed_step": "full_one_link", "message": str(exc), "screenshot_path": screenshot_path})
        _log(logger, "full_one_link", "error", f"full-one-link exception: {exc}", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="error", full_one_link_result=result)
        return result


def _fallback_or_manual(
    result: dict[str, Any],
    page: Any,
    config: dict[str, Any],
    logger: Any | None,
    state: Any | None,
    step: str,
    message: str,
    screenshot_path: str,
) -> dict[str, Any]:
    result["used_plugin_fallback"] = True
    _log(logger, "full_one_link", "warning", f"{message} Falling back to old plugin collection flow.", page=page, screenshot_path=screenshot_path)
    fallback = run_full_one_product(page, config, logger=logger, state=state)
    result["fallback"] = fallback
    result["status"] = fallback.get("status", "unknown")
    result["publish_status"] = fallback.get("publish_status", "not_run")
    result["full_one_link_success"] = fallback.get("status") == "success"
    result["failed_step"] = step if result["status"] != "success" else ""
    return result


def _finish_manual(
    result: dict[str, Any],
    step: str,
    message: str,
    screenshot_path: str,
    state: Any | None,
    logger: Any | None = None,
    page: Any | None = None,
) -> dict[str, Any]:
    result.update(
        {
            "status": "manual_required",
            "full_one_link_success": False,
            "publish_status": "not_run",
            "failed_step": step,
            "message": message,
            "screenshot_path": screenshot_path,
        }
    )
    _log(logger, step, "manual_required", message, page=page, screenshot_path=screenshot_path)
    if state:
        state.update(status="manual_required", full_one_link_result=result)
    return result


def _strip_page(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "page"}


def _alternate_temu_url(product_url: str) -> str:
    if "/uk/goods.html" in product_url:
        return product_url.replace("/uk/goods.html", "/goods.html")
    if "/goods.html" in product_url:
        return product_url.replace("/goods.html", "/uk/goods.html")
    return ""


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
