from __future__ import annotations

import os
import re
import sys
from typing import Any

from .utils import body_text, take_screenshot
from .windows_prompt import wait_user_decision, UserChoseSkip, UserChoseStop


YUNQI_HOME_URL = os.getenv("YUNQI_URL", "https://www.yunqishuju.com/")
YUNQI_TEMU_HOME_URL = os.getenv("YUNQI_TEMU_HOME_URL", "https://www.yunqishuju.com/temu/home/")


def open_yunqi(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "yunqi_open", "start", f"打开云启数据: {YUNQI_HOME_URL}", page=page)
    page.goto(YUNQI_HOME_URL, wait_until="domcontentloaded")
    _wait_ready(page)

    if _looks_like_login(page):
        screenshot_path = take_screenshot(page, "yunqi_login_required")
        _log(logger, "yunqi_open", "manual_required", "云启数据可能未登录，请人工登录后继续。", page=page, screenshot_path=screenshot_path)
        if not _wait_for_continue("请在浏览器中完成云启数据登录，然后输入 continue 继续。"):
            result = {"status": "manual_required", "url": page.url, "opened": True, "screenshot_path": screenshot_path}
            if state:
                state.update(yunqi_open=result)
            return result
        _wait_ready(page)

    result = {"status": "ok", "url": page.url, "opened": True}
    _log(logger, "yunqi_open", "ok", "云启数据已打开。", page=page)
    if state:
        state.update(yunqi_open=result)
    return result


