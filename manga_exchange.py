#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from telegram_bot import send_telegram


BEIJING = timezone(timedelta(hours=8))


# 商品名称必须与 B 漫 API 实际返回的 title 一致。
TARGET_PRODUCTS = {
    "midnight": "【超特惠】限量-0点秒杀",
    "morning": "【特惠】限量-10点秒杀",
}


LIST_PRODUCT_URL = (
    "https://manga.bilibili.com/twirp/pointshop.v1.Pointshop/ListProduct"
)

GET_POINT_URL = (
    "https://manga.bilibili.com/twirp/pointshop.v1.Pointshop/GetUserPoint"
)

EXCHANGE_URL = (
    "https://manga.bilibili.com/twirp/pointshop.v1.Pointshop/Exchange"
)


HEADERS = {
    "User-Agent": (
        "comic-universal/3412 CFNetwork/1410.0.3 Darwin/22.6.0 "
        "os/ios model/iPhone 12 mobi_app/iphone_comic build/3412 "
        "osVer/16.6 network/2 channel/AppStore"
    ),
    "Accept": "application/json, text/plain, */*",
}


# 库存为 0 时，每次重新查询间隔。
STOCK_POLL_INTERVAL = 0.1

# 已经确认有人抢购 / code=4 时，更激进。
EXCHANGE_RETRY_INTERVAL = 0.05


def now_bj():
    return datetime.now(BEIJING)


def log(message):
    print(
        f"[{now_bj().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] "
        f"{message}",
        flush=True,
    )


def get_cookies():
    raw = os.environ.get("BILIBILI_COOKIE", "")
    return [
        item.strip()
        for item in raw.split("###")
        if item.strip()
    ]


def request_json(session, method, url, cookie=None, **kwargs):
    headers = dict(HEADERS)

    if cookie:
        headers["Cookie"] = cookie

    response = session.request(
        method,
        url,
        headers=headers,
        timeout=(5, 10),
        **kwargs,
    )

    response.raise_for_status()

    try:
        return response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API返回非JSON: HTTP {response.status_code}, "
            f"{response.text[:300]}"
        ) from exc


def list_products(session):
    """
    获取积分商城商品列表。

    ListProduct 不需要 Cookie。
    """

    body = request_json(
        session,
        "POST",
        LIST_PRODUCT_URL,
    )

    if body.get("code") != 0:
        raise RuntimeError(
            body.get("msg") or "查询商品列表失败"
        )

    if not isinstance(body.get("data"), list):
        raise RuntimeError("查询商品列表返回数据异常")

    return body["data"]


def get_user_point(session, cookie):
    body = request_json(
        session,
        "POST",
        GET_POINT_URL,
        cookie=cookie,
    )

    if body.get("code") != 0 or not body.get("data"):
        raise RuntimeError(
            body.get("msg") or "查询漫画积分失败"
        )

    return int(body["data"].get("point", 0))


def find_product(products, target):
    """
    精确匹配商品名称，同时兼容括号后面的空格差异。
    """

    for product in products:
        if str(product.get("title", "")) == target:
            return product

    normalized = target.replace("】 ", "】")

    for product in products:
        title = str(
            product.get("title", "")
        ).replace("】 ", "】")

        if title == normalized:
            return product

    return None


def sleep_until_target():
    """
    midnight:
        23:58 启动 → 次日 00:00:00

    morning:
        09:50 启动 → 当天 10:00:00
    """

    target_slot = os.environ.get(
        "MANGA_TARGET_SLOT",
        "morning",
    )

    current = now_bj()

    if target_slot == "midnight":
        target = (
            current + timedelta(days=1)
        ).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )

        target_label = "次日 00:00:00"

    else:
        target = current.replace(
            hour=10,
            minute=0,
            second=0,
            microsecond=0,
        )

        if current >= target:
            log(
                "当前已到/超过 10:00，"
                "不等待，立即开始抢券。"
            )
            return

        target_label = "当天 10:00:00"

    seconds = (
        target - current
    ).total_seconds()

    log(
        f"当前北京时间 "
        f"{current.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}，"
        f"目标={target_label}，"
        f"目标时间="
        f"{target.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}，"
        f"等待 {seconds:.3f} 秒。"
    )

    while True:
        remaining = (
            target - now_bj()
        ).total_seconds()

        if remaining <= 0:
            break

        # 最后 10 秒进入高精度等待。
        if remaining <= 10:
            time.sleep(
                min(0.05, remaining)
            )
        else:
            time.sleep(
                min(1.0, remaining)
            )

    log(
        f"已到目标时间 "
        f"{target.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}，"
        "开始抢券。"
    )


