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
    "https://rolster.bstage.in/shop/products/287,"
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
    "신청하기",
    "예약하기",
    "예매하기",
    "참여하기",
    "응모하기",
    "buy now",
    "order now",
    "add to cart",
    "apply",
    "reserve",
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


def discord_embed(
    *,
    title: str,
    description: str,
    color: int,
    fields: Optional[list[dict]] = None,
) -> dict:
    embed = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {
            "text": "bstage stock monitor",
        },
    }

    if fields:
        embed["fields"] = fields

    return embed


def send_discord(
    message: str = "",
    embeds: Optional[list[dict]] = None,
) -> None:
    if not DISCORD_WEBHOOK_URL:
        raise RuntimeError(
            "DISCORD_WEBHOOK_URL 환경변수가 비어 있습니다."
        )

    payload = {
        "content": message,
        "allowed_mentions": {"parse": []},
    }

    if embeds:
        payload["embeds"] = embeds

    response = requests.post(
        DISCORD_WEBHOOK_URL,
        json=payload,
        timeout=20,
    )

    response.raise_for_status()


async def is_disabled(element) -> bool:
    disabled_attribute = await element.get_attribute("disabled")
    aria_disabled = (
        await element.get_attribute("aria-disabled") or ""
    ).lower()

    return (
        await element.is_disabled()
        or disabled_attribute is not None
        or aria_disabled == "true"
    )


async def dismiss_cookie_banner(page: Page) -> None:
    cookie_buttons = page.locator(
        "button:has-text('전체 쿠키 허용하기'), "
        "button:has-text('Allow all cookies'), "
        "button:has-text('Accept all'), "
        "button:has-text('Accept'), "
        "button:has-text('동의'), "
        "button:has-text('모두 허용')"
    )

    for index in range(await cookie_buttons.count()):
        button = cookie_buttons.nth(index)

        try:
            if await button.is_visible() and not await is_disabled(button):
                await button.click(timeout=3_000)
                await page.wait_for_timeout(500)
                return
        except Exception:
            continue