def set_yunqi_temu_filters(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    result = {
        "status": "ok",
        "temu_selected": False,
        "last_month_selected": False,
        "full_managed_selected": False,
        "warnings": [],
        "manual_intervention": False,
    }

    _log(logger, "yunqi_filter", "start", "开始设置云启筛选：Temu / 近一个月 / 全托管(如有)。", page=page)

    # Try entering a marketplace/product database area first.
    _click_any_text(page, ["商品库", "店铺库", "商品", "店铺"], timeout=1200)
    result["temu_selected"] = _click_any_text(page, ["Temu", "temu", "TEMU"], timeout=1800)
    if not result["temu_selected"] or "temu" not in page.url.lower():
        try:
            page.goto(YUNQI_TEMU_HOME_URL, wait_until="domcontentloaded")
            _wait_ready(page)
            result["temu_selected"] = "temu" in page.url.lower() or "temu" in body_text(page).lower()
        except Exception as exc:
            result["warnings"].append(f"Temu 兜底 URL 打开失败: {exc}")
    if not result["temu_selected"]:
        result["warnings"].append("未自动点击到 Temu 入口")

    result["last_month_selected"] = _click_any_text(page, ["近一个月", "最近30天", "近30天", "一个月", "30天"], timeout=1800)
    if not result["last_month_selected"]:
        result["warnings"].append("未自动点击到近一个月/近30天筛选")

    result["full_managed_selected"] = _click_any_text(page, ["全托管", "半托管"], timeout=1200)
    if not result["full_managed_selected"]:
        result["warnings"].append("未找到全托管筛选，已跳过")

    page.wait_for_timeout(800)
    status = "ok" if result["temu_selected"] or "temu" in body_text(page).lower() else "warning"
    if status == "warning":
        screenshot_path = take_screenshot(page, "yunqi_filter_warning")
        result["screenshot_path"] = screenshot_path
        result["visible_text"] = _visible_text_sample(page)
        _log(logger, "yunqi_filter", "warning", "云启筛选未能确认 Temu，继续尝试搜索；如结果不对请人工修正。", page=page, screenshot_path=screenshot_path, extra=result)
    else:
        _log(logger, "yunqi_filter", "ok", f"云启筛选完成: {result}", page=page, extra=result)

    if state:
        state.update(yunqi_filter=result)
    return result


def search_yunqi_results(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "yunqi_search", "start", "点击云启搜索并等待结果。", page=page)
    clicked_search = _click_any_text(page, ["搜索", "查询", "筛选", "确定"], timeout=1500, button_like=True)
    page.wait_for_timeout(2500)
    sorted_by = ""
    for text in ["销量", "总销量", "日销量", "排名"]:
        if _click_any_text(page, [text], timeout=700):
            sorted_by = text
            page.wait_for_timeout(1200)
            break

    screenshot_path = take_screenshot(page, "full_yunqi_result")
    result = {
        "status": "ok",
        "clicked_search": clicked_search,
        "sorted_by": sorted_by,
        "url": page.url,
        "screenshot_path": screenshot_path,
    }
    _log(logger, "yunqi_search", "ok", f"云启结果页已截图，排序={sorted_by or '默认'}。", page=page, screenshot_path=screenshot_path, extra=result)
    if state:
        state.update(yunqi_search=result)
    return result


def extract_first_temu_product_link(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    """Extract first Yunqi result's Temu product URL or goods_id without opening Temu."""
    _log(logger, "yunqi_extract_link", "start", "Extracting first Yunqi Temu product link/goods_id.", page=page)
    screenshot_path = take_screenshot(page, "full_yunqi_result")
    result = _extract_first_row_link_from_dom(page)
    if not result.get("product_url"):
        modal_result = _extract_link_from_yunqi_detail_modal(page)
        if modal_result.get("product_url"):
            result.update({key: value for key, value in modal_result.items() if value})

    product_url = str(result.get("product_url") or "")
    goods_id = str(result.get("goods_id") or "")
    if not product_url and goods_id:
        product_url = _build_temu_product_url(goods_id)
        result["product_url"] = product_url
    if product_url and not goods_id:
        goods_id = _extract_goods_id(product_url)
        result["goods_id"] = goods_id

    result.update(
        {
            "status": "ok" if result.get("product_url") else "manual_required",
            "screenshot_path": screenshot_path,
            "url": page.url,
        }
    )
    if result["status"] == "ok":
        _log(logger, "yunqi_extract_link", "ok", f"Extracted product URL: {result['product_url']}", page=page, screenshot_path=screenshot_path, extra=result)
    else:
        result["visible_text"] = _visible_text_sample(page)
        _log(logger, "yunqi_extract_link", "manual_required", "Could not extract Temu product URL or goods_id from first Yunqi row.", page=page, screenshot_path=screenshot_path, extra=result)
    if state:
        state.update(yunqi_extract_link=result)
    return result


def _extract_first_row_link_from_dom(page: Any) -> dict[str, Any]:
    try:
        raw = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const rows = Array.from(document.querySelectorAll('tbody tr, .el-table__row, .ant-table-row, .vxe-body--row'))
                    .filter((row) => visible(row) && textOf(row).length > 20)
                    .sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y || a.getBoundingClientRect().x - b.getBoundingClientRect().x);
                const row = rows[0];
                if (!row) return {title: '', shop_name: '', hrefs: [], data_values: [], row_text: ''};
                const rowText = textOf(row);
                const hrefs = Array.from(row.querySelectorAll('a[href]')).map((a) => new URL(a.getAttribute('href') || '', location.href).href);
                const dataValues = [];
                const attrs = ['href', 'data-url', 'data-href', 'data-link', 'data-goods-id', 'data-id', 'title', 'onclick'];
                for (const el of Array.from(row.querySelectorAll('*'))) {
                    for (const attr of attrs) {
                        const value = el.getAttribute(attr);
                        if (value) dataValues.push(value);
                    }
                }
                const cells = Array.from(row.querySelectorAll('td, .cell, [class*=cell]')).filter(visible).map(textOf);
                const title = cells.find((text) => text.length > 20 && !/全托管|同款|明星卖家|广告|更新|上架/.test(text)) || rowText.slice(0, 180);
                const shopCell = cells.find((text) => text.length > 1 && text.length < 80 && !/商品|分类|价格|销量|评分|操作|全托管|同款|明星/.test(text)) || '';
                return {title, shop_name: shopCell, hrefs, data_values: dataValues, row_text: rowText};
            }"""
        )
    except Exception:
        return {}
    return _normalize_extracted_link(raw)


def _extract_link_from_yunqi_detail_modal(page: Any) -> dict[str, Any]:
    try:
        clicked = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const wanted = ['商品详情', '查看详情', '详情', '相关商品', '店内商品'];
                const nodes = [];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let textNode;
                while (textNode = walker.nextNode()) {
                    const text = (textNode.nodeValue || '').trim();
                    if (!wanted.includes(text)) continue;
                    const el = textNode.parentElement;
                    if (!el || !visible(el)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.y < 250) continue;
                    nodes.push({el, y: rect.y, x: rect.x, text});
                }
                nodes.sort((a, b) => a.y - b.y || a.x - b.x);
                const target = nodes[0];
                if (!target) return false;
                target.el.scrollIntoView({block: 'center', inline: 'nearest'});
                target.el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                target.el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                target.el.click();
                return true;
            }"""
        )
        if not clicked:
            return {}
        page.wait_for_timeout(2500)
        raw = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const roots = Array.from(document.querySelectorAll('.el-dialog, .ant-modal, [role=dialog], .modal, body')).filter(visible);
                const root = roots[0] || document.body;
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const hrefs = Array.from(root.querySelectorAll('a[href]')).map((a) => new URL(a.getAttribute('href') || '', location.href).href);
                const dataValues = [];
                const attrs = ['href', 'data-url', 'data-href', 'data-link', 'data-goods-id', 'data-id', 'title', 'onclick', 'src'];
                for (const el of Array.from(root.querySelectorAll('*'))) {
                    for (const attr of attrs) {
                        const value = el.getAttribute(attr);
                        if (value) dataValues.push(value);
                    }
                }
                return {hrefs, data_values: dataValues, row_text: textOf(root)};
            }"""
        )
        return _normalize_extracted_link(raw)
    except Exception:
        return {}


def _normalize_extracted_link(raw: dict[str, Any]) -> dict[str, Any]:
    title = str(raw.get("title") or "").strip()
    shop_name = str(raw.get("shop_name") or "").strip()
    row_text = str(raw.get("row_text") or "")
    values: list[str] = []
    for key in ("hrefs", "data_values"):
        for value in raw.get(key, []) or []:
            if value:
                values.append(str(value))
    values.append(row_text)

    product_url = ""
    goods_id = ""
    for value in values:
        candidate = value.strip()
        if not candidate:
            continue
        if "temu.com" in candidate and _looks_like_temu_product_ref(candidate):
            product_url = candidate
            goods_id = _extract_goods_id(candidate)
            break
        goods_id = _extract_goods_id(candidate)
        if goods_id:
            product_url = _build_temu_product_url(goods_id)
            break

    return {
        "title": title or row_text[:180],
        "shop_name": shop_name,
        "goods_id": goods_id,
        "href": product_url,
        "product_url": product_url,
        "row_text": row_text[:600],
    }


def _looks_like_temu_product_ref(value: str) -> bool:
    return bool(re.search(r"(goods_id=|goods\.html|-[pg]-\d+|/product/|/item/)", value, re.I))


def _extract_goods_id(value: str) -> str:
    patterns = [
        r"goods_id[=/:%3D]+(\d{8,})",
        r"/goods(?:\.html)?[^\\s\"']*?(\d{8,})",
        r"-g-(\d{8,})",
        r"search_key[=/:%3D]+(\d{8,})",
        r"(?:Main|main)/(\d{8,})",
        r"chart_(\d{8,})",
        r"goods[_-]?id[_-]?(\d{8,})",
        r"(?:商品ID|goods id|product id)\\D*(\d{8,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if match:
            return match.group(1)
    return ""


def _build_temu_product_url(goods_id: str) -> str:
    return f"https://www.temu.com/goods.html?goods_id={goods_id}"


def click_first_store_or_product(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "yunqi_click_first", "start", "尝试点击云启第一条店铺/商品。", page=page)
    action_result = _click_first_result_action(page)
    if action_result.get("clicked"):
        target_page = action_result.get("page") or page
        result = {
            "status": "ok",
            "source": "yunqi",
            "shop_name": action_result.get("text", ""),
            "title": action_result.get("row_text", ""),
            "url": target_page.url,
            "href": action_result.get("href", ""),
            "page": target_page,
            "action": action_result.get("action", ""),
        }
        _log(logger, "yunqi_click_first", "ok", f"已点击云启第一条结果操作: {result['action']} -> {target_page.url}", page=target_page, extra={k: v for k, v in result.items() if k != "page"})
        if state:
            state.update(yunqi_click_first={k: v for k, v in result.items() if k != "page"})
        return result

    candidate = _first_result_anchor(page)
    if not candidate:
        screenshot_path = take_screenshot(page, "yunqi_click_first_missing")
        result = {
            "status": "manual_required",
            "message": "未找到可点击的第一条店铺/商品链接。",
            "screenshot_path": screenshot_path,
            "visible_text": _visible_text_sample(page),
        }
        _log(logger, "yunqi_click_first", "manual_required", result["message"], page=page, screenshot_path=screenshot_path, extra=result)
        if state:
            state.update(yunqi_click_first=result)
        return result

    before_url = page.url
    context = page.context
    new_page = None
    try:
        with context.expect_page(timeout=5000) as popup_info:
            page.evaluate("(index) => window.__yunqiCandidateLinks[index].click()", candidate["index"])
        new_page = popup_info.value
        _wait_ready(new_page)
    except Exception:
        try:
            page.evaluate("(index) => window.__yunqiCandidateLinks[index].click()", candidate["index"])
            page.wait_for_timeout(2500)
        except Exception:
            pass

    target_page = new_page or page
    if not new_page and page.url == before_url and candidate.get("href"):
        target_page.goto(candidate["href"], wait_until="domcontentloaded")
        _wait_ready(target_page)

    result = {
        "status": "ok",
        "source": "yunqi",
        "shop_name": candidate.get("text", "")[:120],
        "title": candidate.get("title", "")[:180],
        "url": target_page.url,
        "href": candidate.get("href", ""),
        "page": target_page,
    }
    _log(logger, "yunqi_click_first", "ok", f"已进入云启第一条结果: {target_page.url}", page=target_page, extra={k: v for k, v in result.items() if k != "page"})
    if state:
        state.update(yunqi_click_first={k: v for k, v in result.items() if k != "page"})
    return result


def click_first_store_or_product(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    """Click the first Yunqi result, preferring the store logo/avatar image."""
    _log(logger, "yunqi_click_first", "start", "Clicking first Yunqi store logo/avatar.", page=page)
    result_screenshot = take_screenshot(page, "full_yunqi_result")

    image_result = _click_first_store_image(page)
    if image_result.get("clicked"):
        target_page = image_result.get("page") or page
        result = {
            "status": "ok",
            "source": "yunqi",
            "shop_name": image_result.get("shop_name", ""),
            "title": image_result.get("row_text", ""),
            "url": target_page.url,
            "href": image_result.get("href", ""),
            "page": target_page,
            "action": "first_store_image",
            "screenshot_path": result_screenshot,
        }
        _log(logger, "yunqi_click_first", "ok", f"Clicked first Yunqi store image/logo -> {target_page.url}", page=target_page, screenshot_path=result_screenshot, extra={k: v for k, v in result.items() if k != "page"})
        if state:
            state.update(yunqi_click_first={k: v for k, v in result.items() if k != "page"})
        return result

    action_result = _click_first_result_action(page)
    if action_result.get("clicked"):
        target_page = action_result.get("page") or page
        result = {
            "status": "ok",
            "source": "yunqi",
            "shop_name": action_result.get("text", ""),
            "title": action_result.get("row_text", ""),
            "url": target_page.url,
            "href": action_result.get("href", ""),
            "page": target_page,
            "action": action_result.get("action", ""),
            "screenshot_path": result_screenshot,
        }
        _log(logger, "yunqi_click_first", "ok", f"Clicked Yunqi fallback action {result['action']} -> {target_page.url}", page=target_page, screenshot_path=result_screenshot, extra={k: v for k, v in result.items() if k != "page"})
        if state:
            state.update(yunqi_click_first={k: v for k, v in result.items() if k != "page"})
        return result

    candidate = _first_result_anchor(page)
    if not candidate:
        screenshot_path = take_screenshot(page, "yunqi_click_first_missing")
        result = {
            "status": "manual_required",
            "message": "No clickable Yunqi first result was found.",
            "screenshot_path": screenshot_path,
            "visible_text": _visible_text_sample(page),
        }
        _log(logger, "yunqi_click_first", "manual_required", result["message"], page=page, screenshot_path=screenshot_path, extra=result)
        if state:
            state.update(yunqi_click_first=result)
        return result

    before_url = page.url
    context = page.context
    new_page = None
    try:
        with context.expect_page(timeout=5000) as popup_info:
            page.evaluate("(index) => window.__yunqiCandidateLinks[index].click()", candidate["index"])
        new_page = popup_info.value
        _wait_ready(new_page)
    except Exception:
        try:
            page.evaluate("(index) => window.__yunqiCandidateLinks[index].click()", candidate["index"])
            page.wait_for_timeout(2500)
        except Exception:
            pass

    target_page = new_page or page
    if not new_page and page.url == before_url and candidate.get("href"):
        target_page.goto(candidate["href"], wait_until="domcontentloaded")
        _wait_ready(target_page)

    result = {
        "status": "ok",
        "source": "yunqi",
        "shop_name": candidate.get("text", "")[:120],
        "title": candidate.get("title", "")[:180],
        "url": target_page.url,
        "href": candidate.get("href", ""),
        "page": target_page,
        "screenshot_path": result_screenshot,
    }
    _log(logger, "yunqi_click_first", "ok", f"Opened Yunqi first result -> {target_page.url}", page=target_page, screenshot_path=result_screenshot, extra={k: v for k, v in result.items() if k != "page"})
    if state:
        state.update(yunqi_click_first={k: v for k, v in result.items() if k != "page"})
    return result


def _click_first_store_image(page: Any) -> dict[str, Any]:
    context = page.context
    before_url = page.url
    try:
        candidate = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const textOf = (el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                const header = Array.from(document.querySelectorAll('th, .el-table__header th, .ant-table-thead th, [class*=header] [class*=cell]'))
                    .filter(visible)
                    .find((el) => textOf(el) === '店铺' || textOf(el).includes('店铺'));
                const headerRect = header ? header.getBoundingClientRect() : null;
                const rows = Array.from(document.querySelectorAll('tbody tr, .el-table__row, .ant-table-row, .vxe-body--row, [class*=table] [class*=row], [class*=goods], [class*=shop], [class*=store]'))
                    .filter(visible)
                    .map((row) => {
                        const rect = row.getBoundingClientRect();
                        const images = Array.from(row.querySelectorAll('img')).filter((img) => {
                            const r = img.getBoundingClientRect();
                            const src = img.getAttribute('src') || '';
                            return visible(img) && r.y > 160 && r.width >= 28 && r.height >= 28 && !/icon|sprite|arrow|logo-yunqi|captcha/i.test(src);
                        }).map((img) => {
                            const r = img.getBoundingClientRect();
                            let score = 0;
                            if (headerRect) {
                                const center = r.x + r.width / 2;
                                if (center >= headerRect.x - 20 && center <= headerRect.right + 20) score += 200;
                            }
                            if (r.width >= 40 && r.height >= 40) score += 10;
                            return {img, score, x: r.x, y: r.y};
                        }).sort((a, b) => b.score - a.score || a.y - b.y || a.x - b.x).map((item) => item.img);
                        return {row, rect, images, text: textOf(row)};
                    })
                    .filter((item) => item.images.length > 0 && item.rect.y > 120 && item.rect.height > 20)
                    .sort((a, b) => a.rect.y - b.rect.y || a.rect.x - b.rect.x);

                let rowItem = rows[0];
                let img = rowItem ? rowItem.images[0] : null;
                if (!img) {
                    const images = Array.from(document.querySelectorAll('img')).filter((candidate) => {
                        const r = candidate.getBoundingClientRect();
                        const src = candidate.getAttribute('src') || '';
                        return visible(candidate) && r.y > 220 && r.width >= 36 && r.height >= 36 && !/icon|sprite|arrow|logo-yunqi|captcha/i.test(src);
                    }).sort((a, b) => a.getBoundingClientRect().y - b.getBoundingClientRect().y || a.getBoundingClientRect().x - b.getBoundingClientRect().x);
                    img = images[0] || null;
                    rowItem = img ? {row: img.closest('tr, .el-table__row, .ant-table-row, .vxe-body--row, [class*=goods], [class*=shop], [class*=store]') || img.parentElement, text: ''} : null;
                }
                if (!img) return {clicked: false, reason: 'store image not found'};

                const target = img.closest('a, button, [role=button], [onclick], .cursor-pointer, .link, [class*=link]') || img;
                const row = rowItem && rowItem.row ? rowItem.row : target.closest('tr, .el-table__row, .ant-table-row') || target.parentElement;
                const hrefNode = target.closest('a[href]') || (row ? row.querySelector('a[href]') : null);
                const href = hrefNode ? new URL(hrefNode.getAttribute('href') || '', location.href).href : '';
                const shopNameNode = row ? row.querySelector('[class*=shop], [class*=store], a, span') : null;
                const shopName = shopNameNode ? textOf(shopNameNode).slice(0, 120) : '';
                const rowText = row ? textOf(row).slice(0, 300) : '';
                target.scrollIntoView({block: 'center', inline: 'nearest'});
                target.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                target.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                target.click();
                return {clicked: true, href, shop_name: shopName, row_text: rowText, img_alt: img.getAttribute('alt') || '', img_src: img.getAttribute('src') || ''};
            }"""
        )
    except Exception as exc:
        return {"clicked": False, "reason": str(exc)}

    if not candidate.get("clicked"):
        return candidate

    page.wait_for_timeout(3500)
    target_page = _latest_new_page(context, page, before_url) or page
    if target_page != page:
        _wait_ready(target_page)
    elif page.url == before_url and not candidate.get("href"):
        return {"clicked": False, "reason": "store image clicked but did not open a new page"}

    if target_page == page and page.url == before_url and candidate.get("href"):
        try:
            page.goto(candidate["href"], wait_until="domcontentloaded")
            _wait_ready(page)
        except Exception:
            return {"clicked": False, "reason": "store image href navigation failed"}

    if "temu.com" not in target_page.url.lower():
        return {"clicked": False, "reason": f"store image did not navigate to Temu: {target_page.url}", "row_text": candidate.get("row_text", "")}

    candidate["page"] = target_page
    candidate["url"] = target_page.url
    return candidate


