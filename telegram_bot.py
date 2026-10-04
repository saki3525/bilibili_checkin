#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Minimal Telegram Bot sender.

Required environment variables:
  TG_BOT_TOKEN
  TG_CHAT_ID
"""

import os

import requests


def send_telegram(text: str):
    token = os.environ.get("TG_BOT_TOKEN")
    chat_id = os.environ.get("TG_CHAT_ID")

    if not token:
        raise RuntimeError("TG_BOT_TOKEN 未配置")
    if not chat_id:
        raise RuntimeError("TG_CHAT_ID 未配置")

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    response = requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": text,
        },
        timeout=(5, 10),
    )
    response.raise_for_status()

    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(str(body))

    return body


if __name__ == "__main__":
    result = send_telegram("Bilibili Checkin Telegram Bot 测试消息")
    print("Telegram 推送成功:", result.get("ok"))
