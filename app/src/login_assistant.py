from __future__ import annotations

import os
import re
from typing import Any

from .captcha_guard import check_and_wait_if_captcha, has_captcha
from .utils import ManualRequiredError, body_text, take_screenshot
from .windows_prompt import show_manual_action_popup, wait_user_decision, UserChoseSkip, UserChoseStop


def ensure_yunqi_login(page: Any, browser_name: str = "", logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    return _ensure_site_login(
        page=page,
        browser_name=browser_name,
        site="yunqi",
        username=os.getenv("YUNQI_USERNAME", "").strip(),
        password=os.getenv("YUNQI_PASSWORD", "").strip(),
        login_url_markers=["login", "passport", "auth"],
        login_text_markers=["登录", "账号", "手机号", "密码", "验证码", "Sign in", "Login"],
        username_selectors=[
            'input[type="tel"]',
            'input[type="text"]',
            'input[name*="phone" i]',
            'input[name*="mobile" i]',
            'input[name*="user" i]',
            'input[name*="account" i]',
            'input[placeholder*="手机号"]',
            'input[placeholder*="账号"]',
        ],
        password_selectors=['input[type="password"]', 'input[placeholder*="密码"]'],
        button_texts=["登录", "立即登录", "Sign in", "Login"],
        logger=logger,
        state=state,
    )


def ensure_dxm_login(page: Any, browser_name: str = "", logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    return _ensure_site_login(
        page=page,
        browser_name=browser_name,
        site="dianxiaomi",
        username=os.getenv("DXM_USERNAME", "").strip(),
        password=os.getenv("DXM_PASSWORD", "").strip(),
        login_url_markers=["login", "passport", "auth"],
        login_text_markers=["登录", "店小秘", "账号", "密码", "验证码", "Sign in", "Login"],
        username_selectors=[
            'input[type="text"]',
            'input[type="email"]',
            'input[name*="user" i]',
            'input[name*="account" i]',
            'input[placeholder*="账号"]',
            'input[placeholder*="用户名"]',
            'input[placeholder*="邮箱"]',
        ],
        password_selectors=['input[type="password"]', 'input[placeholder*="密码"]'],
        button_texts=["登录", "立即登录", "Sign in", "Login"],
        logger=logger,
        state=state,
    )


def ensure_google_temu_login(page: Any, browser_name: str = "", logger: Any | None = None, state: Any | None = None) -> dict[str, Any]:
    check_and_wait_if_captcha(page, logger=logger)
    if not _looks_like_temu_or_google_login(page):
        result = {"site": "temu_google", "status": "ok", "login_required": False, "url": page.url}
        _log(logger, "login_temu_google", "ok", f"{browser_name}: Temu/Google login not required.", page=page, extra=result)
        return result

    email = os.getenv("GOOGLE_EMAIL", "").strip()
    password = os.getenv("GOOGLE_PASSWORD", "").strip()

    clicked_google = _click_any_text(page, ["Continue with Google", "Sign in with Google", "Google", "使用 Google", "Google 登录"], timeout=2500)
    if clicked_google:
        _wait_ready(page)
        page.wait_for_timeout(2500)
        target = _latest_interesting_page(page.context, page)
    else:
        target = page

    if "google." in target.url.lower() and email and password:
        _fill_google_account(target, email, password, browser_name, logger)
        _wait_ready(target)
        check_and_wait_if_captcha(target, logger=logger)

    if _looks_like_temu_or_google_login(target) or has_captcha(target):
        screenshot_path = take_screenshot(target, f"{browser_name}_temu_google_login")
        message = (
            f"{browser_name} 需要人工完成 Temu/Google 登录或验证。"
            "完成后输入 continue；跳过当前浏览器输入 skip；停止输入 stop。"
        )
        _log(logger, "login_temu_google", "manual_required", message, page=target, screenshot_path=screenshot_path)
        show_manual_action_popup(f"{browser_name} Temu/Google 登录验证", message, logger=logger)
        decision = wait_user_decision(message, logger=logger)
        if decision == "skip":
            raise UserChoseSkip(message)
        if decision == "stop":
            raise UserChoseStop(message)
        _wait_ready(target)

    result = {"site": "temu_google", "status": "ok", "login_required": True, "url": target.url}
    _log(logger, "login_temu_google", "ok", f"{browser_name}: Temu/Google login check completed.", page=target, extra=result)
    if state:
        state.update(login_temu_google=result)
    return result


def _ensure_site_login(
    *,
    page: Any,
    browser_name: str,
    site: str,
    username: str,
    password: str,
    login_url_markers: list[str],
    login_text_markers: list[str],
    username_selectors: list[str],
    password_selectors: list[str],
    button_texts: list[str],
    logger: Any | None,
    state: Any | None,
) -> dict[str, Any]:
    check_and_wait_if_captcha(page, logger=logger)
    if not _looks_like_login(page, login_url_markers, login_text_markers):
        result = {"site": site, "status": "ok", "login_required": False, "url": page.url}
        _log(logger, f"login_{site}", "ok", f"{browser_name}: {site} login not required.", page=page, extra=result)
        return result

    if username and password:
        try:
            _fill_first_visible(page, username_selectors, username, secret=False)
            _fill_first_visible(page, password_selectors, password, secret=True)
            if not _click_any_text(page, button_texts, timeout=2500):
                _click_first_submit(page)
            page.wait_for_timeout(3500)
            _wait_ready(page)
            check_and_wait_if_captcha(page, logger=logger)
        except Exception as exc:
            _log(logger, f"login_{site}", "warning", f"{browser_name}: automatic {site} login fill failed: {type(exc).__name__}", page=page)

    if _looks_like_login(page, login_url_markers, login_text_markers) or has_captcha(page):
        screenshot_path = take_screenshot(page, f"{browser_name}_{site}_login")
        message = (
            f"{browser_name} 需要人工完成 {site} 登录或验证码。"
            "完成后输入 continue；跳过当前浏览器输入 skip；停止输入 stop。"
        )
        _log(logger, f"login_{site}", "manual_required", message, page=page, screenshot_path=screenshot_path)
        show_manual_action_popup(f"{browser_name} {site} 登录验证", message, logger=logger)
        decision = wait_user_decision(message, logger=logger)
        if decision == "skip":
            raise UserChoseSkip(message)
        if decision == "stop":
            raise UserChoseStop(message)
        _wait_ready(page)

    result = {"site": site, "status": "ok", "login_required": True, "url": page.url}
    _log(logger, f"login_{site}", "ok", f"{browser_name}: {site} login check completed.", page=page, extra=result)
    if state:
        state.update(**{f"login_{site}": result})
    return result


def _fill_google_account(page: Any, email: str, password: str, browser_name: str, logger: Any | None = None) -> None:
    try:
        if _fill_first_visible(page, ['input[type="email"]', 'input[name="identifier"]'], email):
            _click_any_text(page, ["Next", "下一步"], timeout=2500)
            page.wait_for_timeout(2500)
    except Exception as exc:
        _log(logger, "login_google", "warning", f"{browser_name}: Google email fill failed: {type(exc).__name__}", page=page)

    try:
        if _fill_first_visible(page, ['input[type="password"]', 'input[name="Passwd"]'], password, secret=True):
            _click_any_text(page, ["Next", "下一步"], timeout=2500)
            page.wait_for_timeout(3500)
    except Exception as exc:
        _log(logger, "login_google", "warning", f"{browser_name}: Google password fill failed: {type(exc).__name__}", page=page)


def _looks_like_login(page: Any, url_markers: list[str], text_markers: list[str]) -> bool:
    url = page.url.lower()
    if any(marker.lower() in url for marker in url_markers):
        return True
    try:
        if page.locator('input[type="password"]:visible').count() > 0:
            text = body_text(page, timeout=1500)
            return any(marker.lower() in text.lower() for marker in text_markers)
    except Exception:
        pass
    text = body_text(page, timeout=2000)
    lowered = text.lower()
    marker_count = sum(1 for marker in text_markers if marker.lower() in lowered)
    return marker_count >= 2


def _looks_like_temu_or_google_login(page: Any) -> bool:
    url = page.url.lower()
    if any(marker in url for marker in ["login", "signin", "accounts.google", "bgn_verification"]):
        return True
    text = body_text(page, timeout=2000)
    lowered = text.lower()
    return any(
        marker.lower() in lowered
        for marker in [
            "登录 / 注册",
            "登录/注册",
            "sign in",
            "continue with google",
            "sign in with google",
            "安全验证",
            "security verification",
        ]
    )


def _fill_first_visible(page: Any, selectors: list[str], value: str, secret: bool = False) -> bool:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=1800)
            locator.fill(value, timeout=2500)
            return True
        except Exception:
            continue
    return False


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


def _click_first_submit(page: Any) -> bool:
    for selector in ['button[type="submit"]', 'input[type="submit"]', 'button:visible']:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=1500)
            locator.click(timeout=2000)
            return True
        except Exception:
            continue
    return False


def _latest_interesting_page(context: Any, fallback: Any) -> Any:
    pages = [page for page in context.pages if not page.is_closed()]
    for page in reversed(pages):
        url = page.url.lower()
        if "google." in url or "temu." in url or "temu.com" in url:
            return page
    return fallback


def _wait_ready(page: Any) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass


def _log(logger: Any | None, step: str, status: str, message: str, **kwargs: Any) -> None:
    if logger:
        logger.log_step(step, status, message, **kwargs)
    else:
        print(f"[{step}] {status}: {message}")
