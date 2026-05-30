from __future__ import annotations





import datetime


import hashlib


import json


import random


import re


from decimal import Decimal, InvalidOperation


from typing import Any





from .dianxiaomi_pages import (


    fill_product_title,


    process_sku_fields,


    read_original_title,


    select_origin_country_and_province,


)


from .easyrouter_client import EasyRouterClient


from .publish_pages import (


    click_immediate_publish,


    fill_package_info_required,


    fill_required_product_attributes,


    fill_variant_dimensions_and_weight,


    handle_publish_dialogs,


    ensure_product_description_image_module,


    run_publish_current_edit_page,


    scan_required_errors,


    verify_publish_status,


)


from .sku_cleaner import contains_chinese


from .utils import PROJECT_ROOT, take_screenshot


from .windows_prompt import show_manual_action_popup








PUBLISH_SUCCESS_STATUSES = {"success"}


SECOND_PUBLISH_TEXTS = [
    "创建新产品(保留已填内容)",
    "创建新产品（保留已填内容）",
    "保留已填内容",
    "同款二次发布",
    "二次发布",
    "二次编辑",
    "再次发布",
    "复制发布",
    "创建新产品",
]

CONTINUE_PUBLISH_TEXTS = ["继续刊登"]








class DxmTwiceFlowError(RuntimeError):


    def __init__(


        self,


        step: str,


        message: str,


        screenshot_path: str = "",


        result: dict[str, Any] | None = None,


    ):


        super().__init__(message)


        self.step = step


        self.message = message


        self.screenshot_path = screenshot_path


        self.result = result or {}








def fail_with_popup_and_screenshot(


    page: Any,


    step: str,


    message: str,


    logger: Any | None = None,


    state: Any | None = None,


    extra: dict[str, Any] | None = None,


) -> None:


    screenshot_path = ""


    page_title = ""


    current_url = getattr(page, "url", "")


    try:


        screenshot_path = take_screenshot(page, f"dxm_twice_{step}")


    except Exception as exc:


        screenshot_path = f"screenshot_failed: {exc}"


    try:


        page_title = page.title()


    except Exception:


        page_title = ""





    log_extra = {"current_url": current_url, "page_title": page_title}


    if extra:


        log_extra.update(extra)


    _log(logger, step, "failed", message, page=page, screenshot_path=screenshot_path, extra=log_extra)


    if state:


        state.update(


            status="failed",


            failed_step=step,


            error=message,


            screenshot_path=screenshot_path,


            current_url=current_url,


            page_title=page_title,


        )





    popup_message = (
        f"Step: {step}\n"
        f"Reason: {message}\n"
        f"Screenshot: {screenshot_path}\n\n"
        "Flow stopped. Please fix and rerun."
    )

    show_manual_action_popup("Dianxiaomi publish flow error", popup_message, logger=logger)


    raise DxmTwiceFlowError(step, message, screenshot_path, log_extra)








def run_dxm_publish_once(


    page: Any,


    config: dict[str, Any],


    logger: Any | None = None,


    state: Any | None = None,


) -> dict[str, Any]:


    context = build_context_from_first_row(page, logger=logger)


    result: dict[str, Any] = {"status": "running", "states": [], "context": context}


    try:


        if _is_edit_page(page):


            edit_page = page


            _log(logger, "open_existing_dxm_product", "ok", f"???????????��???????????? {page.url}", page=page)


        else:


            edit_page = open_first_product_edit_from_current_dxm_page(page, logger=logger, state=state)


        context["first_edit_url"] = edit_page.url


        context["first_edit_id"] = _extract_edit_context_id(edit_page.url)


        context["first_title_before"] = context.get("first_title_before") or _safe_read_title(edit_page)


        context["source_list_title"] = context.get("source_list_title") or context["first_title_before"]


        _state(result, "first_edit_start", edit_page, logger, extra={"first_edit_url": context["first_edit_url"], "first_edit_id": context["first_edit_id"]})


        first = run_first_publish_edit(edit_page, config, context=context, logger=logger, state=state)


        result.update({"status": first.get("status", "unknown"), "first_publish": first, "context": context})


        if state:


            state.update(dxm_publish_once=result)


        return result


    except DxmTwiceFlowError as exc:


        result.update(


            {


                "status": "failed",


                "step": exc.step,


                "message": exc.message,


                "screenshot_path": exc.screenshot_path,


                "context": context,


            }


        )


        return result


    except Exception as exc:


        try:


            fail_with_popup_and_screenshot(page, "dxm_publish_once_unexpected", str(exc), logger=logger, state=state)


        except DxmTwiceFlowError as wrapped:


            result.update(


                {


                    "status": "failed",


                    "step": wrapped.step,


                    "message": wrapped.message,


                    "screenshot_path": wrapped.screenshot_path,


                    "context": context,


                }


            )


        return result








def run_dxm_publish_twice(


    page: Any,


    config: dict[str, Any],


    logger: Any | None = None,


    state: Any | None = None,


) -> dict[str, Any]:


    context = build_context_from_first_row(page, logger=logger)


    result: dict[str, Any] = {"status": "running", "states": [], "context": context}


    try:


        current_is_edit = _is_edit_page(page)


        context["started_from_edit_page"] = current_is_edit


        if current_is_edit:


            edit_page = page


            _log(logger, "open_existing_dxm_product", "ok", f"???????????��???????????��??��??? {page.url}", page=page)


        else:


            edit_page = open_first_product_edit_from_current_dxm_page(page, logger=logger, state=state)


        context["first_edit_url"] = edit_page.url


        context["first_edit_id"] = _extract_edit_context_id(edit_page.url)


        context["first_title_before"] = context.get("first_title_before") or _safe_read_title(edit_page)


        context["source_list_title"] = context.get("source_list_title") or context["first_title_before"]


        context["source_list_sku"] = context.get("source_list_sku") or _read_first_sku_from_edit_page(edit_page)


        result.update({"initial_title": context["first_title_before"], "initial_edit_url": edit_page.url})


        _state(result, "open_existing_dxm_product", edit_page, logger, {"title": context["first_title_before"]})





        if _is_second_edit_page(edit_page):


            raw_id = _extract_product_id(edit_page.url)


            context["first_edit_url"] = f"https://www.dianxiaomi.com/web/temu/edit?id={raw_id}"


            context["first_edit_id"] = f"edit:{raw_id}"


            first = {"status": "success", "resumed_on_second_edit": True}


            result["first_publish"] = first


            context["first_publish_submitted"] = True


            context["first_publish_time"] = datetime.datetime.now().isoformat()


            context["first_publish_status"] = "resumed_on_second_edit"


            second_page = edit_page


            _log(logger, "first_publish_success", "ok", "Already on second edit page; skipping first publish and second-entry click.", page=edit_page)


        else:


            if _has_first_publish_success_prompt(edit_page):


                first = {"status": "success", "resumed_after_first_publish": True}


                result["first_publish"] = first


                context["first_publish_submitted"] = True


                context["first_publish_time"] = datetime.datetime.now().isoformat()


                context["first_publish_status"] = "resumed_after_first_publish"

                _log(logger, "first_publish_success", "ok", "Detected first publish success prompt; continuing to second publish entry.", page=edit_page)


            else:


                first = run_first_publish_edit(edit_page, config, context=context, logger=logger, state=state)


                result["first_publish"] = first


                if first.get("status") not in PUBLISH_SUCCESS_STATUSES:


                    fail_with_popup_and_screenshot(


                        edit_page,


                        "first_publish_success",


                        f"????��???��???��??????????????????????????? {first.get('status')}",


                        logger=logger,


                        state=state,


                        extra={"first_publish": first},


                    )


                # Record as submitted, NOT final success


                context["first_publish_submitted"] = True


                context["first_publish_time"] = datetime.datetime.now().isoformat()


                context["first_publish_status"] = first.get("status", "unknown")


            _state(result, "first_publish_success", edit_page, logger, {"status": first.get("status"), "first_publish_submitted": True})





            second_page = click_second_publish_entry(edit_page, logger=logger, state=state)


            _state(result, "second_publish_entry_clicked", second_page, logger)





        # Validate second edit page is a NEW product


        assert_second_edit_is_new_product(second_page, context, logger=logger, state=state)


        context["second_edit_url"] = second_page.url


        context["second_edit_id"] = _extract_edit_context_id(second_page.url)


        context["second_title_before"] = _safe_read_title(second_page)


        _state(result, "second_edit_start", second_page, logger, extra={"second_edit_url": context["second_edit_url"], "second_edit_id": context["second_edit_id"]})





        second = run_second_publish_edit(second_page, config, context=context, logger=logger, state=state)


        result["second_publish"] = second


        if second.get("status") not in PUBLISH_SUCCESS_STATUSES:


            fail_with_popup_and_screenshot(


                second_page,


                "second_publish_success",


                f"????��???��???��??????????????????????????? {second.get('status')}",


                logger=logger,


                state=state,


                extra={"second_publish": second},


            )


        # Record as submitted, NOT final success


        context["second_publish_submitted"] = True


        context["second_publish_time"] = datetime.datetime.now().isoformat()


        context["second_publish_status"] = second.get("status", "unknown")


        _state(result, "second_publish_success", second_page, logger, {"status": second.get("status"), "second_publish_submitted": True})





        # ====== FINAL BACKEND VERIFICATION ======


        _log(logger, "verify_two_distinct", "start", "Both publishes submitted. Now verifying 2 distinct records in backend...", page=second_page)


        verification = verify_two_distinct_publish_records(second_page, context, logger=logger, state=state)


        result["verification"] = verification


        context["verify_result"] = verification.get("verify_result", "not_run")





        if verification.get("verify_result") != "dual_publish_verified":


            # Verification failed - report as failure


            result["status"] = "dual_publish_verify_failed"


            _state(result, "done", second_page, logger, {"status": result["status"], "verify_result": verification.get("verify_result")})


            if state:


                state.update(dxm_publish_twice=result, dual_publish_verify_failed=True)


            return result





        result["status"] = "success"


        _state(result, "done", second_page, logger, {"status": result["status"], "dual_publish_verified": True})


        if state:


            state.update(dxm_publish_twice=result)


        return result


    except DxmTwiceFlowError as exc:


        result.update(


            {


                "status": "failed",


                "step": exc.step,


                "message": exc.message,


                "screenshot_path": exc.screenshot_path,


                "context": context,


            }


        )


        return result


    except Exception as exc:


        try:


            fail_with_popup_and_screenshot(page, "dxm_publish_twice_unexpected", str(exc), logger=logger, state=state)


        except DxmTwiceFlowError as wrapped:


            result.update(


                {


                    "status": "failed",


                    "step": wrapped.step,


                    "message": wrapped.message,


                    "screenshot_path": wrapped.screenshot_path,


                    "context": context,


                }


            )


        return result








def open_existing_dxm_product(page: Any, logger: Any | None = None, state: Any | None = None) -> Any:


    return open_first_product_edit_from_current_dxm_page(page, logger=logger, state=state)








def open_first_product_edit_from_current_dxm_page(page: Any, logger: Any | None = None, state: Any | None = None) -> Any:


    if _is_edit_page(page):


        _log(logger, "open_existing_dxm_product", "ok", f"??????????????��????????: {page.url}", page=page)


        if state:


            state.update(last_edit_url=page.url)


        return page





    if "dianxiaomi.com" not in (page.url or "").lower():


        fail_with_popup_and_screenshot(


            page,


            "open_first_product_edit",


            "Current page is not a Dianxiaomi backend page. This command will not open Yunqi or Temu.",


            logger=logger,


            state=state,


        )





    row_info = _mark_first_dxm_list_row(page)


    if not row_info.get("ok"):


        fail_with_popup_and_screenshot(


            page,


            "open_first_product_edit",


            row_info.get("message", "Could not find the first editable product row on current Dianxiaomi page."),


            logger=logger,


            state=state,


            extra={"row_info": row_info},


        )





    screenshot_path = take_screenshot(page, "dxm_first_row_before_edit")


    _log(


        logger,


        "open_first_product_edit",


        "start",


        "Opening edit page from the first Dianxiaomi list row.",


        page=page,


        screenshot_path=screenshot_path,


        extra={"first_row": row_info},


    )


    if state:


        state.update(dxm_first_row=row_info, dxm_first_row_screenshot=screenshot_path)





    context = page.context


    before_pages = list(context.pages)


    clicked = _click_first_row_edit_action(page)


    if not clicked:


        fail_with_popup_and_screenshot(


            page,


            "open_first_product_edit",


            "Found the first product row, but could not click an edit/create-product action.",


            logger=logger,


            state=state,


            extra={"first_row": row_info},


        )





    page.wait_for_timeout(2500)


    new_pages = [candidate for candidate in context.pages if candidate not in before_pages]


    target = new_pages[-1] if new_pages else page


    try:


        target.bring_to_front()


    except Exception:


        pass


    _wait_ready(target)





    for _ in range(12):


        if _is_edit_page(target):


            _log(


                logger,


                "open_first_product_edit",


                "ok",


                f"Entered Dianxiaomi edit page from first row: {target.url}",


                page=target,


                extra={"first_row": row_info},


            )


            if state:


                state.update(last_edit_url=target.url, dxm_first_row=row_info)


            return target


        target.wait_for_timeout(1000)





    fail_with_popup_and_screenshot(


        target,


        "open_first_product_edit",


        "Clicked the first row edit entry, but the resulting page was not recognized as a Dianxiaomi edit page.",


        logger=logger,


        state=state,


        extra={"first_row": row_info, "target_url": getattr(target, "url", "")},


    )





    fail_with_popup_and_screenshot(


        page,


        "open_existing_dxm_product",


        "Please open the target Dianxiaomi product edit page before running dxm-publish-twice.",


        logger=logger,


        state=state,


    )








def _await_precomputed_title_from_context(
    page: Any,
    ctx: dict[str, Any],
    prefix: str,
    logger: Any | None = None,
    timeout_seconds: int = 120,
) -> str:
    future = ctx.pop(f"_{prefix}_title_future", None)
    if future is not None and hasattr(future, "result"):
        try:
            title = str(future.result(timeout=timeout_seconds) or "").strip()
            if title:
                ctx[f"{prefix}_precomputed_title"] = title
                ctx[f"{prefix}_title_ai_finished_at"] = datetime.datetime.now().isoformat()
                _log(logger, f"{prefix}_title_generation_finished", "ok", "AI title rewrite returned before publishing.", page=page)
                return title
        except Exception as exc:
            ctx[f"{prefix}_title_ai_error"] = str(exc)
            _log(logger, f"{prefix}_title_generation_finished", "warning", f"AI title rewrite was not ready: {exc}", page=page)
    return str(ctx.get(f"{prefix}_precomputed_title") or "").strip()


