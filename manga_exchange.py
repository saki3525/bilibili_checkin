#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from telegram_bot import send_telegram


BEIJING = timezone(timedelta(hours=8))

# 目标名称以当前参考脚本为准；TEST MODE 会打印 API 实际返回的标题，
# 如果 B 漫后续调整了空格/名称，可以据测试日志修改这里。
TARGET_PRODUCTS = [
    "【超特惠】限量-0点秒杀",
    "【特惠】限量-10点秒杀",
]

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


def now_bj():
    return datetime.now(BEIJING)


def log(message):
    print(
        f"[{now_bj().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}] {message}",
        flush=True,
    )


def get_cookies():
    raw = os.environ.get("BILIBILI_COOKIE", "")
    return [item.strip() for item in raw.split("###") if item.strip()]


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
    # 与 NobyDa 当前 ExchangePoints.js 的实现保持一致：
    # ListProduct 不需要携带 Cookie。
    body = request_json(session, "POST", LIST_PRODUCT_URL)

    if body.get("code") != 0 or not isinstance(body.get("data"), list):
        raise RuntimeError(body.get("msg") or "查询商品列表失败")

    return body["data"]


def get_user_point(session, cookie):
    body = request_json(
        session,
        "POST",
        GET_POINT_URL,
        cookie=cookie,
    )

    if body.get("code") != 0 or not body.get("data"):
        raise RuntimeError(body.get("msg") or "查询漫画积分失败")

    return int(body["data"].get("point", 0))


def find_product(products, target):
    for product in products:
        if str(product.get("title", "")) == target:
            return product

    # 兼容“】限量”与“】 限量”这种仅空格不同的情况。
    normalized = target.replace("】 ", "】")
    for product in products:
        title = str(product.get("title", "")).replace("】 ", "】")
        if title == normalized:
            return product

    return None


def sleep_until_target():
    target = now_bj().replace(
        hour=int(os.environ.get("MANGA_TARGET_HOUR", "12")),
        minute=int(os.environ.get("MANGA_TARGET_MINUTE", "0")),
        second=0,
        microsecond=0,
    )

    current = now_bj()

    if current >= target:
        log("当前已到/超过目标时间，不等待，立即开始抢券。")
        return

    seconds = (target - current).total_seconds()
    log(
        f"已启动。北京时间目标 {target.strftime('%H:%M:%S')}，"
        f"sleep {seconds:.3f} 秒。"
    )

    while True:
        remaining = (target - now_bj()).total_seconds()
        if remaining <= 0:
            break
        time.sleep(min(remaining, 5))

    log("到达北京时间 12:00:00，开始抢券。")


def exchange_once(session, cookie, product, quantity):
    cost = int(product["real_cost"])

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

    if body.get("code") == 0:
        return True, "兑换成功"

    return False, body.get("msg") or f"兑换失败(code={body.get('code')})"


def exchange_product(session, cookie, product_name, retry_count, quantity):
    # 进入抢购前重新查询一次商品，动态取得 product_id / real_cost / stock。
    products = list_products(session)
    product = find_product(products, product_name)

    if not product:
        titles = [str(item.get("title", "")) for item in products]
        return False, (
            f'未找到 "{product_name}"；当前商品: '
            + " | ".join(titles)
        )

    product_id = product.get("id")
    real_cost = int(product.get("real_cost", 0))
    remain = product.get("remain_amount")

    log(
        f'目标="{product.get("title")}" '
        f"id={product_id} cost={real_cost} stock={remain}"
    )

    if not remain:
        return False, "库存为0"

    point = get_user_point(session, cookie)
    log(f"当前漫画积分: {point}")

    if point < real_cost * quantity:
        return False, (
            f"积分不足: 当前{point}，"
            f"需要{real_cost * quantity}"
        )

    # 用户明确要求暴力重试：失败后立即下一次，不人为 sleep。
    for attempt in range(1, retry_count + 1):
        success, message = exchange_once(
            session,
            cookie,
            product,
            quantity,
        )

        if success:
            return True, (
                f"抢券成功: {product_name}，"
                f"数量={quantity}，"
                f"消耗积分={real_cost * quantity}，"
                f"第{attempt}次尝试"
            )

        if attempt <= 3 or attempt % 10 == 0:
            log(
                f"{product_name}: "
                f"第{attempt}/{retry_count}次失败: {message}"
            )

        # 每 10 次重新读取商品信息，防止 product_id / price / stock 变化。
        if attempt % 10 == 0 and attempt < retry_count:
            try:
                refreshed = list_products(session)
                refreshed_product = find_product(
                    refreshed,
                    product_name,
                )
                if refreshed_product:
                    product = refreshed_product
                    real_cost = int(product["real_cost"])
                    if not product.get("remain_amount"):
                        return False, "重查商品时库存为0"
            except Exception as exc:
                log(f"刷新商品失败，继续重试: {exc}")

    return False, f"抢券失败: 已尝试 {retry_count} 次"


