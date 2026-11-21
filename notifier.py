#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页监控平台 · 多渠道消息推送

渠道实现与 huawei_stock_monitor.py 保持一致，方便把原来脚本的 notify.channels
配置原样迁移过来。支持：console / email / wecom / dingtalk / serverchan / bark /
telegram / webhook。

email 渠道（SMTP）配置示例：
    {"type": "email", "smtp_host": "smtp.qq.com", "smtp_port": 465,
     "user": "你的QQ号@qq.com", "password": "SMTP授权码", "to": ["接收邮箱@example.com"]}
    - 465 端口走 SSL（默认），587 端口走 STARTTLS（需 "use_ssl": false）；
    - QQ/163 邮箱的 password 是“SMTP 授权码”，不是登录密码。

telegram 渠道国内直连不通时，可加可选代理：
    {"type": "telegram", "bot_token": "123:abc", "chat_id": "xxx",
     "proxy": "http://127.0.0.1:7890"}   # socks5:// 需 pip install pysocks
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger("web-monitor")

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


# -- 自定义推送模板 ---------------------------------------------------------- #

def render_template(tpl: str, ctx: Dict[str, Any]) -> str:
    """把模板里的 {变量} 替换为上下文取值；未识别的变量原样保留（与前端预览一致）。"""
    out = str(tpl or "")
    for key, value in ctx.items():
        out = out.replace("{" + key + "}", str(value))
    return out


def channel_text(channel: Dict[str, Any], default_title: str,
                 default_content: str, ctx: Dict[str, Any]) -> "tuple[str, str]":
    """按渠道自定义模板渲染推送标题/正文；未自定义（留空）时使用平台默认。
    自定义正文渲染后自动去掉空行。"""
    title_tpl = str(channel.get("title_template") or "").strip()
    content_tpl = str(channel.get("content_template") or "").strip()
    title = render_template(title_tpl, ctx) if title_tpl else default_title
    content = render_template(content_tpl, ctx) if content_tpl else default_content
    if content_tpl:
        content = "\n".join(line for line in content.split("\n") if line.strip())
    return title, content


def sample_notify_context() -> Dict[str, str]:
    """推送模板变量的示例值：供「测试此渠道」渲染自定义模板，与前端实时预览一致。
    中文主键 + 英文别名，两种写法都能用。"""
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    zh = {
        "平台名": "网页监控平台",
        "监控名": "华为云 2核2G 特价机库存",
        "分组": "抢购",
        "状态": "有货提醒",
        "结果": "✅ 有货！剩余 30 台，今日 110/140，进度 78%",
        "库存变化": "剩余：50 → 30",
        "连续失败": "0",
        "耗时": "1514ms",
        "时间": now,
        "厂商": "华为云",
        "地区": "华南-广州",
    }
    en = {
        "site": zh["平台名"], "name": zh["监控名"], "group": zh["分组"],
        "status": zh["状态"], "message": zh["结果"], "qty_change": zh["库存变化"],
        "fail_count": zh["连续失败"], "duration": zh["耗时"], "time": now,
        "vendor": zh["厂商"], "region": zh["地区"],
    }
    out = dict(zh)
    out.update(en)
    return out


