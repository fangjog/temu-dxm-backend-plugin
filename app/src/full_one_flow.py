from __future__ import annotations

from typing import Any

from .dxm_claim_pages import (
    claim_to_temu_store,
    find_recent_collected_product,
    open_claimed_edit_page,
    open_collect_box_or_use_current,
    wait_claim_finished,
)
from .dxm_plugin_collect import click_go_to_collect_box, trigger_dxm_plugin_collect, wait_collect_success
from .publish_pages import run_publish_current_edit_page
from .temu_front_pages import ensure_temu_product_detail, ensure_temu_region, ensure_temu_shop_products_visible, handle_temu_security
from .utils import ManualRequiredError, take_screenshot
from .yunqi_pages import click_first_store_or_product, open_yunqi, search_yunqi_results, set_yunqi_temu_filters


def run_full_one_product(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "running",
        "full_one_success": False,
        "publish_status": "not_run",
        "manual_interventions": [],
        "screenshots": {},
    }

    try:
        result["yunqi_open"] = open_yunqi(page, logger=logger, state=state)
        if result["yunqi_open"].get("status") != "ok":
            return _finish_manual(result, "yunqi_open", "云启数据需要人工登录，当前执行环境无法继续。", result["yunqi_open"].get("screenshot_path", ""), state, logger=logger, page=page)
        result["yunqi_filter"] = set_yunqi_temu_filters(page, logger=logger, state=state)
        result["yunqi_search"] = search_yunqi_results(page, logger=logger, state=state)
        if result["yunqi_search"].get("screenshot_path"):
            result["screenshots"]["full_yunqi_result"] = result["yunqi_search"]["screenshot_path"]

        clicked = click_first_store_or_product(page, logger=logger, state=state)
        result["yunqi_click_first"] = _strip_page(clicked)
        if clicked.get("status") != "ok":
            return _finish_manual(result, "yunqi_click_first", clicked.get("message", "云启第一条结果点击失败。"), clicked.get("screenshot_path", ""), state)

        temu_page = clicked.get("page") or page
        result["temu_security"] = handle_temu_security(temu_page, logger=logger, state=state)
        result["temu_region"] = ensure_temu_region(temu_page, logger=logger, state=state)
        temu_page = ensure_temu_shop_products_visible(temu_page, yunqi_page=page, logger=logger, state=state)
        result["temu_shop_products_visible"] = {"status": "ok", "url": temu_page.url}
        product_detail = ensure_temu_product_detail(temu_page, logger=logger, state=state)
        result["temu_product_detail"] = product_detail
        product_url = product_detail.get("product_url", temu_page.url)
        if product_detail.get("screenshot_path"):
            result["screenshots"]["full_temu_product"] = product_detail["screenshot_path"]

        collect = trigger_dxm_plugin_collect(temu_page, logger=logger, state=state)
        result["dxm_plugin_collect"] = collect
        if collect.get("status") == "manual_required":
            result["manual_interventions"].append("dxm_plugin_collect")
            if not collect.get("continued", False):
                return _finish_manual(result, "dxm_plugin_collect", "店小秘插件采集需要人工处理，当前执行环境无法继续。", collect.get("screenshot_path", ""), state, logger=logger, page=temu_page)

        collect_success = wait_collect_success(temu_page, logger=logger, state=state)
        result["dxm_collect_success"] = collect_success
        if collect_success.get("screenshot_path"):
            result["screenshots"]["full_collect_success"] = collect_success["screenshot_path"]
        if collect_success.get("status") == "manual_required":
            result["manual_interventions"].append("dxm_collect_success")
            if not collect_success.get("continued", False):
                return _finish_manual(result, "dxm_collect_success", "采集成功状态需要人工确认，当前执行环境无法继续。", collect_success.get("screenshot_path", ""), state, logger=logger, page=temu_page)

        collect_page = click_go_to_collect_box(temu_page, logger=logger, state=state)
        collect_box = open_collect_box_or_use_current(collect_page, logger=logger, state=state)
        collect_page = collect_box.get("page") or collect_page
        result["dxm_collect_box"] = _strip_page(collect_box)
        if collect_box.get("status") == "manual_required" and not collect_box.get("continued", False):
            return _finish_manual(result, "dxm_collect_box", "采集箱需要人工打开，当前执行环境无法继续。", collect_box.get("screenshot_path", ""), state, logger=logger, page=collect_page)

        recent = find_recent_collected_product(collect_page, temu_product_url=product_url, logger=logger, state=state)
        result["dxm_recent_collected"] = recent
        if recent.get("status") != "ok":
            return _finish_manual(result, "dxm_recent_collected", "采集箱未找到可认领商品。", recent.get("screenshot_path", ""), state)

        claim = claim_to_temu_store(collect_page, logger=logger, state=state)
        result["dxm_claim"] = claim
        if claim.get("status") != "ok":
            return _finish_manual(result, "dxm_claim", claim.get("message", "认领到 Temu 店铺失败。"), claim.get("screenshot_path", ""), state)

        claim_wait = wait_claim_finished(collect_page, logger=logger, state=state)
        result["dxm_claim_wait"] = claim_wait
        if claim_wait.get("screenshot_path"):
            result["screenshots"]["full_claim_result"] = claim_wait["screenshot_path"]
        if claim_wait.get("status") == "manual_required":
            result["manual_interventions"].append("dxm_claim_wait")
            if not claim_wait.get("continued", False):
                return _finish_manual(result, "dxm_claim_wait", "认领完成状态需要人工确认，当前执行环境无法继续。", claim_wait.get("screenshot_path", ""), state, logger=logger, page=collect_page)

        edit_page = open_claimed_edit_page(collect_page, config, logger=logger, state=state)
        result["edit_url"] = edit_page.url
        result["dxm_open_edit"] = {"status": "ok", "url": edit_page.url}

        publish = run_publish_current_edit_page(
            edit_page,
            config,
            logger=logger,
            state=state,
            product_context={"source": "full_one", "temu_product_url": product_url},
        )
        result["publish"] = publish
        result["publish_status"] = publish.get("status", "unknown")
        result["status"] = publish.get("status", "unknown")
        result["full_one_success"] = publish.get("status") == "success"
        if publish.get("full_publish_screenshot"):
            result["screenshots"]["full_publish_result"] = publish["full_publish_screenshot"]

        _log(
            logger,
            "full_one",
            result["status"],
            f"full-one 流程完成，publish_status={result['publish_status']}",
            page=edit_page,
            screenshot_path=result["screenshots"].get("full_publish_result", ""),
            extra={"publish_status": result["publish_status"], "manual_interventions": result["manual_interventions"]},
        )
        if state:
            state.update(status=result["status"], full_one_result=result)
        return result
    except ManualRequiredError as exc:
        return _finish_manual(result, exc.step, exc.message, exc.screenshot_path, state, logger=logger, page=page)
    except Exception as exc:
        screenshot_path = ""
        try:
            screenshot_path = take_screenshot(page, "full_one_error")
        except Exception:
            pass
        result.update(
            {
                "status": "error",
                "full_one_success": False,
                "failed_step": "full_one",
                "message": str(exc),
                "screenshot_path": screenshot_path,
            }
        )
        _log(logger, "full_one", "error", f"full-one 异常: {exc}", page=page, screenshot_path=screenshot_path)
        if state:
            state.update(status="error", full_one_result=result)
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
            "full_one_success": False,
            "publish_status": "not_run",
            "failed_step": step,
            "message": message,
            "screenshot_path": screenshot_path,
        }
    )
    if logger:
        logger.log_step(step, "manual_required", message, page=page, screenshot_path=screenshot_path)
    if state:
        state.update(status="manual_required", full_one_result=result)
    return result


def _strip_page(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "page"}


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
