import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import requests
from playwright.async_api import async_playwright, Page

PRODUCT_URLS = [
    "https://rolster.bstage.in/shop/products/279",
    "https://rolster.bstage.in/shop/products/280",
]

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
CHECK_INTERVAL_SECONDS = max(30, int(os.getenv("CHECK_INTERVAL_SECONDS", "120")))
HEARTBEAT_INTERVAL_SECONDS = max(
    CHECK_INTERVAL_SECONDS,
    int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "3600")),
)
STARTUP_NOTIFICATION = os.getenv("STARTUP_NOTIFICATION", "true").lower() == "true"
HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"
NOTIFY_ON_ERROR = os.getenv("NOTIFY_ON_ERROR", "false").lower() == "true"

SOLD_OUT_WORDS = (
    "품절",
    "sold out",
    "판매 종료",
    "판매종료",
    "구매 불가",
    "구매불가",
)

BUY_WORDS = (
    "구매하기",
    "바로 구매",
    "바로구매",
    "장바구니",
    "buy now",
    "add to cart",
)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("bstage-monitor")


@dataclass
class StockResult:
    available: bool
    reason: str
    title: str


def now_kst() -> str:
    # UTC+9 (Korea Standard Time)
    timestamp = datetime.now(timezone.utc).timestamp() + (9 * 60 * 60)
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S KST")


def send_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError("DISCORD_WEBHOOK_URL 환경변수가 비어 있습니다.")

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": message,
            "allowed_mentions": {"parse": []},
        },
        timeout=20,
    )
    response.raise_for_status()


async def detect_stock(page: Page) -> StockResult:
    await page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60_000)
    await page.wait_for_timeout(5_000)

    title = (await page.title()).strip() or "비스테이지 상품"
    body_text = (await page.locator("body").inner_text()).strip()
    normalized = re.sub(r"\s+", " ", body_text).lower()

    interactive = page.locator("button, a, [role='button']")
    count = min(await interactive.count(), 300)

    active_buy_control = False
    disabled_buy_control = False

    for index in range(count):
        element = interactive.nth(index)

        try:
            text = re.sub(
                r"\s+",
                " ",
                (await element.inner_text()).strip(),
            ).lower()

            if not text or not any(word in text for word in BUY_WORDS):
                continue

            is_disabled = (
                await element.is_disabled()
                or await element.get_attribute("disabled") is not None
                or (await element.get_attribute("aria-disabled") or "").lower() == "true"
            )

            if is_disabled:
                disabled_buy_control = True
            else:
                active_buy_control = True

        except Exception:
            continue

    sold_out_word = next(
        (word for word in SOLD_OUT_WORDS if word in normalized),
        None,
    )

    if active_buy_control and not sold_out_word:
        return StockResult(
            available=True,
            reason="활성화된 구매 버튼을 찾았습니다.",
            title=title,
        )

    if sold_out_word:
        return StockResult(
            available=False,
            reason=f"페이지에서 '{sold_out_word}' 문구를 찾았습니다.",
            title=title,
        )

    if disabled_buy_control:
        return StockResult(
            available=False,
            reason="구매 버튼이 비활성화되어 있습니다.",
            title=title,
        )

    return StockResult(
        available=False,
        reason="구매 가능 여부를 확실히 판단하지 못했습니다.",
        title=title,
    )


async def new_page(browser):
    return await browser.new_page(
        locale="ko-KR",
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    )


async def run_monitor() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "Railway Variables에 DISCORD_WEBHOOK_URL을 추가해 주세요."
        )

    previous_available: Optional[bool] = None
    error_notified = False
    last_heartbeat_at = time.monotonic()

    if STARTUP_NOTIFICATION:
        send_discord(
            "✅ **비스테이지 재고 감시를 시작했어요.**\n"
            f"상품: {PRODUCT_URL}\n"
            f"재고 확인 주기: {CHECK_INTERVAL_SECONDS}초\n"
            f"상태 알림 주기: {HEARTBEAT_INTERVAL_SECONDS}초"
        )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=HEADLESS,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        page = await new_page(browser)

        while True:
            try:
                result = await detect_stock(page)

                log.info(
                    "available=%s | %s | %s",
                    result.available,
                    result.reason,
                    result.title,
                )

                # 품절 또는 불확실 상태에서 구매 가능 상태로 바뀌면 즉시 알림
                if result.available and previous_available is not True:
                    send_discord(
                        "🚨 **재입고 감지! 지금 확인해 봐!**\n"
                        f"**{result.title}**\n"
                        f"{PRODUCT_URL}\n"
                        f"확인 시각: {now_kst()}"
                    )

                previous_available = result.available
                error_notified = False

                # 정해진 주기마다 현재 상태 알림
                current_monotonic = time.monotonic()
                if current_monotonic - last_heartbeat_at >= HEARTBEAT_INTERVAL_SECONDS:
                    status_text = "구매 가능" if result.available else "품절"
                    status_emoji = "🟢" if result.available else "🟡"

                    send_discord(
                        f"{status_emoji} **아직 정상 감시 중입니다.**\n"
                        f"상품: **{result.title}**\n"
                        f"현재 상태: **{status_text}**\n"
                        f"마지막 확인: {now_kst()}\n"
                        f"{PRODUCT_URL}"
                    )
                    last_heartbeat_at = current_monotonic

            except Exception as exc:
                log.exception("재고 확인 실패: %s", exc)

                if NOTIFY_ON_ERROR and not error_notified:
                    try:
                        send_discord(
                            "⚠️ 비스테이지 재고 확인 중 오류가 발생했어요.\n"
                            "Railway 로그를 확인해 주세요."
                        )
                        error_notified = True
                    except Exception:
                        log.exception("오류 알림 전송도 실패했습니다.")

                try:
                    await page.close()
                except Exception:
                    pass

                page = await new_page(browser)

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(run_monitor())
