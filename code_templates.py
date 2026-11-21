#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页监控平台 · 内置监控代码模板

前端“新建监控”时可选，填入编辑器后可随意修改。
所有模板都遵守统一约定：用 emit(ok, message, data) 上报结果，
PARAMS 里是监控项配置的 JSON 参数。
"""

from __future__ import annotations

from typing import Any, Dict, List

TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "blank",
        "name": "空白模板",
        "description": "从零开始写，自带 emit / PARAMS 用法注释",
        "params": "{}",
        "code": '''# 监控代码：每次到点后由平台在独立 python 子进程里运行
#
# 可用环境：
#   PARAMS                        本监控项配置的 JSON 参数（字典）
#   emit(ok, message, data=None)  上报结果：ok=是否正常，message=一句话结果
#
# 结束方式（任选其一，推荐 emit）：
#   1. 调用 emit(True, "一切正常")          —— 以 __RESULT__ 行上报
#   2. 不调用 emit，正常退出（退出码 0=成功，非 0=失败）

import json

# 例：读取 PARAMS 里的关键字
keyword = PARAMS.get("keyword", "")

# 在这里写你的监控逻辑 ...
ok = True
emit(ok, f"示例结果：keyword={keyword!r}", {"keyword": keyword})
''',
    },
    {
        "id": "http",
        "name": "HTTP 探测",
        "description": "请求一个 URL，检查状态码与页面关键字，统计耗时",
        "params": '{"url": "https://www.huaweicloud.com", "expect_status": 200, "keyword": ""}',
        "code": '''# HTTP 可用性探测
# PARAMS: {"url": "...", "expect_status": 200, "keyword": "可选，页面必须包含的关键字"}

import time

import requests

url = PARAMS.get("url")
if not url:
    emit(False, "PARAMS 里缺少 url")
    raise SystemExit

expect_status = int(PARAMS.get("expect_status") or 200)
keyword = PARAMS.get("keyword") or ""

started = time.time()
try:
    resp = requests.get(
        url, timeout=max(1, PARAMS.get("timeout") or 10),
        headers={"User-Agent": "Mozilla/5.0 (web-monitor)"},
    )
except Exception as exc:
    emit(False, f"请求失败：{exc}", {"url": url})
    raise SystemExit

cost_ms = int((time.time() - started) * 1000)
problems = []
if resp.status_code != expect_status:
    problems.append(f"状态码 {resp.status_code} != 期望 {expect_status}")
if keyword and keyword not in resp.text:
    problems.append(f"页面里没有找到关键字「{keyword}」")

message = f"HTTP {resp.status_code}，耗时 {cost_ms}ms" + ("" if not problems else "，" + "；".join(problems))
emit(not problems, message, {
    "url": url,
    "status_code": resp.status_code,
    "cost_ms": cost_ms,
    "body_bytes": len(resp.content),
})
''',
    },
    {
        "id": "huawei_stock",
        "name": "华为云活动库存（推荐）",
        "description": "监控华为云促销商品库存，有货/售罄一目了然，可作为状态变化提醒",
        "params": '{"promotion_id": "P2608120945080031D691X61IVR22B", "goods": "Flexus L实例-2核2G2M 1年（36元/年）"}',
        "code": '''# 华为云促销库存监控（数据来自活动页前端同款接口）
# PARAMS: {"promotion_id": "P...", "goods": "商品名，可选"}
#
# 说明：emit(ok=...) 里“ok”表达的是“本次查询是否正常执行”，
# 而库存状态放在 data.in_stock 里；想“售罄 -> 有货”时推送，
# 请把监控项的“推送策略”设为 change，并让 ok 跟随 in_stock（见下方开关）。

import json
import math

import requests

PROMOTION_ID = PARAMS.get("promotion_id") or ""
GOODS = PARAMS.get("goods") or PROMOTION_ID
SKU = PARAMS.get("sku") or "Flexus L实例-2核2G2M 1年"
PRICE = float(PARAMS.get("price") or 36)
SITE_URL = PARAMS.get("url") or "https://activity.huaweicloud.com/"
# True: 有货算正常(ok=True)、售罄算异常(ok=False)，配合“状态变化时推送”= 抢到货提醒
OK_FOLLOWS_STOCK = True

PROGRESS_URL = (
    "https://portal.huaweicloud.com/api/cbc/global/store/rest/"
    "csbpromotionmanageservice/v1/promotionprogress"
)

if not PROMOTION_ID:
    emit(False, "PARAMS 里缺少 promotion_id")
    raise SystemExit

resp = requests.get(
    PROGRESS_URL,
    params={"promotionId": PROMOTION_ID, "isQueryOnlyPromotion": 1},
    headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
        "Referer": "https://activity.huaweicloud.com/",
    },
    timeout=15,
)
resp.raise_for_status()
data = resp.json()
if str(data.get("retCode")) != "0":
    emit(False, f"接口返回异常：{data.get('retCode')} {data.get('retDesc')}")
    raise SystemExit

info = ((data.get("rateOfProgressInfo") or {}).get("promotionRateOfProgressInfo")) or {}
if not info:
    emit(False, "接口未返回库存信息")
    raise SystemExit

rate = info.get("rateOfProgress") or 0
total = info.get("totalTimesPerDay")
sold = info.get("promotionTotalTimesPerDay")
persons = info.get("promotionTotalPersonsPerDay")

persons_ratio = persons / sold if (persons is not None and sold) else 0
times_ratio = sold / total if (total and sold is not None) else 0
ratio = 1.0 if rate >= 1 else (persons_ratio or times_ratio or 0)
percent = min(100, math.floor(ratio * 100))
remaining = max(0, total - sold) if isinstance(total, int) and isinstance(sold, int) else None
in_stock = percent < 100 and (remaining is None or remaining > 0)

if in_stock:
    detail = f"有货！剩余 {remaining} 台，今日 {sold}/{total}，进度 {percent}%"
else:
    detail = f"已售罄（今日 {sold}/{total}），次日 00:00 重新放量"

emit(bool(in_stock) if OK_FOLLOWS_STOCK else True,
     f"{GOODS}：{detail}",
     {"goods": GOODS, "in_stock": in_stock, "sku": SKU, "price": PRICE,
      "percent": percent, "remaining": remaining,
      "stock": total, "usage": sold, "url": SITE_URL})
''',
    },
    {
        "id": "aliyun_flash_stock",
        "name": "阿里云限时抢购库存（推荐）",
        "description": "监控阿里云活动页「限时抢购」商品的场次与已抢进度；默认仅 10:00-12:00 / 15:00-17:00 监控",
        "params": '{"activity_id": "1613051415182591", "goods": "轻量应用服务器 2核2G（38元/1年起）"}',
        "active_windows": '[{"start": "10:00", "end": "12:00"}, {"start": "15:00", "end": "17:00"}]',
        "code": '''# 阿里云限时抢购（秒杀）库存监控
# PARAMS: {"activity_id": "1613...", "goods": "商品名，可选"}
#
# 数据来源：活动页抢购卡片同款接口 /abs/promotion/queryActivityInventory
#   返回该活动每天的场次列表，每场带 progress（已抢百分比，100=抢光）
#
# 说明：emit 的 ok 表示“现在能不能抢到”：
#   有货 -> ok=True；抢光/未开场 -> ok=False。
# 配合推送策略「状态变化时推送」，就是“抢光 -> 有货”的抢购提醒。
# 只想看进度、不想被推送打扰时，把 OK_FOLLOWS_STOCK 改成 False。

import time
from datetime import datetime, timedelta, timezone

import requests

ACTIVITY_ID = str(PARAMS.get("activity_id") or "").strip()
GOODS = PARAMS.get("goods") or ACTIVITY_ID
SKU = PARAMS.get("sku") or "轻量应用服务器 2核2G / 200M峰值带宽 / 宝塔Linux面板 1年"
PRICE = float(PARAMS.get("price") or 38)
SITE_URL = PARAMS.get("url") or "https://www.aliyun.com/activity/hub/1111-cloud"
OK_FOLLOWS_STOCK = True
API = "https://t.aliyun.com/abs/promotion/queryActivityInventory"
TZ = timezone(timedelta(hours=8))          # 阿里云场次时间均为北京时间

if not ACTIVITY_ID:
    emit(False, "PARAMS 里缺少 activity_id")
    raise SystemExit


def fmt(ts):
    """毫秒时间戳 -> 北京时间 MM-DD HH:MM。"""
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts / 1000, TZ).strftime("%m-%d %H:%M")


try:
    resp = requests.get(
        API,
        params={"activityIds": ACTIVITY_ID, "preView": "true"},
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Referer": "https://www.aliyun.com/activity/hub/1111-cloud",
        },
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
except Exception as exc:
    emit(False, f"{GOODS}：查询失败 {exc}")
    raise SystemExit

if not payload.get("success"):
    emit(False, f"{GOODS}：接口返回异常 {payload.get('code')} {payload.get('message')}")
    raise SystemExit

sessions = (payload.get("data") or {}).get(ACTIVITY_ID) or []
if not sessions:
    emit(False, f"{GOODS}：接口没有返回场次数据（活动可能已结束）")
    raise SystemExit

now_ms = int(time.time() * 1000)

current = None          # 正在进行的场次
upcoming = None         # 最近一场未开始的场次
for item in sessions:
    start, end = item.get("startTime"), item.get("endTime")
    if not start or not end:
        continue
    if start <= now_ms <= end:
        current = item
    elif start > now_ms and (upcoming is None or start < upcoming["startTime"]):
        upcoming = item


def progress_of(item):
    """已抢百分比；接口缺字段时按 100（抢光）处理，与活动页表现一致。"""
    if item is None or item.get("progress") is None:
        return 100.0
    return float(item["progress"])


if current is not None:
    percent = progress_of(current)
    in_stock = percent < 100
    if in_stock:
        detail = (f"有货！第 {sessions.index(current) + 1} 场"
                  f"（{fmt(current.get('startTime'))}-{fmt(current.get('endTime'))[-5:]}）"
                  f"已抢 {percent:.0f}%，抓紧下单")
    else:
        detail = (f"第 {sessions.index(current) + 1} 场已抢光（{percent:.0f}%）"
                  + (f"，下一场 {fmt(upcoming.get('startTime'))} 开抢" if upcoming else ""))
else:
    percent = 0.0
    in_stock = False
    if upcoming is not None:
        detail = f"当前无进行中场次，下一场 {fmt(upcoming.get('startTime'))} 开抢"
    else:
        detail = "今日场次已全部结束，等待次日 10:00 / 15:00 放量"

emit(
    bool(in_stock) if OK_FOLLOWS_STOCK else True,
    f"{GOODS}：{detail}",
    {
        "goods": GOODS,
        "in_stock": in_stock,
        "sku": SKU,
        "price": PRICE,
        "progress": percent,
        # 阿里云只给百分比：按 100% 为总量换算剩余/已抢，便于和京东云统一展示
        "stock": 100,
        "remaining": (int(round(max(0.0, 100 - percent))) if in_stock else 0),
        "usage": (100 - int(round(max(0.0, 100 - percent))) if in_stock else 100),
        "percent_based": True,
        "current_session": ({"start": fmt(current.get("startTime")),
                             "end": fmt(current.get("endTime")),
                             "progress": percent} if current else None),
        "next_session": ({"start": fmt(upcoming.get("startTime")),
                          "end": fmt(upcoming.get("endTime")),
                          "progress": progress_of(upcoming)} if upcoming else None),
        "sessions": [{"start": fmt(s.get("startTime")), "end": fmt(s.get("endTime")),
                      "progress": progress_of(s)} for s in sessions],
        "checked_at": fmt(now_ms),
        "url": SITE_URL,
    },
)
''',
    },
    {
        "id": "aliyun_newuser_price",
        "name": "阿里云新人专享轻量服务器监控（推荐）",
        "description": "监控阿里云活动页「新人专享」轻量服务器 68元/年 等固定优惠价是否仍可下单（取自活动页询价接口）",
        "params": '{"goods_id": "10000014849", "goods": "轻量应用服务器 2核2G 200M峰值带宽 68元/年 新人专享", "sku": "2核2G / 200M峰值带宽 / 40G ESSD / 1年", "price": 68, "url": "https://www.aliyun.com/activity/hub/1111-cloud"}',
        "code": '''# 阿里云活动页「新人专享」轻量服务器价格监控
# PARAMS: {"goods_id": "10000014849", "goods": "商品名，可选",
#          "sku": "规格，可选", "price": 68, "url": "活动页，可选",
#          "promotion_keyword": "优惠名关键字，可选"}
#
# 数据来源：活动页商品卡片同款询价接口
#   1) GET  https://alioth.alicdn.com/promotion-commodity-<goods_id>
#          取该商品的默认下单参数（地域/规格/时长等）
#   2) POST https://t.aliyun.com/abs/promotion/queryPrice
#          查当前折后价与命中的优惠（如「68元1年」新人专享）
#
# emit 的 ok 表示“当前是否还能以目标价买到”：
#   优惠价仍在 -> ok=True；优惠下线/涨价 -> ok=False。
# 配合「状态变化时推送」，就是优惠恢复/失效提醒。
# 只想看价格、不想被推送打扰时，把 OK_FOLLOWS_PRICE 改成 False。

import json

import requests

GOODS_ID = str(PARAMS.get("goods_id") or "").strip()
GOODS = PARAMS.get("goods") or GOODS_ID
SKU = PARAMS.get("sku") or ""
TARGET_PRICE = float(PARAMS.get("price") or 0)
SITE_URL = PARAMS.get("url") or "https://www.aliyun.com/activity/hub/1111-cloud"
KEYWORD = str(PARAMS.get("promotion_keyword") or "").strip()
OK_FOLLOWS_PRICE = True
GOODS_API = "https://alioth.alicdn.com/promotion-commodity-" + GOODS_ID
PRICE_API = "https://t.aliyun.com/abs/promotion/queryPrice"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": SITE_URL,
}

if not GOODS_ID:
    emit(False, "PARAMS 里缺少 goods_id")
    raise SystemExit

try:
    goods_resp = requests.get(GOODS_API, headers=HEADERS, timeout=10)
    goods_resp.raise_for_status()
    goods_info = goods_resp.json()
    defaults = goods_info.get("defaultValues") or []
    if not defaults or not (defaults[0].get("values") or []):
        emit(False, f"{GOODS}：商品已下架或无默认配置")
        raise SystemExit

    order_params = defaults[0]["values"][0].get("orderParams") or {}
    # 下单参数里的 config 等嵌套字段对询价无用，去掉避免请求过大
    order_params.pop("config", None)
    order_params.pop("orderParams", None)
    payload = {"commodityId": goods_info.get("id") or GOODS_ID,
               "commodities": [order_params]}
    resp = requests.post(
        PRICE_API,
        data={"param": json.dumps(payload, ensure_ascii=False)},
        headers=dict(HEADERS, **{"Content-Type": "application/x-www-form-urlencoded"}),
        timeout=10,
    )
    resp.raise_for_status()
    result = resp.json()
except Exception as exc:
    emit(False, f"{GOODS}：询价失败 {exc}")
    raise SystemExit

if not result.get("success"):
    emit(False, f"{GOODS}：询价接口返回异常 {result.get('code')} {result.get('message')}")
    raise SystemExit

data = result.get("data") or {}
articles = data.get("articleItemResults") or []
price_info = (articles[0].get("price") if articles else {}) or {}
# 接口价格单位为分，转成元
price_fen = price_info.get("discountedPrice")
list_fen = price_info.get("totalPrice")
price_yuan = round(price_fen / 100.0, 2) if isinstance(price_fen, (int, float)) else None
list_yuan = round(list_fen / 100.0, 2) if isinstance(list_fen, (int, float)) else None

promotions = []
for promo in (data.get("promotionResults") or {}).values():
    option = promo.get("promotionOption") or {}
    promo_fen = option.get("discountedPrice")
    promotions.append({
        "name": promo.get("promotionName") or option.get("optionTitle") or "",
        "price": round(promo_fen / 100.0, 2) if isinstance(promo_fen, (int, float)) else None,
    })

if KEYWORD:
    matched = [p for p in promotions if KEYWORD in p["name"]]
elif TARGET_PRICE > 0:
    matched = [p for p in promotions
               if p["price"] is not None and p["price"] <= TARGET_PRICE]
else:
    matched = promotions

if price_yuan is None:
    in_stock = False
elif TARGET_PRICE > 0:
    in_stock = price_yuan <= TARGET_PRICE
else:
    in_stock = bool(matched)

promo_name = matched[0]["name"] if matched else ""
if in_stock:
    detail = f"可下单！折后 {price_yuan:g} 元/年"
    if list_yuan:
        detail += f"（原价 {list_yuan:g} 元）"
    if promo_name:
        detail += f"，优惠「{promo_name}」"
else:
    detail = f"当前折后 {price_yuan if price_yuan is not None else '?'} 元/年"
    if TARGET_PRICE > 0:
        detail += f"，目标价 ≤{TARGET_PRICE:g} 元尚未生效"
    else:
        detail += "，未命中任何活动优惠"

emit(bool(in_stock) if OK_FOLLOWS_PRICE else True,
     f"{GOODS}：{detail}",
     {"goods": GOODS,
      "in_stock": in_stock,
      "sku": SKU,
      "price": (price_yuan if price_yuan is not None else TARGET_PRICE),
      # 与其他云厂商卡片保持一致：用百分比表达优惠是否可下单
      "percent_based": True,
      "progress": (0.0 if in_stock else 100.0),
      "remaining": (100 if in_stock else 0),
      "stock": 100,
      "usage": (0 if in_stock else 100),
      "url": SITE_URL})
''',
    },
    {
        "id": "tencent_lighthouse_stock",
        "name": "腾讯云活动商品库存（推荐）",
        "description": "监控腾讯云双11活动页轻量应用服务器等商品的剩余配额，有货/抢光一目了然",
        "params": '{"goods_id": "2001194084080021", "goods": "轻量应用服务器 4核4G3M", "sku": "4核4G3M / 3M带宽 / 300GB月流量 / 1年", "price": 109, "url": "https://cloud.tencent.com/act/pro/double11-202611#qingliang"}',
        "code": '''# 腾讯云活动商品库存监控（数据来自活动页前端同款接口）
# PARAMS: {"goods_id": "2001...", "goods": "商品名，可选", "sku": "规格，可选",
#          "price": 109, "url": "活动页，可选"}
#
# 接口：POST https://act-api.cloud.tencent.com/dianshi/calc-goods-quota
#   请求 {"goods_id_list": [<商品ID>], "preview": 0}
#   返回 data.goods_quota_list[0] = {goods_id, total_num, available}
#     total_num = 活动总配额，available = 当前可购（剩余）
# emit 的 ok 表示“现在有没有货”，配合“状态变化时推送”就是补货提醒。

import requests

GOODS_ID = str(PARAMS.get("goods_id") or "").strip()
GOODS = PARAMS.get("goods") or GOODS_ID
SKU = PARAMS.get("sku") or ""
PRICE = PARAMS.get("price")
SITE_URL = PARAMS.get("url") or "https://cloud.tencent.com/act/pro/double11-202611#qingliang"
OK_FOLLOWS_STOCK = True
API = "https://act-api.cloud.tencent.com/dianshi/calc-goods-quota"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Content-Type": "application/json",
    "Origin": "https://cloud.tencent.com",
    "Referer": "https://cloud.tencent.com/act/pro/double11-202611",
}

if not GOODS_ID:
    emit(False, "PARAMS 里缺少 goods_id")
    raise SystemExit

try:
    resp = requests.post(
        API,
        json={"goods_id_list": [int(GOODS_ID)], "preview": 0},
        headers=HEADERS,
        timeout=10,
    )
    resp.raise_for_status()
    payload = resp.json()
except Exception as exc:
    emit(False, f"{GOODS}：查询失败 {exc}")
    raise SystemExit

if str(payload.get("code")) != "0":
    emit(False, f"{GOODS}：接口返回异常 {payload.get('code')} {payload.get('msg')}")
    raise SystemExit

items = (payload.get("data") or {}).get("goods_quota_list") or []
item = next((x for x in items if str(x.get("goods_id")) == GOODS_ID), items[0] if items else None)
if not item:
    emit(False, f"{GOODS}：接口未返回配额信息（商品可能已下架）")
    raise SystemExit

total = int(item.get("total_num") or 0)
remaining = max(0, int(item.get("available") or 0))
used = max(0, total - remaining)
percent = round(used * 100 / total) if total else 0
in_stock = remaining > 0

if in_stock:
    detail = f"有货！剩余 {remaining} 台（总量 {total}，已抢 {percent}%）"
else:
    detail = f"已抢光（总量 {total} 台），等待补货"

data = {
    "goods": GOODS,
    "in_stock": in_stock,
    "price": PRICE,
    "remaining": remaining,
    "stock": total,
    "percent": percent,
    "url": SITE_URL,
}
if SKU:
    data["sku"] = SKU

emit(
    bool(in_stock) if OK_FOLLOWS_STOCK else True,
    f"{GOODS}：{detail}",
    data,
)
''',
    },
    {
        "id": "jd_flash_stock",
        "name": "京东云秒杀库存（推荐）",
        "description": "监控京东云轻量云主机秒杀活动的本场剩余台数，默认仅 0/9/15 点场次前后执行",
        "params": '{"activity_no": "e6X8WC7r5w9hjsIS94jK", "goods": "轻量云主机 68元/1年", "url": "https://www.jdcloud.com/cn/pages/cloudpromotion?utm_source=cpscf&type=1&channelReferrer=3418798148814466&channelCode=JD_YTKCPS"}',
        "active_windows": '[{"start": "00:00", "end": "02:00"}, {"start": "09:00", "end": "11:00"}, {"start": "15:00", "end": "17:00"}]',
        "code": '''# 京东云秒杀库存监控（数据来自活动页同款接口）
# PARAMS: {"activity_no": "活动编号", "goods": "商品名，可选", "url": "活动页，可选"}
#
# 接口：
#   /bffApi/common/activity/queryCurrentStockInfo?activityNo=<编号>
#   /bffApi/common/activity/describeSecKillingDetail?activityNo=<编号>（取商品名与价格）
# emit 的 ok 表示“现在有没有货”，配合“状态变化时推送”就是抢购提醒。

import json

import requests

ACTIVITY_NO = str(PARAMS.get("activity_no") or "").strip()
GOODS = PARAMS.get("goods") or ACTIVITY_NO
SITE_URL = PARAMS.get("url") or "https://www.jdcloud.com/cn/pages/cloudpromotion?utm_source=cpscf&type=1&channelReferrer=3418798148814466&channelCode=JD_YTKCPS"
OK_FOLLOWS_STOCK = True
BASE = "https://www.jdcloud.com/bffApi/common/activity"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Referer": "https://www.jdcloud.com/",
}

if not ACTIVITY_NO:
    emit(False, "PARAMS 里缺少 activity_no")
    raise SystemExit


def unwrap(value):
    """接口 result 有时是对象，有时是 JSON 字符串。"""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


try:
    resp = requests.get(BASE + "/queryCurrentStockInfo",
                        params={"activityNo": ACTIVITY_NO},
                        headers=HEADERS, timeout=10)
    resp.raise_for_status()
    payload = resp.json()
    code_value = payload.get("code")
    if code_value is None or int(code_value) != 0:
        emit(False, f"{GOODS}：库存接口异常 {payload.get('message')}")
        raise SystemExit
    info = unwrap((payload.get("result") or {}).get("result")) or {}
    stock = int(info.get("stock") or 0)
    usage = int(info.get("usage") or 0)
except Exception as exc:
    emit(False, f"{GOODS}：查询库存失败 {exc}")
    raise SystemExit

remaining = max(0, stock - usage)
in_stock = remaining > 0

# 商品名与价格查不到也不影响库存判定
sku_name, price, buy_url = GOODS, None, SITE_URL
try:
    detail_resp = requests.get(BASE + "/describeSecKillingDetail",
                               params={"activityNo": ACTIVITY_NO},
                               headers=HEADERS, timeout=10)
    detail_wrap = unwrap(((detail_resp.json().get("result") or {}).get("result") or {}).get("data"))
    detail = unwrap(detail_wrap) or {}
    products = detail.get("products") or []
    if products:
        product = products[0]
        try:
            props = json.loads(product.get("properties") or "{}")
        except ValueError:
            props = {}
        sku_name = props.get("skuName") or detail.get("title") or GOODS
        prices = product.get("priceDetail") or []
        if prices:
            price = prices[0].get("price")
except Exception:
    pass

detail_text = (f"有货！剩余 {remaining} 台（本场 {stock}，已抢 {usage}）" if in_stock
               else f"已抢光（本场 {stock} 台）")

emit(bool(in_stock) if OK_FOLLOWS_STOCK else True,
     f"{sku_name}：{detail_text}",
     {"goods": GOODS, "in_stock": in_stock, "sku": sku_name,
      "price": price, "remaining": remaining, "stock": stock,
      "usage": usage, "url": buy_url})
''',
    },
    {
        "id": "tcp",
        "name": "TCP 端口探测",
        "description": "检查主机端口是否可连通（内网服务/路由器/摄像头等）",
        "params": '{"host": "192.168.1.1", "port": 80}',
        "code": '''# TCP 端口连通性探测
# PARAMS: {"host": "192.168.1.1", "port": 80}

import socket
import time

host = PARAMS.get("host") or ""
port = int(PARAMS.get("port") or 0)
if not host or not port:
    emit(False, "PARAMS 里缺少 host / port")
    raise SystemExit

started = time.time()
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5)
try:
    sock.connect((host, port))
except Exception as exc:
    emit(False, f"{host}:{port} 连接失败：{exc}", {"host": host, "port": port})
    raise SystemExit
finally:
    sock.close()

cost_ms = int((time.time() - started) * 1000)
emit(True, f"{host}:{port} 可连通，耗时 {cost_ms}ms", {"host": host, "port": port, "cost_ms": cost_ms})
''',
    },
    {
        "id": "local_script",
        "name": "运行本地 Python 脚本",
        "description": "执行本机已有的 .py 脚本（如 check_stock_once.py），自动解析末行 JSON 的 ok/detail",
        "params": '{"script": "/绝对路径/check_stock_once.py", "args": [], "timeout": 25}',
        "code": '''# 运行本地 Python 脚本并上报其结果
# PARAMS: {
#   "script": "脚本绝对路径（必填）",
#   "args":   ["可选的命令行参数"],
#   "python": "可选，解释器路径，默认用平台当前 python3",
#   "timeout": "可选，脚本超时秒数，默认 25"
# }
#
# 结果判定（与平台约定一致，天然兼容 check_stock_once.py）：
#   1. 脚本 stdout 末行为 JSON 且带 ok 字段 -> 以该 JSON 为准（detail/message 作为消息）
#   2. 否则退出码 0 = 成功，非 0 = 失败（消息取 stderr 末行）

import json
import subprocess
import sys

script = PARAMS.get("script") or ""
if not script:
    emit(False, "PARAMS 里缺少 script（脚本绝对路径）")
    raise SystemExit

args = [str(a) for a in (PARAMS.get("args") or [])]
python = PARAMS.get("python") or sys.executable
timeout = float(PARAMS.get("timeout") or 25)

try:
    proc = subprocess.run([python, script] + args, capture_output=True, text=True, timeout=timeout)
except subprocess.TimeoutExpired:
    emit(False, f"{script}：执行超时（上限 {timeout:g} 秒）", {"script": script, "timed_out": True})
    raise SystemExit
except Exception as exc:
    emit(False, f"{script}：无法启动：{exc}", {"script": script})
    raise SystemExit

lines = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]

# 末行 JSON 解析（check_stock_once.py 的输出约定：{"ok": true, "detail": "..."}）
if lines:
    try:
        payload = json.loads(lines[-1])
        if isinstance(payload, dict):
            ok = bool(payload.get("ok", proc.returncode == 0))
            message = str(payload.get("detail") or payload.get("message") or lines[-1])
            emit(ok, f"{script}：{message}", payload)
            raise SystemExit
    except ValueError:
        pass  # 末行不是 JSON，走退出码逻辑

if proc.returncode == 0:
    emit(True, f"{script}：执行成功" + (f"，{lines[-1]}" if lines else ""),
         {"script": script, "returncode": 0})
else:
    err_lines = [line.strip() for line in (proc.stderr or "").splitlines() if line.strip()]
    reason = err_lines[-1][:200] if err_lines else ""
    emit(False, f"{script}：退出码 {proc.returncode}" + (f"：{reason}" if reason else ""),
         {"script": script, "returncode": proc.returncode})
''',
    },
]


def find(template_id: str) -> Dict[str, Any]:
    for tpl in TEMPLATES:
        if tpl["id"] == template_id:
            return tpl
    raise KeyError(template_id)
