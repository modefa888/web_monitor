#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""站点跳转链接加密（/go/<token>）

公开看板上的「前往站点」按钮不直接暴露目标网址，而是携带一个加密 token：
    /go/<token>  ->  服务端解密出目标地址 -> 记一次点击统计 -> 302 跳转

实现说明（仅用标准库，不引入新依赖）：
    采用 HMAC-SHA256 作为流密码的确定性认证加密：
      - nonce  = HMAC(Kn, 明文)[:16]          # 由明文决定，同一地址永远得到同一个 token
      - 密文   = 明文 XOR HMAC(Kc, nonce||计数器) 生成的密钥流
      - tag    = HMAC(Kt, nonce + 密文)[:16]  # 完整性校验，防篡改/防伪造
    解密后重算 nonce 与 tag 做常量时间比对，任一不符即判定 token 无效。
    确定性意味着同一目标地址每次生成的 token 相同：看板每 10 秒轮询刷新
    也不会让链接变来变去，统计按 token 内的地址归组也更稳定。

payload 格式（明文）： "m:<monitor_id>|<url>"   monitor_id=0 表示来源未知
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import urllib.parse
from typing import Optional, Tuple

# 密钥派生的域分隔盐（与 session 密钥复用同一份 secret_key，但派生出独立子密钥）
_SALT_NONCE = b"web-monitor/go-link/nonce"
_SALT_CRYPT = b"web-monitor/go-link/crypt"
_SALT_TAG = b"web-monitor/go-link/tag"

_NONCE_LEN = 16
_TAG_LEN = 16
_BLOCK = 32  # 一块密钥流的字节数（SHA256 输出长度）

# token 最大长度（base64url 后）；对应明文约 1000 字节，足够容纳长活动页链接
MAX_TOKEN_LEN = 1400
MAX_URL_LEN = 900


def _keys(secret: str) -> Tuple[bytes, bytes, bytes]:
    """从 secret_key 派生三个独立子密钥（加域分隔防跨用途复用）。"""
    seed = (secret or "").encode("utf-8", "ignore")
    k_n = hashlib.sha256(_SALT_NONCE + seed).digest()
    k_c = hashlib.sha256(_SALT_CRYPT + seed).digest()
    k_t = hashlib.sha256(_SALT_TAG + seed).digest()
    return k_n, k_c, k_t


def _keystream(k_c: bytes, nonce: bytes, length: int) -> bytes:
    """HMAC-CTR 密钥流：HMAC(Kc, nonce || counter) 逐块拼接。"""
    out = bytearray()
    counter = 0
    while len(out) < length:
        out.extend(hmac.new(k_c, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest())
        counter += 1
    return bytes(out[:length])


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> Optional[bytes]:
    try:
        pad = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError):
        return None


def encode_site_token(secret: str, url: str, monitor_id: int = 0) -> str:
    """把目标地址加密成 /go/<token> 用的 token（同一输入永远得到同一 token）。"""
    url = (url or "").strip()
    if not url:
        raise ValueError("url 为空")
    payload = f"m:{int(monitor_id or 0)}|{url[:MAX_URL_LEN]}".encode("utf-8")
    k_n, k_c, k_t = _keys(secret)
    nonce = hmac.new(k_n, payload, hashlib.sha256).digest()[:_NONCE_LEN]
    stream = _keystream(k_c, nonce, len(payload))
    ciphertext = bytes(a ^ b for a, b in zip(payload, stream))
    tag = hmac.new(k_t, nonce + ciphertext, hashlib.sha256).digest()[:_TAG_LEN]
    return _b64url_encode(nonce + ciphertext + tag)


def decode_site_token(secret: str, token: str) -> Optional[Tuple[int, str]]:
    """解密 token，校验通过返回 (monitor_id, url)，无效返回 None。"""
    raw = _b64url_decode((token or "").strip())
    if not raw or len(raw) < _NONCE_LEN + _TAG_LEN + 1 or len(token) > MAX_TOKEN_LEN:
        return None
    nonce, ciphertext, tag = raw[:_NONCE_LEN], raw[_NONCE_LEN:-_TAG_LEN], raw[-_TAG_LEN:]
    k_n, k_c, k_t = _keys(secret)
    expect_tag = hmac.new(k_t, nonce + ciphertext, hashlib.sha256).digest()[:_TAG_LEN]
    if not hmac.compare_digest(tag, expect_tag):
        return None
    stream = _keystream(k_c, nonce, len(ciphertext))
    try:
        payload = bytes(a ^ b for a, b in zip(ciphertext, stream)).decode("utf-8")
    except UnicodeDecodeError:
        return None
    # 完整性第二道校验：解出的明文必须能重新生成同一个 nonce
    if not hmac.compare_digest(nonce,
                               hmac.new(k_n, payload.encode("utf-8"), hashlib.sha256).digest()[:_NONCE_LEN]):
        return None
    head, sep, url = payload.partition("|")
    if not sep or not head.startswith("m:"):
        return None
    try:
        monitor_id = int(head[2:])
    except ValueError:
        return None
    url = url.strip()
    if not _is_http_url(url):
        return None
    return monitor_id, url


def _is_http_url(url: str) -> bool:
    """只允许 http/https 跳转，防止 token 被构造出 javascript: 等危险协议。"""
    if not url or len(url) > MAX_URL_LEN:
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)