def exchange_once(
    session,
    cookie,
    product,
    quantity,
):
    """
    执行一次 Exchange。

    返回：
        success
        code
        message
    """

    cost = int(
        product.get("real_cost", 0)
    )

    payload = {
        "product_id": product["id"],
        "product_num": quantity,
        "point": quantity * cost,
    }

    body = request_json(
        session,
        "POST",
        EXCHANGE_URL,
        cookie=cookie,
        json=payload,
    )

    code = body.get("code")
    message = (
        body.get("msg")
        or body.get("message")
        or f"兑换失败(code={code})"
    )

    if code == 0:
        return True, 0, "兑换成功"

    return False, code, message


def exchange_product(
    session,
    cookie,
    product_name,
    retry_count,
    quantity,
):
    """
    核心抢券循环。

    逻辑：

    stock=0
        → 100ms
        → 重新 ListProduct

    stock>0
        → Exchange

    Exchange code=0
        → 成功

    Exchange code=2
        → 库存不足
        → 100ms
        → 重新 ListProduct

    Exchange code=4
        → 抢购人数太多
        → 50ms
        → 继续 Exchange

    Exchange code=3
        → 积分状态可能变化
        → 重新查询积分和商品

    Exchange code=1
        → 积分不足
        → 直接结束

    其他错误
        → 100ms 后继续
    """

    # stock=0 不消耗 Exchange retry。
    #
    # 默认 retry=100 时：
    # 最多允许 1000 次库存轮询，
    # 每次 100ms，约 100 秒。
    #
    # 这样不会因为商品在刚开始时 stock=0
    # 就立刻退出，也不会无限循环。
    stock_poll_limit = max(
        100,
        retry_count * 10,
    )

    exchange_attempts = 0
    stock_polls = 0

    product = None
    real_cost = 0

    while exchange_attempts < retry_count:
        # =========================================================
        # 1. 获取最新商品信息
        # =========================================================

        try:
            products = list_products(session)

            product = find_product(
                products,
                product_name,
            )

        except Exception as exc:
            log(
                f"{product_name}: "
                f"ListProduct 异常: {exc}"
            )

            time.sleep(
                STOCK_POLL_INTERVAL
            )

            continue

        if not product:
            log(
                f"{product_name}: "
                "当前商品列表中未找到目标，"
                "100ms 后重新查询。"
            )

            stock_polls += 1

            if stock_polls >= stock_poll_limit:
                return (
                    False,
                    f"等待商品超时："
                    f"已轮询 {stock_polls} 次",
                )

            time.sleep(
                STOCK_POLL_INTERVAL
            )

            continue

        product_id = product.get("id")

        real_cost = int(
            product.get("real_cost", 0)
        )

        remain = product.get(
            "remain_amount"
        )

        log(
            f'目标="{product.get("title")}" '
            f"id={product_id} "
            f"cost={real_cost} "
            f"stock={remain}"
        )

        # =========================================================
        # 2. 当前没有库存
        # =========================================================

        if not remain:
            stock_polls += 1

            if stock_polls >= stock_poll_limit:
                return (
                    False,
                    f"等待库存超时："
                    f"已轮询 {stock_polls} 次",
                )

            # 用户要求：100ms。
            time.sleep(
                STOCK_POLL_INTERVAL
            )

            continue

        # =========================================================
        # 3. 有库存，确认积分
        # =========================================================

        try:
            point = get_user_point(
                session,
                cookie,
            )
        except Exception as exc:
            log(
                f"{product_name}: "
                f"查询积分异常: {exc}"
            )

            time.sleep(
                STOCK_POLL_INTERVAL
            )

            continue

        required_point = (
            real_cost * quantity
        )

        log(
            f"当前漫画积分: {point}，"
            f"本次需要: {required_point}"
        )

        if point < required_point:
            return (
                False,
                f"积分不足: 当前{point}，"
                f"需要{required_point}",
            )

        # =========================================================
        # 4. Exchange
        # =========================================================

        success, code, message = exchange_once(
            session,
            cookie,
            product,
            quantity,
        )

        exchange_attempts += 1

        if success:
            return (
                True,
                f"抢券成功: {product_name}，"
                f"数量={quantity}，"
                f"消耗积分={required_point}，"
                f"第{exchange_attempts}次 Exchange",
            )

        # ---------------------------------------------------------
        # code=1：积分不足
        # ---------------------------------------------------------

        if code == 1:
            return (
                False,
                f"积分不足: {message}",
            )

        # ---------------------------------------------------------
        # code=2：库存不足
        #
        # 不算最终失败。
        # 重新 ListProduct，100ms 后继续。
        # ---------------------------------------------------------

        if code == 2:
            if (
                exchange_attempts <= 3
                or exchange_attempts % 10 == 0
            ):
                log(
                    f"{product_name}: "
                    f"第{exchange_attempts}/"
                    f"{retry_count}次 Exchange "
                    f"库存不足，100ms 后重新查询库存。"
                )

            time.sleep(
                STOCK_POLL_INTERVAL
            )

            continue

        # ---------------------------------------------------------
        # code=3：积分不匹配
        #
        # 重新获取积分和商品信息。
        # ---------------------------------------------------------

        if code == 3:
            log(
                f"{product_name}: "
                f"第{exchange_attempts}/"
                f"{retry_count}次 Exchange "
                f"积分不匹配，重新获取积分。"
            )

            time.sleep(
                STOCK_POLL_INTERVAL
            )

            continue

        # ---------------------------------------------------------
        # code=4：抢的人太多
        #
        # 这是最值得暴力重试的情况。
        # 不重新 ListProduct，直接继续 Exchange。
        # ---------------------------------------------------------

        if code == 4:
            if (
                exchange_attempts <= 3
                or exchange_attempts % 10 == 0
            ):
                log(
                    f"{product_name}: "
                    f"第{exchange_attempts}/"
                    f"{retry_count}次 Exchange "
                    f"code=4，"
                    f"50ms 后继续抢。"
                )

            time.sleep(
                EXCHANGE_RETRY_INTERVAL
            )

            continue

        # ---------------------------------------------------------
        # 其他错误
        # ---------------------------------------------------------

        if (
            exchange_attempts <= 3
            or exchange_attempts % 10 == 0
        ):
            log(
                f"{product_name}: "
                f"第{exchange_attempts}/"
                f"{retry_count}次 Exchange "
                f"失败(code={code}): {message}"
            )

        time.sleep(
            STOCK_POLL_INTERVAL
        )

    return (
        False,
        f"抢券失败："
        f"已尝试 {exchange_attempts} 次 Exchange",
    )


