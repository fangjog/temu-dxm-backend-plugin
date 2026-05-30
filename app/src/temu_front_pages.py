from __future__ import annotations

import os
import re
import sys
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from .captcha_guard import check_and_wait_if_captcha
from .utils import ManualRequiredError, body_text, take_screenshot
from .windows_prompt import show_manual_action_popup, wait_user_decision, UserChoseSkip, UserChoseStop


SECURITY_KEYWORDS = [
    "安全验证",
    "Security verification",
    "CAPTCHA",
    "Verify",
    "drag",
    "拖动",
    "滑块",
    "验证",
]

LOGIN_KEYWORDS = [
    "登录 / 注册",
    "登录/注册",
    "Sign in",
    "Register",
    "Continue with Google",
    "Sign in with Google",
]

ABNORMAL_PRODUCT_KEYWORDS = [
    "无法正常浏览商品",
    "建议清理缓存",
    "清理缓存后继续",
    "清除缓存",
    "商品已售罄",
    "This item is sold out",
    "Something went wrong",
]


def handle_temu_security(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    text = body_text(page)
    detected = any(keyword.lower() in text.lower() for keyword in SECURITY_KEYWORDS)
    handled = check_and_wait_if_captcha(page, logger=logger)
    result = {"status": "ok", "detected": detected or handled, "url": page.url}
    _log(logger, "temu_security", "ok", f"Temu security check completed, detected={result['detected']}.", page=page, extra=result)
    if state:
        state.update(temu_security=result)
    return result


def ensure_temu_login(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "temu_login", "start", "Checking Temu login state.", page=page)
    if not _looks_like_login(page):
        result = {"status": "ok", "login_required": False, "url": page.url}
        _log(logger, "temu_login", "ok", "Temu login does not appear required.", page=page, extra=result)
        return result

    clicked_google = _click_any_text(page, ["Continue with Google", "Sign in with Google", "Google"], timeout=2500)
    if clicked_google:
        page.wait_for_timeout(3000)
        _wait_ready(page)
        if not _looks_like_login(page):
            result = {"status": "ok", "login_required": True, "google_clicked": True, "url": page.url}
            _log(logger, "temu_login", "ok", "Temu Google login completed or continued from existing session.", page=page, extra=result)
            if state:
                state.update(temu_login=result)
            return result

    screenshot_path = take_screenshot(page, "temu_login_required")
    message = "Temu requires login. Please complete Google/Temu login in the isolated browser, then type continue."
    _log(logger, "temu_login", "manual_required", message, page=page, screenshot_path=screenshot_path)
    show_manual_action_popup("Temu 登录/验证", message, logger=logger)
    decision = wait_user_decision(message, logger=logger)
    if decision == "skip":
        raise UserChoseSkip(message)
    if decision == "stop":
        raise UserChoseStop(message)

    _wait_ready(page)
    result = {"status": "ok", "login_required": True, "manual_intervention": True, "url": page.url}
    _log(logger, "temu_login", "ok", "Temu login manual step completed.", page=page, extra=result)
    if state:
        state.update(temu_login=result)
    return result


def ensure_temu_region(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "temu_region", "start", "Checking Temu region/language state.", page=page)
    if "temu." not in page.url.lower() and "temu.com" not in page.url.lower():
        result = {"status": "warning", "changed": False, "url": page.url, "message": "Current page is not a Temu page."}
        _log(logger, "temu_region", "warning", result["message"], page=page, extra=result)
        if state:
            state.update(temu_region=result)
        return result
    changed = _choose_united_kingdom_if_dialog_open(page)
    result = {"status": "ok", "changed": changed, "url": page.url}
    _log(logger, "temu_region", "ok", f"Temu region check completed, changed={changed}.", page=page, extra=result)
    if state:
        state.update(temu_region=result)
    return result


def try_switch_temu_region(page: Any, region_name: str, logger: Any | None = None, state: Any | None = None) -> bool:
    _log(logger, "temu_region_switch", "start", f"Trying Temu region: {region_name}", page=page)
    if "temu.com" not in page.url.lower() and "temu." not in page.url.lower():
        _log(logger, "temu_region_switch", "warning", "Current page is not Temu; skip region switch.", page=page)
        return False

    opened = _click_any_text(
        page,
        ["Ship to", "Country/region", "Country", "Region", "Deliver to", "送至", "国家/地区", "United Kingdom", "United States"],
        timeout=1200,
    )
    if not opened:
        _log(logger, "temu_region_switch", "warning", "Could not find Temu region entry.", page=page)
        return False

    page.wait_for_timeout(1000)
    selected = _select_region_in_open_dialog(page, region_name)
    if not selected:
        screenshot_path = take_screenshot(page, "temu_region_switch_failed")
        _log(logger, "temu_region_switch", "warning", f"Could not select region: {region_name}", page=page, screenshot_path=screenshot_path)
        return False

    page.wait_for_timeout(800)
    _click_any_text(page, ["Save", "Confirm", "Apply", "Done", "确定", "确认", "保存"], timeout=1800)
    page.wait_for_timeout(1000)
    try:
        page.reload(wait_until="domcontentloaded")
        _wait_ready(page)
    except Exception:
        pass
    handle_temu_security(page, logger=logger, state=state)
    visible = _has_visible_product_cards(page) or (_is_temu_product_url(page.url) and is_temu_product_page_healthy(page))
    _log(logger, "temu_region_switch", "ok" if visible else "warning", f"Region {region_name} selected, products_visible={visible}.", page=page, extra={"region": region_name, "products_visible": visible})
    return visible


def recover_temu_shop_visibility_with_regions(page: Any, config: dict[str, Any], logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    regions = list(config.get("temu_recovery", {}).get("regions", [])) or ["United Kingdom", "United States", "Germany", "France"]
    max_attempts = int(config.get("temu_recovery", {}).get("max_region_switch_per_browser", 3))
    tried: list[str] = []
    for region in regions[:max_attempts]:
        tried.append(region)
        if try_switch_temu_region(page, region, logger=logger, state=state):
            result = {"status": "ok", "visible": True, "tried_regions": tried, "url": page.url}
            if state:
                state.update(temu_region_recovery=result)
            return result
    screenshot_path = take_screenshot(page, "temu_region_recovery_failed")
    result = {"status": "failed", "visible": False, "tried_regions": tried, "url": page.url, "screenshot_path": screenshot_path}
    _log(logger, "temu_region_recovery", "warning", f"Temu products not visible after region recovery: {tried}", page=page, screenshot_path=screenshot_path, extra=result)
    if state:
        state.update(temu_region_recovery=result)
    return result


def ensure_temu_shop_products_visible(page: Any, yunqi_page: Any | None = None, logger: Any | None = None, state: Any | None = None) -> Any:
    """Return a Temu shop page that visibly contains product cards."""
    current_page = page
    for attempt in range(1, 4):
        _log(logger, "temu_shop_products", "start", f"Checking Temu shop products, attempt {attempt}/3.", page=current_page)
        handle_temu_security(current_page, logger=logger, state=state)
        ensure_temu_login(current_page, logger=logger, state=state)
        if _is_temu_product_url(current_page.url) and is_temu_product_page_healthy(current_page):
            _log(logger, "temu_shop_products", "ok", "Current Temu page is already a healthy product detail page.", page=current_page, extra={"attempt": attempt})
            return current_page
        if _has_visible_product_cards(current_page):
            _log(logger, "temu_shop_products", "ok", "Visible Temu product cards found.", page=current_page, extra={"attempt": attempt})
            return current_page

        screenshot_path = take_screenshot(current_page, f"temu_shop_no_products_attempt_{attempt}")
        _log(logger, "temu_shop_products", "warning", "No visible product cards on Temu shop page.", page=current_page, screenshot_path=screenshot_path, extra={"attempt": attempt})

        _switch_region_to_united_kingdom(current_page, logger=logger)
        try:
            current_page.reload(wait_until="domcontentloaded")
            _wait_ready(current_page)
        except Exception:
            pass

        if _has_visible_product_cards(current_page):
            _log(logger, "temu_shop_products", "ok", "Visible products found after region refresh.", page=current_page, extra={"attempt": attempt})
            return current_page

        if yunqi_page is not None:
            try:
                from .yunqi_pages import click_first_store_or_product

                clicked = click_first_store_or_product(yunqi_page, logger=logger, state=state)
                candidate = clicked.get("page") or current_page
                if "temu." in candidate.url.lower() or "temu.com" in candidate.url.lower():
                    current_page = candidate
                    continue
            except Exception as exc:
                _log(logger, "temu_shop_products", "warning", f"Re-clicking Yunqi store image failed: {exc}", page=current_page)

    screenshot_path = take_screenshot(current_page, "temu_shop_products_manual")
    message = "Temu shop still has no visible product cards after 3 attempts."
    _log(logger, "temu_shop_products", "manual_required", message, page=current_page, screenshot_path=screenshot_path)
    if state:
        state.update(status="manual_required", manual_step="temu_shop_products", screenshot_path=screenshot_path)
    raise ManualRequiredError("temu_shop_products", message, screenshot_path)


def extract_first_product_link_from_temu_shop(page: Any, logger: Any | None = None, state: Any | None = None) -> str:
    direct = _direct_goods_url_from_current_page(page)
    if direct:
        _log(logger, "temu_product_detail", "ok", f"Built direct Temu goods URL from current page: {direct}", page=page)
        return direct

    links = _extract_product_links_from_shop(page)
    link = links[0] if links else ""
    if link:
        _log(logger, "temu_product_detail", "ok", f"Extracted first Temu product detail link: {link}", page=page)
        return str(link)

    screenshot_path = take_screenshot(page, "temu_product_link_missing")
    message = "Could not extract a Temu product detail link from the shop page."
    _log(logger, "temu_product_detail", "manual_required", message, page=page, screenshot_path=screenshot_path)
    if state:
        state.update(status="manual_required", manual_step="temu_product_link_missing", screenshot_path=screenshot_path)
    raise ManualRequiredError("temu_product_detail", message, screenshot_path)


def _extract_product_links_from_shop(page: Any) -> list[str]:
    try:
        links = page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const seen = new Set();
                const items = Array.from(document.querySelectorAll('a[href]')).filter(visible).map((a) => {
                    const href = new URL(a.getAttribute('href') || '', location.href).href;
                    const text = (a.innerText || a.textContent || '').trim();
                    const rect = a.getBoundingClientRect();
                    let score = 0;
                    if (!/temu\\.com/i.test(href)) score -= 300;
                    if (/goods_id=|goods\\.html|product|item|\\-g-\\d+/i.test(href)) score += 100;
                    if (a.querySelector('img')) score += 30;
                    if (/activity|promo|coupon|affiliate|login|support|help|sitemap|about|privacy|terms|category/i.test(href)) score -= 200;
                    if (rect.width > 80 && rect.height > 80) score += 10;
                    return {href, text, y: rect.y, x: rect.x, score};
                }).filter((item) => item.score > 50).sort((a, b) => b.score - a.score || a.y - b.y || a.x - b.x);
                const result = [];
                for (const item of items) {
                    const clean = item.href.split('#')[0];
                    if (seen.has(clean)) continue;
                    seen.add(clean);
                    result.push(clean);
                    if (result.length >= 12) break;
                }
                return result;
            }"""
        )
        return [str(link) for link in links if link]
    except Exception:
        return []


def _legacy_extract_first_product_link(page: Any) -> str:
    return page.evaluate(
        """() => {
            const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
            const items = Array.from(document.querySelectorAll('a[href]')).filter(visible).map((a) => {
                const href = new URL(a.getAttribute('href') || '', location.href).href;
                const text = (a.innerText || a.textContent || '').trim();
                const rect = a.getBoundingClientRect();
                let score = 0;
                if (!/temu\\.com/i.test(href)) score -= 300;
                if (/goods_id=|goods\\.html|product|item|\\-g-\\d+/i.test(href)) score += 100;
                if (a.querySelector('img')) score += 30;
                if (/activity|promo|coupon|affiliate|login|support|help|sitemap|about|privacy|terms|category/i.test(href)) score -= 200;
                if (rect.width > 80 && rect.height > 80) score += 10;
                return {href, text, y: rect.y, x: rect.x, score};
            }).filter((item) => item.score > 50).sort((a, b) => b.score - a.score || a.y - b.y || a.x - b.x);
            return items.length ? items[0].href : '';
        }"""
    )


def get_any_product_detail_from_visible_shop(page: Any, logger: Any | None = None, state: Any | None = None) -> str:
    if _is_temu_product_url(page.url):
        return page.url
    if not _has_visible_product_cards(page):
        raise ManualRequiredError("temu_shop_products", "Temu shop has no visible product cards.", take_screenshot(page, "temu_shop_no_products_for_detail"))
    links = _extract_product_links_from_shop(page)
    if not links:
        product_url = extract_first_product_link_from_temu_shop(page, logger=logger, state=state)
        links = [product_url]

    shop_url = page.url
    last_screenshot = ""
    for index, product_url in enumerate(links[:8], start=1):
        absolute_url = urljoin(shop_url, product_url)
        _log(logger, "temu_product_detail", "start", f"Opening Temu product candidate {index}/{min(len(links), 8)}: {absolute_url}", page=page)
        page.goto(absolute_url, wait_until="domcontentloaded")
        _wait_ready(page)
        handle_temu_security(page, logger=logger, state=state)
        ensure_temu_login(page, logger=logger, state=state)
        if is_temu_product_page_healthy(page):
            _log(logger, "temu_product_detail", "ok", f"Healthy Temu product candidate selected: {page.url}", page=page)
            return page.url
        last_screenshot = take_screenshot(page, f"temu_product_candidate_unhealthy_{index}")
        _log(logger, "temu_product_detail", "warning", f"Temu product candidate {index} is sold out or unhealthy; trying next.", page=page, screenshot_path=last_screenshot)

    raise ManualRequiredError("temu_product_detail", "No healthy Temu product detail page found from visible shop products.", last_screenshot)


def ensure_temu_product_detail(page: Any, logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    _log(logger, "temu_product_detail", "start", "Confirming or opening Temu product detail page.", page=page)
    handle_temu_security(page, logger=logger, state=state)
    ensure_temu_login(page, logger=logger, state=state)

    product_url = _direct_goods_url_from_current_page(page) or page.url
    if not _is_temu_product_url(product_url):
        ensure_temu_shop_products_visible(page, logger=logger, state=state)
        product_url = get_any_product_detail_from_visible_shop(page, logger=logger, state=state)
    if product_url != page.url:
        page.goto(product_url, wait_until="domcontentloaded")
        _wait_ready(page)
        handle_temu_security(page, logger=logger, state=state)
        ensure_temu_login(page, logger=logger, state=state)

    if not is_temu_product_page_healthy(page):
        screenshot_path = take_screenshot(page, "temu_product_detail_manual")
        message = "Temu product detail page is not healthy enough for Dianxiaomi collection."
        _log(logger, "temu_product_detail", "manual_required", message, page=page, screenshot_path=screenshot_path, extra={"text_preview": body_text(page, timeout=1500)[:1000]})
        if state:
            state.update(status="manual_required", manual_step="temu_product_detail", screenshot_path=screenshot_path)
        raise ManualRequiredError("temu_product_detail", message, screenshot_path)

    screenshot_path = take_screenshot(page, "full_temu_product")
    result = {"status": "ok", "product_url": page.url, "screenshot_path": screenshot_path}
    _log(logger, "temu_product_detail", "ok", f"Temu product detail page ready: {page.url}", page=page, screenshot_path=screenshot_path, extra=result)
    if state:
        state.update(temu_product_detail=result)
    return result


def is_temu_product_page_healthy(page: Any) -> bool:
    url = page.url.lower()
    if "temu.com" not in url or not _is_temu_product_url(page.url):
        return False
    text = body_text(page, timeout=2500)
    lowered = text.lower()
    if any(keyword.lower() in lowered for keyword in LOGIN_KEYWORDS + ABNORMAL_PRODUCT_KEYWORDS):
        return False
    try:
        return bool(page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const text = document.body ? document.body.innerText : '';
                const hasPrice = /[$£€]\\s*\\d|\\d+[.,]\\d{2}|CA\\$|US\\$/.test(text);
                const images = Array.from(document.querySelectorAll('img')).filter((img) => {
                    const r = img.getBoundingClientRect();
                    const src = img.getAttribute('src') || '';
                    return visible(img) && r.width > 120 && r.height > 120 && !/captcha|logo|icon|sprite/i.test(src);
                });
                const hasTitle = Array.from(document.querySelectorAll('h1, h2, [data-testid*=title], [class*=title]')).some((el) => visible(el) && (el.innerText || el.textContent || '').trim().length > 8);
                return hasPrice && images.length > 0 && (hasTitle || text.length > 1200);
            }"""
        ))
    except Exception:
        return False