class Notifier:
    """按配置的渠道列表逐个推送；单个渠道失败不影响其他渠道。"""

    def __init__(self, channels: List[Dict[str, Any]], timeout: float = 10.0,
                 on_result: Optional[Any] = None) -> None:
        self.channels = [c for c in (channels or [])
                         if isinstance(c, dict) and c.get("type") and not c.get("disabled")]
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_UA})
        # 可选回调 on_result(channel_type, ok, detail)，用于记录通知日志
        self.on_result = on_result

    def send(self, title: str, content: str,
             on_result: Optional[Any] = None, ctx: Optional[Dict[str, Any]] = None) -> List[str]:
        """推送并返回成功的渠道名列表（供“测试推送”接口反馈）。
        on_result(channel_type, ok, detail, channel_name)：本次发送的逐渠道结果回调，
        优先于实例回调；channel_name 为用户自定义的通知名（可能为空）。
        ctx 为模板变量上下文：渠道配置了自定义 title_template / content_template 时，
        按渠道逐个渲染替换；未自定义的渠道仍用传入的默认标题/正文。"""
        sink = on_result or self.on_result
        sent: List[str] = []
        if not self.channels:
            log.warning("没有配置任何推送渠道，消息只写入服务端日志：%s", title)
            return sent
        for channel in self.channels:
            kind = str(channel.get("type", "")).lower()
            cname = str(channel.get("name") or "").strip()
            handler = getattr(self, f"_send_{kind}", None)
            if handler is None:
                log.warning("未知的推送渠道类型：%s", kind)
                self._emit_result(sink, kind, False, f"未知渠道类型 {kind}", cname)
                continue
            try:
                if ctx:
                    ch_title, ch_content = channel_text(channel, title, content, ctx)
                else:
                    ch_title, ch_content = title, content
                handler(channel, ch_title, ch_content)
                sent.append(kind)
                self._emit_result(sink, kind, True, "", cname)
                if kind != "console":
                    log.info("已通过 %s 推送：%s", kind, ch_title)
            except Exception as exc:  # 推送失败不能影响监控主流程
                log.error("%s 推送失败：%s", kind, exc)
                self._emit_result(sink, kind, False, str(exc), cname)
        return sent

    @staticmethod
    def _emit_result(sink: Optional[Any], kind: str, ok: bool,
                     detail: str, cname: str = "") -> None:
        """通知日志回调；回调自身出错也不影响推送主流程。"""
        if not sink:
            return
        try:
            sink(kind, ok, detail, cname)
        except Exception as exc:
            log.warning("通知日志回调失败：%s", exc)

    # -- 各渠道实现 --------------------------------------------------------- #

    def _proxies(self, channel: Dict[str, Any]) -> Dict[str, str]:
        """渠道级可选代理，主要给 telegram 这类国内直连不通的渠道用。"""
        proxy = channel.get("proxy")
        return {"http": proxy, "https": proxy} if proxy else {}

    def _send_console(self, channel: Dict[str, Any], title: str, content: str) -> None:
        border = "=" * 60
        print(f"\a{border}\n{title}\n{content}\n{border}", flush=True)

    def _send_email(self, channel: Dict[str, Any], title: str, content: str) -> None:
        """SMTP 邮件推送（QQ/163 用授权码，465=SSL 默认，587=STARTTLS）。"""
        import smtplib
        from email.header import Header
        from email.mime.text import MIMEText
        from email.utils import formataddr

        host = channel.get("smtp_host")
        user = channel.get("user") or channel.get("username")
        password = channel.get("password") or channel.get("auth_code")
        to = channel.get("to")
        if isinstance(to, str):
            to = [to]
        to = [addr.strip() for addr in (to or []) if addr and addr.strip()]
        if not host or not user or not password or not to:
            raise ValueError("email 缺少 smtp_host / user / password / to 配置")

        port_raw = channel.get("smtp_port")
        # use_ssl 缺省时按端口猜：465=SSL，587/25=非 SSL；587 再默认走 STARTTLS
        use_ssl = bool(channel.get("use_ssl", int(port_raw or 465) == 465))
        port = int(port_raw or (465 if use_ssl else 25))
        use_starttls = bool(channel.get("use_starttls", not use_ssl and port == 587))

        msg = MIMEText(content, "plain", "utf-8")
        display_name = channel.get("from_name", "网页监控平台")
        msg["Subject"] = Header(title, "utf-8")
        msg["From"] = formataddr((str(Header(display_name, "utf-8")), user))
        msg["To"] = ", ".join(to)

        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=self.timeout)
        else:
            server = smtplib.SMTP(host, port, timeout=self.timeout)
            if use_starttls:
                server.starttls()
        try:
            server.login(user, password)
            server.sendmail(user, to, msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass

    def _post_json(self, url: str, payload: Dict[str, Any],
                   proxies: Optional[Dict[str, str]] = None) -> None:
        resp = self.session.post(url, json=payload, timeout=self.timeout, proxies=proxies or None)
        resp.raise_for_status()
        log.debug("%s -> %s %s", url, resp.status_code, resp.text[:200])

    def _send_wecom(self, channel: Dict[str, Any], title: str, content: str) -> None:
        """企业微信群机器人。"""
        url = channel.get("webhook") or f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={channel.get('key', '')}"
        self._post_json(url, {"msgtype": "text", "text": {"content": f"{title}\n{content}"}})

    def _send_dingtalk(self, channel: Dict[str, Any], title: str, content: str) -> None:
        """钉钉群机器人（支持加签）。"""
        url = channel.get("webhook") or "https://oapi.dingtalk.com/robot/send"
        query = {}
        token = channel.get("access_token") or channel.get("token")
        if token:
            query["access_token"] = token
        secret = channel.get("secret")
        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            digest = hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
            query.update({"timestamp": timestamp, "sign": urllib.parse.quote_plus(base64.b64encode(digest))})
        full_url = url + ("?" + urllib.parse.urlencode(query) if query else "")
        self._post_json(full_url, {"msgtype": "text", "text": {"content": f"{title}\n{content}"}})

    def _send_serverchan(self, channel: Dict[str, Any], title: str, content: str) -> None:
        """Server酱（微信推送）。"""
        key = channel.get("sendkey") or channel.get("key")
        if not key:
            raise ValueError("serverchan 缺少 sendkey")
        resp = self.session.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": content.replace("\n", "\n\n")},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def _send_bark(self, channel: Dict[str, Any], title: str, content: str) -> None:
        """Bark（iOS 推送）。"""
        base = (channel.get("url") or f"https://api.day.app/{channel.get('key', '')}").rstrip("/")
        resp = self.session.get(
            base,
            params={"title": title, "body": content, "group": "web-monitor", "level": "timeSensitive"},
            timeout=self.timeout,
        )
        resp.raise_for_status()

    def _send_telegram(self, channel: Dict[str, Any], title: str, content: str) -> None:
        token = channel.get("bot_token") or channel.get("token")
        chat_id = channel.get("chat_id")
        if not token or not chat_id:
            raise ValueError("telegram 缺少 bot_token / chat_id")
        self._post_json(
            f"https://api.telegram.org/bot{token}/sendMessage",
            {"chat_id": chat_id, "text": f"{title}\n{content}"},
            proxies=self._proxies(channel),
        )

    def _send_webhook(self, channel: Dict[str, Any], title: str, content: str) -> None:
        """通用 Webhook：POST JSON，方便接自建服务 / n8n / 飞书自定义机器人。"""
        url = channel.get("url")
        if not url:
            raise ValueError("webhook 缺少 url")
        payload = {"title": title, "content": content, "text": f"{title}\n{content}"}
        headers = {"Content-Type": "application/json; charset=utf-8"}
        headers.update(channel.get("headers") or {})
        resp = self.session.post(
            url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers, timeout=self.timeout,
        )
        resp.raise_for_status()