def run_first_publish_edit(


    page: Any,


    config: dict[str, Any],


    context: dict[str, Any] | None = None,


    logger: Any | None = None,


    state: Any | None = None,


) -> dict[str, Any]:


    ctx = context or {}


    ctx["first_title_before"] = str(ctx.get("first_title_before") or "").strip() or _safe_read_title(page)


    original_title = ctx["first_title_before"]


    _log(logger, "first_edit_start", "start", f"????��??????????={original_title[:120]}??URL={page.url}", page=page)





    images = ensure_and_shuffle_product_images(page, "first_publish", logger=logger, state=state)


    if images.get("status") not in {"ok", "skipped"}:


        fail_with_popup_and_screenshot(


            page,


            "image_shuffle_done",


            images.get("message", "Image shuffle failed."),


            logger=logger,


            state=state,


            extra={"image_shuffle": images},


        )


    _log(


        logger,


        "image_shuffle_done",


        images.get("status", "unknown"),


        f"?????????: {images}",


        page=page,


        screenshot_path=images.get("screenshot_path", ""),


    )





    prices = increase_sku_prices(page, amount=10, logger=logger)


    if prices.get("status") != "ok":


        fail_with_popup_and_screenshot(


            page,


            "sku_price_increased",


            prices.get("message", "SKU price increase failed."),


            logger=logger,


            state=state,


            extra={"sku_price_increase": prices},


        )


    _log(


        logger,


        "sku_price_increased",


        "ok",


        f"SKU ????????: count={len(prices.get('items', []))}, already_increased={prices.get('already_increased', False)}",


        page=page,


        screenshot_path=prices.get("screenshot_path", ""),


    )





    sku_run_suffix = str(ctx.setdefault("sku_run_suffix", datetime.datetime.now().strftime("%H%M%S")))[-4:]
    first_sku_suffix = append_second_publish_sku_suffix(page, suffix=f"-A{sku_run_suffix}", logger=logger)
    precomputed_title = _await_precomputed_title_from_context(page, ctx, "first", logger=logger)


    publish = run_publish_current_edit_page(


        page,


        config,


        logger=logger,


        state=state,


        product_context={
            "source": "dxm_publish_twice_first",
            "original_title": original_title,
            "precomputed_title": precomputed_title,
        },


    )


    publish["image_shuffle"] = images


    publish["sku_price_increase"] = prices
    publish["sku_suffix"] = first_sku_suffix


    if publish.get("status") in {"manual_required", "error", "failed"}:


        fail_with_popup_and_screenshot(


            page,


            "first_publish_clicked",


            f"????��?????????��?? {publish.get('status')}",


            logger=logger,


            state=state,


            extra={"publish": publish},


        )
    elif publish.get("status") not in PUBLISH_SUCCESS_STATUSES:
        _log(
            logger,
            "first_publish_clicked",
            "warning",
            f"First publish status is {publish.get('status')}; treating as submitted and deferring success to backend verification.",
            page=page,
            extra={"publish": publish},
        )


    # Record first publish context


    ctx["first_title_after"] = publish.get("title") or _safe_read_title(page)


    ctx["first_publish_submitted"] = True


    ctx["first_publish_time"] = datetime.datetime.now().isoformat()


    ctx["first_publish_status"] = publish.get("status", "unknown")


    return publish











def append_second_publish_sku_suffix(page: Any, suffix: str = "-2", logger: Any | None = None) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """(suffix) => {
                const visibleEnabled = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && !el.disabled;
                const setValue = (input, value) => {
                    const proto = input.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                    if (setter) setter.call(input, value);
                    else input.value = value;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    input.dispatchEvent(new Event('blur', {bubbles: true}));
                };
                const fields = Array.from(document.querySelectorAll('input[name="variationSku"], textarea[name="variationSku"]')).filter(visibleEnabled);
                const items = [];
                for (const field of fields) {
                    const oldValue = String(field.value || '').trim();
                    if (!oldValue) {
                        items.push({old: oldValue, new: oldValue, status: 'empty'});
                        continue;
                    }
                    if (oldValue.toUpperCase().endsWith(String(suffix).toUpperCase())) {
                        items.push({old: oldValue, new: oldValue, status: 'already_suffixed'});
                        continue;
                    }
                    let base = oldValue.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/-+/g, '-').replace(/^[-_]+|[-_]+$/g, '');
                    base = base.replace(/-[AB][0-9]{3,8}$/i, '');
                    base = base.slice(0, 76);
                    const newValue = (base || 'SKU') + suffix;
                    setValue(field, newValue);
                    items.push({old: oldValue, new: newValue, status: 'ok'});
                }
                return {status: fields.length ? 'ok' : 'skipped', suffix, items};
            }""",
            suffix,
        )
        _log(logger, "second_sku_suffix", result.get("status", "unknown"), f"Second publish SKU suffix result: {result}", page=page)
        return result
    except Exception as exc:
        return {"status": "failed", "message": str(exc), "suffix": suffix, "items": []}


def run_second_publish_edit(


    page: Any,


    config: dict[str, Any],


    context: dict[str, Any] | None = None,


    logger: Any | None = None,


    state: Any | None = None,


) -> dict[str, Any]:


    ctx = context or {}


    _close_continue_edit_modal(page, logger=logger, state=state)


    ctx["second_title_before"] = _safe_read_title(page)


    original_title = ctx["second_title_before"]


    _log(logger, "second_edit_start", "start", f"????��??????????={original_title[:120]}??URL={page.url}", page=page)





    images = ensure_and_shuffle_product_images(
        page,
        "second_publish",
        logger=logger,
        state=state,
        set_800px=False,
        ensure_variant_preview=False,
    )


    if images.get("status") not in {"ok", "skipped"}:


        fail_with_popup_and_screenshot(


            page,


            "second_image_shuffle_done",


            images.get("message", "Second publish image selection/shuffle failed."),


            logger=logger,


            state=state,


            extra={"image_shuffle": images},


        )





    category = change_to_sibling_category(page, logger=logger)


    if category.get("status") != "ok":


        fail_with_popup_and_screenshot(


            page,


            "category_changed",


            category.get("message", "Category change failed."),


            logger=logger,


            state=state,


            extra={"category": category},


        )





    title_result = shorten_product_title(page, logger=logger)


    if title_result.get("status") != "ok":


        fail_with_popup_and_screenshot(


            page,


            "title_shortened",


            title_result.get("message", "Title shortening failed."),


            logger=logger,


            state=state,


            extra={"title_result": title_result},


        )


    # Record short title


    ctx["second_title_after"] = title_result.get("new_title") or _safe_read_title(page)





    product_id = _extract_product_id(page.url)


    origin_result = select_origin_country_and_province(page, config, logger=logger, state=state)


    sku_result = process_sku_fields(page, config, logger=logger, state=state)
    sku_run_suffix = str(ctx.setdefault("sku_run_suffix", datetime.datetime.now().strftime("%H%M%S")))[-4:]
    sku_suffix_result = append_second_publish_sku_suffix(page, suffix=f"-B{sku_run_suffix}", logger=logger)


    dimensions = fill_variant_dimensions_and_weight(page, config, logger=logger)


    product_data = {


        "product_id": product_id,


        "title": ctx.get("second_title_after") or _safe_read_title(page),


        "original_title": original_title,


        "sku_items": sku_result.get("items", []),


        "url": page.url,


        "source": "dxm_publish_twice_second",


    }


    package = fill_package_info_required(page, product_id, config, logger=logger)


    attributes = fill_required_product_attributes(page, product_data, config, logger=logger)


    description_image = ensure_product_description_image_module(page, product_data, config, logger=logger)


    extra_attributes = fill_visible_required_attribute_selects(page, logger=logger)





    for step, item in (


        ("second_origin", origin_result),


        ("second_sku", sku_result),


        ("second_sku_suffix", sku_suffix_result),


        ("second_dimensions", dimensions),


    ):


        if item.get("status") != "ok":


            fail_with_popup_and_screenshot(


                page,


                step,


                f"????��??????????????: {step}",


                logger=logger,


                state=state,


                extra={step: item},


            )





    errors = scan_required_errors(page)


    if errors:


        fail_with_popup_and_screenshot(


            page,


            "second_required_fields_checked",


            f"Second publish still has {len(errors)} required field error(s); publishing is blocked.",


            logger=logger,


            state=state,


            extra={"required_errors": errors, "required_errors_count": len(errors)},


        )


    _log(logger, "second_required_fields_checked", "ok", "Second publish required field scan found 0 errors.", page=page)





    click = click_publish_button(page, config, logger=logger)


    if click.get("status") not in {"ok", "unknown"}:


        fail_with_popup_and_screenshot(


            page,


            "second_publish_clicked",


            f"????��????????? {click.get('message', click.get('status'))}",


            logger=logger,


            state=state,


            extra={"click_publish": click},


        )





    dialogs = handle_publish_dialogs(page, logger=logger)


    if dialogs.get("status") == "manual_required":


        fail_with_popup_and_screenshot(


            page,


            "second_publish_dialogs",


            "Second publish dialog requires manual handling; stopping flow.",


            logger=logger,


            state=state,


            extra={"dialogs": dialogs},


        )





    publish_status = verify_publish_status(page, product_data, config, logger=logger)


    screenshot_path = take_screenshot(page, "dxm_second_publish_result")


    result = {


        "status": publish_status.get("status", "unknown"),


        "product_id": product_id,
        "image_shuffle": images,


        "category": category,


        "title": title_result,


        "origin": origin_result,


        "sku": sku_result,


        "sku_suffix": sku_suffix_result,


        "dimensions": dimensions,


        "package": package,


        "attributes": attributes,


        "description_image": description_image,


        "extra_attributes": extra_attributes,


        "click_publish": click,


        "dialogs": dialogs,


        "publish_status": publish_status,


        "screenshot_path": screenshot_path,


    }


    # Record second publish as submitted only


    ctx["second_publish_submitted"] = True


    ctx["second_publish_time"] = datetime.datetime.now().isoformat()


    ctx["second_publish_status"] = publish_status.get("status", "unknown")


    return result











def _scroll_to_product_images_section(page: Any) -> dict[str, Any]:


    try:


        result = page.evaluate(


            """() => {
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    return !!(r.width || r.height || el.getClientRects().length);
                };
                const candidates = Array.from(document.querySelectorAll('body *'))
                    .filter(visible)
                    .map((el) => ({el, text: textOf(el), rect: el.getBoundingClientRect()}))
                    .filter((item) => {
                        if (!item.text || item.text.length > 120) return false;
                        if (/图片检测|图片空间|上传文件/.test(item.text)) return false;
                        return /产品轮播图|产品图片|商品图片|主图|素材图|轮播图|图片/.test(item.text);
                    })
                    .sort((a, b) => a.rect.y - b.rect.y);
                const preferred =
                    candidates.find((item) => /产品轮播图|产品图片|商品图片|主图|轮播图/.test(item.text)) ||
                    candidates.find((item) => /素材图/.test(item.text)) ||
                    candidates[0];
                if (!preferred) return {ok: false, message: 'image section label not found'};
                preferred.el.scrollIntoView({block: 'start', inline: 'nearest'});
                window.scrollBy(0, -120);
                return {ok: true, text: preferred.text.slice(0, 120)};
            }"""


        )


        page.wait_for_timeout(900)


        return result if isinstance(result, dict) else {"ok": False}


    except Exception as exc:


        return {"ok": False, "message": str(exc)}





def ensure_and_shuffle_product_images(


    page: Any,


    stage: str,


    logger: Any | None = None,


    state: Any | None = None,


    set_800px: bool = True,


    ensure_variant_preview: bool = True,


) -> dict[str, Any]:


    _scroll_to_product_images_section(page)


    before = _mark_product_image_cards(page)


    before_screenshot = take_screenshot(page, f"images_{stage}_before")


    select_result = select_available_images_up_to_limit(page, stage=stage, limit=10, logger=logger)


    selected = _mark_product_image_cards(page)





    if int(selected.get("selected_count", 0)) < int(before.get("selected_count", 0)):


        return {


            "status": "failed",


            "message": "Image selection count decreased; stopping to avoid deleting/clearing images.",


            "before": before,


            "after_select": selected,


            "before_screenshot": before_screenshot,


        }





    shuffle = _shuffle_selected_product_images(page, stage=stage, seed=f"{stage}:{_safe_read_title(page)}:{page.url}")


    edit_800 = (
        _set_selected_images_square_800(page, stage=stage, logger=logger)
        if set_800px
        else {"status": "skipped", "message": "800px image generation skipped for this stage."}
    )


    variant_preview_800 = (
        _ensure_variant_preview_images_square_800(page, stage=stage, logger=logger)
        if ensure_variant_preview
        else {"status": "skipped", "message": "Variant preview 800px check skipped for this stage."}
    )


    child_check = _check_selected_images_for_child_keywords(page, stage=stage, logger=logger)


    if child_check.get("status") == "failed":


        return {


            "status": "failed",


            "message": "Selected image child-related keyword detected; stopping before publish.",


            "before": before,


            "after_select": selected,


            "shuffle": shuffle,


            "set_800px": edit_800,


            "child_check": child_check,


            "variant_preview_800": variant_preview_800,


            "before_screenshot": before_screenshot,


        }


    final = _mark_product_image_cards(page)


    after_screenshot = take_screenshot(page, f"images_{stage}_after")


    if int(final.get("selected_count", 0)) < int(selected.get("selected_count", 0)):


        return {


            "status": "failed",


            "message": "Image count decreased after shuffle; stopping before publish.",


            "before": before,


            "after_select": selected,


            "final": final,


            "before_screenshot": before_screenshot,


            "after_screenshot": after_screenshot,


        }





    result = {


        "status": shuffle.get("status", "unknown"),


        "stage": stage,


        "available_before": before.get("available_count", 0),


        "selected_before": before.get("selected_count", 0),


        "available_after_select": selected.get("available_count", 0),


        "selected_after_select": selected.get("selected_count", 0),


        "selected_final": final.get("selected_count", 0),


        "select_result": select_result,


        "shuffle": shuffle,


        "set_800px": edit_800,


        "set_800px_status": edit_800.get("status", "unknown"),


        "child_check": child_check,


        "variant_preview_800": variant_preview_800,


        "variant_preview_800_status": variant_preview_800.get("status", "unknown"),


        "child_check_status": child_check.get("status", "unknown"),


        "child_check_result": child_check.get("result", "unknown"),


        "before_order": before.get("selected_order", []),


        "after_order": final.get("selected_order", []),


        "before_screenshot": before_screenshot,


        "screenshot_path": after_screenshot,


    }


    if variant_preview_800.get("status") == "failed":


        result["status"] = "failed"


        result["message"] = variant_preview_800.get("message", "Variant preview image 1:1/800 handling failed.")


    elif shuffle.get("status") == "skipped":


        result["status"] = "skipped"


    elif shuffle.get("status") != "ok":


        result["status"] = "failed"


        result["message"] = shuffle.get("message", "Image shuffle failed.")


    else:


        result["status"] = "ok"


    _log(


        logger,


        "image_shuffle_done",


        result["status"],


        f"{stage} image handling: selected {result['selected_before']} -> {result['selected_final']} of {result['available_before']} available.",


        page=page,


        screenshot_path=after_screenshot,


        extra=result,


    )


    if state:


        state.update(**{f"images_{stage}": result})


    return result