def _has_visible_product_cards(page: Any) -> bool:
    if "temu.com" not in page.url.lower() and "temu." not in page.url.lower():
        return False
    try:
        return bool(page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                const links = Array.from(document.querySelectorAll('a[href]')).filter((a) => {
                    if (!visible(a)) return false;
                    const href = new URL(a.getAttribute('href') || '', location.href).href;
                    if (!/temu\\.com/i.test(href)) return false;
                    if (!/(goods_id=|goods\\.html|product|item|-g-\\d+)/i.test(href)) return false;
                    if (/activity|promo|coupon|login|support|help|sitemap|privacy|terms|category/i.test(href)) return false;
                    const r = a.getBoundingClientRect();
                    return r.width > 60 && r.height > 60;
                });
                if (links.length > 0) return true;
                const cards = Array.from(document.querySelectorAll('[class*=goods], [class*=product], [data-testid*=product]')).filter((el) => {
                    const r = el.getBoundingClientRect();
                    return visible(el) && r.width > 80 && r.height > 80 && el.querySelector('img');
                });
                return cards.length > 0;
            }"""
        ))
    except Exception:
        return False


def _looks_like_login(page: Any) -> bool:
    url = page.url.lower()
    if "login" in url or "signin" in url:
        return True
    text = body_text(page, timeout=2000)
    lowered = text.lower()
    return any(keyword.lower() in lowered for keyword in LOGIN_KEYWORDS)


def _switch_region_to_united_kingdom(page: Any, logger: Any | None = None) -> bool:
    changed = _choose_united_kingdom_if_dialog_open(page)
    if changed:
        _log(logger, "temu_region", "ok", "Selected United Kingdom from an already-open region dialog.", page=page)
        return True

    opened = _click_any_text(
        page,
        ["Ship to", "Country/region", "Country", "Region", "United States", "Canada", "Deliver to", "送至", "国家/地区"],
        timeout=1200,
    )
    if not opened:
        _log(logger, "temu_region", "warning", "Could not find Temu region entry; continuing without forced region switch.", page=page)
        return False
    page.wait_for_timeout(1000)
    changed = _choose_united_kingdom_if_dialog_open(page)
    if changed:
        _log(logger, "temu_region", "ok", "Switched Temu region to United Kingdom.", page=page)
    else:
        _log(logger, "temu_region", "warning", "Region dialog opened but United Kingdom option was not selected.", page=page)
    return changed


def _choose_united_kingdom_if_dialog_open(page: Any) -> bool:
    if not _click_any_text(page, ["United Kingdom", "英国", "UK"], timeout=1000):
        return False
    page.wait_for_timeout(500)
    _click_any_text(page, ["Save", "Confirm", "Apply", "Done", "确定", "确认", "保存"], timeout=1400)
    page.wait_for_timeout(1000)
    return True


def _select_region_in_open_dialog(page: Any, region_name: str) -> bool:
    aliases = {
        "United Kingdom": ["United Kingdom", "英国", "UK"],
        "United States": ["United States", "美国", "USA"],
        "Germany": ["Germany", "德国", "Deutschland"],
        "France": ["France", "法国"],
    }
    labels = aliases.get(region_name, [region_name])

    if _click_any_text(page, labels, timeout=1500):
        return True

    try:
        inputs = page.locator('input:visible')
        count = min(inputs.count(), 5)
        for idx in range(count):
            item = inputs.nth(idx)
            try:
                item.fill(region_name, timeout=1000)
                page.wait_for_timeout(500)
                if _click_any_text(page, labels, timeout=1500):
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


def _is_temu_product_url(url: str) -> bool:
    if re.search(r"(sitemap|help|support|privacy|terms|category|login)", url, re.I):
        return False
    return bool(re.search(r"(goods_id=|goods\.html|product|item|-[pg]-\d+|_bg_fs=)", url, re.I))


def _direct_goods_url_from_current_page(page: Any) -> str:
    parsed = urlparse(page.url)
    query = parse_qs(parsed.query)
    search_key = (query.get("search_key") or [""])[0]
    if re.fullmatch(r"\d{8,}", search_key or ""):
        return f"https://www.temu.com/uk/goods.html?goods_id={search_key}"
    from_url = unquote((query.get("from") or [""])[0])
    from_query = parse_qs(urlparse(from_url).query)
    nested_search_key = (from_query.get("search_key") or [""])[0]
    if re.fullmatch(r"\d{8,}", nested_search_key or ""):
        return f"https://www.temu.com/uk/goods.html?goods_id={nested_search_key}"
    text = body_text(page, timeout=1500)
    match = re.search(r"(?:商品ID|goods_id|product id)\D*(\d{8,})", text, re.I)
    if match:
        return f"https://www.temu.com/uk/goods.html?goods_id={match.group(1)}"
    return ""


def _click_any_text(page: Any, texts: list[str], timeout: int = 1000) -> bool:
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