async def detect_stock(
    page: Page,
    product_url: str,
) -> StockResult:
    await page.goto(
        product_url,
        wait_until="domcontentloaded",
        timeout=60_000,
    )

    # SPA 렌더링과 구매 버튼 표시를 기다립니다.
    try:
        await page.wait_for_load_state(
            "networkidle",
            timeout=15_000,
        )
    except Exception:
        pass

    await dismiss_cookie_banner(page)

    try:
        await page.locator(
            "button:has-text('구매하기'), "
            "a:has-text('구매하기'), "
            "button:has-text('장바구니'), "
            "button:has-text('신청하기'), "
            "button:has-text('예약하기'), "
            "button:has-text('Order Now'), "
            "a:has-text('Order Now')"
        ).first.wait_for(
            state="attached",
            timeout=15_000,
        )
    except Exception:
        pass

    await page.wait_for_timeout(2_000)

    title = (await page.title()).strip() or "비스테이지 상품"

    body_text = (
        await page.locator("body").inner_text()
    ).strip()

    normalized = re.sub(
        r"\s+",
        " ",
        body_text,
    ).lower()

    # 1차: 실제 상품 구매 버튼을 직접 찾습니다.
    direct_buy_controls = page.locator(
        "button.ProductButton_btn__4N26E, "
        "button[class*='ProductButton_btn'], "
        "button:has-text('구매하기'), "
        "a:has-text('구매하기'), "
        "button:has-text('바로 구매'), "
        "button:has-text('장바구니'), "
        "button:has-text('신청하기'), "
        "button:has-text('예약하기'), "
        "button:has-text('예매하기'), "
        "button:has-text('참여하기'), "
        "button:has-text('응모하기'), "
        "button:has-text('Order Now'), "
        "a:has-text('Order Now')"
    )

    direct_count = await direct_buy_controls.count()
    direct_disabled_found = False

    for index in range(direct_count):
        element = direct_buy_controls.nth(index)

        try:
            if not await element.is_visible():
                continue

            text = re.sub(
                r"\s+",
                " ",
                " ".join(
                    [
                        (await element.inner_text()).strip(),
                        (await element.get_attribute("aria-label") or "").strip(),
                        (await element.get_attribute("title") or "").strip(),
                    ]
                ),
            ).lower().strip()

            if not any(word in text for word in BUY_WORDS):
                continue

            if await is_disabled(element):
                direct_disabled_found = True
                continue

            return StockResult(
                available=True,
                reason=f"활성화된 구매 버튼을 찾았습니다: {text}",
                title=title,
            )

        except Exception as exc:
            log.debug(
                "직접 구매 버튼 검사 실패 | index=%s | error=%s",
                index,
                exc,
            )

    # 2차: 전체 인터랙티브 요소를 검사합니다.
    interactive = page.locator(
        "button, a, [role='button']"
    )

    count = await interactive.count()

    active_buy_control = False
    disabled_buy_control = direct_disabled_found

    for index in range(count):
        element = interactive.nth(index)

        try:
            if not await element.is_visible():
                continue

            text = re.sub(
                r"\s+",
                " ",
                " ".join(
                    [
                        (await element.inner_text()).strip(),
                        (await element.get_attribute("aria-label") or "").strip(),
                        (await element.get_attribute("title") or "").strip(),
                    ]
                ),
            ).lower().strip()

            if not text:
                continue

            if not any(
                word in text
                for word in BUY_WORDS
            ):
                continue

            if await is_disabled(element):
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

    # 활성 구매 버튼이 있으면 페이지 다른 영역의 품절 문구보다 우선합니다.
    if active_buy_control:
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

    # 판정 실패 시 실제 버튼 문구를 Railway 로그에 출력합니다.
    visible_controls = []

    for index in range(count):
        element = interactive.nth(index)

        try:
            if not await element.is_visible():
                continue

            control_text = re.sub(
                r"\s+",
                " ",
                " ".join(
                    [
                        (await element.inner_text()).strip(),
                        (await element.get_attribute("aria-label") or "").strip(),
                        (await element.get_attribute("title") or "").strip(),
                    ]
                ),
            ).strip()

            if control_text:
                visible_controls.append(control_text[:120])

        except Exception:
            continue

    log.warning(
        "구매 상태 판정 실패 | url=%s | visible_controls=%s",
        product_url,
        visible_controls[:40],
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
            embeds=[
                discord_embed(
                    title="비스테이지 재고 감시 시작",
                    description="상품 재고 감시를 시작했습니다.",
                    color=0x3498DB,
                    fields=[
                        {
                            "name": "감시 상품",
                            "value": f"{len(PRODUCT_URLS)}개",
                            "inline": True,
                        },
                        {
                            "name": "재고 확인 주기",
                            "value": f"{CHECK_INTERVAL_SECONDS}초",
                            "inline": True,
                        },
                        {
                            "name": "상태 알림 주기",
                            "value": f"{HEARTBEAT_INTERVAL_SECONDS}초",
                            "inline": True,
                        },
                        {
                            "name": "시작 시각",
                            "value": now_kst(),
                            "inline": False,
                        },
                    ],
                ),
            ],
        )
        send_discord(product_list[:2000])

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

                        if result.available:
                            send_discord(
                                "🚨 재입고 감지",
                                embeds=[
                                    discord_embed(
                                        title="재입고 감지",
                                        description=(
                                            "구매 가능한 버튼을 찾았습니다."
                                        ),
                                        color=0x2ECC71,
                                        fields=[
                                            {
                                                "name": "상품",
                                                "value": result.title[:1024],
                                                "inline": False,
                                            },
                                            {
                                                "name": "상태",
                                                "value": "구매 가능",
                                                "inline": True,
                                            },
                                            {
                                                "name": "확인 시각",
                                                "value": now_kst(),
                                                "inline": True,
                                            },
                                            {
                                                "name": "판정 사유",
                                                "value": result.reason[:1024],
                                                "inline": False,
                                            },
                                        ],
                                    ),
                                ],
                            )
                            send_discord(product_url)

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
                                "⚠️ 감시 오류",
                                embeds=[
                                    discord_embed(
                                        title="재고 확인 오류",
                                        description=(
                                            "비스테이지 재고 확인 중 "
                                            "오류가 발생했습니다."
                                        ),
                                        color=0xE74C3C,
                                        fields=[
                                            {
                                                "name": "조치",
                                                "value": (
                                                    "Railway 로그를 확인해 주세요."
                                                ),
                                                "inline": False,
                                            },
                                            {
                                                "name": "발생 시각",
                                                "value": now_kst(),
                                                "inline": True,
                                            },
                                        ],
                                    )
                                ],
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
                    status_fields = []

                    for product_url in PRODUCT_URLS:
                        result = latest_results.get(product_url)

                        if result is None:
                            status_fields.append(
                                {
                                    "name": "확인 전",
                                    "value": "아직 확인 결과가 없습니다.",
                                    "inline": False,
                                }
                            )
                            continue

                        if result.available:
                            status = "구매 가능"
                        else:
                            status = "품절 또는 구매 불가"

                        status_fields.append(
                            {
                                "name": result.title[:256],
                                "value": (
                                    f"상태: **{status}**\n"
                                    f"사유: {result.reason}"
                                )[:1024],
                                "inline": False,
                            }
                        )

                    send_discord(
                        embeds=[
                            discord_embed(
                                title="비스테이지 감시 상태",
                                description="상품을 정상 감시 중입니다.",
                                color=0xF1C40F,
                                fields=[
                                    *status_fields[:24],
                                    {
                                        "name": "마지막 확인",
                                        "value": now_kst(),
                                        "inline": False,
                                    },
                                ],
                            ),
                        ],
                    )
                    send_discord(product_list[:2000])

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