def _first_result_anchor(page: Any) -> dict[str, Any] | None:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const anchors = Array.from(document.querySelectorAll('a[href]')).filter(visible).map((el, index) => {
                    const href = new URL(el.getAttribute('href') || '', location.href).href;
                    const text = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
                    const rect = el.getBoundingClientRect();
                    let score = 0;
                    if (/beian\\.miit\\.gov\\.cn|icp|privacy|agreement|help|download|customer|客服|下载|协议|备案/i.test(href + ' ' + text)) score -= 200;
                    if (/temu\\.com/i.test(href)) score += 80;
                    if (/店铺|店铺详情|商品|查看|详情|链接/.test(text)) score += 25;
                    if (/goods|product|shop|store|mall|temu/i.test(href)) score += 20;
                    if (/登录|注册|帮助|客服|下载|充值/.test(text + href)) score -= 50;
                    if (rect.width > 20 && rect.height > 10) score += 5;
                    return {el, index, href, text, title: el.getAttribute('title') || '', y: rect.y, x: rect.x, score};
                }).filter((item) => item.score > 0).sort((a, b) => b.score - a.score || a.y - b.y || a.x - b.x);
                window.__yunqiCandidateLinks = anchors.map((item) => item.el);
                if (!anchors.length) return null;
                const first = anchors[0];
                return {index: 0, href: first.href, text: first.text, title: first.title, score: first.score};
            }"""
        )
    except Exception:
        return None


def _click_first_result_action(page: Any) -> dict[str, Any]:
    context = page.context
    before_url = page.url
    try:
        action = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const actionTexts = ['监控店铺', '店内商品', '商品详情', '查看商品', '详情', '查看详情'];
                const nodes = [];
                const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                let textNode;
                while (textNode = walker.nextNode()) {
                    const text = (textNode.nodeValue || '').trim();
                    if (!actionTexts.includes(text)) continue;
                    const el = textNode.parentElement;
                    if (!el || !visible(el)) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.y < 450) continue;
                    nodes.push({el, text, y: rect.y, x: rect.x});
                }
                nodes.sort((a, b) => a.y - b.y || a.x - b.x);
                const target = nodes[0];
                if (!target) return {clicked: false};
                const row = target.el.closest('tr, .el-table__row, .goods-item') || target.el.parentElement;
                const rowText = row ? (row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 300) : '';
                target.el.scrollIntoView({block: 'center', inline: 'nearest'});
                target.el.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                target.el.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                target.el.click();
                return {clicked: true, action: target.text, text: target.text, row_text: rowText};
            }"""
        )
    except Exception as exc:
        return {"clicked": False, "message": str(exc)}

    if not action.get("clicked"):
        return action

    new_page = None
    try:
        page.wait_for_timeout(2500)
        new_page = _latest_new_page(context, page, before_url)
        if not new_page and "yunqishuju.com" in page.url:
            clicked_icon = False
            for _ in range(10):
                clicked_icon = _click_modal_product_link_icon(page)
                if clicked_icon:
                    break
                page.wait_for_timeout(500)
            if clicked_icon:
                action["modal_product_link_clicked"] = True
            page.wait_for_timeout(4000)
            new_page = _latest_new_page(context, page, before_url)
        if new_page:
            _wait_ready(new_page)
    except Exception:
        new_page = None

    target_page = new_page or page
    action["page"] = target_page
    action["url"] = target_page.url
    action["href"] = target_page.url if target_page.url != before_url else ""
    return action