def _set_selected_images_square_800(page: Any, stage: str = "", logger: Any | None = None) -> dict[str, Any]:
    try:
        texts = {
            "carousel": "\u4ea7\u54c1\u8f6e\u64ad\u56fe",
            "edit": "\u7f16\u8f91\u56fe\u7247",
            "batch_a": "\u6279\u91cf\u6539\u56fe\u7247\u5c3a\u5bf8",
            "batch_b": "\u6279\u91cf\u7f16\u8f91\u5c3a\u5bf8",
            "change_to": "\u53d8\u5316\u81f3",
            "jpg_a": "\u751f\u6210JPG\u56fe\u7247",
            "jpg_b": "\u751f\u6210jpg\u56fe\u7247",
            "custom_ratio": "\u81ea\u5b9a\u4e49\u6bd4\u4f8b\u8c03\u6574",
            "custom_size": "\u81ea\u5b9a\u4e49\u5bbd\u9ad8",
        }
        clicked = page.evaluate(
            """(t) => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const clickLikeUser = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mouseover', 'mouseenter', 'mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                };
                const label = Array.from(document.querySelectorAll('*')).filter(visible).find((el) => textOf(el).includes(t.carousel));
                if (label) {
                    label.scrollIntoView({block: 'center', inline: 'nearest'});
                    window.scrollBy(0, -80);
                }
                const spans = Array.from(document.querySelectorAll('span.m-left5.m-right5')).filter(visible)
                    .map((el) => ({el, text: textOf(el), rect: el.getBoundingClientRect()}));
                let target = spans.find((item) => item.text.includes(t.edit));
                if (!target) {
                    const all = Array.from(document.querySelectorAll('span, a, button, [role="button"]')).filter(visible)
                        .map((el) => {
                            const rect = el.getBoundingClientRect();
                            return {el, text: textOf(el), rect, area: rect.width * rect.height};
                        })
                        .filter((item) => item.text.includes(t.edit))
                        .sort((a, b) => a.area - b.area);
                    target = all[0];
                }
                if (!target) return {ok: false, reason: 'edit_image_button_not_found', spans: spans.map((item) => item.text)};
                clickLikeUser(target.el);
                return {ok: true, text: target.text, x: target.rect.x, y: target.rect.y};
            }""",
            texts,
        )
        if not isinstance(clicked, dict) or not clicked.get("ok"):
            return {"status": "skipped", "message": str(clicked)}
        page.wait_for_timeout(1000)
        batch_clicked = page.evaluate(
            """(t) => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const clickLikeUser = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mouseover', 'mouseenter', 'mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                };
                const selectors = ['.ant-dropdown-menu-item', '.ant-dropdown li', '.ant-dropdown-menu-title-content', '.dropdown-menu li', '[role="menuitem"]'];
                const items = [];
                for (const selector of selectors) {
                    for (const el of Array.from(document.querySelectorAll(selector)).filter(visible)) {
                        const text = textOf(el);
                        const rect = el.getBoundingClientRect();
                        if ((text.includes(t.batch_a) || text.includes(t.batch_b)) && rect.width < 400 && rect.height < 80) {
                            items.push({el, text, rect, area: rect.width * rect.height, selector});
                        }
                    }
                }
                const target = items.sort((a, b) => a.area - b.area)[0];
                if (!target) {
                    const menus = Array.from(document.querySelectorAll('.ant-dropdown,.ant-dropdown-menu')).filter(visible).map((el) => textOf(el));
                    return {ok: false, reason: 'batch_size_item_not_found', menus};
                }
                clickLikeUser(target.el);
                return {ok: true, text: target.text, selector: target.selector, x: target.rect.x, y: target.rect.y};
            }""",
            texts,
        )
        if not isinstance(batch_clicked, dict) or not batch_clicked.get("ok"):
            return {"status": "skipped", "button": clicked, "batch": batch_clicked, "message": "batch image size item not found"}
        page.wait_for_timeout(1500)
        python_selects: list[dict[str, Any]] = []
        try:
            page.locator(".ant-modal:visible .ant-select").nth(0).click(timeout=5000)
            page.wait_for_timeout(500)
            page.locator('.ant-select-dropdown:visible .ant-select-item-option[title="自定义比例调整"]').last.click(timeout=5000)
            page.wait_for_timeout(900)
            python_selects.append({"target": "自定义比例调整", "ok": True})
        except Exception as exc:
            python_selects.append({"target": "自定义比例调整", "ok": False, "message": str(exc)})
        try:
            page.locator(".ant-modal:visible .ant-select").nth(1).click(timeout=5000)
            page.wait_for_timeout(500)
            page.locator('.ant-select-dropdown:visible .ant-select-item-option[title="自定义宽高"]').last.click(timeout=5000)
            page.wait_for_timeout(900)
            python_selects.append({"target": "自定义宽高", "ok": True})
        except Exception as exc:
            python_selects.append({"target": "自定义宽高", "ok": False, "message": str(exc)})
        configured = page.evaluate(
            """async (t) => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const setValue = (input, value) => {
                    input.focus();
                    const proto = Object.getPrototypeOf(input);
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && desc.set) desc.set.call(input, value);
                    else input.value = value;
                    input.dispatchEvent(new Event('input', {bubbles: true}));
                    input.dispatchEvent(new Event('change', {bubbles: true}));
                    input.dispatchEvent(new Event('blur', {bubbles: true}));
                };
                const clickLikeUser = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mouseover', 'mouseenter', 'mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                };
                const selectOption = async (select, targetText) => {
                    if (!select || !targetText) return {ok: false, reason: 'missing_select_or_target'};
                    clickLikeUser(select);
                    await sleep(600);
                    const options = Array.from(document.querySelectorAll('.ant-select-dropdown .ant-select-item-option')).filter(visible)
                        .map((el) => ({el, text: textOf(el), title: el.getAttribute('title') || ''}));
                    const option = options.find((item) => item.text.includes(targetText) || item.title === targetText);
                    if (!option) return {ok: false, reason: 'option_not_found', target: targetText, options: options.map((item) => item.text || item.title).slice(0, 20)};
                    clickLikeUser(option.el);
                    await sleep(800);
                    return {ok: true, target: targetText};
                };
                const roots = Array.from(document.querySelectorAll('.ant-modal, .ant-drawer, .el-dialog, .modal, [role="dialog"]')).filter(visible);
                const root = roots.find((el) => textOf(el).includes(t.change_to) && (textOf(el).includes(t.jpg_a) || textOf(el).includes(t.jpg_b)));
                if (!root) return {ok: false, reason: 'batch_size_modal_not_found'};
                const selects = Array.from(root.querySelectorAll('.ant-select')).filter(visible);
                const ratioSelect = selects[0] || null;
                const ratioResult = await selectOption(ratioSelect, t.custom_ratio);
                const refreshedRoot = Array.from(document.querySelectorAll('.ant-modal, .ant-drawer, .el-dialog, .modal, [role="dialog"]')).filter(visible)
                    .find((el) => textOf(el).includes(t.change_to) && (textOf(el).includes(t.jpg_a) || textOf(el).includes(t.jpg_b))) || root;
                const refreshedSelects = Array.from(refreshedRoot.querySelectorAll('.ant-select')).filter(visible);
                const sizeSelect = refreshedSelects[1] || null;
                const sizeResult = await selectOption(sizeSelect, t.custom_size);
                const inputs = Array.from(refreshedRoot.querySelectorAll('input.ant-input, input')).filter(visible)
                    .filter((input) => !['file', 'checkbox', 'radio', 'search'].includes(input.type));
                if (!inputs.length) return {ok: false, reason: 'input_not_found', inputCount: inputs.length, rootText: textOf(refreshedRoot).slice(0, 500), ratioResult, sizeResult};
                for (const input of inputs) setValue(input, '800');
                const buttons = Array.from(refreshedRoot.querySelectorAll('button.ant-btn-primary, button, a')).filter(visible)
                    .map((el) => ({el, text: textOf(el), cls: String(el.className || ''), rect: el.getBoundingClientRect()}));
                const button = buttons.find((item) => item.text.includes(t.jpg_a) || item.text.includes(t.jpg_b));
                if (!button) return {ok: false, reason: 'jpg_button_not_found', filled: inputs.map((input) => input.value), buttons: buttons.map((item) => item.text).filter(Boolean).slice(0, 30), rootText: textOf(refreshedRoot).slice(0, 800), ratioResult, sizeResult};
                clickLikeUser(button.el);
                await sleep(9000);
                const finalModal = Array.from(document.querySelectorAll('.ant-modal')).filter(visible)
                    .find((el) => textOf(el).includes(t.jpg_a) || textOf(el).includes(t.jpg_b)) || refreshedRoot;
                const imageSizes = Array.from(finalModal.querySelectorAll('img')).filter(visible)
                    .map((img) => ({width: img.naturalWidth, height: img.naturalHeight, text: textOf(img.parentElement || img)}))
                    .slice(0, 30);
                return {ok: true, filled: inputs.map((input) => input.value), clicked: button.text, x: button.rect.x, y: button.rect.y, ratioResult, sizeResult, imageSizes};
            }""",
            texts,
        )
        close_result: dict[str, Any] = {}
        if isinstance(configured, dict) and configured.get("ok"):
            page.wait_for_timeout(2500)
            close_result = page.evaluate(
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
                            return text.includes('批量改图片尺寸') || text.includes('生成JPG图片') || text.includes('生成PNG图片') || text.includes('变化至');
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
                    return {ok: true, closed};
                }"""
            )
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(800)
        status = "ok" if isinstance(configured, dict) and configured.get("ok") else "skipped"
        result = {"status": status, "stage": stage, "button": clicked, "batch": batch_clicked, "python_selects": python_selects, "configured": configured, "modal_close": close_result}
        _log(logger, "image_edit_800", status, f"{stage} image 1:1/800 edit result: {result}", page=page)
        return result
    except Exception as exc:
        return {"status": "failed", "stage": stage, "message": str(exc)}


def _ensure_variant_preview_images_square_800(page: Any, stage: str = "", logger: Any | None = None) -> dict[str, Any]:
    """Replace SKU/variant preview images with generated 800x800 carousel images."""
    try:
        def read_state() -> list[dict[str, Any]]:
            value = page.evaluate(
                """() => {
                    const visible = (el) => !!el && el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
                    return Array.from(document.querySelectorAll('.sku-image-box img')).filter(visible).map((img, index) => ({
                        index,
                        src: img.currentSrc || img.src || '',
                        width: img.naturalWidth || 0,
                        height: img.naturalHeight || 0,
                    }));
                }"""
            )
            return value if isinstance(value, list) else []

        before = read_state()
        bad_indices = [
            int(item.get("index", 0))
            for item in before
            if item.get("src") and (int(item.get("width") or 0) != int(item.get("height") or 0) or int(item.get("width") or 0) < 800 or int(item.get("height") or 0) < 800)
        ]
        actions: list[dict[str, Any]] = []
        if not bad_indices:
            result = {"status": "ok", "stage": stage, "before": before, "after": before, "actions": actions, "message": "variant preview images already square"}
            _log(logger, "variant_preview_image_800", "ok", f"{stage} variant preview images already square.", page=page, extra=result)
            return result

        for index in bad_indices[:20]:
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            try:
                box = page.locator(".sku-image-box.ant-dropdown-trigger").nth(index)
                box.scroll_into_view_if_needed(timeout=3000)
                box.click(timeout=5000, force=True)
                page.wait_for_timeout(500)
            except Exception as exc:
                actions.append({"index": index, "status": "failed", "step": "open_dropdown", "message": str(exc)})
                continue

            menu_result = page.evaluate(
                """() => {
                    const visible = (el) => !!el && el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
                    const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const target = '\\u5f15\\u7528\\u4ea7\\u54c1\\u8f6e\\u64ad\\u56fe';
                    const items = Array.from(document.querySelectorAll('.ant-dropdown-menu-item, .ant-dropdown-menu-title-content, li, span, div')).filter(visible);
                    const el = items.find((item) => textOf(item) === target) || items.find((item) => textOf(item).includes(target));
                    if (!el) return {ok: false, reason: 'reference_carousel_item_not_found', texts: items.map(textOf).filter(Boolean).slice(0, 80)};
                    for (const type of ['mouseover', 'mouseenter', 'mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                    return {ok: true, text: textOf(el)};
                }"""
            )
            if not isinstance(menu_result, dict) or not menu_result.get("ok"):
                actions.append({"index": index, "status": "failed", "step": "reference_carousel", "result": menu_result})
                continue

            page.wait_for_timeout(900)
            choose_result = page.evaluate(
                """() => {
                    const visible = (el) => !!el && el.offsetParent !== null && getComputedStyle(el).visibility !== 'hidden';
                    const textOf = (el) => (el?.innerText || el?.textContent || '').replace(/\\s+/g, ' ').trim();
                    const modalTitle = '\\u5f15\\u7528\\u4ea7\\u54c1\\u56fe\\u7247';
                    const chooseImage = '\\u9009\\u62e9\\u56fe\\u7247';
                    const choose = '\\u9009\\u62e9';
                    const modal = Array.from(document.querySelectorAll('.ant-modal')).filter((el) => visible(el) && textOf(el).includes(modalTitle)).pop();
                    if (!modal) return {ok: false, reason: 'reference_image_modal_not_found'};
                    const labels = Array.from(modal.querySelectorAll('label.ant-checkbox-wrapper')).filter(visible)
                        .filter((label) => textOf(label).includes(chooseImage))
                        .map((label, idx) => {
                            const img = label.querySelector('img');
                            return {label, idx, src: img ? (img.currentSrc || img.src || '') : '', width: img ? (img.naturalWidth || 0) : 0, height: img ? (img.naturalHeight || 0) : 0};
                        });
                    const target = labels.find((item) => item.width === item.height && item.width >= 800) || labels[0];
                    if (!target) return {ok: false, reason: 'no_reference_images'};
                    target.label.scrollIntoView({block: 'center', inline: 'center'});
                    for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
                        target.label.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                    const button = Array.from(modal.querySelectorAll('button')).filter(visible)
                        .find((btn) => textOf(btn) === choose)
                        || Array.from(modal.querySelectorAll('button')).filter(visible).find((btn) => textOf(btn).includes(choose));
                    if (!button) return {ok: false, reason: 'choose_button_not_found', selected: {src: target.src, width: target.width, height: target.height}};
                    for (const type of ['mouseover', 'mousedown', 'mouseup', 'click']) {
                        button.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                    return {ok: true, selected: {src: target.src, width: target.width, height: target.height}, button: textOf(button)};
                }"""
            )
            actions.append({"index": index, "status": "ok" if isinstance(choose_result, dict) and choose_result.get("ok") else "failed", "menu": menu_result, "choose": choose_result})
            page.wait_for_timeout(2500)

        after = read_state()
        remaining = [
            item
            for item in after
            if item.get("src") and (int(item.get("width") or 0) != int(item.get("height") or 0) or int(item.get("width") or 0) < 800 or int(item.get("height") or 0) < 800)
        ]
        status = "ok" if not remaining else "failed"
        result = {
            "status": status,
            "stage": stage,
            "before": before,
            "after": after,
            "actions": actions,
            "remaining_bad": remaining,
            "message": "variant preview images updated to 800x800" if status == "ok" else "variant preview images still not 1:1/800",
        }
        _log(logger, "variant_preview_image_800", status, f"{stage} variant preview image result: {result}", page=page, extra=result)
        return result
    except Exception as exc:
        result = {"status": "failed", "stage": stage, "message": str(exc)}
        _log(logger, "variant_preview_image_800", "failed", str(exc), page=page, extra=result)
        return result


def _check_selected_images_for_child_keywords(page: Any, stage: str = "", logger: Any | None = None) -> dict[str, Any]:
    try:
        result = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const cards = Array.from(document.querySelectorAll('[data-dxm-image-selected="1"]')).filter(visible);
                const words = ['child', 'children', 'kids', 'kid', 'toddler', 'baby', 'infant', 'boy', 'girl', 'boys', 'girls'];
                const hits = [];
                for (const card of cards) {
                    const text = textOf(card);
                    const img = card.querySelector('img');
                    const src = img ? (img.currentSrc || img.src || '') : '';
                    const alt = img ? (img.alt || img.title || '') : '';
                    const haystack = `${text} ${src} ${alt}`.toLowerCase();
                    const matched = words.filter((word) => haystack.includes(word));
                    if (matched.length) hits.push({matched, src, text: text.slice(0, 200)});
                }
                return {checked_count: cards.length, hits};
            }"""
        )
        hits = result.get("hits", []) if isinstance(result, dict) else []
        status = "failed" if hits else "ok"
        check = {
            "status": status,
            "stage": stage,
            "method": "dom_alt_src_keyword_scan",
            "result": "child_keyword_found" if hits else "no_child_keyword_found",
            "details": result,
        }
        _log(logger, "image_child_check", status, f"{stage} child keyword check: {check['result']}", page=page, extra=check)
        return check
    except Exception as exc:
        return {"status": "warning", "stage": stage, "method": "dom_alt_src_keyword_scan", "result": "unknown", "message": str(exc)}


def select_available_images_up_to_limit(


    page: Any,


    stage: str = "",


    limit: int = 10,


    logger: Any | None = None,


) -> dict[str, Any]:


    before = _mark_product_image_cards(page)


    available = int(before.get("available_count", 0))


    selected = int(before.get("selected_count", 0))


    target = min(limit, available)


    if available <= 0:


        return {"status": "skipped", "message": "No product image cards were detected.", "before": before}


    if selected >= target:


        return {"status": "ok", "selected": selected, "target": target, "available": available, "already_full": True}





    clicked = int(


        page.evaluate(


            """({limit}) => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                const cards = Array.from(document.querySelectorAll('[data-dxm-image-card]'))


                    .filter(visible)


                    .sort((a, b) => Number(a.getAttribute('data-dxm-image-card')) - Number(b.getAttribute('data-dxm-image-card')));


                let selected = cards.filter((card) => card.getAttribute('data-dxm-image-selected') === '1').length;


                let clicked = 0;


                for (const card of cards) {


                    if (selected >= limit) break;


                    if (card.getAttribute('data-dxm-image-selected') === '1') continue;


                    const idx = card.getAttribute('data-dxm-image-card');


                    const action =


                        document.querySelector(`[data-dxm-image-select-action="${idx}"]`) ||


                        card.querySelector('label, input[type="checkbox"], .ant-checkbox, .ant-checkbox-wrapper') ||


                        card;


                    action.scrollIntoView({block: 'center', inline: 'nearest'});


                    action.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));


                    action.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));


                    action.click();


                    selected += 1;


                    clicked += 1;


                }


                return clicked;


            }""",


            {"limit": target},


        )


    )


    page.wait_for_timeout(1200)


    after = _mark_product_image_cards(page)


    _log(


        logger,


        "image_select",


        "ok" if int(after.get("selected_count", 0)) >= selected else "warning",


        f"{stage} selected image count {selected} -> {after.get('selected_count', 0)}; clicked={clicked}; target={target}",


        page=page,


        extra={"before": before, "after": after, "clicked": clicked, "target": target},


    )


    return {"status": "ok", "before": before, "after": after, "clicked": clicked, "target": target}








def get_selected_image_count(page: Any) -> int:


    return int(_mark_product_image_cards(page).get("selected_count", 0))








def get_available_image_count(page: Any) -> int:


    return int(_mark_product_image_cards(page).get("available_count", 0))








def _shuffle_selected_product_images(page: Any, stage: str = "", seed: str = "") -> dict[str, Any]:


    before = _mark_product_image_cards(page)


    count = int(before.get("selected_count", 0))


    if count < 2:


        return {"status": "skipped", "before_count": count, "after_count": count, "message": "Fewer than 2 selected product images; shuffle skipped."}


    limit = min(count, 10)


    rnd = random.Random(int(hashlib.md5((seed or page.url).encode("utf-8", errors="ignore")).hexdigest()[:8], 16))


    max_target = min(4, limit - 1)


    target_index = rnd.randint(1, max_target) if max_target >= 1 else 1


    if stage == "second_publish" and limit > 2:


        target_index = 2 if target_index != 2 else min(3, max_target)


    before_order = before.get("selected_order", [])


    try:


        source = page.locator('[data-dxm-image-selected-card="0"]').first


        target = page.locator(f'[data-dxm-image-selected-card="{target_index}"]').first


        source.scroll_into_view_if_needed(timeout=3000)


        target.scroll_into_view_if_needed(timeout=3000)


        source.drag_to(target, timeout=9000)


        page.wait_for_timeout(1200)


    except Exception as exc:


        screenshot_path = take_screenshot(page, f"images_{stage}_shuffle_failed")


        return {


            "status": "failed",


            "message": f"Image drag/drop failed: {exc}",


            "before_count": count,


            "after_count": count,


            "screenshot_path": screenshot_path,


        }


    after = _mark_product_image_cards(page)


    after_count = int(after.get("selected_count", 0))


    after_order = after.get("selected_order", [])


    screenshot_path = take_screenshot(page, f"images_{stage}_shuffle_done")


    if after_count != count:


        return {


            "status": "failed",


            "message": "Selected image count changed after shuffle.",


            "before_count": count,


            "after_count": after_count,


            "screenshot_path": screenshot_path,


        }


    if before_order[:limit] == after_order[:limit]:


        return {


            "status": "failed",


            "message": "Image order did not change after drag/drop.",


            "before_count": count,


            "after_count": after_count,


            "screenshot_path": screenshot_path,


        }


    return {


        "status": "ok",


        "before_count": count,


        "after_count": after_count,


        "moved_from": 0,


        "moved_to": target_index,


        "before_order": before_order,


        "after_order": after_order,


        "screenshot_path": screenshot_path,


    }








def shuffle_product_images(page: Any, seed: str = "", logger: Any | None = None) -> dict[str, Any]:


    before = _mark_product_image_cards(page)


    count = int(before.get("count", 0))


    if count < 2:


        return {"status": "skipped", "before_count": count, "after_count": count, "message": "Fewer than 2 images; shuffle skipped."}





    limit = min(count, 10)


    rnd = random.Random(int(hashlib.md5((seed or page.url).encode("utf-8", errors="ignore")).hexdigest()[:8], 16))


    target_index = rnd.randint(1, min(4, limit - 1))


    before_order = before.get("order", [])





    try:


        source = page.locator('[data-dxm-image-card="0"]').first


        target = page.locator(f'[data-dxm-image-card="{target_index}"]').first


        source.scroll_into_view_if_needed(timeout=3000)


        target.scroll_into_view_if_needed(timeout=3000)


        source.drag_to(target, timeout=8000)


        page.wait_for_timeout(1200)


    except Exception as exc:


        screenshot_path = take_screenshot(page, "image_shuffle_failed")


        return {


            "status": "failed",


            "message": f"????????: {exc}",


            "before_count": count,


            "after_count": count,


            "screenshot_path": screenshot_path,


        }





    after = _mark_product_image_cards(page)


    after_count = int(after.get("count", 0))


    after_order = after.get("order", [])


    screenshot_path = take_screenshot(page, "image_shuffle_done")


    if after_count != count:


        return {


            "status": "failed",


            "message": "Image count changed after shuffle; stopping.",


            "before_count": count,


            "after_count": after_count,


            "screenshot_path": screenshot_path,


        }


    if before_order[:limit] == after_order[:limit]:


        return {


            "status": "failed",


            "message": "Image order did not change after shuffle.",


            "before_count": count,


            "after_count": after_count,


            "screenshot_path": screenshot_path,


        }


    return {


        "status": "ok",


        "before_count": count,


        "after_count": after_count,


        "moved_from": 0,


        "moved_to": target_index,


        "screenshot_path": screenshot_path,


    }








def increase_sku_prices(page: Any, amount: int | float = 10, logger: Any | None = None) -> dict[str, Any]:


    candidates = _price_candidates(page)


    if not candidates:


        screenshot_path = take_screenshot(page, "sku_price_candidates_missing")


        return {"status": "failed", "message": "No clear SKU price input fields found.", "items": [], "screenshot_path": screenshot_path}





    ambiguous = [item for item in candidates if item.get("excluded")]


    if ambiguous and len(candidates) == len(ambiguous):


        screenshot_path = take_screenshot(page, "sku_price_candidates_ambiguous")


        return {


            "status": "failed",


            "message": "Price fields are ambiguous; stopping to avoid editing the wrong fields.",


            "items": candidates,


            "screenshot_path": screenshot_path,


        }





    valid_candidates = [item for item in candidates if not item.get("excluded")]


    previous_updates = _latest_price_updates_for_url(page.url)


    if previous_updates and len(previous_updates) == len(valid_candidates):


        already_done = True


        for candidate, previous in zip(valid_candidates, previous_updates):


            if str(candidate.get("value") or "").strip() != str(previous.get("new_price") or "").strip():


                already_done = False


                break


        if already_done:


            screenshot_path = take_screenshot(page, "sku_price_already_increased")


            _log(


                logger,


                "sku_price_increased",


                "ok",


                f"Current page prices already include +{amount}; skipping duplicate increase.",


                page=page,


                screenshot_path=screenshot_path,


                extra={"items": previous_updates, "already_increased": True},


            )


            return {"status": "ok", "items": previous_updates, "already_increased": True, "screenshot_path": screenshot_path}





    updates: list[dict[str, Any]] = []


    for item in valid_candidates:


        old_raw = str(item.get("value") or "").strip()


        parsed = _parse_price(old_raw)


        if parsed is None:


            screenshot_path = take_screenshot(page, "sku_price_parse_failed")


            return {


                "status": "failed",


                "message": f"??????????: {old_raw}",


                "items": candidates,


                "screenshot_path": screenshot_path,


            }


        decimals = _decimal_places(old_raw)


        new_value = parsed + Decimal(str(amount))


        formatted = f"{new_value:.{decimals if decimals is not None else 2}f}"


        updates.append({"index": item["index"], "old_price": old_raw, "new_price": formatted, "label": item.get("label", "")})





    page.evaluate(


        """(updates) => {


            const setValue = (el, value) => {


                const proto = Object.getPrototypeOf(el);


                const desc = Object.getOwnPropertyDescriptor(proto, 'value');


                if (desc && desc.set) desc.set.call(el, value);


                else el.value = value;


                el.dispatchEvent(new Event('input', {bubbles: true}));


                el.dispatchEvent(new Event('change', {bubbles: true}));


                el.dispatchEvent(new Event('blur', {bubbles: true}));


            };


            for (const item of updates) {


                const el = document.querySelector(`[data-dxm-price-candidate="${item.index}"]`);


                if (el) setValue(el, item.new_price);


            }


        }""",


        updates,


    )


    page.wait_for_timeout(500)


    screenshot_path = take_screenshot(page, "sku_price_increased")


    _log(logger, "sku_price_increased", "ok", f"Updated {len(updates)} SKU price field(s).", page=page, screenshot_path=screenshot_path, extra={"items": updates})


    return {"status": "ok", "items": updates, "screenshot_path": screenshot_path}








def fill_visible_required_attribute_selects(page: Any, logger: Any | None = None) -> dict[str, Any]:


    specs = [
        {"label": "??", "preferred": ["??", "Other", "PVC", "??", "??"]},
        {"label": "????", "preferred": ["???", "????", "??", "??", "USB", "??", "Other"]},
        {"label": "??", "preferred": ["????", "????", "?", "??", "Other"]},
    ]


    filled: list[dict[str, Any]] = []


    for spec in specs:


        opened = _open_attribute_select_by_label(page, spec["label"])


        if not opened.get("opened"):


            filled.append({"label": spec["label"], "status": "not_found", **opened})


            continue


        page.wait_for_timeout(600)


        selected = _select_open_dropdown_option(page, spec["preferred"])


        page.wait_for_timeout(700)


        filled.append({"label": spec["label"], **selected})


    status = "ok" if any(item.get("status") == "ok" for item in filled) else "skipped"


    _log(logger, "fill_visible_required_attribute_selects", status, f"Extra visible attribute selects handled: {filled}", page=page, extra={"items": filled})


    return {"status": status, "items": filled}








def _open_attribute_select_by_label(page: Any, label: str) -> dict[str, Any]:


    try:


        return page.evaluate(


            """(label) => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                const roots = Array.from(document.querySelectorAll('.ant-form-item, .el-form-item, [class*=form-item], tr, .ant-row, div'))


                    .filter(visible)


                    .filter((el) => {


                        const labelNode = el.querySelector('.ant-form-item-label, label, .label, [class*=label]');


                        const labelText = labelNode ? textOf(labelNode) : '';


                        const fullText = textOf(el);


                        return labelText.includes(label) || (fullText.includes(label) && fullText.length < 500);


                    })


                    .sort((a, b) => {


                        const ar = a.getBoundingClientRect();


                        const br = b.getBoundingClientRect();


                        return (ar.height - br.height) || (ar.y - br.y);


                    });


                let root = roots[0];


                if (!root) {


                    const broader = Array.from(document.querySelectorAll('.ant-form-item, .el-form-item, [class*=form-item], tr, .ant-row, div'))


                        .filter(visible)


                        .filter((el) => textOf(el).includes(label))


                        .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y);


                    root = broader[0];


                }


                if (!root) return {opened: false, message: 'root not found: ' + label};


                let control = root.querySelector('.ant-select:not(.ant-select-disabled), .el-select:not(.is-disabled), select:not([disabled])');


                if (!control) control = root.closest('[class*=row]')?.querySelector('.ant-select:not(.ant-select-disabled), .el-select:not(.is-disabled), select:not([disabled])');


                if (!control) control = root.parentElement?.querySelector('.ant-select:not(.ant-select-disabled), .el-select:not(.is-disabled), select:not([disabled])');


                if (!control) {


                    const allSelects = Array.from(document.querySelectorAll('.ant-select:not(.ant-select-disabled), .el-select:not(.is-disabled), select:not([disabled])')).filter(visible);


                    const rootRect = root.getBoundingClientRect();


                    control = allSelects.find((sel) => {


                        const sr = sel.getBoundingClientRect();


                        return Math.abs(sr.y - rootRect.y) < 100;


                    });


                }


                if (!control) return {opened: false, message: 'control not found: ' + label};


                const current = textOf(control);


                if (current && !/\u8bf7\u9009\u62e9|Select/.test(current)) return {opened: false, already_selected: true, value: current};


                control.scrollIntoView({block: 'center', inline: 'nearest'});


                const clickable = control.querySelector('.ant-select-selector, input, .el-input__inner, .el-input') || control;


                clickable.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));


                clickable.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));


                clickable.click();


                return {opened: true};


            }""",


            label,


        )


    except Exception as exc:


        return {"opened": False, "message": str(exc)}











def _select_open_dropdown_option(page: Any, preferred: list[str]) -> dict[str, Any]:


    try:


        return page.evaluate(


            """(preferred) => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                const options = Array.from(document.querySelectorAll(


                    '.ant-select-dropdown:not(.ant-select-dropdown-hidden) .ant-select-item-option-content, .el-select-dropdown__item, [role="option"]'


                )).filter(visible).map((el) => ({el, text: textOf(el)}))


                  .filter((item) => item.text && !/请选择|全部|Select/i.test(item.text));


                if (!options.length) return {status: 'failed', message: 'no visible dropdown options'};


                let target = null;


                for (const value of preferred) {


                    target = options.find((item) => item.text === value) || options.find((item) => item.text.includes(value));


                    if (target) break;


                }


                if (!target) target = options[0];


                target.el.scrollIntoView({block: 'center', inline: 'nearest'});


                target.el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));


                target.el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));


                target.el.click();


                return {status: 'ok', value: target.text};


            }""",


            preferred,


        )


    except Exception as exc:


        return {"status": "failed", "message": str(exc)}








def click_publish_button(page: Any, config: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:


    return click_immediate_publish(page, config, logger=logger)








def click_second_publish_entry(page: Any, logger: Any | None = None, state: Any | None = None) -> Any:


    _log(logger, "second_publish_entry_clicked", "start", "Looking for second publish entry.", page=page)


    context = page.context


    before_pages = list(context.pages)





    clicked = _click_second_publish_text(page)


    opened_continue_menu = False


    if not clicked:


        opened_continue_menu = _click_modal_action_button(page, CONTINUE_PUBLISH_TEXTS)


    if opened_continue_menu:


        page.wait_for_timeout(3000)


        clicked = _click_second_publish_text(page)


        page.wait_for_timeout(3000)


    if not clicked:


        clicked = _click_second_publish_text(page)


    if not clicked:


        _open_publish_dropdown(page)


        page.wait_for_timeout(800)


        clicked = _click_second_publish_text(page)


    if not clicked:


        fail_with_popup_and_screenshot(page, "second_publish_entry_clicked", "Could not find the second publish entry.", logger=logger, state=state)





    page.wait_for_timeout(2500)


    new_pages = [candidate for candidate in context.pages if candidate not in before_pages]


    target = new_pages[-1] if new_pages else page


    _wait_ready(target)





    if _publish_success_modal_visible(target):


        fail_with_popup_and_screenshot(target, "second_publish_entry_clicked", "Second publish entry did not open an edit page.", logger=logger, state=state)


    if not _is_edit_page(target):


        fail_with_popup_and_screenshot(target, "second_edit_start", "After clicking second publish entry, target page is not recognized as an edit page.", logger=logger, state=state)


    _log(logger, "second_publish_entry_clicked", "ok", f"???????��??: {target.url}", page=target)


    if state:


        state.update(second_edit_url=target.url)


    return target








def change_to_sibling_category(page: Any, logger: Any | None = None) -> dict[str, Any]:


    old_category = _read_category_text(page)


    opened = _open_category_selector(page)


    if not opened:


        screenshot_path = take_screenshot(page, "category_selector_missing")


        return {"status": "failed", "old_category": old_category, "new_category": "", "screenshot_path": screenshot_path, "message": "Category selector not found."}





    page.wait_for_timeout(1000)


    selected = _select_sibling_category_option_v2(page, old_category)


    page.wait_for_timeout(1800)


    changed_category = _wait_category_change(page, old_category, timeout_ms=8000)


    if changed_category:


        _close_category_modal_if_open(page)


    new_category = _read_category_text(page)


    screenshot_path = take_screenshot(page, "category_changed")


    if selected and new_category and new_category != old_category:


        _log(logger, "category_changed", "ok", f"???????? {old_category} -> {new_category}", page=page, screenshot_path=screenshot_path)


        return {"status": "ok", "old_category": old_category, "new_category": new_category, "change_status": "auto_success", "screenshot_path": screenshot_path}


    return {


        "status": "failed",


        "old_category": old_category,


        "new_category": new_category,


        "change_status": "failed",


        "screenshot_path": screenshot_path,


        "message": "Could not select a sibling category automatically.",


    }








def shorten_product_title(page: Any, logger: Any | None = None) -> dict[str, Any]:


    original = read_original_title(page, logger=logger)


    short = _ai_short_title(original, logger=logger)


    if not short:


        short = _local_short_title(original)


    short = _clean_short_title(short)


    if not short or contains_chinese(short) or len(short) > 80:


        screenshot_path = take_screenshot(page, "short_title_failed")


        return {"status": "failed", "old_title": original, "new_title": short, "screenshot_path": screenshot_path, "message": "Short title is empty, contains Chinese, or exceeds 80 characters."}


    fill_product_title(page, short, logger=logger)


    screenshot_path = take_screenshot(page, "title_shortened")


    _log(logger, "title_shortened", "ok", f"?????????��: {short}", page=page, screenshot_path=screenshot_path)


    return {"status": "ok", "old_title": original, "new_title": short, "screenshot_path": screenshot_path}








def _mark_first_dxm_list_row(page: Any) -> dict[str, Any]:


    try:


        return page.evaluate(


            """() => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                document.querySelectorAll('[data-dxm-first-row],[data-dxm-first-edit-action],[data-dxm-first-more-action]')


                    .forEach((el) => {


                        el.removeAttribute('data-dxm-first-row');


                        el.removeAttribute('data-dxm-first-edit-action');


                        el.removeAttribute('data-dxm-first-more-action');


                    });


                const rowSelectors = [


                    'tr.ant-table-row',


                    '.ant-table-row',


                    '.vxe-body--row',


                    '.el-table__row',


                    'tbody tr',


                    '[class*="table"] [class*="row"]',


                    '[class*="list"] [class*="item"]'


                ];


                const rows = [];


                for (const selector of rowSelectors) {


                    for (const row of Array.from(document.querySelectorAll(selector)).filter(visible)) {


                        const text = textOf(row);


                        const rect = row.getBoundingClientRect();


                        if (rect.y < 80 || rect.height < 20 || text.length < 8) continue;


                        if (/商品信息|操作|产品标题|图片|全选|店铺/.test(text) && text.length < 60) continue;


                        rows.push(row);


                    }


                    if (rows.length) break;


                }


                const row = rows.sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y)[0];


                if (!row) return {ok: false, message: 'No visible product row found.'};


                row.setAttribute('data-dxm-first-row', '1');





                const actions = Array.from(row.querySelectorAll('button, a, span, div[role="button"]')).filter(visible)


                    .filter((el) => /编辑|修改|查看\\/编辑|同步后编辑|创建产品|创建新产品|复制为|Edit|Modify|Create/i.test(textOf(el)));


                const action = actions.find((el) => !/删除|移除/.test(textOf(el))) || actions[0];


                if (action) action.setAttribute('data-dxm-first-edit-action', '1');





                const more = Array.from(row.querySelectorAll('button, a, span, div[role="button"]')).filter(visible)


                    .find((el) => /更多|操作|下拉|\\.\\.\\./.test(textOf(el)) || /dropdown|more|ellipsis|down/i.test(String(el.className || '')));


                if (more) more.setAttribute('data-dxm-first-more-action', '1');





                const img = Array.from(row.querySelectorAll('img')).filter(visible)[0];


                const lines = textOf(row).split(/\\s+/).filter(Boolean);


                const actionWords = /编辑|修改|删除|更多|操作|价格|库存|状态|店铺|时间|SKU/;


                const title = lines.find((line) => line.length >= 6 && !actionWords.test(line)) || lines[0] || '';


                const rowText = textOf(row);
                const skuLabelMatch = rowText.match(/(?:SKU|sku|\u8d27\u53f7|\u5546\u54c1\u7f16\u7801)[:\uFF1A]?\\s*([A-Za-z0-9_-]{4,})/i);
                const sku = (skuLabelMatch || [])[1] || '';


                return {


                    ok: true,


                    title,


                    sku,


                    image_src: img ? (img.currentSrc || img.src || '') : '',


                    row_text_preview: textOf(row).slice(0, 500),


                    has_direct_edit: !!action,


                    has_more: !!more


                };


            }"""


        )


    except Exception as exc:


        return {"ok": False, "message": f"Failed to inspect Dianxiaomi list row: {exc}"}








def _click_first_row_edit_action(page: Any) -> bool:


    try:


        clicked = bool(


            page.evaluate(


                """() => {


                    const target = document.querySelector('[data-dxm-first-edit-action="1"]');


                    if (!target) return false;


                    target.scrollIntoView({block: 'center', inline: 'nearest'});


                    target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));


                    target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));


                    target.click();


                    return true;


                }"""


            )


        )


        if clicked:


            return True


    except Exception:


        pass





    try:


        opened_more = bool(


            page.evaluate(


                """() => {


                    const target = document.querySelector('[data-dxm-first-more-action="1"]');


                    if (!target) return false;


                    target.scrollIntoView({block: 'center', inline: 'nearest'});


                    target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));


                    target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));


                    target.click();


                    return true;


                }"""


            )


        )


        if not opened_more:


            return False


        page.wait_for_timeout(800)


        return bool(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                    const items = Array.from(document.querySelectorAll('.ant-dropdown:not(.ant-dropdown-hidden) li, .ant-dropdown:not(.ant-dropdown-hidden) a, .ant-dropdown:not(.ant-dropdown-hidden) span, .el-dropdown-menu__item, [role="menuitem"], button, a'))


                        .filter(visible)


                        .filter((el) => /编辑|修改|查看\\/编辑|同步后编辑|创建产品|创建新产品|复制为|Edit|Modify|Create/i.test(textOf(el)));


                    const target = items[0];


                    if (!target) return false;


                    target.scrollIntoView({block: 'center', inline: 'nearest'});


                    target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));


                    target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));


                    target.click();


                    return true;


                }"""


            )


        )


    except Exception:


        return False








def _is_edit_page(page: Any) -> bool:
    try:
        url = (page.url or "").lower()
        if "dianxiaomi.com" not in url:
            return False
        if "choicetemulist" in url:
            return False
        edit_url_match = any(token in url for token in ["/edit", "quoteedit", "/create", "/newproduct", "/copy"])
        body_has_edit_markers = bool(
            page.evaluate(
                """() => {
                    const text = document.body ? document.body.innerText : "";
                    return /产品标题|商品标题|标题|发布|图片|SKU|变种信息|包装信息/.test(text);
                }"""
            )
        )
        if edit_url_match and body_has_edit_markers:
            return True
        return bool(
            page.evaluate(
                """() => {
                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                    const text = document.body ? document.body.innerText : "";
                    const inputs = Array.from(document.querySelectorAll("input, textarea")).filter(visible);
                    const hasTitleInput = inputs.some((el) => {
                        const p = el.getAttribute("placeholder") || "";
                        const n = el.getAttribute("name") || "";
                        return /title|name/i.test(n) || /产品标题|商品标题|标题|名称/.test(p);
                    });
                    const hasSku = /SKU|sku|\u8d27\u53f7|\u5546\u54c1\u7f16\u7801/.test(text) || inputs.some((el) => /sku/i.test(el.getAttribute("name") || ""));
                    const hasImages = /产品轮播图|产品图片|主图|图片/.test(text) || document.querySelectorAll("img").length >= 2;
                    const hasPublish = /发布|刊登|保存|提交|立即发布|继续刊登/.test(text);
                    return hasTitleInput && hasSku && hasImages && hasPublish;
                }"""
            )
        )
    except Exception:
        return False








def _mark_product_image_cards(page: Any) -> dict[str, Any]:


    return page.evaluate(


        """() => {


            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


            const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


            document.querySelectorAll('[data-dxm-image-card],[data-dxm-image-selected-card],[data-dxm-image-selected],[data-dxm-image-select-action]')


                .forEach((el) => {


                    el.removeAttribute('data-dxm-image-card');


                    el.removeAttribute('data-dxm-image-selected-card');


                    el.removeAttribute('data-dxm-image-selected');


                    el.removeAttribute('data-dxm-image-select-action');


                });





            const all = Array.from(document.querySelectorAll('body *')).filter(visible);


            const imageScope =


                document.querySelector('.mainImage, [class*="mainImage"]') ||


                all.find((el) => {
                    const text = textOf(el);
                    return text.includes('产品轮播图') && text.includes('最多选用') && text.includes('已经选用');
                }) || null;


            const headerCandidates = all


                .map((el) => ({el, text: textOf(el), rect: el.getBoundingClientRect()}))


                .filter((item) => item.rect.y >= 0 && item.text && item.text.length <= 220)


                .filter((item) => !/图片检测|图片空间|上传文件/.test(item.text))


                .filter((item) => /product carousel|product images|main image|images/i.test(item.text) || item.text.includes('产品轮播图') || item.text.includes('产品图片') || item.text.includes('主图') || item.text.includes('商品图片') || item.text.includes('轮播图') || item.text.includes('素材图'));


            const header =


                headerCandidates.find((item) => item.text.includes('产品轮播图')) ||


                headerCandidates.find((item) => item.text.includes('产品图片') || item.text.includes('商品图片') || item.text.includes('主图')) ||


                headerCandidates.find((item) => /product carousel|product images|main image/i.test(item.text)) ||


                headerCandidates[0];


            const scopeRect = imageScope && visible(imageScope) ? imageScope.getBoundingClientRect() : null;


            const headerY = scopeRect ? scopeRect.y - 20 : (header ? header.rect.y - 20 : 0);


            const nextHeader = all


                .map((el) => ({el, text: textOf(el), rect: el.getBoundingClientRect()}))


                .filter((item) => item.rect.y > headerY + 40 && item.text && item.text.length <= 80)


                .find((item) => /variant|sku|package|attribute|description|detail/i.test(item.text) || item.text.includes('变种信息') || item.text.includes('包装信息') || item.text.includes('产品属性') || item.text.includes('详情') || item.text.includes('描述'));


            const nextY = scopeRect ? scopeRect.y + scopeRect.height + 30 : (nextHeader ? nextHeader.rect.y : Number.MAX_SAFE_INTEGER);


            const bad = /icon|logo|avatar|captcha|sprite|delete|remove|loading|arrow|blank|empty/i;





            const imgRoot = imageScope || document;


            const imgs = Array.from(imgRoot.querySelectorAll('img')).filter((img) => {


                const r = img.getBoundingClientRect();


                const src = img.currentSrc || img.src || '';


                if (!visible(img) || !src || bad.test(src)) return false;


                if (r.width < 42 || r.height < 42) return false;


                if (!imageScope && (r.y < headerY || r.y > nextY)) return false;


                const near = textOf(img.closest('label, li, div') || img);


                if (/delete|remove|upload|add|select|empty/i.test(near) || near.includes('删除') || near.includes('移除') || near.includes('上传') || near.includes('添加') || near.includes('选择') || near.includes('暂无')) return false;


                return true;


            }).sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y || a.getBoundingClientRect().x - b.getBoundingClientRect().x);





            const cards = [];


            const seen = new Set();


            const findRoot = (img) => {


                const selectors = [


                    'label',


                    '.ant-upload-list-item',


                    '.el-upload-list__item',


                    'li',


                    '[draggable=true]',


                    '[class*=image]',


                    '[class*=img]',


                    '[class*=upload]',


                    '[class*=picture]'


                ];


                for (const selector of selectors) {


                    const root = img.closest(selector);


                    if (root && visible(root)) return root;


                }


                return img;


            };


            for (const img of imgs) {


                const root = findRoot(img);


                if (seen.has(root)) continue;


                seen.add(root);


                const parent = root.parentElement || root;


                const checkbox =


                    root.querySelector('input[type="checkbox"]') ||


                    parent.querySelector('input[type="checkbox"]') ||


                    root.closest('label')?.querySelector('input[type="checkbox"]');


                const label = checkbox ? (checkbox.closest('label') || checkbox.parentElement || checkbox) : null;


                const rootText = textOf(root) + ' ' + textOf(parent);


                const selected =


                    !!(checkbox && checkbox.checked) ||


                    /已选|选中|使用中/.test(rootText) ||


                    /checked|selected|active/i.test(String(root.className || ''));


                cards.push({


                    card: root,


                    checkbox,


                    label,


                    selected,


                    src: img.currentSrc || img.src || '',


                    x: root.getBoundingClientRect().x,


                    y: root.getBoundingClientRect().y


                });


                if (cards.length >= 40) break;


            }





            const bodyText = document.body ? document.body.innerText : '';


            const selectedText = (bodyText.match(/(?:已经选用了|已经选用|已选用|已选择|已选)\\s*(\\d+)\\s*(?:张|个)?/) || [])[1] || '';


            const selectedTextCount = selectedText ? Number(selectedText) : 0;


            if (!cards.some((item) => item.selected) && selectedTextCount > 0) {


                cards.slice(0, selectedTextCount).forEach((item) => { item.selected = true; });


            }


            const selectedCards = [];


            cards.forEach((item, index) => {


                item.card.setAttribute('data-dxm-image-card', String(index));


                item.card.setAttribute('data-dxm-image-selected', item.selected ? '1' : '0');


                const action = item.label || item.checkbox || item.card;


                action.setAttribute('data-dxm-image-select-action', String(index));


                if (item.selected) {


                    item.card.setAttribute('data-dxm-image-selected-card', String(selectedCards.length));


                    selectedCards.push(item);


                }


            });


            const selectedCount = selectedCards.length || selectedTextCount;


            return {


                count: selectedCards.length || cards.length,


                available_count: cards.length,


                selected_count: selectedCount,


                selected_text_count: selectedText ? selectedTextCount : null,


                order: cards.map((item) => item.src),


                selected_order: selectedCards.map((item) => item.src),


                positions: cards.map((item) => ({x: item.x, y: item.y, selected: item.selected})),


                header_y: headerY,


                next_y: nextY


            };


        }"""


    )


    return page.evaluate(


        """() => {


            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


            document.querySelectorAll('[data-dxm-image-card]').forEach((el) => el.removeAttribute('data-dxm-image-card'));


            const bad = /icon|logo|avatar|captcha|sprite|delete|remove|loading|arrow/i;


            const imgs = Array.from(document.querySelectorAll('img')).filter((img) => {


                const r = img.getBoundingClientRect();


                const src = img.currentSrc || img.src || '';


                if (!visible(img) || !src || bad.test(src)) return false;


                if (r.width < 45 || r.height < 45 || r.y < 80) return false;


                const text = (img.closest('body *')?.innerText || '').slice(0, 200);


                if (/删除|移除|上传|暂无|delete|remove|upload|empty/i.test(text)) return false;


                return true;


            }).sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y || a.getBoundingClientRect().x - b.getBoundingClientRect().x);


            const cards = [];


            const seen = new Set();


            for (const img of imgs) {


                const card = img.closest('.ant-upload-list-item, .el-upload-list__item, li, [draggable=true], [class*=image], [class*=img], [class*=upload]') || img;


                if (seen.has(card)) continue;


                seen.add(card);


                cards.push({card, src: img.currentSrc || img.src || '', x: card.getBoundingClientRect().x, y: card.getBoundingClientRect().y});


                if (cards.length >= 20) break;


            }


            cards.forEach((item, index) => item.card.setAttribute('data-dxm-image-card', String(index)));


            return {count: cards.length, order: cards.map((item) => item.src), positions: cards.map((item) => ({x: item.x, y: item.y}))};


        }"""


    )








def _price_candidates(page: Any) -> list[dict[str, Any]]:


    return page.evaluate(


        """() => {


            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


            const textOf = (el) => (el ? (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim() : '');


            document.querySelectorAll('[data-dxm-price-candidate]').forEach((el) => el.removeAttribute('data-dxm-price-candidate'));


            const inputs = Array.from(document.querySelectorAll('input')).filter((el) => visible(el) && !el.disabled && el.type !== 'file' && el.type !== 'checkbox' && el.type !== 'radio');


            const out = [];


            inputs.forEach((el) => {


                const name = el.getAttribute('name') || '';


                const id = el.getAttribute('id') || '';


                const placeholder = el.getAttribute('placeholder') || '';


                const aria = el.getAttribute('aria-label') || '';


                const row = el.closest('tr, .ant-table-row, .el-table__row, .vxe-body--row, [class*=row]') || el.closest('.ant-form-item, .el-form-item, [class*=form]');


                const rowText = textOf(row);


                const nearby = [name, id, placeholder, aria, rowText].join(' ');


                let score = 0;


                if (/price|salePrice|skuPrice|retail|价格|售价|申报/i.test(nearby)) score += 60;


                if (/SKU|sku|货号|价格/i.test(rowText)) score += 20;


                if (/价格|售价|申报/.test(rowText + placeholder)) score += 30;


                const directAttrs = [name, id, placeholder, aria].join(' ');
                const excluded = /尺寸|长度|宽度|高度|重量|库存|成本|skuLength|skuWidth|skuHeight|weight|stock|inventory|cost/i.test(directAttrs);


                if (excluded) score -= 100;


                const value = String(el.value || '').trim();


                if (!/^\\s*[$￥¥]?\\s*\\d+(?:[.,]\\d+)?\\s*$/.test(value)) score -= 30;


                if (score >= 50 || (/价格|售价|申报/.test(rowText + placeholder) && !excluded)) {


                    const index = out.length;


                    el.setAttribute('data-dxm-price-candidate', String(index));


                    out.push({index, value, name, id, placeholder, label: rowText.slice(0, 180), score, excluded});


                }


            });


            return out;


        }"""


    )








def _parse_price(value: str) -> Decimal | None:


    cleaned = re.sub(r"[^\d.,-]", "", value or "").replace(",", ".")


    if not cleaned:


        return None


    try:


        return Decimal(cleaned)


    except InvalidOperation:


        return None








def _decimal_places(value: str) -> int | None:


    match = re.search(r"[.,](\d+)", value or "")


    return len(match.group(1)) if match else 2








def _open_publish_dropdown(page: Any) -> bool:


    try:


        return bool(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                    const candidates = Array.from(document.querySelectorAll('button, .ant-dropdown-trigger, [role=button], span, a')).filter((el) => {


                        const text = textOf(el);


                        const cls = String(el.className || '');


                        return visible(el) && (/发布|刊登|立即发布|继续刊登|保存|提交/.test(text) || /dropdown|down|arrow/.test(cls));


                    }).sort((a, b) => b.getBoundingClientRect().y - a.getBoundingClientRect().y);


                    const target = candidates[0];


                    if (!target) return false;


                    target.scrollIntoView({block: 'center', inline: 'nearest'});


                    target.click();


                    return true;


                }"""


            )


        )


    except Exception:


        return False








