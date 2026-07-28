import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from playwright.async_api import Page, async_playwright


DEFAULT_PRODUCT_URLS = (
    "https://rolster.bstage.in/shop/products/278,"
    "https://rolster.bstage.in/shop/products/280"
)

PRODUCT_URLS = [
    url.strip()
    for url in os.getenv("PRODUCT_URLS", DEFAULT_PRODUCT_URLS).split(",")
    if url.strip()
]

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

CHECK_INTERVAL_SECONDS = max(
    30,
    int(os.getenv("CHECK_INTERVAL_SECONDS", "120")),
)

HEARTBEAT_INTERVAL_SECONDS = max(
    CHECK_INTERVAL_SECONDS,
    int(os.getenv("HEARTBEAT_INTERVAL_SECONDS", "3600")),
)

STARTUP_NOTIFICATION = (
    os.getenv("STARTUP_NOTIFICATION", "true").lower() == "true"
)

HEADLESS = os.getenv("HEADLESS", "true").lower() == "true"

NOTIFY_ON_ERROR = (
    os.getenv("NOTIFY_ON_ERROR", "false").lower() == "true"
)


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
    kst = timezone(timedelta(hours=9))
    return datetime.now(kst).strftime("%Y-%m-%d %H:%M:%S KST")


def send_discord(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL 환경변수가 비어 있습니다."
        )

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json={
            "content": message,
            "allowed_mentions": {"parse": []},
        },
        timeout=20,
    )

    response.raise_for_status()


async def detect_stock(
    page: Page,
    product_url: str,
) -> StockResult:
    await page.goto(
        product_url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    await page.wait_for_timeout(5_000)

    title = (await page.title()).strip() or "비스테이지 상품"

    body_text = (
        await page.locator("body").inner_text()
    ).strip()

    normalized = re.sub(
        r"\s+",
        " ",
        body_text,
    ).lower()

    interactive = page.locator(
        "button, a, [role='button']"
    )

    count = min(
        await interactive.count(),
        300,
    )

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

            if not text:
                continue

            if not any(
                word in text
                for word in BUY_WORDS
            ):
                continue

            disabled_attribute = (
                await element.get_attribute("disabled")
            )

            aria_disabled = (
                await element.get_attribute("aria-disabled")
                or ""
            ).lower()

            is_disabled = (
                await element.is_disabled()
                or disabled_attribute is not None
                or aria_disabled == "true"
            )

            if is_disabled:
                disabled_buy_control = True
            else:
                active_buy_control = True

        except Exception:
            continue

    sold_out_word = next(
        (
            word
            for word in SOLD_OUT_WORDS
            if word in normalized
        ),
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
            reason=(
                f"페이지에서 '{sold_out_word}' "
                "문구를 찾았습니다."
            ),
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


async def new_page(browser) -> Page:
    return await browser.new_page(
        locale="ko-KR",
        viewport={
            "width": 1280,
            "height": 900,
        },
        user_agent=(
            "Mozilla/5.0 "
            "(Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/124.0.0.0 "
            "Safari/537.36"
        ),
    )


async def run_monitor() -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "Railway Variables에 "
            "DISCORD_WEBHOOK_URL을 추가해 주세요."
        )

    if not PRODUCT_URLS:
        raise RuntimeError(
            "감시할 상품 URL이 없습니다. "
            "PRODUCT_URLS 환경변수를 확인해 주세요."
        )

    previous_available: dict[str, Optional[bool]] = {
        product_url: None
        for product_url in PRODUCT_URLS
    }

    latest_results: dict[str, StockResult] = {}

    error_notified = False
    last_heartbeat_at = time.monotonic()

    product_list = "\n".join(
        f"- {url}"
        for url in PRODUCT_URLS
    )

    log.info(
        "감시 상품 %d개를 시작합니다.",
        len(PRODUCT_URLS),
    )

    for product_url in PRODUCT_URLS:
        log.info("감시 URL: %s", product_url)

    if STARTUP_NOTIFICATION:
        send_discord(
            "✅ **비스테이지 재고 감시를 시작했어요.**\n"
            f"감시 상품: **{len(PRODUCT_URLS)}개**\n"
            f"{product_list}\n"
            f"재고 확인 주기: "
            f"**{CHECK_INTERVAL_SECONDS}초**\n"
            f"상태 알림 주기: "
            f"**{HEARTBEAT_INTERVAL_SECONDS}초**"
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

        try:
            while True:
                cycle_had_error = False

                for product_url in PRODUCT_URLS:
                    try:
                        result = await detect_stock(
                            page,
                            product_url,
                        )

                        latest_results[product_url] = result

                        log.info(
                            "url=%s | available=%s | "
                            "reason=%s | title=%s",
                            product_url,
                            result.available,
                            result.reason,
                            result.title,
                        )

                        previous_state = (
                            previous_available[product_url]
                        )

                        if (
                            result.available
                            and previous_state is not True
                        ):
                            send_discord(
                                "🚨 **재입고 감지! "
                                "지금 확인해 봐!**\n"
                                f"상품: **{result.title}**\n"
                                f"상태: **구매 가능**\n"
                                f"확인 시각: {now_kst()}\n"
                                f"{product_url}"
                            )

                        if (
                            previous_state is True
                            and not result.available
                        ):
                            log.info(
                                "상품이 다시 품절 상태로 "
                                "변경되었습니다: %s",
                                product_url,
                            )

                        previous_available[product_url] = (
                            result.available
                        )

                    except Exception as exc:
                        cycle_had_error = True

                        log.exception(
                            "상품 재고 확인 실패 | "
                            "url=%s | error=%s",
                            product_url,
                            exc,
                        )

                if cycle_had_error:
                    if NOTIFY_ON_ERROR and not error_notified:
                        try:
                            send_discord(
                                "⚠️ 비스테이지 재고 확인 중 "
                                "오류가 발생했어요.\n"
                                "Railway 로그를 확인해 주세요."
                            )
                            error_notified = True
                        except Exception:
                            log.exception(
                                "Discord 오류 알림 전송도 "
                                "실패했습니다."
                            )

                    try:
                        await page.close()
                    except Exception:
                        pass

                    page = await new_page(browser)

                else:
                    error_notified = False

                current_monotonic = time.monotonic()

                heartbeat_due = (
                    current_monotonic - last_heartbeat_at
                    >= HEARTBEAT_INTERVAL_SECONDS
                )

                if heartbeat_due:
                    status_lines = []

                    for product_url in PRODUCT_URLS:
                        result = latest_results.get(product_url)

                        if result is None:
                            status_lines.append(
                                "⚪ **확인 전**\n"
                                f"{product_url}"
                            )
                            continue

                        if result.available:
                            emoji = "🟢"
                            status = "구매 가능"
                        else:
                            emoji = "🟡"
                            status = "품절 또는 구매 불가"

                        status_lines.append(
                            f"{emoji} **{result.title}**\n"
                            f"현재 상태: **{status}**\n"
                            f"{product_url}"
                        )

                    send_discord(
                        "🔍 **비스테이지 상품을 "
                        "정상 감시 중입니다.**\n\n"
                        + "\n\n".join(status_lines)
                        + f"\n\n마지막 확인: {now_kst()}"
                    )

                    last_heartbeat_at = current_monotonic

                await asyncio.sleep(
                    CHECK_INTERVAL_SECONDS
                )

        finally:
            try:
                await page.close()
            except Exception:
                pass

            await browser.close()


if __name__ == "__main__":
    asyncio.run(run_monitor())