def _latest_new_page(context: Any, current_page: Any, before_url: str) -> Any | None:
    pages = [candidate for candidate in context.pages if candidate != current_page]
    opened_pages = [
        candidate
        for candidate in pages
        if candidate.url not in {"about:blank", before_url} and not candidate.url.startswith("chrome://")
    ]
    if not opened_pages:
        return None
    temu_pages = [candidate for candidate in opened_pages if "temu.com" in candidate.url.lower()]
    return (temu_pages or opened_pages)[-1]


def _click_modal_product_link_icon(page: Any) -> bool:
    try:
        return bool(page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const icon = Array.from(document.querySelectorAll('.image-wrapper_20 img, img.el-tooltip')).find((img) => {
                    const src = img.getAttribute('src') || '';
                    const rect = img.getBoundingClientRect();
                    return visible(img) && (src.includes('icon-splj') || (rect.x > 700 && rect.y > 450 && rect.y < 650));
                });
                if (!icon) return false;
                icon.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, view: window}));
                icon.dispatchEvent(new MouseEvent('mouseup', {bubbles: true, cancelable: true, view: window}));
                icon.click();
                return true;
            }"""
        ))
    except Exception:
        return False


def _click_any_text(page: Any, texts: list[str], timeout: int = 1200, button_like: bool = False) -> bool:
    selectors = []
    if button_like:
        selectors.extend([f'button:has-text("{text}")' for text in texts])
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


def _looks_like_login(page: Any) -> bool:
    url = page.url.lower()
    if any(token in url for token in ["login", "signin", "passport", "auth"]):
        return True
    try:
        visible_password_inputs = page.locator('input[type="password"]:visible').count()
        if visible_password_inputs > 0:
            return True
    except Exception:
        pass
    try:
        login_form_markers = page.locator(
            'form:has(input[type="password"]), '
            'div:has(input[type="password"]):has-text("登录"), '
            'div:has(input[type="password"]):has-text("密码")'
        ).count()
        return login_form_markers > 0
    except Exception:
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


def _visible_text_sample(page: Any) -> str:
    text = body_text(page, timeout=2000)
    return text[:2000]


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