def _click_second_publish_text(page: Any) -> bool:


    for text in SECOND_PUBLISH_TEXTS:


        for exact in (True, False):


            try:


                locator = page.get_by_text(text, exact=exact).last


                locator.wait_for(state="visible", timeout=1200)


                locator.scroll_into_view_if_needed(timeout=1200)


                locator.click(timeout=2500)


                return True


            except Exception:


                continue


    return False








def _click_modal_action_button(page: Any, texts: list[str]) -> bool:


    for text in texts:


        try:


            locator = page.locator(".ant-modal button").filter(has_text=text).last


            locator.wait_for(state="visible", timeout=1500)


            locator.scroll_into_view_if_needed(timeout=1500)


            locator.click(timeout=3000, force=True)


            return True


        except Exception:


            continue


    try:


        return bool(


            page.evaluate(


                """(texts) => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                    const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);


                    for (const modal of modals) {


                        const buttons = Array.from(modal.querySelectorAll('button')).filter(visible);


                        for (const text of texts) {


                            const target = buttons.find((button) => textOf(button).includes(text));


                            if (target) {


                                target.scrollIntoView({block: 'center', inline: 'nearest'});


                                target.click();


                                return true;


                            }


                        }


                    }


                    return false;


                }""",


                texts,


            )


        )


    except Exception:


        return False