def send_result(results):
    text = "哔哩哔哩漫画抢券\n\n" + "\n".join(results)

    if not os.environ.get("TG_BOT_TOKEN") or not os.environ.get("TG_CHAT_ID"):
        log("未配置 TG_BOT_TOKEN/TG_CHAT_ID，跳过 Telegram 推送。")
        return

    try:
        send_telegram(text)
        log("Telegram 推送成功。")
    except Exception as exc:
        # 推送失败不影响抢券结果。
        log(f"Telegram 推送失败: {exc}")


def main():
    cookies = get_cookies()
    if not cookies:
        log("错误: 未设置 BILIBILI_COOKIE")
        return 2

    retry_count = max(
        1,
        int(os.environ.get("MANGA_EXCHANGE_RETRY", "100")),
    )
    quantity = max(
        1,
        int(os.environ.get("MANGA_EXCHANGE_NUM", "1")),
    )
    test_only = os.environ.get("MANGA_TEST_ONLY", "0") == "1"

    log(f"检测到 {len(cookies)} 个 B站账号。")
    log(
        "目标: "
        + " / ".join(TARGET_PRODUCTS)
    )
    log(
        f"每个商品兑换数量={quantity}，"
        f"失败重试={retry_count}"
    )

    if not test_only:
        sleep_until_target()
    else:
        log("TEST MODE：只查询商品，不调用 Exchange。")

    session = requests.Session()
    results = []
    overall_success = True

    for index, cookie in enumerate(cookies, 1):
        log(f"===== 账号 {index} =====")

        try:
            products = list_products(session)

            # 测试阶段重点打印 API 当前真实返回的商品标题。
            for product in products:
                title = str(product.get("title", ""))
                if "秒杀" in title or "特惠" in title:
                    log(
                        f"API商品: {title} | "
                        f"id={product.get('id')} | "
                        f"cost={product.get('real_cost')} | "
                        f"stock={product.get('remain_amount')}"
                    )

            if test_only:
                for target in TARGET_PRODUCTS:
                    product = find_product(products, target)
                    if product:
                        msg = (
                            f"账号{index}: 找到 {product.get('title')} "
                            f"(id={product.get('id')}, "
                            f"cost={product.get('real_cost')}, "
                            f"stock={product.get('remain_amount')})"
                        )
                    else:
                        msg = f"账号{index}: 未找到 {target}"
                        overall_success = False

                    log(msg)
                    results.append(msg)
                continue

            # 按用户指定顺序：0点券 -> 10点券。
            for target in TARGET_PRODUCTS:
                success, message = exchange_product(
                    session,
                    cookie,
                    target,
                    retry_count,
                    quantity,
                )

                log(message)
                results.append(f"账号{index}: {message}")

                if not success:
                    # 一个目标失败不阻止另一个目标继续抢。
                    overall_success = False

        except Exception as exc:
            message = f"账号{index}: 执行异常: {exc}"
            log(message)
            results.append(message)
            overall_success = False

    send_result(results)

    if test_only:
    return 0

    return 0 if overall_success else 1


if __name__ == "__main__":
    sys.exit(main())
