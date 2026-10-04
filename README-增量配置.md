# bilibili_checkin 漫画积分抢券增量配置

本增量功能不修改原有 `bilibili.py` / `main.py` / 原签到 Workflow。

新增：

- `manga_exchange.py`：B 漫积分商城抢券
- `telegram_bot.py`：Telegram Bot 推送
- `.github/workflows/bilibili_manga_exchange.yml`：独立抢券 Action

目标：

- `〖超特惠〗限量-0点秒杀`
- `〖特惠〗限量-10点秒杀`

流程：

1. 每天北京时间 11:58 启动 Action。
2. Python 等待到北京时间 12:00:00。
3. 动态调用 `ListProduct` 获取当天商品 ID、积分价格、库存。
4. 查询账号积分。
5. 先抢 0 点商品，再抢 10 点商品。
6. Exchange 失败立即重试，默认每个目标 100 次。
7. 每 10 次重新获取商品信息。
8. 最后通过 Telegram 推送结果。

## GitHub Actions Secrets

Settings -> Secrets and variables -> Actions -> Secrets：

- `BILIBILI_COOKIE`：已有配置，保持不变
- `TG_BOT_TOKEN`：Telegram Bot Token
- `TG_CHAT_ID`：Telegram Chat ID

## GitHub Actions Variables

Settings -> Secrets and variables -> Actions -> Variables：

- `MANGA_EXCHANGE_RETRY` = `100`
- `MANGA_EXCHANGE_NUM` = `1`

## 第一次测试

Actions -> Bilibili Manga Coupon Exchange -> Run workflow：

- `test_only = true`
- `wait_until_target = false`

这个模式只调用 `ListProduct`，不会调用 `Exchange`。

重点检查日志中 API 实际返回的商品标题，例如：

`API商品: ...`

以及：

`账号1: 找到 ...`

确认两个目标名称后，再用：

- `test_only = false`
- `wait_until_target = false`

进行一次手动真实抢券测试。

正式定时运行无需手动操作。

## Telegram

先用 BotFather 创建 Bot。

向 Bot 发送一条消息，然后通过：

`https://api.telegram.org/bot<BOT_TOKEN>/getUpdates`

查看 `message.chat.id`，把它填入 `TG_CHAT_ID`。

如果是群组，先把 Bot 加入群，再在群里发送消息后查询 `chat.id`。

不要把 Bot Token 写入代码或 Variables；Token 应放在 GitHub Actions Secrets。