def _click_last_modal_primary_button(page: Any) -> bool:


    try:


        return bool(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);


                    const modal = modals[modals.length - 1];


                    if (!modal) return false;


                    const target = modal.querySelector('button.ant-btn-primary') || modal.querySelector('button');


                    if (!target) return false;


                    target.scrollIntoView({block: 'center', inline: 'nearest'});


                    target.click();


                    return true;


                }"""


            )


        )


    except Exception:


        return False








def _publish_success_modal_visible(page: Any) -> bool:


    try:


        return bool(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    return Array.from(document.querySelectorAll('.ant-modal')).filter(visible).some((modal) => {


                        const text = (modal.innerText || modal.textContent || '').replace(/\\s+/g, ' ').trim();


                        return (text.includes('产品已提交发布') || text.includes('提交发布') || text.includes('发布成功'))
                            && (text.includes('继续刊登') || text.includes('在线产品') || text.includes('发布中'));


                    });


                }"""


            )


        )


    except Exception:


        return False








def _continue_edit_modal_visible(page: Any) -> bool:


    try:


        return bool(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    return Array.from(document.querySelectorAll('.ant-modal')).filter(visible).some((modal) => {


                        const text = (modal.innerText || modal.textContent || '').replace(/\\s+/g, ' ').trim();


                        return (text.includes('????') || text.includes('????') || text.includes('???????'))
                            && (text.includes('????') || text.includes('?????') || text.includes('???'));


                    });


                }"""


            )


        )


    except Exception:


        return False








def _close_continue_edit_modal(page: Any, logger: Any | None = None, state: Any | None = None) -> None:


    if not _continue_edit_modal_visible(page):


        return


    clicked = _click_modal_action_button(page, ["????", "????", "??", "??"])


    if not clicked:


        clicked = _click_last_modal_primary_button(page)


    page.wait_for_timeout(1500)


    if _continue_edit_modal_visible(page):


        fail_with_popup_and_screenshot(page, "second_edit_start", "Continue-edit modal is still blocking the page.", logger=logger, state=state)


    if clicked:


        _log(logger, "second_edit_start", "ok", "Closed blocking continue-edit modal.", page=page)








def _is_second_edit_page(page: Any) -> bool:


    try:


        url = page.url


        if "quoteedit" in url.lower():


            return True


        if _visible_modal_contains(page, "????") or _visible_modal_contains(page, "?????"):


            return True


        return _recent_log_has_step_url("second_edit_start", url) or _recent_log_has_step_url("second_publish_entry_clicked", url)


    except Exception:


        return False








def _has_first_publish_success_prompt(page: Any) -> bool:


    try:


        return bool(


            page.evaluate(


                """() => {


                    const text = document.body ? document.body.innerText : '';


                    return /继续编辑|产品编辑成功|已保存到待发布|发布中|提交成功|创建新产品|保留已填内容/.test(text);


                }"""


            )


        )


    except Exception:


        return False








def _read_category_text(page: Any) -> str:


    try:


        return str(


            page.evaluate(


                """() => {


                    const text = document.body ? document.body.innerText : '';


                    const lines = text.split('\\n').map((line) => line.replace(/\\s+/g, ' ').trim()).filter(Boolean);


                    const root = document.querySelector('#productBasicInfo');
                    const path = root ? Array.from(root.querySelectorAll('.category-list, [class*="category-list"]'))
                        .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
                        .find((value) => value.includes(' > ') && value.length < 300) : '';
                    if (path) return path;

                    const productIdx = lines.findIndex((line) => line === '产品分类');


                    if (productIdx >= 0) {


                        const chooseIdx = lines.findIndex((line, index) => index > productIdx && line.includes('选择分类'));


                        if (chooseIdx >= 0 && lines[chooseIdx + 1]) return lines[chooseIdx + 1];


                        if (lines[productIdx + 1]) return lines[productIdx + 1];


                    }


                    const modal = Array.from(document.querySelectorAll('.ant-modal')).find((el) => {


                        const visible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                        return visible && (el.innerText || el.textContent || '').includes('选择类目');


                    });


                    if (modal) {


                        const path = Array.from(modal.querySelectorAll('div')).map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())


                          .find((value) => value.includes(' > ') && value.length < 300);


                        if (path) return path;


                    }


                    return '';


                }"""


            )


            or ""


        ).strip()


    except Exception:


        return ""








def _open_category_selector(page: Any) -> bool:


    try:


        if page.evaluate("""() => Array.from(document.querySelectorAll('.ant-modal')).some((el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length) && ((el.innerText || el.textContent || '').includes('选择类目') || (el.innerText || el.textContent || '').includes('选择分类')))"""):


            return True


    except Exception:


        pass


    try:
        clicked = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const root = document.querySelector('#productBasicInfo') || document;
                const button = Array.from(root.querySelectorAll('button')).filter(visible)
                    .find((el) => textOf(el).includes('选择分类') || textOf(el).includes('选择类目'));
                if (!button) return false;
                button.scrollIntoView({block: 'center', inline: 'nearest'});
                button.click();
                return true;
            }"""
        )
        if clicked:
            page.wait_for_timeout(1200)
            return True
    except Exception:
        pass

    for text in ["选择分类", "选择类目", "更换分类", "修改分类"]:


        try:


            locator = page.locator(f'button:has-text("{text}")').first


            locator.wait_for(state="visible", timeout=1200)


            locator.scroll_into_view_if_needed(timeout=1200)


            locator.click(timeout=2500)


            return True


        except Exception:


            pass


    for text in ["选择分类", "选择类目", "更换分类", "修改分类", "管理分类", "产品分类", "类目"]:


        try:


            locator = page.get_by_text(text, exact=False).last


            locator.wait_for(state="visible", timeout=1200)


            locator.scroll_into_view_if_needed(timeout=1200)


            locator.click(timeout=2000)


            return True


        except Exception:


            continue


    return False








