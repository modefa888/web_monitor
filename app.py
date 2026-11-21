#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页监控平台 · Flask 后端 + 启动入口

启动：
    python3 web_monitor/app.py                # 默认 http://127.0.0.1:8787
    python3 web_monitor/app.py --port 9000
    python3 web_monitor/app.py --host 0.0.0.0 # 供局域网访问（管理页需登录）

页面：
    /              公开只读看板：各服务器存量情况（无需登录）
    /go/<token>    加密跳转：解密 token -> 记录点击 -> 302 到目标站点（无需登录）
    /mianban       管理后台（需管理员登录后方可操作）

接口一览：
    GET    /                          公开存量看板页面（只读）
    GET    /mianban                   管理后台页面（前端做登录门）
    POST   /api/login                 管理员登录
    POST   /api/logout                退出登录
    GET    /api/me                    当前登录状态（含是否默认密码）
    POST   /api/password              修改管理员密码
    GET    /api/public/stock          公开只读：各监控项存量情况（无需登录）
    GET    /api/public/stock/<id>/runs 公开只读：某监控项的最近执行数据，供曲线图使用（无需登录）
    GET    /go/<token>               加密跳转：解密目标地址 -> 记录点击 -> 302 跳转（无需登录）
    GET    /api/clicks/stats         「前往站点」跳转点击统计【登录】
    GET    /api/monitors              监控项列表（含最近状态/统计/下次运行时间）【登录】
    POST   /api/monitors              新建监控项【登录】
    GET    /api/monitors/<id>         监控项详情【登录】
    PUT    /api/monitors/<id>         修改监控项【登录】
    DELETE /api/monitors/<id>         删除监控项【登录】
    POST   /api/monitors/<id>/run     立即执行一次（同步等待结果）【登录】
    GET    /api/monitors/<id>/runs    执行历史【登录】
    POST   /api/run_test              不落库的测试运行（编辑器里“测试运行”用）【登录】
    GET    /api/templates             内置监控代码模板【登录】
    GET    /api/settings              全局设置（推送渠道等）【登录】
    PUT    /api/settings              保存全局设置【登录】
    POST   /api/settings/test         发送一条测试推送【登录】
    GET    /api/visits/stats          公开看板访问统计【登录】
    GET    /api/visits/list           访客明细列表（支持关键字搜索）【登录】
    GET    /api/api-stats             接口调用统计（次数/错误/耗时，按路由聚合）【登录】
    POST   /api/api-manage            启用 / 停用某个接口路由【登录】

