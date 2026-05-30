from __future__ import annotations

import time
from typing import Any

from .utils import ManualRequiredError, body_text, take_screenshot
from .windows_prompt import show_manual_action_popup, wait_user_decision, UserChoseSkip, UserChoseStop


CAPTCHA_KEYWORDS = [
    "安全验证",
    "验证码",
    "人机验证",
    "请完成验证",
    "请进行安全验证",
    "拖动指定数量",
    "方框区域",
    "根据需要单击每个图像",
    "所有对象都面向同一方向",
    "Security verification",
    "CAPTCHA",
    "click each image",
    "same direction",
]

CAPTCHA_COMBINATIONS = [
    ("验证", "拖动"),
    ("验证", "滑块"),
    ("图像", "方向"),
    ("方框", "提交"),
    ("拖动", "物体"),
    ("拖动", "方框"),
    ("verify", "human"),
    ("verify", "captcha"),
    ("security", "verification"),
    ("slider", "verification"),
    ("drag", "verify"),
    ("click", "image"),
    ("same", "direction"),
]


def has_captcha(page: Any) -> bool:
    segments = _captcha_segments(page)
    if not segments:
        segments = [body_text(page, timeout=2000)]

    lowered_segments = [segment.lower() for segment in segments if segment]
    if any(any(keyword.lower() in segment for keyword in CAPTCHA_KEYWORDS) for segment in lowered_segments):
        return True
    return any(
        left.lower() in segment and right.lower() in segment
        for segment in lowered_segments
        for left, right in CAPTCHA_COMBINATIONS
    )


def check_and_wait_if_captcha(page: Any, product_id: str = "", logger: Any | None = None) -> bool:
    """Detect security verification and wait for a human to solve it.

    This function intentionally does not recognize CAPTCHA images, drag objects,
    click image grids, call OCR, or bypass security verification.
    """
    handled = False
    while has_captcha(page):
        handled = True
        screenshot_path = take_screenshot(page, "captcha", product_id)
        message = (
            "检测到验证码/安全验证。请在当前浏览器中人工完成验证；"
            "完成后回到终端输入 continue。输入 skip 跳过当前浏览器，输入 stop 停止流程。"
        )
        if logger:
            logger.log_step("captcha_guard", "manual_required", message, page=page, screenshot_path=screenshot_path)
        print(message)
        show_manual_action_popup("验证码/安全验证", message, logger=logger)

        decision = wait_user_decision(message, logger=logger)
        if decision == "skip":
            raise UserChoseSkip(f"User skipped CAPTCHA/security verification. Screenshot: {screenshot_path}")
        if decision == "stop":
            raise UserChoseStop(f"User stopped at CAPTCHA/security verification. Screenshot: {screenshot_path}")

        try:
            page.wait_for_timeout(1000)
        except Exception:
            pass

        if has_captcha(page):
            if logger:
                logger.log_step("captcha_guard", "manual_required", "验证码/安全验证仍存在，继续等待人工处理。", page=page, screenshot_path=screenshot_path)
            continue

    if handled and logger:
        logger.log_step("captcha_guard", "ok", "验证码/安全验证已由人工处理完成，继续流程。", page=page)
    return handled


def _captcha_segments(page: Any) -> list[str]:
    try:
        return page.evaluate(
            """() => {
                const visible = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
                return Array.from(document.querySelectorAll('div, section, dialog, iframe, canvas, button, span, p, h1, h2, h3'))
                    .filter(visible)
                    .map((el) => {
                        const aria = el.getAttribute('aria-label') || '';
                        const title = el.getAttribute('title') || '';
                        const text = el.innerText || el.textContent || '';
                        return `${aria} ${title} ${text}`.replace(/\\s+/g, ' ').trim();
                    })
                    .filter(Boolean)
                    .slice(0, 800);
            }"""
        )
    except Exception:
        return []


def wait_until_captcha_disappears(page: Any, timeout_seconds: int = 900) -> None:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not has_captcha(page):
            return
        try:
            page.wait_for_timeout(3000)
        except Exception:
            time.sleep(3)
    raise ManualRequiredError("captcha_guard", "验证码/安全验证仍未消失，已停止自动流程。")