def _select_sibling_category_option(page: Any, old_category: str) -> bool:


    try:


        return bool(


            page.evaluate(


                """(oldCategory) => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                    const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);


                    const modal = modals.find((el) => textOf(el).includes('选择类目') || textOf(el).includes('选择分类'));


                    if (!modal) return false;


                    const boxes = Array.from(modal.querySelectorAll('.categories-box')).filter(visible);


                    if (!boxes.length) return false;


                    const lastBox = boxes[boxes.length - 1];


                    const items = Array.from(lastBox.querySelectorAll('.categories-item')).filter(visible)


                        .map((el) => ({el, text: textOf(el), active: String(el.className || '').includes('active')}))


                        .filter((item) => item.text && item.text.length < 80 && !/请选择|全部|搜索|当前/.test(item.text));


                    let target = items.find((item) => !item.active && item.text.includes('其他'));


                    if (!target) target = items.find((item) => !item.active && !item.text.includes('当前'));


                    if (!target) target = items.find((item) => !item.active);


                    if (!target) return false;


                    target.el.scrollIntoView({block: 'center', inline: 'nearest'});


                    target.el.click();


                    const buttons = Array.from(modal.querySelectorAll('button')).filter(visible);


                    const ok = buttons.find((btn) => textOf(btn) === '确定') ||


                        buttons.find((btn) => /确定|确认|保存|OK|Confirm/i.test(textOf(btn)));


                    if (ok) ok.click();


                    return {selected: target.text};


                }""",


                old_category,


            )


        )


    except Exception:


        return False