鉴权：除 login / logout / me / public/stock 外，所有 /api/* 都需要管理员登录
（Flask session cookie，默认保留 7 天，且绑定登录时的协议/主机/端口，换入口需重新登录）。首次启动自动创建默认账号
admin / admin123，登录后请立即在右上角「修改密码」里改掉。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import secrets
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))  # 允许直接 python3 app.py 运行

from flask import Flask, g, jsonify, redirect, request, send_from_directory, session

from code_templates import TEMPLATES
from notifier import Notifier, sample_notify_context
from scheduler import Scheduler, run_code
from sitelink import MAX_URL_LEN, decode_site_token, encode_site_token
from store import DEFAULT_ADMIN_PASSWORD, DEFAULT_ADMIN_USERNAME, Store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("web-monitor")

app = Flask(__name__, static_folder="static", static_url_path="")
store: Store = Store()          # 启动时在 main() 里按参数重建
scheduler: Optional[Scheduler] = None

# 登录会话密钥：占位值，main() 里替换为 data/secret_key 的持久化密钥（重启不失效）
app.config["SECRET_KEY"] = secrets.token_hex(32)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)


def _load_secret_key(data_dir: Path) -> str:
    """读取/生成 session 签名密钥（放在 data 目录，随数据库一起备份）。"""
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "secret_key"
    try:
        if key_file.exists():
            key = key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        key = secrets.token_hex(32)
        key_file.write_text(key, encoding="utf-8")
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
        return key
    except OSError:
        return secrets.token_hex(32)


# 无需登录即可访问的 API（其余 /api/* 一律要求管理员登录）
PUBLIC_API_PATHS = {
    "/api/login", "/api/logout", "/api/me", "/api/public/stock",
}

# 受保护的接口路由：不允许通过接口管理停用（避免把自己锁在外面）
PROTECTED_API_ROUTES = {
    "/api/login", "/api/logout", "/api/me",
    "/api/api-manage", "/api/api-stats",
}

# 接口说明：路由模板 -> 一句话功能描述（接口统计面板展示用，与 docstring 保持一致）
API_DESCRIPTIONS = {
    "/api/login": "管理员登录（用户名 + 密码换取会话 Cookie）",
    "/api/logout": "退出登录，清除会话",
    "/api/me": "查询当前登录状态与是否默认密码",
    "/api/password": "修改管理员密码",
    "/api/public/stock": "公开看板数据：各监控项最新存量 / 状态（无需登录）",
    "/api/public/stock/<int:monitor_id>": "公开只读：某监控项最近执行数据，供曲线图使用（无需登录）",
    "/api/monitors": "GET=监控项列表（含状态 / 统计 / 下次运行）；POST=新建监控项",
    "/api/monitors/<int:monitor_id>": "GET=监控项详情；PUT=修改监控项；DELETE=删除监控项",
    "/api/monitors/<int:monitor_id>/run": "立即执行一次监控项（同步等待结果）",
    "/api/monitors/<int:monitor_id>/runs": "某监控项的执行历史",
    "/api/monitors/order": "保存后台拖动的监控项排序",
    "/api/groups": "GET=分组与图标列表；POST=新建空分组",
    "/api/groups/icon": "设置 / 清除分组图标",
    "/api/groups/rename": "重命名分组（组下监控项同步改名）",
    "/api/groups/order": "保存分组展示顺序",
    "/api/settings": "GET=读取全局设置；PUT=保存推送渠道 / 通知 / 网站信息",
    "/api/settings/test": "发送一条测试推送",
    "/api/notify/log": "推送通知日志（含手动测试）",
    "/api/visits/stats": "公开看板访问统计（PV / UV / 设备分布 / 趋势）",
    "/api/visits/list": "访客明细列表（支持关键字搜索）",
    "/api/clicks/stats": "「前往站点」跳转点击统计",
    "/api/api-stats": "接口调用统计（本面板的数据源）",
    "/api/api-manage": "启用 / 停用某个接口路由",
    "/api/templates": "内置监控代码模板",
    "/api/run_test": "编辑器「测试运行」（不落库）",
}


def _api_route() -> str:
    """当前请求的接口路由模板；未知路径命中静态兜底路由时按真实路径记录。"""
    route = request.url_rule.rule if request.url_rule else request.path
    if request.endpoint == "static":
        route = request.path
    return route

# 公开看板只展示关键字段：冗长的原始数据（场次明细、检查时间、商品名等）
# 留在历史记录里供管理员排查，不在只读看板上刷一屏。
# url / site_url（跳转地址）也不直接下发：改为加密后的 go 跳转链接，
# 页面源码里看不到目标网址，点击时由 /go/<token> 解密跳转并记一次点击统计。
PUBLIC_HIDDEN_DATA_KEYS = {
    "goods", "checked_at", "current_session", "next_session", "sessions",
    "url", "site_url",
}

# 公开看板不能展示采集脚本的技术异常细节（内部域名、端口、接口路径、堆栈等）。
# 失败消息里命中这些特征时，对外统一替换成友好提示；完整原文仍保留在
# 管理员可见的执行历史（runs.message / runs.output）和推送通知里。
#
# 技术异常的对外容忍窗口（秒）：最新一次运行失败且属于技术异常时，公开看板
# 先继续展示上一次成功的快照（消息/数据/状态），偶尔的网络抖动不刷屏；
# 距上次成功超过「基础窗口 + 2 个采集间隔」仍失败才对外显示异常。
# 仅公开接口生效，管理后台与推送通知始终立即反映真实状态。
PUBLIC_ERROR_TOLERANT_SECONDS = 300

_PUBLIC_ERROR_MARKERS = (
    "Traceback", "Error:", "Exception", "NewConnectionError", "ConnectionError",
    "HTTPSConnectionPool", "HTTPConnectionPool", "Max retries exceeded",
    "NewConnectionError", "Connection refused", "Network is unreachable",
    "timed out", "Timeout", "SSLError", "JSONDecodeError", "KeyError",
    "TypeError", "ValueError", "IndexError", "AttributeError", "Errno",
    "Caused by", "Failed to establish", "unreachable", "not known",
    "requests.exceptions", "Connection aborted", "RemoteDisconnected",
)


def _is_technical_error(text: str) -> bool:
    """是否为采集脚本抛出的技术异常（堆栈 / 连接错误 / 类型错误等）。"""
    return bool(text) and any(
        marker.lower() in text.lower() for marker in _PUBLIC_ERROR_MARKERS
    )


def _public_message(raw: Any) -> str:
    """公开结果消息的脱敏：技术异常对外只显示一句友好提示。"""
    text = str(raw or "")
    if _is_technical_error(text):
        return "数据获取异常，管理员正在处理"
    return text


def _admin_message(raw: Any) -> str:
    """管理端卡片消息脱敏：异常原文只留在执行历史与推送里。"""
    text = str(raw or "")
    if _is_technical_error(text):
        return "数据获取异常，详情请查看执行历史"
    return text


def _request_origin() -> str:
    """浏览器 cookie 不区分端口，用 scheme://host:port 标识当前访问入口。"""
    return f"{request.scheme}://{request.host.lower()}"


def _session_origin_ok() -> bool:
    """登录态只在登录时的地址（含端口）有效，换端口或换 IP/域名都要重新登录。"""
    return bool(session.get("user")) and session.get("login_origin") == _request_origin()


def _api_is_public(path: str) -> bool:
    """是否公共接口（无需登录即可调用）：/api/public/* 前缀 + 登录/登出/会话。"""
    return path in PUBLIC_API_PATHS or path.startswith("/api/public/")


@app.before_request
def auth_guard():
    """管理类 API 统一鉴权 + 已停用接口拦截；页面与静态资源放行。"""
    if not request.path.startswith("/api/"):
        return None
    # 已停用的接口直接返回 503（受保护路由不可能出现在停用列表，双保险再挡一次）
    disabled = set(store.get_settings().get("disabled_apis") or [])
    if _api_route() in disabled:
        return error("该接口已被管理员停用", 503)
    if _api_is_public(request.path):
        return None
    if not _session_origin_ok():
        session.clear()
        return error("登录状态仅对当前地址有效，请重新登录", 401)
    return None


def _client_ip() -> str:
    """取访问 IP：优先反向代理头，其次直连地址。"""
    for header in ("X-Forwarded-For", "X-Real-IP"):
        value = request.headers.get(header) or ""
        if value.strip():
            return value.split(",")[0].strip()[:64]
    return (request.remote_addr or "").strip()[:64]


def _ua_version(ua_lower: str, key: str) -> str:
    """提取 UA 里客户端标识后的版本号，如 Chrome/131.0 -> 131.0。"""
    match = re.search(re.escape(key) + r"[\s/]*([\d.]+)", ua_lower)
    return match.group(1).rstrip(".") if match else ""


def _parse_user_agent(ua: str) -> Tuple[str, str, str]:
    """从 User-Agent 解析 (设备, 浏览器, 系统)，纯正则实现，无需额外依赖。"""
    text = ua or ""
    low = text.lower()

    # 操作系统：Android / iOS 尽量带版本号
    match = re.search(r"Android\s+([\d.]+)", text, re.I)
    if match:
        os_name = f"Android {match.group(1)}"
    else:
        match = re.search(r"(?:CPU (?:iPhone )?OS|iPhone OS)\s+([\d_]+)", text, re.I)
        if match:
            os_name = f"iOS {match.group(1).replace('_', '.')}"
        else:
            match = re.search(r"Windows NT ([\d.]+)", text, re.I)
            nt_map = {"10.0": "Windows 10/11", "6.3": "Windows 8.1",
                      "6.2": "Windows 8", "6.1": "Windows 7"}
            if match:
                os_name = nt_map.get(match.group(1), f"Windows {match.group(1)}")
            elif "mac os x" in low or "macintosh" in low:
                os_name = "macOS"
            elif "linux" in low:
                os_name = "Linux"
            else:
                os_name = "未知系统"

    # 设备类型与常见型号
    if "ipad" in low or "tablet" in low:
        device = "平板 · iPad" if "ipad" in low else "平板"
    elif "iphone" in low or "ipod" in low:
        device = "手机 · iPhone"
    elif "android" in low:
        match = re.search(r"Android[^;)]*;\s*([^;)]+)", text, re.I)
        model = re.sub(r"\s+Build/.*$", "", match.group(1).strip() if match else "",
                       flags=re.I).strip()[:40]
        device = f"手机 · {model}" if model else "手机"
    elif "windows" in low or "mac os x" in low or "macintosh" in low or "linux" in low:
        device = "桌面电脑"
    else:
        device = "其他设备"

    # 浏览器：先匹配特征更明确的客户端，再匹配通用浏览器
    if "micromessenger" in low:
        name, key = "微信", "micromessenger"
    elif "mqqbrowser" in low:
        name, key = "QQ浏览器", "mqqbrowser"
    elif "edg" in low:
        name, key = "Edge", "edg"
    elif "opr/" in low or "opera" in low:
        name, key = "Opera", "opr/"
    elif "firefox" in low or "fxios" in low:
        name, key = "Firefox", "fxios" if "fxios" in low else "firefox"
    elif "chrome" in low or "crios" in low:
        name, key = "Chrome", "crios" if "crios" in low else "chrome"
    elif "safari" in low:
        name, key = "Safari", "version"
    elif "msie" in low or "trident" in low:
        name, key = "IE", "msie"
    elif any(k in low for k in ("curl", "wget", "python-requests", "postman",
                               "bot", "spider", "crawler")):
        name, key = "脚本/爬虫", ""
    else:
        name, key = "未知浏览器", ""
    version = _ua_version(low, key) if key else ""
    browser = f"{name} {version}" if version else name
    return device, browser, os_name


@app.before_request
def track_public_visit():
    """记录普通用户对公开看板页面（/）的访问。
    只统计页面打开：看板每 10 秒轮询一次数据接口，不计入访问次数。
    访客以 ip+UA 哈希匿名标识，另存 IP、设备、浏览器、系统与原始 UA（仅管理员可见）。"""
    if request.path != "/" or request.method != "GET":
        return None
    ip = _client_ip()
    ua = request.headers.get("User-Agent") or ""
    visitor = hashlib.sha256(f"{ip}|{ua}".encode("utf-8", "ignore")).hexdigest()[:12]
    device, browser, os_name = _parse_user_agent(ua)
    try:
        store.log_visit("/", visitor, request.headers.get("Referer") or "",
                        ip=ip, device=device, browser=browser,
                        os_name=os_name, user_agent=ua)
    except Exception as exc:  # 统计失败不影响页面访问
        log.warning("记录访问统计失败：%s", exc)
    return None


@app.before_request
def start_api_timer():
    """记录 /api/* 请求的开始时间，供 after_request 计算处理耗时。"""
    if request.path.startswith("/api/"):
        g.api_start = time.time()
    return None


@app.after_request
def log_api_call(response):
    """把 /api/* 每次调用的路由/方法/状态码/耗时/IP 记入接口统计（仅管理端可见）。
    统计失败不影响响应。"""
    if not request.path.startswith("/api/"):
        return response
    try:
        start = getattr(g, "api_start", None)
        dur_ms = (time.time() - start) * 1000 if start else 0
        store.log_api_call(request.method, _api_route(), response.status_code,
                           dur_ms, _client_ip(),
                           auth=0 if _api_is_public(request.path) else 1)
    except Exception as exc:
        log.warning("记录接口统计失败：%s", exc)
    return response


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def error(message: str, status: int = 400):
    return jsonify({"error": message}), status


@app.errorhandler(404)
def handle_not_found(_exc):
    """未知路径展示带小游戏的 404 页面；未知 API 仍返回 JSON。"""
    if request.path.startswith("/api/"):
        return error("接口不存在", 404)
    return send_from_directory(app.static_folder, "404.html"), 404


def parse_json() -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    payload = request.get_json(silent=True)
    if payload is None or not isinstance(payload, dict):
        return None, "请求体必须是 JSON 对象"
    return payload, None


def validate_params(raw: Any) -> Optional[str]:
    """params 允许任意 JSON，但为了能注入 PARAMS 变量，要求是对象。"""
    if raw in (None, ""):
        return None
    if isinstance(raw, dict):
        return None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return "params 不是合法的 JSON"
        if isinstance(parsed, dict):
            return None
        return "params 必须是 JSON 对象（如 {\"key\": \"value\"}）"
    return "params 必须是 JSON 对象"


WINDOW_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
PRICE_TYPES = {"new_user", "renew", "normal"}   # 新用户首年 / 老用户续费 / 常规价
NOTIFY_ON_VALUES = {"change", "in_stock", "fail", "never"}  # 状态变化 / 有库存 / 仅失败 / 不推送


def _validate_meta(vendor: str, region: str, price_type: str,
                   purchase_limit: int, realname: str) -> Optional[str]:
    """校验厂商名 / 地区 / 价格类型 / 限购数量 / 是否需要实名。"""
    if len(vendor) > 30:
        return "厂商名最多 30 个字"
    if len(region) > 30:
        return "地区名称最多 30 个字"
    if price_type and price_type not in PRICE_TYPES:
        return "价格类型不合法"
    if purchase_limit < 0 or purchase_limit > 9999:
        return "限购数量需在 0 ~ 9999 之间"
    if realname not in ("", "yes", "no"):
        return "实名设置不合法"
    return None


def _normalize_windows(raw: Any) -> Tuple[str, Optional[str]]:
    """校验并规范监控时段：JSON 数组 [{start: HH:MM, end: HH:MM}]，空=全天。"""
    if raw in (None, ""):
        return "", None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return "", "监控时段不是合法的 JSON 数组"
    else:
        parsed = raw
    if not isinstance(parsed, list):
        return "", "监控时段必须是数组"
    result = []
    for item in parsed:
        if not isinstance(item, dict):
            return "", "监控时段的每一项必须是含 start / end 的对象"
        start, end = item.get("start"), item.get("end")
        if not all(isinstance(x, str) and WINDOW_TIME_RE.match(x) for x in (start, end)):
            return "", "监控时段格式必须是 HH:MM（如 10:00-12:00）"
        result.append({"start": start, "end": end})
    return (json.dumps(result, ensure_ascii=False) if result else ""), None


def monitor_view(monitor: Dict[str, Any], stats: Dict[int, Dict[str, Any]],
                 slow_map: Optional[Dict[int, bool]] = None) -> Dict[str, Any]:
    """组装给前端的监控项视图：定义字段 + 最近状态 + 聚合统计 + 调度信息。"""
    assert scheduler is not None
    row = {
        "id": monitor["id"],
        "name": monitor["name"],
        "group_name": monitor.get("group_name") or "",
        "code": monitor["code"],
        "interval_seconds": monitor["interval_seconds"],
        "timeout_seconds": monitor["timeout_seconds"],
        "enabled": bool(monitor["enabled"]),
        "params": monitor["params"],
        "notify_on": monitor["notify_on"],
        "retry_count": monitor.get("retry_count") or 0,
        "retry_interval": (monitor.get("retry_interval")
                           if monitor.get("retry_interval") is not None else 5),
        "slow_threshold_ms": monitor.get("slow_threshold_ms") or 0,
        "active_windows": monitor.get("active_windows") or "",
        "vendor": monitor.get("vendor") or "",
        "region": monitor.get("region") or "",
        "price_type": monitor.get("price_type") or "",
        "purchase_limit": monitor.get("purchase_limit") or 0,
        "realname": monitor.get("realname") or "",
        "last_ok": None if monitor["last_ok"] is None else bool(monitor["last_ok"]),
        "last_message": _admin_message(monitor["last_message"]),
        "last_run_at": monitor["last_run_at"],
        "last_slow": bool(slow_map and slow_map.get(monitor["id"])),
        "last_data": json.loads(monitor["last_data"]) if monitor.get("last_data") else None,
        "fail_count": monitor["fail_count"],
        "running": scheduler.is_running(monitor["id"]),
        "next_run_at": scheduler.next_run_at(monitor["id"]) if monitor["enabled"] else None,
        "created_at": monitor["created_at"],
        "updated_at": monitor["updated_at"],
    }
    stat = stats.get(monitor["id"])
    if stat:
        row["stats"] = {
            "total": stat["total"],
            "ok_count": stat["ok_count"],
            "success_rate": round(stat["ok_count"] / stat["total"] * 100, 1) if stat["total"] else None,
            "avg_ms": int(stat["avg_ms"]) if stat["avg_ms"] is not None else None,
        }
    else:
        row["stats"] = {"total": 0, "ok_count": 0, "success_rate": None, "avg_ms": None}
    return row


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #

@app.get("/")
def stock_page():
    """公开只读看板：各服务器存量情况（无需登录）。"""
    return send_from_directory(app.static_folder, "stock.html")


@app.get("/mianban")
def index():
    """管理后台页面：前端做登录门，接口由 auth_guard 鉴权。"""
    return send_from_directory(app.static_folder, "index.html")


# --------------------------------------------------------------------------- #
# 管理员登录 / 会话
# --------------------------------------------------------------------------- #

@app.post("/api/login")
def api_login():
    payload, err = parse_json()
    if err:
        return error(err)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return error("请输入用户名和密码")
    user = store.verify_user(username, password)
    if not user:
        log.warning("登录失败：%s（来自 %s）", username, request.remote_addr)
        return error("用户名或密码错误", 401)
    session.clear()
    session["user"] = user["username"]
    session["login_origin"] = _request_origin()
    session.permanent = True
    log.info("管理员 %s 已登录（来自 %s，入口 %s）",
             username, request.remote_addr, session["login_origin"])
    return jsonify({
        "user": {"username": user["username"]},
        "password_is_default": store.is_default_password(user["username"]),
    })


@app.post("/api/logout")
def api_logout():
    username = session.get("user")
    session.clear()
    if username:
        log.info("管理员 %s 已退出登录", username)
    return jsonify({"ok": True})


@app.get("/api/me")
def api_me():
    if not _session_origin_ok():
        session.clear()
        return jsonify({"authenticated": False, "user": None})
    user = session.get("user")
    return jsonify({
        "authenticated": True,
        "user": {"username": user},
        "password_is_default": store.is_default_password(user),
    })


@app.post("/api/password")
def api_change_password():
    username = session.get("user")
    if not username:            # before_request 已拦截，这里兜底
        return error("未登录或登录已过期", 401)
    payload, err = parse_json()
    if err:
        return error(err)
    old_password = payload.get("old_password") or ""
    new_password = payload.get("new_password") or ""
    if not store.verify_user(username, old_password):
        return error("原密码不正确")
    if len(new_password) < 6:
        return error("新密码至少 6 位")
    if new_password == DEFAULT_ADMIN_PASSWORD:
        return error("新密码不能与出厂默认密码相同")
    store.update_password(username, new_password)
    log.info("管理员 %s 已修改密码", username)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# 公开只读：各服务器存量情况（无需登录）
# --------------------------------------------------------------------------- #

# 公开看板响应缓存：所有访客共享同一份结果（数据对每位访客完全相同），
# 千级并发轮询不会反复查询数据库 / 重复加密跳转链接。
# TTL 5 秒兜底；监控写入新结果（set_runtime）时通过监听器立即失效。
PUBLIC_STOCK_CACHE_TTL = 5.0
_public_stock_lock = threading.Lock()
_public_stock_cache = {"body": None, "ts": 0.0}


def _invalidate_public_stock_cache() -> None:
    with _public_stock_lock:
        _public_stock_cache["body"] = None


def _public_data(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    """公开看板的结果数据：过滤冗余字段与空值，只留可读的关键信息。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return {
        k: v for k, v in data.items()
        if k not in PUBLIC_HIDDEN_DATA_KEYS and v is not None and v != ""
    }


def _site_url_of(raw: Optional[str]) -> str:
    """从最近一次结果的 data JSON 里取跳转目标（url / site_url 任一非空者）。"""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return ""
    if not isinstance(data, dict):
        return ""
    for key in ("url", "site_url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _go_link(url: str, monitor_id: int) -> str:
    """把目标地址加密成站内跳转链接 /go/<token>（secret 沿用 data/secret_key）。"""
    if not url or len(url) > MAX_URL_LEN:
        return ""   # 超长地址不生成跳转链接，避免截断出错误目标
    try:
        token = encode_site_token(app.config["SECRET_KEY"], url, monitor_id)
    except ValueError:
        return ""
    return f"/go/{token}"


@app.get("/go/<token>")
def go_redirect(token: str):
    """加密跳转：解出目标地址后记一次点击统计，再 302 到目标站点（新标签页打开）。"""
    decoded = decode_site_token(app.config["SECRET_KEY"], token)
    if not decoded:
        # token 无效/过期（如换了 secret_key）时回看板首页，不暴露错误细节
        return redirect("/", code=302)
    monitor_id, url = decoded
    ip = _client_ip()
    ua = request.headers.get("User-Agent") or ""
    visitor = hashlib.sha256(f"{ip}|{ua}".encode("utf-8", "ignore")).hexdigest()[:12]
    device, browser, os_name = _parse_user_agent(ua)
    monitor_name = ""
    if monitor_id:
        monitor = store.get_monitor(monitor_id)
        monitor_name = monitor["name"] if monitor else ""
    try:
        store.log_click(url, monitor_id or None, monitor_name,
                        visitor=visitor, ip=ip, device=device,
                        browser=browser, os_name=os_name,
                        referer=request.headers.get("Referer") or "")
    except Exception as exc:  # 统计失败不影响跳转
        log.warning("记录跳转点击失败：%s", exc)
    resp = redirect(url, code=302)
    # 不向目标站点透露来源页（看板地址），也避免目标站点看到加密 token
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@app.get("/api/public/stock")
def api_public_stock():
    """公开接口：只暴露名称/状态/结果消息/结果数据等展示字段，
    不含监控代码、PARAMS、stdout、历史记录等敏感内容。
    响应做短 TTL 缓存：所有访客共享同一份结果，千级并发轮询不会打穿数据库；
    监控写入新结果（set_runtime）时立即失效，看板最多滞后 1 个刷新周期。"""
    now = time.time()
    with _public_stock_lock:
        body = _public_stock_cache["body"]
        if body and now - _public_stock_cache["ts"] < PUBLIC_STOCK_CACHE_TTL:
            return app.response_class(body, mimetype="application/json")
    monitors = store.list_monitors(enabled_only=True)
    stats = store.stats_by_monitor()
    items = []
    now_ts = time.time()
    for m in monitors:
        stat = stats.get(m["id"], {})
        ok_flag = m["last_ok"]
        message = m["last_message"]
        data = m.get("last_data")
        # 技术异常容忍：偶尔一次采集失败（网络抖动等）继续展示上一次成功的
        # 快照，不让看板闪成红色异常；持续失败超过容忍窗口才对外显示异常。
        # 有意义的结果（如"已抢光"）不属于技术异常，照常展示。
        if (ok_flag == 0 and _is_technical_error(message)):
            tolerant = PUBLIC_ERROR_TOLERANT_SECONDS + 2 * float(m["interval_seconds"] or 30)
            last_ok_run = store.get_last_ok_run(m["id"])
            if last_ok_run and (now_ts - last_ok_run["started_at"]) <= tolerant:
                ok_flag = 1
                message = last_ok_run["message"]
                data = last_ok_run["data"]
        go_link = _go_link(_site_url_of(data), m["id"]) if data else ""
        items.append({
            "id": m["id"],
            "name": m["name"],
            "group_name": m.get("group_name") or "",
            "status": ("pending" if ok_flag is None
                       else ("ok" if ok_flag else "fail")),
            "running": bool(scheduler and scheduler.is_running(m["id"])),
            "message": _public_message(message),
            "data": _public_data(data),
            # 前往站点：原始地址不下发，只给加密跳转链接
            "go": go_link or None,
            "last_run_at": m["last_run_at"],
            "interval_seconds": m["interval_seconds"],
            "active_windows": m.get("active_windows") or "",
            "vendor": m.get("vendor") or "",
            "region": m.get("region") or "",
            "price_type": m.get("price_type") or "",
            "purchase_limit": m.get("purchase_limit") or 0,
            "realname": m.get("realname") or "",
            "next_run_at": (scheduler.next_run_at(m["id"])
                            if scheduler and m["enabled"] else None),
            "fail_count": m["fail_count"] or 0,
            "success_rate": (round(stat["ok_count"] / stat["total"] * 100, 1)
                             if stat.get("total") else None),
        })
    payload = json.dumps({
        "items": items,
        "groups": store.list_groups(),   # 分组展示顺序（后台拖动排序后同步）
        "group_icons": store.group_icons(),  # 分组图标映射，看板标题栏同步展示
        "all_icon": store.all_icon(),    # 「全部」标签图标
        "site": store.site_info(),       # 网站基础信息（看板标题 / 页脚）
        "generated_at": time.time(),
    }, ensure_ascii=False)
    with _public_stock_lock:
        _public_stock_cache["body"] = payload
        _public_stock_cache["ts"] = time.time()
    return app.response_class(payload, mimetype="application/json")


@app.get("/api/public/stock/<int:monitor_id>/runs")
def api_public_stock_runs(monitor_id: int):
    """公开只读：某个启用监控项的最近执行数据（按时间正序），供看板曲线图使用。"""
    monitor = store.get_monitor(monitor_id)
    if not monitor or not monitor["enabled"]:
        return error("监控项不存在或未启用", 404)
    try:
        limit = int(request.args.get("limit", 1000))
    except ValueError:
        limit = 1000
    try:
        since = float(request.args.get("since") or 0)
    except ValueError:
        since = 0.0
    runs = store.list_runs(monitor_id, min(max(limit, 1), 5000))
    items = [{
        "started_at": run["started_at"],
        "ok": bool(run["ok"]),
        "message": _public_message(run["message"]),
        "data": _public_data(run.get("data")),
    } for run in reversed(runs)       # 历史查询是倒序，画图要正序
        if not since or run["started_at"] >= since]
    return jsonify({
        "name": monitor["name"],
        "items": items,
        "generated_at": time.time(),
    })


# --------------------------------------------------------------------------- #
# 监控项 CRUD
# --------------------------------------------------------------------------- #

@app.get("/api/monitors")
def api_list_monitors():
    monitors = store.list_monitors()
    stats = store.stats_by_monitor()
    slow_map = store.last_slow_by_monitor()
    return jsonify({"monitors": [monitor_view(m, stats, slow_map) for m in monitors]})


@app.post("/api/monitors")
def api_create_monitor():
    payload, err = parse_json()
    if err:
        return error(err)
    if not (payload.get("name") or "").strip():
        return error("监控名称不能为空")
    if err := validate_params(payload.get("params")):
        return error(err)
    active_windows, err = _normalize_windows(payload.get("active_windows"))
    if err:
        return error(err)
    vendor = str(payload.get("vendor") or "").strip()
    region = str(payload.get("region") or "").strip()
    price_type = str(payload.get("price_type") or "").strip()
    realname = str(payload.get("realname") or "").strip()
    try:
        purchase_limit = int(payload.get("purchase_limit") or 0)
    except (TypeError, ValueError):
        return error("限购数量必须是整数")
    if err := _validate_meta(vendor, region, price_type, purchase_limit, realname):
        return error(err)
    notify_on = str(payload.get("notify_on") or "change")
    if notify_on not in NOTIFY_ON_VALUES:
        return error("推送策略不合法")
    monitor_id = store.create_monitor({
        "name": payload["name"].strip(),
        "group_name": payload.get("group_name") or "",
        "code": payload.get("code") or "",
        "interval_seconds": payload.get("interval_seconds") or 30,
        "timeout_seconds": payload.get("timeout_seconds") or 30,
        "enabled": payload.get("enabled", True),
        "params": _normalize_params(payload.get("params")),
        "notify_on": notify_on,
        "retry_count": payload.get("retry_count") or 0,
        "retry_interval": payload.get("retry_interval"),   # None 时由 store 层取默认 5
        "slow_threshold_ms": payload.get("slow_threshold_ms") or 0,
        "active_windows": active_windows,
        "vendor": vendor,
        "region": region,
        "price_type": price_type,
        "purchase_limit": purchase_limit,
        "realname": realname,
    })
    log.info("新建监控项 #%s %s", monitor_id, payload["name"])
    monitor = store.get_monitor(monitor_id)
    return jsonify({"monitor": monitor_view(monitor, store.stats_by_monitor(),
                                            store.last_slow_by_monitor())}), 201


@app.post("/api/monitors/order")
def api_reorder_monitors():
    """保存后台拖动排序：按 ids 顺序写入组内 sort_order（公开看板同样按此排序）。"""
    payload, err = parse_json()
    if err:
        return error(err)
    raw = payload.get("ids")
    if not isinstance(raw, list) or not raw:
        return error("ids 不能为空")
    ids = []
    for value in raw:
        try:
            ids.append(int(value))
        except (TypeError, ValueError):
            return error("ids 必须是整数数组")
    existing = {m["id"] for m in store.list_monitors()}
    missing = [i for i in ids if i not in existing]
    if missing:
        return error(f"监控项 #{missing[0]} 不存在")
    store.reorder_monitors(ids)
    log.info("调整监控项顺序：%s", ids)
    return jsonify({"ok": True, "count": len(ids)})


@app.get("/api/monitors/<int:monitor_id>")
def api_get_monitor(monitor_id: int):
    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return error("监控项不存在", 404)
    return jsonify({"monitor": monitor_view(monitor, store.stats_by_monitor(),
                                            store.last_slow_by_monitor())})


@app.put("/api/monitors/<int:monitor_id>")
def api_update_monitor(monitor_id: int):
    if not store.get_monitor(monitor_id):
        return error("监控项不存在", 404)
    payload, err = parse_json()
    if err:
        return error(err)
    if "name" in payload and not (payload.get("name") or "").strip():
        return error("监控名称不能为空")
    if err := validate_params(payload.get("params")):
        return error(err)
    fields = {k: payload[k] for k in
              ("name", "group_name", "code", "interval_seconds", "timeout_seconds",
               "enabled", "notify_on", "retry_count", "retry_interval",
               "slow_threshold_ms", "active_windows", "vendor", "region", "price_type",
               "purchase_limit", "realname")
              if k in payload}
    if "name" in fields:
        fields["name"] = fields["name"].strip()
    if "notify_on" in fields:
        fields["notify_on"] = str(fields["notify_on"] or "change")
        if fields["notify_on"] not in NOTIFY_ON_VALUES:
            return error("推送策略不合法")
    if "active_windows" in fields:
        normalized, err = _normalize_windows(fields["active_windows"])
        if err:
            return error(err)
        fields["active_windows"] = normalized
    if any(k in fields for k in ("vendor", "region", "price_type", "purchase_limit", "realname")):
        current = store.get_monitor(monitor_id) or {}
        try:
            limit_value = int(fields.get("purchase_limit", current.get("purchase_limit") or 0) or 0)
        except (TypeError, ValueError):
            return error("限购数量必须是整数")
        if err := _validate_meta(
            str(fields.get("vendor", current.get("vendor") or "") or "").strip(),
            str(fields.get("region", current.get("region") or "") or "").strip(),
            str(fields.get("price_type", current.get("price_type") or "") or "").strip(),
            limit_value,
            str(fields.get("realname", current.get("realname") or "") or "").strip(),
        ):
            return error(err)
        for key in ("vendor", "region", "price_type", "realname"):
            if key in fields:
                fields[key] = str(fields[key] or "").strip()
        if "purchase_limit" in fields:
            fields["purchase_limit"] = limit_value
    if "params" in payload:
        fields["params"] = _normalize_params(payload.get("params"))
    store.update_monitor(monitor_id, fields)
    log.info("修改监控项 #%s %s", monitor_id, fields.get("name") or "")
    monitor = store.get_monitor(monitor_id)
    return jsonify({"monitor": monitor_view(monitor, store.stats_by_monitor(),
                                            store.last_slow_by_monitor())})


@app.delete("/api/monitors/<int:monitor_id>")
def api_delete_monitor(monitor_id: int):
    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return error("监控项不存在", 404)
    store.delete_monitor(monitor_id)
    log.info("删除监控项 #%s %s", monitor_id, monitor["name"])
    return jsonify({"deleted": monitor_id})


def _normalize_params(raw: Any) -> str:
    """统一存成 JSON 字符串；空值存 {}。"""
    if raw in (None, ""):
        return "{}"
    if isinstance(raw, dict):
        return json.dumps(raw, ensure_ascii=False)
    try:
        parsed = json.loads(raw)
        return json.dumps(parsed, ensure_ascii=False) if isinstance(parsed, dict) else "{}"
    except ValueError:
        return "{}"


@app.get("/api/groups")
def api_list_groups():
    """所有分组名：监控项里出现的 + 单独创建的空分组；icons 为分组图标映射。"""
    return jsonify({"groups": store.list_groups(), "icons": store.group_icons(),
                    "all_icon": store.all_icon()})


@app.post("/api/groups")
def api_create_group():
    """新建一个空分组（不与监控项绑定，删光监控后仍保留），可同时设置图标。"""
    payload, err = parse_json()
    if err:
        return error(err)
    name = (payload.get("name") or "").strip()
    if not name:
        return error("分组名称不能为空")
    if len(name) > 30:
        return error("分组名称最多 30 个字")
    groups = store.add_group(name)
    icon = (payload.get("icon") or "").strip()
    icons = store.set_group_icon(name, icon) if icon else store.group_icons()
    log.info("新建分组：%s（图标：%s）", name, icon or "默认")
    return jsonify({"groups": groups, "icons": icons}), 201


@app.post("/api/groups/icon")
def api_set_group_icon():
    """设置 / 清除某个分组的图标（icon 传空则恢复默认）；
    name 传 __all__ 时设置的是「全部」标签的图标。"""
    payload, err = parse_json()
    if err:
        return error(err)
    name = str(payload.get("name") or "")
    icon = str(payload.get("icon") or "")
    try:
        if name == "__all__":
            all_icon = store.set_all_icon(icon)
            log.info("设置「全部」标签图标：%s", all_icon or "默认")
            return jsonify({"icons": store.group_icons(), "all_icon": all_icon})
        icons = store.set_group_icon(name, icon)
    except ValueError as e:
        return error(str(e))
    log.info("设置分组图标：%s -> %s", name, icon or "默认")
    return jsonify({"icons": icons})


@app.post("/api/groups/rename")
def api_rename_group():
    """重命名分组：该组下所有监控项一起改名，空分组也会同步。"""
    payload, err = parse_json()
    if err:
        return error(err)
    old = (payload.get("old") or "").strip()
    new = (payload.get("new") or "").strip()
    try:
        groups = store.rename_group(old, new)
    except ValueError as exc:
        return error(str(exc))
    log.info("分组重命名：%s -> %s", old, new)
    return jsonify({"groups": groups})


@app.post("/api/groups/order")
def api_reorder_groups():
    """保存分组展示顺序（后台拖动分组标签后调用）。"""
    payload, err = parse_json()
    if err:
        return error(err)
    raw = payload.get("groups")
    if not isinstance(raw, list):
        return error("groups 必须是数组")
    groups = store.reorder_groups([str(g) for g in raw if isinstance(g, str)])
    log.info("调整分组顺序：%s", groups)
    return jsonify({"groups": groups})


# --------------------------------------------------------------------------- #
# 执行：手动运行 / 测试运行 / 历史
# --------------------------------------------------------------------------- #

@app.post("/api/monitors/<int:monitor_id>/run")
def api_run_monitor(monitor_id: int):
    """立即执行一次并落库（trigger=manual），同步等待结果。"""
    monitor = store.get_monitor(monitor_id)
    if not monitor:
        return error("监控项不存在", 404)
    assert scheduler is not None
    result = run_code(monitor["code"], scheduler._safe_params(monitor["params"]),
                      monitor["timeout_seconds"])
    scheduler._record(monitor, result, "manual")
    return jsonify({"result": _client_result(result, friendly=True)})


@app.get("/api/monitors/<int:monitor_id>/runs")
def api_list_runs(monitor_id: int):
    if not store.get_monitor(monitor_id):
        return error("监控项不存在", 404)
    limit = request.args.get("limit", 100)
    try:
        limit = int(limit)
    except ValueError:
        limit = 100
    runs = store.list_runs(monitor_id, limit)
    for run in runs:
        run["ok"] = bool(run["ok"])
        run["slow"] = bool(run.get("slow"))
        run["attempt"] = int(run.get("attempt") or 1)
        run["data"] = json.loads(run["data"]) if run.get("data") else None
    return jsonify({"runs": runs})


@app.post("/api/run_test")
def api_run_test():
    """编辑器里的“测试运行”：直接跑当前编辑器内容，不落库。"""
    payload, err = parse_json()
    if err:
        return error(err)
    if err := validate_params(payload.get("params")):
        return error(err)
    params = payload.get("params")
    if isinstance(params, str):
        params = json.loads(params or "{}")
    result = run_code(payload.get("code") or "", params or {},
                      float(payload.get("timeout_seconds") or 30))
    return jsonify({"result": _client_result(result)})


def _client_result(result: Dict[str, Any], friendly: bool = False) -> Dict[str, Any]:
    message = _admin_message(result["message"]) if friendly else result["message"]
    return {
        "ok": result["ok"],
        "message": message,
        "data": result.get("data"),
        "output": result.get("output") or "",
        "duration_ms": int(result["duration_ms"]),
        "timed_out": result.get("timed_out", False),
    }


# --------------------------------------------------------------------------- #
# 模板 / 设置
# --------------------------------------------------------------------------- #

@app.get("/api/templates")
def api_templates():
    return jsonify({"templates": TEMPLATES})


@app.get("/api/settings")
def api_get_settings():
    return jsonify({"settings": store.get_settings()})


@app.put("/api/settings")
def api_save_settings():
    payload, err = parse_json()
    if err:
        return error(err)
    channels = payload.get("channels")
    if channels is None:
        return error("缺少 channels 字段")
    if not isinstance(channels, list):
        return error("channels 必须是数组")
    for channel in channels:
        if not isinstance(channel, dict) or not channel.get("type"):
            return error("每个推送渠道都需要有 type 字段")
    notify = payload.get("notify") or {}
    if not isinstance(notify, dict):
        return error("notify 必须是对象")
    try:
        cooldown = float(notify.get("cooldown_seconds") or 300)
    except (TypeError, ValueError):
        return error("cooldown_seconds 必须是数字")
    notify["cooldown_seconds"] = max(0.0, cooldown)

    prev = store.get_settings()
    # 网站基础信息（可选传入；未传则保留现值）
    site = payload.get("site")
    if site is None:
        site = prev.get("site") or {}
    if not isinstance(site, dict):
        return error("site 必须是对象")
    defaults_site = {"name": "网页监控平台", "stock_title": "服务器存量看板", "footer": ""}
    clean_site = {}
    for key, default_value in defaults_site.items():
        value = str(site.get(key) or "").strip()[:60]
        # 页脚允许留空；名称类字段留空时回落为默认值
        clean_site[key] = value if key == "footer" else (value or default_value)
    settings = {
        "channels": channels, "notify": notify,
        # 保留分组相关数据（空分组 / 展示顺序 / 图标）与已停用接口列表，
        # 别被通知设置保存覆盖掉
        "groups": prev.get("groups") or [],
        "group_order": prev.get("group_order") or [],
        "group_icons": prev.get("group_icons") or {},
        "all_icon": prev.get("all_icon") or "",
        "site": clean_site,
        "disabled_apis": prev.get("disabled_apis") or [],
    }
    store.save_settings(settings)
    log.info("已保存全局设置（%d 个推送渠道）", len(channels))
    return jsonify({"settings": store.get_settings()})


@app.post("/api/settings/test")
def api_test_settings():
    """用当前已保存（或请求体里临时传入）的渠道配置发一条测试消息。"""
    payload = request.get_json(silent=True) or {}
    channels = payload.get("channels")
    if channels is None:
        channels = store.get_settings().get("channels")
    notifier = Notifier(channels or [])
    if not notifier.channels:
        return error("没有可用的推送渠道")
    title = "【网页监控】测试推送"
    content = "如果你看到这条消息，说明推送渠道配置正常。\n来自：网页监控平台"
    # 带上示例变量上下文：渠道配置了自定义模板时，测试推送按模板渲染，方便核对效果
    # 手动测试的逐渠道结果也写入通知日志
    sent = notifier.send(
        title, content, ctx=sample_notify_context(),
        on_result=lambda kind, ok, detail, cname:
            store.log_notify(None, "手动测试", kind, ok, detail, cname),
    )
    return jsonify({"sent": sent, "failed_count": len(notifier.channels) - len(sent)})


@app.get("/api/notify/log")
def api_notify_log():
    """最近的通知推送日志（含手动测试），按时间倒序。"""
    try:
        limit = int(request.args.get("limit", 50))
    except ValueError:
        limit = 50
    return jsonify({"log": store.list_notify_log(min(max(limit, 1), 500))})


@app.get("/api/visits/stats")
def api_visit_stats():
    """公开看板访问统计：今日/累计 PV·UV、近 14 天逐日明细、最近访问列表。"""
    try:
        days = int(request.args.get("days", 14))
    except ValueError:
        days = 14
    return jsonify(store.visit_stats(min(max(days, 1), 90)))


@app.get("/api/visits/list")
def api_visit_list():
    """访客明细（管理员）：可按关键字搜索 IP / 设备 / 浏览器 / 系统 / 来源。"""
    try:
        limit = int(request.args.get("limit", 500))
    except ValueError:
        limit = 500
    rows = store.list_visits(request.args.get("q") or "", limit)
    return jsonify({"items": rows, "count": len(rows)})


@app.get("/api/clicks/stats")
def api_click_stats():
    """「前往站点」跳转点击统计：今日/累计点击量与人数、点击排行、趋势、最近点击。"""
    try:
        days = int(request.args.get("days", 14))
    except ValueError:
        days = 14
    return jsonify(store.click_stats(min(max(days, 1), 90)))


@app.get("/api/api-stats")
def api_call_stats():
    """接口调用统计：今日/累计请求与错误、按路由聚合（次数/错误/耗时/最近调用）、
    近 N 天趋势、最近 50 条调用明细。auth=public 仅公共接口，auth=admin 仅需登录接口。"""
    try:
        days = int(request.args.get("days", 14))
    except ValueError:
        days = 14
    auth_param = (request.args.get("auth") or "all").lower()
    auth = {"public": 0, "admin": 1}.get(auth_param)   # all/其他 -> None（全部）
    data = store.api_call_stats(min(max(days, 1), 30), auth=auth)
    disabled = set(store.get_settings().get("disabled_apis") or [])
    for ep in data["endpoints"]:
        ep["disabled"] = ep["route"] in disabled
    data["protected"] = sorted(PROTECTED_API_ROUTES)
    data["descriptions"] = API_DESCRIPTIONS
    return jsonify(data)


@app.post("/api/api-manage")
def api_manage_toggle():
    """启用 / 停用某个接口路由：停用后该接口立即返回 503，可随时恢复。
    受保护路由（登录/登出/会话/接口管理/接口统计）不可停用。"""
    payload, err = parse_json()
    if err:
        return error(err)
    route = str(payload.get("route") or "").strip()
    disabled = bool(payload.get("disabled"))
    if route not in _api_routes():
        return error("未知接口路由", 404)
    if route in PROTECTED_API_ROUTES:
        return error("该接口受保护，不能停用", 400)
    settings = store.get_settings()
    disabled_list = list(settings.get("disabled_apis") or [])
    if disabled and route not in disabled_list:
        disabled_list.append(route)
    if not disabled and route in disabled_list:
        disabled_list.remove(route)
    settings["disabled_apis"] = disabled_list
    store.save_settings(settings)
    log.info("接口 %s 已%s", route, "停用" if disabled else "启用")
    return jsonify({"ok": True, "route": route, "disabled": disabled})


def _api_routes() -> set:
    """当前应用注册的所有 /api/* 路由模板。"""
    return {r.rule for r in app.url_map.iter_rules() if r.rule.startswith("/api/")}


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> int:
    global store, scheduler

    parser = argparse.ArgumentParser(description="网页监控平台（参考 huawei_stock_monitor.py）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=8787, help="监听端口，默认 8787")
    parser.add_argument("--db", help="SQLite 数据库路径（默认 web_monitor/data/monitor.db）")
    args = parser.parse_args()

    store = Store(args.db)
    app.config["SECRET_KEY"] = _load_secret_key(Path(store.path).parent)
    # 修正历史接口统计的公共/需登录分类（auth 列加入前的老记录默认值有误）
    try:
        fixed = store.reclassify_api_auth(_api_is_public)
        if fixed:
            log.info("已按路由属性修正 %d 条接口统计记录的公共/需登录分类", fixed)
    except Exception as exc:
        log.warning("修正接口统计分类失败：%s", exc)

    # 首次启动（没有任何账号）自动创建默认管理员 admin / admin123
    if store.ensure_default_admin():
        log.warning("已创建默认管理员账号：%s / %s —— 请登录后立即修改密码！",
                    DEFAULT_ADMIN_USERNAME, DEFAULT_ADMIN_PASSWORD)

    scheduler = Scheduler(store)
    scheduler.start()
    # 监控写入新结果时立即失效公开看板响应缓存（缓存另有 5 秒 TTL 兜底）
    store.on_runtime_change(_invalidate_public_stock_cache)

    log.info("网页监控平台已启动：公开看板 http://%s:%d/ （管理后台 /mianban）",
             args.host, args.port)
    # threaded=True：手动执行/历史查询不被长请求阻塞；调度在独立线程里跑
    app.run(host=args.host, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