def send_result(
    results,
    target_slot,
):
    """
    Telegram 汇总通知。
    """

    if target_slot == "midnight":
        slot_name = "00:00 场"
    else:
        slot_name = "10:00 场"

    text = (
        "哔哩哔哩漫画抢券\n"
        f"{slot_name}\n\n"
        + "\n".join(results)
    )

    if not os.environ.get(
        "TG_BOT_TOKEN"
    ) or not os.environ.get(
        "TG_CHAT_ID"
    ):
        log(
            "未配置 "
            "TG_BOT_TOKEN/TG_CHAT_ID，"
            "跳过 Telegram 推送。"
        )
        return

    try:
        send_telegram(text)
        log("Telegram 推送成功。")

    except Exception as exc:
        # Telegram 失败不影响抢券结果。
        log(
            f"Telegram 推送失败: {exc}"
        )


def main():
    cookies = get_cookies()

    if not cookies:
        log(
            "错误: 未设置 BILIBILI_COOKIE"
        )
        return 2

    retry_count = max(
        1,
        int(
            os.environ.get(
                "MANGA_EXCHANGE_RETRY",
                "100",
            )
        ),
    )

    quantity = max(
        1,
        int(
            os.environ.get(
                "MANGA_EXCHANGE_NUM",
                "1",
            )
        ),
    )

    test_only = os.environ.get(
        "MANGA_TEST_ONLY",
        "false",
    ).lower() in (
        "1",
        "true",
        "yes",
    )

    target_slot = os.environ.get(
        "MANGA_TARGET_SLOT",
        "morning",
    )

    if target_slot not in (
        "midnight",
        "morning",
    ):
        log(
            f"未知 MANGA_TARGET_SLOT="
            f"{target_slot}，使用 morning。"
        )
        target_slot = "morning"

    target_product = TARGET_PRODUCTS[
        target_slot
    ]

    log(
        f"检测到 {len(cookies)} 个 B站账号。"
    )

    log(
        f"抢券场次: "
        f"{'00:00' if target_slot == 'midnight' else '10:00'}"
    )

    log(
        f"目标商品: {target_product}"
    )

    log(
        f"兑换数量={quantity}，"
        f"Exchange 最大重试={retry_count}，"
        f"库存轮询间隔={STOCK_POLL_INTERVAL * 1000:.0f}ms，"
        f"code=4 重试间隔="
        f"{EXCHANGE_RETRY_INTERVAL * 1000:.0f}ms"
    )

    log(
        f"测试模式: "
        f"test_only={test_only!r}, "
        f"MANGA_TEST_ONLY="
        f"{os.environ.get('MANGA_TEST_ONLY')!r}"
    )

    # =============================================================
    # 测试模式
    # =============================================================

    if test_only:
        log(
            "TEST MODE：只查询商品，"
            "不调用 Exchange。"
        )

    else:
        sleep_until_target()

    session = requests.Session()

    results = []
    overall_success = True

    for index, cookie in enumerate(
        cookies,
        1,
    ):
        log(
            f"===== 账号 {index} ====="
        )

        try:
            # =====================================================
            # TEST MODE
            #
            # 测试时把两个目标都打印出来，方便确认 API。
            # =====================================================

            if test_only:
                products = list_products(
                    session
                )

                for product in products:
                    title = str(
                        product.get(
                            "title",
                            "",
                        )
                    )

                    if (
                        "秒杀" in title
                        or "特惠" in title
                    ):
                        log(
                            f"API商品: {title} | "
                            f"id={product.get('id')} | "
                            f"cost={product.get('real_cost')} | "
                            f"stock={product.get('remain_amount')}"
                        )

                # 测试两个目标都检查。
                for slot, target in (
                    TARGET_PRODUCTS.items()
                ):
                    product = find_product(
                        products,
                        target,
                    )

                    if product:
                        msg = (
                            f"账号{index}: "
                            f"{slot} 找到 "
                            f"{product.get('title')} "
                            f"(id={product.get('id')}, "
                            f"cost={product.get('real_cost')}, "
                            f"stock={product.get('remain_amount')})"
                        )

                    else:
                        msg = (
                            f"账号{index}: "
                            f"{slot} 未找到 "
                            f"{target}"
                        )

                        overall_success = False

                    log(msg)
                    results.append(msg)

                continue

            # =====================================================
            # 正式抢券
            #
            # 一个时间点只抢对应商品。
            # =====================================================

            success, message = exchange_product(
                session,
                cookie,
                target_product,
                retry_count,
                quantity,
            )

            log(message)

            results.append(
                f"账号{index}: {message}"
            )

            if not success:
                overall_success = False

        except Exception as exc:
            message = (
                f"账号{index}: 执行异常: {exc}"
            )

            log(message)

            results.append(message)

            overall_success = False

    # =============================================================
    # Telegram
    # =============================================================

    send_result(
        results,
        target_slot,
    )

    # 测试模式无论库存是否为 0，都应该让 GitHub Actions 成功。
    if test_only:
        return 0

    return (
        0
        if overall_success
        else 1
    )


if __name__ == "__main__":
    sys.exit(main())