def _select_sibling_category_option_v2(page: Any, old_category: str) -> bool:
    try:
        result = page.evaluate(
            """async (oldCategory) => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const normalize = (value) => String(value || '').replace(/\\s+/g, '').trim();
                const leafName = String(oldCategory || '').split(/[>›/]/).map((item) => item.trim()).filter(Boolean).pop() || '';
                const badText = (text) => !text || text.length >= 80 || /请选择|全部|搜索|当前|Select|Choose/i.test(text);
                const clickLikeUser = (el) => {
                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                    for (const type of ['mouseover', 'mouseenter', 'mousemove', 'mousedown', 'mouseup', 'click']) {
                        el.dispatchEvent(new MouseEvent(type, {bubbles: true, cancelable: true, view: window}));
                    }
                };
                const hasChildArrow = (el) => !!el.querySelector('.anticon-right, .icon_right, .icon-right, [class*="right"], [class*="arrow"]');
                const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);
                const modal = modals.find((el) => {
                    const text = textOf(el);
                    return text.includes('\\u9009\\u62e9\\u7c7b\\u76ee') || text.includes('\\u9009\\u62e9\\u5206\\u7c7b') || text.includes('Category');
                });
                if (!modal) return {ok: false, reason: 'modal_not_found'};
                const collectItems = () => {
                    const boxes = Array.from(modal.querySelectorAll('.categories-box')).filter(visible);
                    if (!boxes.length) return [];
                    const lastBox = boxes[boxes.length - 1];
                    return Array.from(lastBox.querySelectorAll('.categories-item, li, [role="option"]')).filter(visible)
                        .map((el) => ({el, text: textOf(el), active: String(el.className || '').includes('active'), child: hasChildArrow(el)}))
                        .filter((item) => !badText(item.text) && normalize(item.text) !== normalize(leafName));
                };
                let items = collectItems();
                if (!items.length) return {ok: false, reason: 'items_not_found'};
                const invalidLeaf = (item) => /^(\\u5176\\u4ed6|\\u5176\\u5b83|\\u540c\\u7c7b|other|same category)$/i.test(normalize(item.text));
                let target = items.find((item) => !item.active && !item.child && !invalidLeaf(item)) ||
                    items.find((item) => !item.active && !invalidLeaf(item)) ||
                    items.find((item) => !item.active && !item.child) ||
                    items.find((item) => !item.active);
                if (!target) return {ok: false, reason: 'target_not_found'};
                clickLikeUser(target.el);
                await sleep(700);
                if (target.child) {
                    const childItems = collectItems();
                    const childTarget = childItems.find((item) => !item.active && !item.child) ||
                        childItems.find((item) => !item.active);
                    if (childTarget && childTarget.el !== target.el) {
                        target = childTarget;
                        clickLikeUser(target.el);
                        await sleep(500);
                    }
                }
                const buttons = Array.from(modal.querySelectorAll('button')).filter(visible);
                const okButton = buttons.find((btn) => textOf(btn) === '\\u9009\\u62e9') ||
                    buttons.find((btn) => textOf(btn) === '\\u786e\\u5b9a') ||
                    buttons.find((btn) => /选择|确定|确认|保存|OK|Confirm|Apply/i.test(textOf(btn)));
                if (okButton) {
                    clickLikeUser(okButton);
                    await sleep(800);
                }
                return {ok: true, selected: target.text, confirmed: !!okButton};
            }""",
            old_category,
        )
        return bool(result and result.get("ok"))
    except Exception:
        return False


def _wait_category_change(page: Any, old_category: str, timeout_ms: int = 8000) -> bool:


    deadline = page.evaluate("Date.now()") + timeout_ms


    while True:


        try:


            current = _read_category_text(page)


            if current and current != old_category:


                return True


            if page.evaluate("Date.now()") >= deadline:


                return False


            page.wait_for_timeout(500)


        except Exception:


            return False








def _close_category_modal_if_open(page: Any) -> bool:


    try:


        return bool(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                    const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);


                    const modal = modals.find((el) => textOf(el).includes('选择类目') || textOf(el).includes('选择分类'));


                    if (!modal) return false;


                    const closeButton = Array.from(modal.querySelectorAll('button')).filter(visible)


                        .find((button) => textOf(button) === '关闭') || modal.querySelector('.ant-modal-close');


                    if (!closeButton) return false;


                    closeButton.click();


                    return true;


                }"""


            )


        )


    except Exception:


        return False








def _ai_short_title(original: str, logger: Any | None = None) -> str:


    try:


        client = EasyRouterClient(max_tokens=120, temperature=0.2, model_tier="text", logger=logger)


        return client.chat_text(


            [


                {"role": "system", "content": "Return one short English Temu product title only. No Chinese. No brand names. No claims."},


                {"role": "user", "content": f"Shorten this product title to 60 characters if possible, maximum 80 characters. Keep core keywords only:\n{original}"},


            ],


            max_tokens=120,


            temperature=0.2,


        )


    except Exception as exc:


        _log(logger, "title_shortened", "warning", f"AI ??????????????????: {exc}")


        return ""








def _local_short_title(original: str) -> str:


    cleaned = _clean_short_title(original)


    words = re.findall(r"[A-Za-z0-9+&-]+", cleaned)


    return " ".join(words[:9])[:80].strip()








def _clean_short_title(value: str) -> str:


    value = re.sub(r"[\u4e00-\u9fff]+", " ", str(value or ""))


    value = re.sub(r"[\r\n\t]+", " ", value)


    value = re.sub(r"\s+", " ", value).strip(" -_,;:/\"'")


    banned = ["best", "no.1", "no 1", "100% guaranteed", "guaranteed", "miracle"]


    for word in banned:


        value = re.sub(rf"\b{re.escape(word)}\b", "", value, flags=re.I)


    value = re.sub(r"\s+", " ", value).strip(" -_,;:/\"'")


    if len(value) > 80:


        value = " ".join(value.split()[:10])[:80].strip(" -_,;:/")


    return value








def _latest_price_updates_for_url(url: str) -> list[dict[str, Any]]:


    log_dir = PROJECT_ROOT / "data" / "logs"


    if not log_dir.exists():


        return []


    try:


        log_files = sorted(log_dir.glob("run_*.log"), key=lambda path: path.stat().st_mtime, reverse=True)


    except Exception:


        return []


    for log_file in log_files[:5]:


        try:


            lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()


        except Exception:


            continue


        for line in reversed(lines):


            try:


                record = json.loads(line)


            except Exception:


                continue


            if record.get("step") != "sku_price_increased" or record.get("status") != "ok":


                continue


            if record.get("url") != url:


                continue


            items = record.get("items")


            if isinstance(items, list) and all(isinstance(item, dict) and item.get("new_price") for item in items):


                return items


    return []








def _recent_log_has_step_url(step: str, url: str) -> bool:


    if not url:


        return False


    log_dir = PROJECT_ROOT / "data" / "logs"


    if not log_dir.exists():


        return False


    try:


        log_files = sorted(log_dir.glob("run_*.log"), key=lambda path: path.stat().st_mtime, reverse=True)


    except Exception:


        return False


    for log_file in log_files[:5]:


        try:


            lines = log_file.read_text(encoding="utf-8", errors="ignore").splitlines()


        except Exception:


            continue


        for line in reversed(lines[-500:]):


            try:


                record = json.loads(line)


            except Exception:


                continue


            if record.get("step") == step and record.get("url") == url:


                return True


    return False








def _visible_modal_contains(page: Any, needle: str) -> bool:


    try:


        return bool(


            page.evaluate(


                """(needle) => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    return Array.from(document.querySelectorAll('.ant-modal')).filter(visible).some((modal) => {


                        return (modal.innerText || modal.textContent || '').includes(needle);


                    });


                }""",


                needle,


            )


        )


    except Exception:


        return False








def _safe_read_title(page: Any) -> str:


    try:


        return read_original_title(page)


    except Exception:


        return ""








def _extract_product_id(url: str) -> str:


    match = re.search(r"(?:id|productId|goodsId|itemId)=([A-Za-z0-9_-]+)", url or "")


    return match.group(1) if match else "DXM"








def _extract_edit_context_id(url: str) -> str:


    raw_id = _extract_product_id(url)


    lowered = (url or "").lower()


    if "quoteedit" in lowered:


        return f"quoteEdit:{raw_id}"


    if "/edit" in lowered:


        return f"edit:{raw_id}"


    return raw_id








def _wait_ready(page: Any) -> None:


    try:


        page.wait_for_load_state("domcontentloaded", timeout=15000)


    except Exception:


        pass


    try:


        page.wait_for_load_state("networkidle", timeout=5000)


    except Exception:


        pass








def _state(result: dict[str, Any], name: str, page: Any, logger: Any | None = None, extra: dict[str, Any] | None = None) -> None:


    item = {"state": name, "url": getattr(page, "url", "")}


    if extra:


        item.update(extra)


    result.setdefault("states", []).append(item)


    _log(logger, name, "ok", f"?????? {name}", page=page, extra=extra or {})








def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:


    if logger:


        logger.log_step(step, status, message, **kwargs)


    else:


        safe_message = str(message).encode("utf-8", errors="replace").decode("utf-8", errors="replace")
        try:
            print(f"[{step}] {status}: {safe_message}")
        except UnicodeEncodeError:
            print(f"[{step}] {status}: {safe_message.encode('ascii', errors='replace').decode('ascii')}")








def _safe_take_screenshot(page: Any, name: str) -> str:


    try:


        return take_screenshot(page, name)


    except Exception as exc:


        return f"screenshot_failed: {exc}"








# ====== Dual Publish Context & Verification ======





def _read_first_sku_from_edit_page(page: Any) -> str:


    try:


        return str(


            page.evaluate(


                """() => {


                    const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                    const inputs = Array.from(document.querySelectorAll('input[name="variationSku"], textarea[name="variationSku"], input, textarea'))


                        .filter((el) => visible(el) && !el.disabled);


                    const direct = inputs.find((el) => /variationSku|sku/i.test(el.getAttribute('name') || '') && (el.value || '').trim());


                    if (direct) return (direct.value || '').trim();


                    const byNearby = inputs.find((el) => {


                        const value = (el.value || '').trim();


                        if (!value) return false;


                        const row = el.closest('tr, .ant-table-row, .el-table__row, .vxe-body--row, [class*=row], .ant-form-item, .el-form-item') || el.parentElement;


                        const text = row ? (row.innerText || row.textContent || '') : '';


                        return /SKU|sku|\u8d27\u53f7|\u5546\u54c1\u7f16\u7801/.test(text);


                    });


                    return byNearby ? (byNearby.value || '').trim() : '';


                }"""


            )


            or ""


        ).strip()


    except Exception:


        return ""








def build_context_from_first_row(page: Any, logger: Any | None = None) -> dict[str, Any]:


    ctx: dict[str, Any] = {


        "source_list_title": "",


        "source_list_sku": "",


        "first_edit_url": "",


        "first_edit_id": "",


        "first_title_before": "",


        "first_title_after": "",


        "first_publish_time": "",


        "first_publish_status": "",


        "first_publish_submitted": False,


        "second_edit_url": "",


        "second_edit_id": "",


        "second_title_before": "",


        "second_title_after": "",


        "second_publish_time": "",


        "second_publish_status": "",


        "second_publish_submitted": False,


        "first_publish_record": {},


        "second_publish_record": {},


        "publish_links": [],


        "distinct_publish_records_count": 0,


        "verify_result": "not_run",


        "_disable_auto_price": False,


    }





    if _is_edit_page(page):


        title = _safe_read_title(page)


        sku = _read_first_sku_from_edit_page(page)


        ctx.update(


            {


                "context_source": "edit_page",


                "started_from_edit_page": True,


                "source_list_title": title,


                "source_list_sku": sku,


                "first_edit_url": page.url,


                "first_edit_id": _extract_edit_context_id(page.url),


                "first_title_before": title,


            }


        )


        _log(


            logger,


            "build_context",


            "ok",


            f"Initial context built from current edit page: title={title[:60]}, url={page.url}",


            page=page,


            extra={"source_list_sku": sku, "started_from_edit_page": True},


        )


        return ctx





    row_info = _mark_first_dxm_list_row(page)


    ctx["context_source"] = "list_row"


    ctx["started_from_edit_page"] = False


    ctx["source_list_title"] = row_info.get("title", "") if row_info.get("ok") else ""


    ctx["source_list_sku"] = row_info.get("sku", "") if row_info.get("ok") else ""


    _log(logger, "build_context", "ok", f"Initial context built from list row: title={ctx['source_list_title'][:60]}", page=page, extra={"source_list_sku": ctx["source_list_sku"], "row_ok": row_info.get("ok", False)})


    return ctx








def assert_second_edit_is_new_product(page: Any, context: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> None:


    """Strictly validate that the second edit page is a NEW product, not the original."""


    second_url = page.url


    context["second_edit_url"] = second_url


    second_id = _extract_edit_context_id(second_url)


    context["second_edit_id"] = second_id





    first_url = context.get("first_edit_url", "")


    first_id = context.get("first_edit_id", "")





    checks: list[dict[str, Any]] = []





    # Check 1: URL must not equal first_edit_url


    if first_url and second_url == first_url:


        checks.append({"check": "url_different", "passed": False, "detail": f"second_edit_url == first_edit_url: {second_url}"})


    else:


        checks.append({"check": "url_different", "passed": True})





    # Check 2: IDs must differ


    if first_id and first_id != "DXM" and second_id == first_id:


        checks.append({"check": "id_different", "passed": False, "detail": f"second_edit_id ({second_id}) == first_edit_id ({first_id})"})


    else:


        checks.append({"check": "id_different", "passed": True})





    # Check 3: URL should contain quoteEdit or look like a new product page


    url_lower = second_url.lower()


    is_quote_edit = "quoteedit" in url_lower


    has_create_product = any(kw in url_lower for kw in ["create", "newproduct", "copy"])





    # Page body may indicate "create new" / "retain content"; either this or quoteEdit is enough.


    try:


        body = page.locator("body").inner_text(timeout=3000)


        has_retain_hint = any(phrase in body for phrase in ["??????", "?????", "????", "????"])


    except Exception:


        has_retain_hint = False


    looks_new = is_quote_edit or has_create_product or has_retain_hint


    checks.append(


        {


            "check": "looks_like_new_product",


            "passed": looks_new,


            "detail": f"url contains quoteEdit={is_quote_edit}, create/new={has_create_product}, page_hint={has_retain_hint}",


        }


    )





    failed = [c for c in checks if not c.get("passed")]


    if failed:


        screenshot_path = take_screenshot(page, "second_edit_not_new_product")


        _log(logger, "second_edit_not_new_product", "failed", f"Second edit page validation FAILED. Checks: {failed}", page=page, screenshot_path=screenshot_path, extra={"context": context, "checks": checks})


        if state:


            state.update(status="second_edit_not_new_product", failed_checks=failed, screenshot_path=screenshot_path)


        fail_with_popup_and_screenshot(


            page,


            "second_edit_not_new_product",


            f"Second edit page is NOT a new product! URL={second_url}, ID={second_id}. "


            f"Failed checks: {[c['check'] for c in failed]}. "


            "Must have distinct URL/ID from first edit and appear to be a create-new-product page.",


            logger=logger,


            state=state,


            extra={"context": context, "checks": checks},


        )





    _log(logger, "second_edit_is_new_product", "ok", f"Second edit page validated as new product: url={second_url}, id={second_id}", page=page, extra={"checks": checks})








def navigate_to_publish_records_list(page: Any, context: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> bool:


    """Navigate from current page to a DXM backend records list.





    The current Temu backend uses these routes:


    - /choiceTemuList/offline?dxmOfflineState=publishing for publishing tasks


    - /choiceTemuList/offline?dxmOfflineState=publishFail for failed tasks


    - /choiceTemuList/online for online products


    """


    _log(logger, "navigate_to_records", "start", "Navigating to backend publish records list for verification.", page=page)





    # Step 1: try clicking "view result" / "check" links in any visible modal or page


    view_keywords = ["?????????", "??????", "????��?", "???��?", "??????", "????��?"]


    for kw in view_keywords:


        try:


            locator = page.get_by_text(kw, exact=False).last


            locator.wait_for(state="visible", timeout=2000)


            locator.scroll_into_view_if_needed(timeout=2000)


            locator.click(timeout=3000)


            page.wait_for_timeout(3000)


            _wait_ready(page)


            _log(logger, "navigate_to_records", "ok", f"Clicked '{kw}' to navigate to records.", page=page)


            return True


        except Exception:


            continue





    # Step 2: try modal buttons


    modal_buttons = ["??", "??", "??", "???"]


    for btn in modal_buttons:


        try:


            locator = page.locator(".ant-modal button").filter(has_text=btn).last


            locator.wait_for(state="visible", timeout=1500)


            locator.click(timeout=3000, force=True)


            page.wait_for_timeout(3000)


            _wait_ready(page)


            _log(logger, "navigate_to_records", "ok", f"Clicked modal button '{btn}'.", page=page)


            return True


        except Exception:


            continue





    # Step 3: navigate by known current backend URLs.


    list_urls = [


        "https://www.dianxiaomi.com/web/temu/choiceTemuList/offline?dxmOfflineState=publishing",


        "https://www.dianxiaomi.com/web/temu/choiceTemuList/online",


        "https://www.dianxiaomi.com/web/temu/choiceTemuList/offline?dxmOfflineState=publishFail",


        "https://www.dianxiaomi.com/web/temu/choiceTemuList/offline",


        "https://www.dianxiaomi.com/web/temu/choiceTemuList/online",


    ]


    for url in list_urls:


        try:


            page.goto(url, wait_until="domcontentloaded", timeout=15000)


            _wait_ready(page)


            page.wait_for_timeout(3000)


            if "dianxiaomi.com" in page.url.lower() and "choiceTemuList" in page.url:


                _log(logger, "navigate_to_records", "ok", f"Navigated to {url}", page=page)


                return True


        except Exception:


            continue





    _log(logger, "navigate_to_records", "failed", "Could not navigate to any publish records list.", page=page)


    return False








def extract_publish_records_from_current_list(page: Any, context: dict[str, Any], logger: Any | None = None) -> dict[str, Any]:


    """Extract visible product/publish records from the current DXM list page."""


    try:


        records = page.evaluate(


            """() => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();


                


                // Try table rows first


                const rowSelectors = [


                    'tr.ant-table-row', '.ant-table-row', '.vxe-body--row', '.el-table__row',


                    'tbody tr', '[class*="table"] [class*="row"]', '[class*="list"] [class*="item"]'


                ];


                let rows = [];


                for (const selector of rowSelectors) {


                    rows = Array.from(document.querySelectorAll(selector)).filter(visible);


                    if (rows.length) break;


                }


                


                const records = [];


                for (const row of rows.slice(0, 30)) {


                    const text = textOf(row);


                    const rect = row.getBoundingClientRect();


                    if (rect.y < 80 || rect.height < 20 || text.length < 10) continue;


                    if (/商品信息|操作|产品标题|图片|全选|店铺/.test(text) && text.length < 60) continue;


                    if (/父\\s*SKU|批量操作|产品标题|操作|图片/.test(text)) continue;


                    if (!/Temu|Kyiki|CNY|SKU|????|??????|??????|??????|????|??|????/.test(text)) continue;


                    


                    // Extract links


                    const links = Array.from(row.querySelectorAll('a[href]')).map(a => ({


                        text: (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 200),


                        href: a.href || ''


                    })).filter(l => l.href && !/javascript:/.test(l.href));


                    


                    // Extract product ID patterns


                    const idMatches = text.match(/(?:ID|???ID|???ID|goodsId)[:??]?\\s*([A-Za-z0-9_-]+)/gi) || [];


                    const skuMatches = text.match(/(?:SKU|????)[:??]?\\s*([A-Za-z0-9_-]+)/gi) || [];


                    const updateMatches = text.match(/(?:????|????)[:??]?\\s*\\d{4}-\\d{2}-\\d{2}\\s+\\d{2}:\\d{2}/g) || [];


                    


                    // Try to find title (longest meaningful text line)


                    const lines = text.split(/\\s{2,}/).filter(l => l.length > 10 && !/ID|SKU|????|??|???|????|????/.test(l.slice(0, 5)));


                    const title = lines.sort((a, b) => b.length - a.length)[0] || '';


                    


                    records.push({


                        title: title.slice(0, 200),


                        text_preview: text.slice(0, 400),


                        links: links,


                        ids: idMatches.slice(0, 5),


                        skus: skuMatches.slice(0, 5),


                        times: updateMatches.slice(0, 5),


                        page_url: location.href,


                        rect_y: Math.round(rect.y)


                    });


                }


                return records;


            }"""


        )


        _log(logger, "extract_records", "ok", f"Extracted {len(records) if isinstance(records, list) else 0} records from current list page.", page=page, extra={"record_count": len(records) if isinstance(records, list) else 0})


        return {"status": "ok", "records": records if isinstance(records, list) else [], "count": len(records) if isinstance(records, list) else 0}


    except Exception as exc:


        _log(logger, "extract_records", "failed", f"Failed to extract records: {exc}", page=page)


        return {"status": "failed", "records": [], "count": 0, "message": str(exc)}








def collect_publish_links(page: Any) -> list[str]:


    """Collect all publish-related links from the current page."""


    try:


        links = page.evaluate(


            """() => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                return Array.from(document.querySelectorAll('a[href]')).filter(visible).map(a => a.href || '')


                    .filter(href => href && !/javascript:|mailto:|tel:/.test(href));


            }"""


        )


        return list(dict.fromkeys(links)) if isinstance(links, list) else []


    except Exception:


        return []








def _verification_page_targets() -> list[dict[str, str]]:


    return [


        {


            "label": "publishing",


            "url": "https://www.dianxiaomi.com/web/temu/choiceTemuList/offline?dxmOfflineState=publishing",


            "kind": "pending_candidate",


        },


        {


            "label": "online",


            "url": "https://www.dianxiaomi.com/web/temu/choiceTemuList/online",


            "kind": "success_candidate",


        },


        {


            "label": "offline_all",


            "url": "https://www.dianxiaomi.com/web/temu/choiceTemuList/offline",


            "kind": "pending_candidate",


        },


        {


            "label": "publish_failed",


            "url": "https://www.dianxiaomi.com/web/temu/choiceTemuList/offline?dxmOfflineState=publishFail",


            "kind": "failure_evidence",


        },


    ]








def _goto_verification_page(page: Any, target: dict[str, str], logger: Any | None = None) -> bool:


    label = target.get("label", "")


    url = target.get("url", "")


    try:


        page.goto(url, wait_until="domcontentloaded", timeout=15000)


        _wait_ready(page)


        page.wait_for_timeout(2500)


        ok = "dianxiaomi.com" in page.url.lower() and "choiceTemuList" in page.url


        _log(logger, "verify_page_open", "ok" if ok else "failed", f"{label}: {page.url}", page=page)


        return ok


    except Exception as exc:


        _log(logger, "verify_page_open", "failed", f"{label}: {exc}", page=page)


        return False








def _record_key(record: dict[str, Any]) -> str:


    links = record.get("links") or []


    link_key = ""


    if links:


        first = links[0]


        link_key = first.get("href", "") if isinstance(first, dict) else str(first)


    return "|".join(


        [


            str(record.get("page_url", "")),


            str(record.get("title", ""))[:120],


            str(record.get("rect_y", "")),


            link_key[:160],


        ]


    )








def _record_matches_title(record: dict[str, Any], title: str) -> bool:


    if not title:


        return False


    text = " ".join([str(record.get("title", "")), str(record.get("text_preview", ""))])


    return _titles_similar(text, title) or _titles_similar(record.get("title", ""), title)








def _dedupe_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:


    deduped: list[dict[str, Any]] = []


    seen: set[str] = set()


    for record in records:


        key = _record_key(record)


        if key in seen:


            continue


        seen.add(key)


        deduped.append(record)


    return deduped








def verify_two_distinct_publish_records(


    page: Any,


    context: dict[str, Any],


    logger: Any | None = None,


    state: Any | None = None,


) -> dict[str, Any]:


    """Verify that the DXM backend shows 2 distinct publish records.





    Called AFTER second publish popup shows success.


    Only returns success if 2 truly distinct records are found.


    """


    _log(logger, "verify_two_distinct", "start", "Starting backend verification for 2 distinct publish records.", page=page, extra={"context_summary": {k: v for k, v in context.items() if k not in ("first_publish_record", "second_publish_record")}})





    result: dict[str, Any] = {


        "status": "running",


        "first_publish_submitted": context.get("first_publish_submitted", False),


        "second_publish_submitted": context.get("second_publish_submitted", False),


        "distinct_publish_records_count": 0,


        "publish_links": [],


        "first_record": None,


        "second_record": None,


        "verify_result": "not_run",


        "reason": "",


        "pages_checked": [],


    }





    # Close any remaining modals first


    _close_any_visible_modals(page, logger)





    # Navigate once from edit/result page into the current DXM backend list area.


    nav_ok = navigate_to_publish_records_list(page, context, logger=logger, state=state)


    if not nav_ok:


        result["verify_result"] = "dual_publish_verify_failed"


        result["status"] = "failed"


        result["reason"] = "Could not navigate to backend records list."


        screenshot_path = _safe_take_screenshot(page, "verify_nav_failed")


        result["screenshot_path"] = screenshot_path


        context["verify_result"] = "dual_publish_verify_failed"


        _log(logger, "verify_two_distinct", "failed", result["reason"], page=page, screenshot_path=screenshot_path)


        show_manual_action_popup("Dual publish verification failed", f"Could not open backend records list.\nScreenshot: {screenshot_path}\nThis run is not successful.", logger=logger)


        return result





    first_title = context.get("first_title_after") or context.get("first_title_before", "")


    second_title = context.get("second_title_after", "")


    success_records: list[dict[str, Any]] = []


    pending_records: list[dict[str, Any]] = []


    failure_records: list[dict[str, Any]] = []


    all_links: list[str] = []





    for target in _verification_page_targets():


        if not _goto_verification_page(page, target, logger=logger):


            continue


        result["pages_checked"].append({"label": target["label"], "url": page.url, "kind": target["kind"]})


        extraction = extract_publish_records_from_current_list(page, context, logger=logger)


        records = extraction.get("records", []) if extraction.get("status") == "ok" else []


        for record in records:


            record["verification_page_label"] = target["label"]


            record["verification_page_kind"] = target["kind"]


        if target["kind"] == "success_candidate":


            success_records.extend(records)


        elif target["kind"] == "failure_evidence":


            failure_records.extend(records)


        else:


            pending_records.extend(records)


        all_links.extend(collect_publish_links(page))





    success_records = _dedupe_records(success_records)


    pending_records = _dedupe_records(pending_records)


    failure_records = _dedupe_records(failure_records)


    result["publish_links"] = list(dict.fromkeys(all_links))[:20]


    result["success_records"] = success_records[:5]


    result["failure_records"] = failure_records[:5]


    result["pending_records"] = pending_records[:5]


    context["publish_links"] = result["publish_links"]





    matched_first = _dedupe_records([record for record in success_records if _record_matches_title(record, first_title)])


    matched_second = _dedupe_records([record for record in success_records if _record_matches_title(record, second_title)])





    verified_pair: tuple[dict[str, Any], dict[str, Any]] | None = None


    for first_record in matched_first:


        for second_record in matched_second:


            if _record_key(first_record) != _record_key(second_record):


                verified_pair = (first_record, second_record)


                break


        if verified_pair:


            break





    if verified_pair:


        first_record, second_record = verified_pair


        context["first_publish_record"] = first_record


        context["second_publish_record"] = second_record


        context["distinct_publish_records_count"] = 2


        context["verify_result"] = "dual_publish_verified"


        result["first_record"] = first_record


        result["second_record"] = second_record


        result["distinct_publish_records_count"] = 2


        result["verify_result"] = "dual_publish_verified"


        result["status"] = "success"


        result["reason"] = "Found two distinct backend records matching the first and second publish titles."


        screenshot_path = _safe_take_screenshot(page, "verify_two_distinct_success")


        result["screenshot_path"] = screenshot_path


        _log(logger, "verify_two_distinct", "ok", "VERIFIED: two distinct publish records found.", page=page, screenshot_path=screenshot_path, extra=result)


        return result





    # Failure evidence is important: a publish failure record is not a successful link.


    failure_matches = _dedupe_records(


        [


            record


            for record in failure_records


            if _record_matches_title(record, first_title) or _record_matches_title(record, second_title)


        ]


    )


    success_matches = _dedupe_records(matched_first + matched_second)


    distinct_count = len(success_matches)


    context["distinct_publish_records_count"] = distinct_count


    result["distinct_publish_records_count"] = distinct_count


    if success_matches:


        # Keep submitted-but-failed first publish visible if it exists. The only


        # successful record may be the second short-title quoteEdit record.


        if not failure_matches:


            context["first_publish_record"] = success_matches[0]


            result["first_record"] = success_matches[0]


        if matched_second:


            context["second_publish_record"] = matched_second[0]


            result["second_record"] = matched_second[0]


        elif len(success_matches) > 1:


            context["second_publish_record"] = success_matches[1]


            result["second_record"] = success_matches[1]


    if failure_matches:


        context["first_publish_record"] = failure_matches[0]


        result["first_record"] = failure_matches[0]





    context["verify_result"] = "dual_publish_verify_failed"


    result["verify_result"] = "dual_publish_verify_failed"


    result["status"] = "failed"


    if failure_matches:


        result["reason"] = "Backend shows publish failed record(s), not two successful/online records."


        result["matched_failure_records"] = failure_matches[:3]


    else:


        result["reason"] = "Could not find two distinct successful backend records matching first and second titles."


    result["possible_causes"] = [


        "Second quoteEdit did not truly create a new product.",


        "Second publish overwrote the first record.",


        "DXM backend merged same SKU / same product.",


        "Publish task failed after the page briefly showed publishing.",


        "Backend list filter position is wrong.",


    ]





    screenshot_path = _safe_take_screenshot(page, "dual_publish_verify_failed")


    result["screenshot_path"] = screenshot_path


    _log(logger, "verify_two_distinct", "failed", f"FAILED: {result['reason']}", page=page, screenshot_path=screenshot_path, extra=result)





    show_manual_action_popup(
        "Dual publish verification failed",
        f"Online list does not contain 2 distinct successful records.\n"
        f"success_records_matched={distinct_count}\n"
        f"failure_records_matched={len(failure_matches)}\n"
        f"reason={result['reason']}\n"
        f"screenshot={screenshot_path}\n\n"
        "This run is not a dual-publish success. "
        "If same SKU caused a merge, consider adding suffix -2 in the next run.",
        logger=logger,
    )





    if state:


        state.update(


            dual_publish_verify_failed=True,


            distinct_publish_records_count=distinct_count,


            verify_result="dual_publish_verify_failed",


            screenshot_path=screenshot_path,


        )


    return result








def _titles_similar(a: str, b: str, threshold: float = 0.35) -> bool:


    """Check if two titles are similar using word overlap."""


    if not a or not b:


        return False


    words_a = set(re.findall(r"[A-Za-z0-9]+", a.lower()))


    words_b = set(re.findall(r"[A-Za-z0-9]+", b.lower()))


    if not words_a or not words_b:


        return False


    overlap = len(words_a & words_b)


    min_len = min(len(words_a), len(words_b))


    if min_len == 0:


        return False


    return (overlap / min_len) >= threshold








def _close_any_visible_modals(page: Any, logger: Any | None = None) -> None:


    """Close all visible ant-modal dialogs."""


    try:


        page.evaluate(


            """() => {


                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);


                const modals = Array.from(document.querySelectorAll('.ant-modal')).filter(visible);


                for (const modal of modals) {


                    const closeBtn = modal.querySelector('.ant-modal-close, .ant-modal-close-x');


                    if (closeBtn) {


                        closeBtn.click();


                        continue;


                    }


                    const cancelBtn = modal.querySelector('button:not(.ant-btn-primary)');


                    if (cancelBtn) {


                        cancelBtn.click();


                    }


                }


            }"""


        )


        page.wait_for_timeout(500)


    except Exception:


        pass
