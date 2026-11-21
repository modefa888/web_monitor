#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页监控平台 · SQLite 存储层

八张表：
    monitors   监控项定义（名称/代码/间隔/通知策略）+ 最近一次运行的状态快照
    runs       每次执行的记录（结果/耗时/输出），每个监控项保留最近 KEEP_RUNS 条
    settings   全局设置（推送渠道、冷却时间等），单行 JSON
    notify_log 通知推送日志
    visit_log  公开看板访问记录（时间/IP/设备/浏览器/系统/来源）
    click_log  「前往站点」跳转点击记录（时间/目标地址/点击者 IP/设备等）
    api_stats  /api/* 接口调用统计（路由/方法/状态码/耗时/IP）
    users      管理员账号（登录后才可配置采集服务）

所有时间统一存 Unix 时间戳（秒，float），由前端负责格式化显示。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from werkzeug.security import check_password_hash, generate_password_hash

KEEP_RUNS = 5000         # 每个监控项保留的历史条数（按间隔 20s 算约 27 小时）
KEEP_SECONDS = 48 * 3600 # 历史最多保留 2 天
KEEP_NOTIFY_LOG = 500    # 通知推送日志最多保留条数
KEEP_VISIT_DAYS = 90     # 访问记录最多保留天数
KEEP_CLICK_DAYS = 90     # 跳转点击记录最多保留天数
KEEP_API_STATS_DAYS = 30 # 接口访问统计最多保留天数
MAX_CLICK_URL = 900      # 点击记录里目标地址的最大长度（与 sitelink.MAX_URL_LEN 一致）
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"   # 首次启动自动创建，请登录后尽快修改
DEFAULT_SETTINGS: Dict[str, Any] = {
    "channels": [{"type": "console"}],   # 与 huawei_stock_monitor.py 同款渠道配置
    "notify": {
        "cooldown_seconds": 300,         # 同一监控项两次推送的最小间隔
    },
    "groups": [],                        # 单独创建的空分组（还没有监控项的分组）
    "group_order": [],                   # 分组展示顺序（分组名数组，可拖动调整）
    "group_icons": {},                   # 分组图标映射 {分组名: emoji}，未设置的分组前端回落为默认图标
    "all_icon": "",                      # 「全部」标签的图标，空=前端默认 🗂
    "site": {                            # 网站基础信息（后台「网站设置」可改）
        "name": "网页监控平台",           # 后台标题
        "stock_title": "服务器存量看板",  # 公开看板标题
        "footer": "",                    # 看板页脚文字（可空）
    },
    "disabled_apis": [],                 # 已停用的接口路由模板（/api/api-manage 管理）
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    name             TEXT NOT NULL,
    group_name       TEXT NOT NULL DEFAULT '',      -- 监控项分组（空=未分组）
    code             TEXT NOT NULL DEFAULT '',
    interval_seconds REAL NOT NULL DEFAULT 30,
    timeout_seconds  REAL NOT NULL DEFAULT 30,
    enabled          INTEGER NOT NULL DEFAULT 1,
    params           TEXT NOT NULL DEFAULT '{}',   -- 注入到监控代码 PARAMS 变量的 JSON
    notify_on        TEXT NOT NULL DEFAULT 'change',  -- never / fail / change
    retry_count      INTEGER NOT NULL DEFAULT 0,   -- 失败自动重试次数（0=不重试）
    retry_interval   REAL NOT NULL DEFAULT 5,      -- 重试间隔（秒）
    slow_threshold_ms REAL NOT NULL DEFAULT 0,     -- 执行耗时告警阈值（毫秒，0=不启用）
    active_windows   TEXT NOT NULL DEFAULT '',     -- 定时监控时段 JSON 数组，空=全天
    vendor           TEXT NOT NULL DEFAULT '',     -- 厂商名（如 腾讯云 / 阿里云）
    region           TEXT NOT NULL DEFAULT '',     -- 地区（如 上海 / 北京 / 广州）
    price_type       TEXT NOT NULL DEFAULT '',     -- 价格类型：new_user / renew / normal
    purchase_limit   INTEGER NOT NULL DEFAULT 0,   -- 限购数量（0=不限）
    realname         TEXT NOT NULL DEFAULT '',     -- 是否需实名：yes / no，空=未设置
    sort_order       INTEGER NOT NULL DEFAULT 0,   -- 组内展示顺序（越小越靠前）
    last_ok          INTEGER,                      -- NULL=从未运行 1=成功 0=失败
    last_message     TEXT DEFAULT '',
    last_run_at      REAL,
    last_data        TEXT,                         -- 最近一次 emit 的 data JSON
    fail_count       INTEGER NOT NULL DEFAULT 0,   -- 连续失败次数
    last_notify_at   REAL NOT NULL DEFAULT 0,
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id  INTEGER NOT NULL,
    started_at  REAL NOT NULL,
    duration_ms REAL NOT NULL DEFAULT 0,
    ok          INTEGER NOT NULL DEFAULT 0,
    message     TEXT DEFAULT '',
    output      TEXT DEFAULT '',                  -- stdout+stderr 尾部，用于排错
    data        TEXT,                             -- emit 的 data JSON
    trigger     TEXT NOT NULL DEFAULT 'schedule', -- schedule / manual
    slow        INTEGER NOT NULL DEFAULT 0,       -- 耗时是否超过告警阈值
    attempt     INTEGER NOT NULL DEFAULT 1        -- 第几次尝试（1=首次，>1=重试）
);
CREATE INDEX IF NOT EXISTS idx_runs_monitor ON runs(monitor_id, id DESC);
CREATE TABLE IF NOT EXISTS settings (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notify_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,                   -- 推送时间（epoch 秒）
    monitor_id   INTEGER,                         -- 关联监控项（手动测试为空）
    monitor_name TEXT DEFAULT '',                 -- 监控项名称（手动测试为「手动测试」）
    channel      TEXT NOT NULL,                   -- 渠道类型 console/email/wecom/...
    channel_name TEXT DEFAULT '',                 -- 渠道通知名（用户自定义，用于区分同类多渠道）
    ok           INTEGER NOT NULL DEFAULT 0,      -- 1=成功 0=失败
    detail       TEXT DEFAULT ''                  -- 失败原因等补充信息
);
CREATE INDEX IF NOT EXISTS idx_notify_log_ts ON notify_log(id DESC);
CREATE TABLE IF NOT EXISTS visit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,                 -- 访问时间（epoch 秒）
    path       TEXT NOT NULL,                 -- 访问路径（目前只有公开首页 /）
    visitor    TEXT NOT NULL,                 -- 访客匿名标识（ip+UA 的 sha256 前 12 位）
    day        TEXT NOT NULL,                 -- 北京时间日期 YYYY-MM-DD，用于按天聚合
    referer    TEXT DEFAULT '',               -- 来源页（可空）
    ip         TEXT DEFAULT '',               -- 访问者 IP（含代理头）
    device     TEXT DEFAULT '',               -- 设备类型/型号（如 手机 · iPhone）
    browser    TEXT DEFAULT '',               -- 浏览器及版本（如 Chrome 131.0）
    os         TEXT DEFAULT '',               -- 操作系统及版本（如 Android 13）
    user_agent TEXT DEFAULT ''                -- 原始 User-Agent，便于核对
);
CREATE INDEX IF NOT EXISTS idx_visit_day ON visit_log(day);
CREATE TABLE IF NOT EXISTS click_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,                 -- 点击时间（epoch 秒）
    day          TEXT NOT NULL,                 -- 北京时间日期 YYYY-MM-DD，用于按天聚合
    monitor_id   INTEGER,                       -- 关联监控项（由跳转 token 解出，可能已删除）
    monitor_name TEXT DEFAULT '',               -- 监控项名称快照（删除后仍可读）
    url          TEXT DEFAULT '',               -- 跳转目标完整地址
    visitor      TEXT DEFAULT '',               -- 点击者匿名标识（ip+UA 的 sha256 前 12 位）
    ip           TEXT DEFAULT '',               -- 点击者 IP（含代理头）
    device       TEXT DEFAULT '',               -- 设备类型/型号
    browser      TEXT DEFAULT '',               -- 浏览器及版本
    os           TEXT DEFAULT '',               -- 操作系统及版本
    referer      TEXT DEFAULT ''                -- 来源页（可空）
);
CREATE INDEX IF NOT EXISTS idx_click_day ON click_log(day);
CREATE TABLE IF NOT EXISTS api_stats (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,                    -- 请求时间（epoch 秒）
    day     TEXT NOT NULL,                    -- 北京时间日期 YYYY-MM-DD
    method  TEXT NOT NULL,                    -- HTTP 方法（GET/POST/...）
    route   TEXT NOT NULL,                    -- 接口路由模板（如 /api/monitors/<id>）
    status  INTEGER NOT NULL,                 -- 响应状态码
    dur_ms  REAL NOT NULL,                    -- 处理耗时（毫秒）
    ip      TEXT DEFAULT '',                  -- 调用方 IP（含代理头）
    auth    INTEGER NOT NULL DEFAULT 1        -- 0=公共接口（无需登录） 1=需登录
);
CREATE INDEX IF NOT EXISTS idx_api_route ON api_stats(route);
CREATE INDEX IF NOT EXISTS idx_api_day ON api_stats(day);
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
"""

MONITOR_FIELDS = {
    "name", "group_name", "code", "interval_seconds", "timeout_seconds",
    "enabled", "params", "notify_on", "retry_count", "retry_interval",
    "slow_threshold_ms", "active_windows", "vendor", "region", "price_type",
    "purchase_limit", "realname",
}


class Store:
    """线程安全的 SQLite 封装：内部用一把锁串行化所有读写。"""

    def __init__(self, db_path: Optional[str] = None) -> None:
        path = Path(db_path) if db_path else Path(__file__).with_name("data") / "monitor.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)       # 供 app 层派生 secret_key 等同目录文件
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL 模式：写不再阻塞读，高并发轮询时接口统计的频繁提交开销更低
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._settings_cache: Optional[Dict[str, Any]] = None      # 设置内存缓存
        self._api_stats_buffer: List[tuple] = []                   # 接口统计写入缓冲
        self._last_api_flush = 0.0
        self._runtime_listeners: List[Any] = []                    # 运行数据变更回调
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def on_runtime_change(self, fn: Any) -> None:
        """注册运行数据变更回调（监控项写入新结果时触发，供响应缓存失效用）。
        回调必须非阻塞；异常会被吞掉，不影响主流程。"""
        self._runtime_listeners.append(fn)

    def _notify_runtime_change(self) -> None:
        for fn in list(self._runtime_listeners):
            try:
                fn()
            except Exception:
                pass

    def _migrate(self) -> None:
        """轻量迁移：给旧库补齐新增的字段（ALTER TABLE ADD COLUMN）。"""
        monitor_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(monitors)").fetchall()}
        added_sort_order = "sort_order" not in monitor_cols
        for name, decl in {
            "group_name": "TEXT NOT NULL DEFAULT ''",
            "retry_count": "INTEGER NOT NULL DEFAULT 0",
            "retry_interval": "REAL NOT NULL DEFAULT 5",
            "slow_threshold_ms": "REAL NOT NULL DEFAULT 0",
            "active_windows": "TEXT NOT NULL DEFAULT ''",
            "vendor": "TEXT NOT NULL DEFAULT ''",
            "region": "TEXT NOT NULL DEFAULT ''",
            "price_type": "TEXT NOT NULL DEFAULT ''",
            "purchase_limit": "INTEGER NOT NULL DEFAULT 0",
            "realname": "TEXT NOT NULL DEFAULT ''",
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
        }.items():
            if name not in monitor_cols:
                self._conn.execute(f"ALTER TABLE monitors ADD COLUMN {name} {decl}")
        if added_sort_order:
            # 旧库首次加排序列：按原 id 顺序初始化，保持现有展示不变
            self._conn.execute("UPDATE monitors SET sort_order = id")
        runs_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()}
        for name, decl in {
            "slow": "INTEGER NOT NULL DEFAULT 0",
            "attempt": "INTEGER NOT NULL DEFAULT 1",
        }.items():
            if name not in runs_cols:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")
        # notify_log 是新表；若更早版本已建过（无 channel_name 列），补齐
        if "notify_log" in {row["name"] for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
            notify_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(notify_log)").fetchall()}
            if "channel_name" not in notify_cols:
                self._conn.execute("ALTER TABLE notify_log ADD COLUMN channel_name TEXT DEFAULT ''")
        # 旧库的 visit_log 只存匿名标识；补齐 IP / 设备 / 浏览器等字段
        visit_cols = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(visit_log)").fetchall()}
        for name, decl in {
            "ip": "TEXT DEFAULT ''",
            "device": "TEXT DEFAULT ''",
            "browser": "TEXT DEFAULT ''",
            "os": "TEXT DEFAULT ''",
            "user_agent": "TEXT DEFAULT ''",
        }.items():
            if name not in visit_cols:
                self._conn.execute(f"ALTER TABLE visit_log ADD COLUMN {name} {decl}")
        # api_stats 补 auth 列（0=公共接口 1=需登录）
        api_cols = {row["name"] for row in self._conn.execute(
            "PRAGMA table_info(api_stats)").fetchall()}
        if "auth" not in api_cols:
            self._conn.execute("ALTER TABLE api_stats ADD COLUMN auth INTEGER NOT NULL DEFAULT 1")

    # ------------------------------------------------------------------ #
    # 监控项
    # ------------------------------------------------------------------ #

    def list_monitors(self, enabled_only: bool = False) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM monitors"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY group_name, sort_order, id"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [dict(row) for row in rows]

    def list_groups(self) -> List[str]:
        """返回所有分组：监控项里出现的 + 单独创建的空分组；按保存的展示顺序排列。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT group_name FROM monitors WHERE group_name != '' ORDER BY group_name"
            ).fetchall()
        names = {row["group_name"] for row in rows}
        settings = self.get_settings()
        for name in settings.get("groups") or []:
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
        ordered = [g for g in settings.get("group_order") or []
                   if isinstance(g, str) and g in names]
        rest = sorted(names - set(ordered), key=lambda x: (x == "", x.lower()))
        return ordered + rest

    def group_icons(self) -> Dict[str, str]:
        """分组图标映射 {分组名: emoji}。"""
        raw = self.get_settings().get("group_icons") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()
                if isinstance(v, str) and v.strip()}

    def set_group_icon(self, name: str, icon: str) -> Dict[str, str]:
        """设置分组图标；icon 传空则清除自定义图标（前端回落为默认图标）。"""
        name = (name or "").strip()
        if not name:
            raise ValueError("分组名称不能为空")
        if name not in self.list_groups():
            raise ValueError(f"分组「{name}」不存在")
        if len(icon) > 16:
            raise ValueError("图标请直接粘贴一个 emoji")
        settings = self.get_settings()
        icons = self.group_icons()
        if (icon or "").strip():
            icons[name] = icon.strip()
        else:
            icons.pop(name, None)
        settings["group_icons"] = icons
        self.save_settings(settings)
        return icons

    def all_icon(self) -> str:
        """「全部」标签的自定义图标，未设置返回空串（前端回落为默认 🗂）。"""
        v = self.get_settings().get("all_icon")
        return v.strip() if isinstance(v, str) else ""

    def set_all_icon(self, icon: str) -> str:
        """设置「全部」标签图标；icon 传空则清除自定义图标。"""
        if len(icon) > 16:
            raise ValueError("图标请直接粘贴一个 emoji")
        settings = self.get_settings()
        settings["all_icon"] = (icon or "").strip()
        self.save_settings(settings)
        return settings["all_icon"]

    def reorder_groups(self, names: List[str]) -> List[str]:
        """保存分组的展示顺序（后台拖动分组标签后调用）。"""
        existing = self.list_groups()
        cleaned: List[str] = []
        for name in names or []:
            if isinstance(name, str) and name in existing and name not in cleaned:
                cleaned.append(name)
        cleaned += [g for g in existing if g not in cleaned]
        settings = self.get_settings()
        settings["group_order"] = cleaned
        self.save_settings(settings)
        return self.list_groups()

    def add_group(self, name: str) -> List[str]:
        """新增一个空分组（持久化在 settings.groups，与监控项无关）。"""
        name = (name or "").strip()
        if not name:
            return self.list_groups()
        settings = self.get_settings()
        groups = [g for g in (settings.get("groups") or []) if isinstance(g, str)]
        if name not in groups:
            groups.append(name)
            settings["groups"] = groups
            order = [g for g in (settings.get("group_order") or []) if isinstance(g, str)]
            if name not in order:
                order.append(name)
            settings["group_order"] = order
            self.save_settings(settings)
        return self.list_groups()

    def rename_group(self, old: str, new: str) -> List[str]:
        """重命名分组：同步更新所有监控项的 group_name 与单独创建的空分组。"""
        old = (old or "").strip()
        new = (new or "").strip()
        if not old:
            raise ValueError("「未分组」不支持重命名")
        if not new:
            raise ValueError("分组名称不能为空")
        if len(new) > 30:
            raise ValueError("分组名称最多 30 个字")
        if old == new:
            return self.list_groups()
        if old not in self.list_groups():
            raise ValueError(f"分组「{old}」不存在")
        if new in self.list_groups():
            raise ValueError(f"分组「{new}」已存在")
        with self._lock:
            self._conn.execute(
                "UPDATE monitors SET group_name = ?, updated_at = ? WHERE group_name = ?",
                (new, time.time(), old),
            )
            self._conn.commit()
        settings = self.get_settings()
        groups = [g for g in (settings.get("groups") or []) if isinstance(g, str)]
        changed = False
        if old in groups:
            settings["groups"] = [new if g == old else g for g in groups]
            changed = True
        order = [g for g in (settings.get("group_order") or []) if isinstance(g, str)]
        if old in order:
            settings["group_order"] = [new if g == old else g for g in order]
            changed = True
        icons = self.group_icons()
        if old in icons:
            icons[new] = icons.pop(old)
            settings["group_icons"] = icons
            changed = True
        if changed:
            self.save_settings(settings)
        return self.list_groups()

    def get_monitor(self, monitor_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM monitors WHERE id = ?", (monitor_id,)
            ).fetchone()
        return dict(row) if row else None

    def create_monitor(self, fields: Dict[str, Any]) -> int:
        now = time.time()
        # 显式传 0 的重试间隔应保持 0（立即重试），只在未提供时取默认 5
        retry_interval = fields.get("retry_interval")
        retry_interval = 5 if retry_interval is None else max(0.0, float(retry_interval))
        group_name = (fields.get("group_name") or "").strip()
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM monitors WHERE group_name = ?",
                (group_name,),
            ).fetchone()
            sort_order = int(row[0]) + 1
            cursor = self._conn.execute(
                """INSERT INTO monitors
                   (name, group_name, code, interval_seconds, timeout_seconds, enabled,
                    params, notify_on, retry_count, retry_interval, slow_threshold_ms,
                    active_windows, vendor, region, price_type, purchase_limit, realname,
                    sort_order, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fields.get("name") or "未命名监控",
                    group_name,
                    fields.get("code") or "",
                    max(3.0, float(fields.get("interval_seconds") or 30)),
                    max(1.0, float(fields.get("timeout_seconds") or 30)),
                    1 if fields.get("enabled", True) else 0,
                    fields.get("params") or "{}",
                    fields.get("notify_on") or "change",
                    max(0, int(fields.get("retry_count") or 0)),
                    retry_interval,
                    max(0.0, float(fields.get("slow_threshold_ms") or 0)),
                    fields.get("active_windows") or "",
                    (fields.get("vendor") or "").strip(),
                    (fields.get("region") or "").strip(),
                    (fields.get("price_type") or "").strip(),
                    max(0, int(fields.get("purchase_limit") or 0)),
                    (fields.get("realname") or "").strip(),
                    sort_order,
                    now, now,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def update_monitor(self, monitor_id: int, fields: Dict[str, Any]) -> None:
        """只允许更新定义类字段；运行状态走 set_runtime。"""
        updates = {k: v for k, v in fields.items() if k in MONITOR_FIELDS}
        if not updates:
            return
        if "interval_seconds" in updates:
            updates["interval_seconds"] = max(3.0, float(updates["interval_seconds"] or 30))
        if "timeout_seconds" in updates:
            updates["timeout_seconds"] = max(1.0, float(updates["timeout_seconds"] or 30))
        if "enabled" in updates:
            updates["enabled"] = 1 if updates["enabled"] else 0
        if "group_name" in updates:
            updates["group_name"] = (updates["group_name"] or "").strip()
        for key in ("vendor", "region", "price_type", "realname"):
            if key in updates:
                updates[key] = (updates[key] or "").strip()
        if "purchase_limit" in updates:
            updates["purchase_limit"] = max(0, int(updates["purchase_limit"] or 0))
        if "retry_count" in updates:
            updates["retry_count"] = max(0, int(updates["retry_count"] or 0))
        if "retry_interval" in updates:
            ri = updates["retry_interval"]
            updates["retry_interval"] = 5 if ri is None else max(0.0, float(ri))
        if "slow_threshold_ms" in updates:
            updates["slow_threshold_ms"] = max(0.0, float(updates["slow_threshold_ms"] or 0))
        updates["updated_at"] = time.time()
        with self._lock:
            # 改分组后放到新分组末尾，避免沿用旧组顺序
            if "group_name" in updates:
                row = self._conn.execute(
                    "SELECT group_name FROM monitors WHERE id = ?", (monitor_id,)
                ).fetchone()
                if row and (row["group_name"] or "") != updates["group_name"]:
                    max_row = self._conn.execute(
                        "SELECT COALESCE(MAX(sort_order), 0) FROM monitors WHERE group_name = ?",
                        (updates["group_name"],),
                    ).fetchone()
                    updates["sort_order"] = int(max_row[0]) + 1
            keys = ", ".join(f"{k} = ?" for k in updates)
            self._conn.execute(
                f"UPDATE monitors SET {keys} WHERE id = ?",
                (*updates.values(), monitor_id),
            )
            self._conn.commit()

    def reorder_monitors(self, monitor_ids: List[int]) -> None:
        """按传入顺序写入组内排序（前端拖动排序后调用）。"""
        now = time.time()
        with self._lock:
            for idx, mid in enumerate(monitor_ids):
                self._conn.execute(
                    "UPDATE monitors SET sort_order = ?, updated_at = ? WHERE id = ?",
                    (idx, now, mid),
                )
            self._conn.commit()

    def delete_monitor(self, monitor_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))
            self._conn.execute("DELETE FROM runs WHERE monitor_id = ?", (monitor_id,))
            self._conn.commit()

    def set_runtime(self, monitor_id: int, **fields: Any) -> None:
        """写入最近一次运行的状态快照（last_ok / last_message / last_notify_at ...）。
        写入成功后触发运行数据变更回调（供公开看板响应缓存失效）。"""
        allowed = {"last_ok", "last_message", "last_run_at", "last_data",
                   "fail_count", "last_notify_at"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        keys = ", ".join(f"{k} = ?" for k in updates)
        with self._lock:
            self._conn.execute(
                f"UPDATE monitors SET {keys} WHERE id = ?",
                (*updates.values(), monitor_id),
            )
            self._conn.commit()
        self._notify_runtime_change()

    # ------------------------------------------------------------------ #
    # 执行历史
    # ------------------------------------------------------------------ #

    def add_run(self, monitor_id: int, started_at: float, duration_ms: float,
                ok: bool, message: str, output: str,
                data: Optional[str], trigger: str,
                slow: bool = False, attempt: int = 1) -> int:
        with self._lock:
            cursor = self._conn.execute(
                """INSERT INTO runs
                   (monitor_id, started_at, duration_ms, ok, message, output, data, trigger,
                    slow, attempt)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (monitor_id, started_at, duration_ms, 1 if ok else 0,
                 message[:500], output[-8000:], data, trigger,
                 1 if slow else 0, max(1, int(attempt))),
            )
            # 只保留最近 KEEP_RUNS 条历史
            self._conn.execute(
                """DELETE FROM runs WHERE monitor_id = ? AND id NOT IN
                   (SELECT id FROM runs WHERE monitor_id = ? ORDER BY id DESC LIMIT ?)""",
                (monitor_id, monitor_id, KEEP_RUNS),
            )
            # 再按时间清理，避免长期高频任务把库撑大
            self._conn.execute(
                "DELETE FROM runs WHERE monitor_id = ? AND started_at < ?",
                (monitor_id, started_at - KEEP_SECONDS),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def list_runs(self, monitor_id: int, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE monitor_id = ? ORDER BY id DESC LIMIT ?",
                (monitor_id, max(1, min(int(limit), KEEP_RUNS))),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_last_run(self, monitor_id: int) -> Optional[Dict[str, Any]]:
        """最近一次执行记录（用于判断慢执行状态是否翻转等）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE monitor_id = ? ORDER BY id DESC LIMIT 1",
                (monitor_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_last_ok_run(self, monitor_id: int) -> Optional[Dict[str, Any]]:
        """最近一次成功的执行记录（供公开看板在技术异常容忍期内回落展示）。"""
        with self._lock:
            row = self._conn.execute(
                """SELECT started_at, message, data FROM runs
                   WHERE monitor_id = ? AND ok = 1
                   ORDER BY id DESC LIMIT 1""",
                (monitor_id,),
            ).fetchone()
        return dict(row) if row else None

    def last_slow_by_monitor(self) -> Dict[int, bool]:
        """每个监控项最近一条记录的 slow 标记（列表页一次查询批量取）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT monitor_id, slow FROM runs WHERE id IN
                   (SELECT MAX(id) FROM runs GROUP BY monitor_id)"""
            ).fetchall()
        return {row["monitor_id"]: bool(row["slow"]) for row in rows}

    def stats_by_monitor(self) -> Dict[int, Dict[str, Any]]:
        """按监控项聚合历史：总次数/成功次数/平均耗时/最近运行时间。
        只统计当天（北京时间 0 点起）的执行，有货率/成功率随每日重置。"""
        day_start = (datetime.now(timezone(timedelta(hours=8)))
                     .replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        with self._lock:
            rows = self._conn.execute(
                """SELECT monitor_id,
                          COUNT(*)              AS total,
                          COALESCE(SUM(ok), 0)  AS ok_count,
                          AVG(duration_ms)      AS avg_ms,
                          MAX(started_at)       AS last_run
                   FROM runs WHERE started_at >= ?
                   GROUP BY monitor_id""",
                (day_start,),
            ).fetchall()
        return {row["monitor_id"]: dict(row) for row in rows}

    # ------------------------------------------------------------------ #
    # 通知推送日志
    # ------------------------------------------------------------------ #

    def log_notify(self, monitor_id: Optional[int], monitor_name: str,
                   channel: str, ok: bool, detail: str = "",
                   channel_name: str = "") -> None:
        """记录一次推送结果；超过 KEEP_NOTIFY_LOG 条时丢弃最旧的。"""
        with self._lock:
            self._conn.execute(
                """INSERT INTO notify_log (ts, monitor_id, monitor_name, channel, channel_name, ok, detail)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (time.time(), monitor_id, (monitor_name or "").strip(),
                 channel, (channel_name or "").strip(), 1 if ok else 0, (detail or "").strip()),
            )
            self._conn.execute(
                """DELETE FROM notify_log WHERE id NOT IN
                   (SELECT id FROM notify_log ORDER BY id DESC LIMIT ?)""",
                (KEEP_NOTIFY_LOG,),
            )
            self._conn.commit()

    def list_notify_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """最近的通知推送日志（按时间倒序）。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id, ts, monitor_id, monitor_name, channel, channel_name, ok, detail
                   FROM notify_log ORDER BY id DESC LIMIT ?""",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ #
    # 公开看板访问统计
    # ------------------------------------------------------------------ #

    @staticmethod
    def _bj_day(ts: Optional[float] = None) -> str:
        """北京时间日期字符串 YYYY-MM-DD（统计口径统一按北京时区）。"""
        tz = timezone(timedelta(hours=8))
        return datetime.fromtimestamp(ts if ts is not None else time.time(), tz).strftime("%Y-%m-%d")

    def site_info(self) -> Dict[str, str]:
        """网站基础信息（名称 / 看板标题 / 页脚），空值回落为默认。"""
        raw = self.get_settings().get("site") or {}
        if not isinstance(raw, dict):
            raw = {}
        defaults = json.loads(json.dumps(DEFAULT_SETTINGS["site"], ensure_ascii=False))
        out = {}
        for key, default_value in defaults.items():
            value = raw.get(key)
            out[key] = str(value).strip() if isinstance(value, str) and value.strip() else default_value
        return out

    def log_visit(self, path: str, visitor: str, referer: str = "", *,
                  ip: str = "", device: str = "", browser: str = "",
                  os_name: str = "", user_agent: str = "") -> None:
        """记录一次公开页访问（IP/设备/浏览器等）；顺带清理超过保留天数的旧记录。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO visit_log
                   (ts, path, visitor, day, referer, ip, device, browser, os, user_agent)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, path, visitor, self._bj_day(now), (referer or "").strip()[:200],
                 (ip or "").strip()[:64], (device or "").strip()[:80],
                 (browser or "").strip()[:60], (os_name or "").strip()[:60],
                 (user_agent or "").strip()[:500]),
            )
            self._conn.execute(
                "DELETE FROM visit_log WHERE ts < ?",
                (now - KEEP_VISIT_DAYS * 86400,),
            )
            self._conn.commit()

    def visit_stats(self, days: int = 14) -> Dict[str, Any]:
        """访问统计：PV/UV/IP、设备浏览器分布、近 N 天逐日明细与最近访问。"""
        tz = timezone(timedelta(hours=8))
        today = self._bj_day()
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS pv,
                          COUNT(DISTINCT visitor) AS uv,
                          COUNT(DISTINCT ip) AS ips,
                          COUNT(DISTINCT device) AS devices
                   FROM visit_log"""
            ).fetchone()
            total_pv, total_uv = row["pv"], row["uv"]
            total_ip, total_devices = row["ips"], row["devices"]
            row = self._conn.execute(
                """SELECT COUNT(*) AS pv,
                          COUNT(DISTINCT visitor) AS uv,
                          COUNT(DISTINCT ip) AS ips,
                          SUM(CASE WHEN device LIKE '手机%' OR device LIKE '平板%'
                                   THEN 1 ELSE 0 END) AS mobile
                   FROM visit_log WHERE day = ?""",
                (today,),
            ).fetchone()
            today_pv, today_uv = row["pv"], row["uv"]
            today_ip, today_mobile = row["ips"] or 0, row["mobile"] or 0
            per_day = {r["day"]: (r["pv"], r["uv"]) for r in self._conn.execute(
                """SELECT day,
                          COUNT(*) AS pv,
                          COUNT(DISTINCT visitor) AS uv
                   FROM visit_log GROUP BY day"""
            ).fetchall()}
            recent = [dict(r) for r in self._conn.execute(
                """SELECT ts, path, visitor, day, referer, ip, device, browser, os, user_agent
                   FROM visit_log ORDER BY id DESC LIMIT 50"""
            ).fetchall()]

            def top_values(column: str) -> List[Dict[str, Any]]:
                rows = self._conn.execute(
                    f"""SELECT {column} AS name, COUNT(*) AS count
                        FROM visit_log
                        WHERE {column} NOT IN ('', '未知系统', '未知浏览器', '其他设备')
                        GROUP BY {column}
                        ORDER BY count DESC, name LIMIT 4"""
                ).fetchall()
                return [{"name": r["name"], "count": r["count"]} for r in rows]

            device_stats = top_values("device")
            browser_stats = top_values("browser")
            os_stats = top_values("os")
        # 补齐最近 N 天（无访问的天也输出，前端画趋势不断档）
        daily = []
        base = datetime.fromtimestamp(time.time(), tz).date()
        for offset in range(days - 1, -1, -1):
            day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
            pv, uv = per_day.get(day, (0, 0))
            daily.append({"day": day, "pv": pv, "uv": uv, "today": day == today})
        return {
            "today_pv": today_pv, "today_uv": today_uv,
            "today_ip": today_ip, "today_mobile": today_mobile,
            "total_pv": total_pv, "total_uv": total_uv,
            "total_ip": total_ip, "total_devices": total_devices,
            "device_stats": device_stats, "browser_stats": browser_stats,
            "os_stats": os_stats,
            "daily": daily, "recent": recent,
        }

    def list_visits(self, query: str = "", limit: int = 200) -> List[Dict[str, Any]]:
        """访客明细：支持按 IP/设备/浏览器/系统/来源/UA 关键字搜索，倒序返回。"""
        keyword = (query or "").strip()
        sql = """SELECT id, ts, path, visitor, day, referer, ip, device, browser, os, user_agent
                 FROM visit_log"""
        params: List[Any] = []
        if keyword:
            like = f"%{keyword}%"
            sql += """ WHERE ip LIKE ? OR device LIKE ? OR browser LIKE ?
                       OR os LIKE ? OR referer LIKE ? OR visitor LIKE ? OR user_agent LIKE ?"""
            params.extend([like] * 7)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 1000))
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def log_click(self, url: str, monitor_id: Optional[int], monitor_name: str, *,
                  visitor: str = "", ip: str = "", device: str = "",
                  browser: str = "", os_name: str = "", referer: str = "") -> None:
        """记录一次「前往站点」跳转点击；顺带清理超过保留天数的旧记录。"""
        now = time.time()
        with self._lock:
            self._conn.execute(
                """INSERT INTO click_log
                   (ts, day, monitor_id, monitor_name, url, visitor, ip, device, browser, os, referer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (now, self._bj_day(now), monitor_id, (monitor_name or "").strip()[:80],
                 (url or "").strip()[:MAX_CLICK_URL], (visitor or "").strip()[:24],
                 (ip or "").strip()[:64], (device or "").strip()[:80],
                 (browser or "").strip()[:60], (os_name or "").strip()[:60],
                 (referer or "").strip()[:200]),
            )
            self._conn.execute("DELETE FROM click_log WHERE ts < ?",
                               (now - KEEP_CLICK_DAYS * 86400,))
            self._conn.commit()

    def click_stats(self, days: int = 14) -> Dict[str, Any]:
        """跳转点击统计：今日/累计点击量与人数、点击排行、近 N 天趋势、最近点击。"""
        tz = timezone(timedelta(hours=8))
        today = self._bj_day()
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS clicks, COUNT(DISTINCT visitor) AS visitors,
                          COUNT(DISTINCT url) AS urls
                   FROM click_log"""
            ).fetchone()
            total_clicks, total_visitors, total_urls = row["clicks"], row["visitors"], row["urls"]
            row = self._conn.execute(
                """SELECT COUNT(*) AS clicks, COUNT(DISTINCT visitor) AS visitors
                   FROM click_log WHERE day = ?""",
                (today,),
            ).fetchone()
            today_clicks, today_visitors = row["clicks"], row["visitors"]
            top = [dict(r) for r in self._conn.execute(
                """SELECT monitor_id, monitor_name, url,
                          COUNT(*) AS clicks, COUNT(DISTINCT visitor) AS visitors
                   FROM click_log
                   GROUP BY COALESCE(monitor_id, -rowid), url
                   ORDER BY clicks DESC LIMIT 8"""
            ).fetchall()]
            per_day = {r["day"]: (r["clicks"], r["visitors"]) for r in self._conn.execute(
                """SELECT day, COUNT(*) AS clicks, COUNT(DISTINCT visitor) AS visitors
                   FROM click_log GROUP BY day"""
            ).fetchall()}
            recent = [dict(r) for r in self._conn.execute(
                """SELECT ts, monitor_id, monitor_name, url, visitor, ip, device, browser, os, referer
                   FROM click_log ORDER BY id DESC LIMIT 50"""
            ).fetchall()]
        # 补齐最近 N 天（无点击的天也输出，前端画趋势不断档）
        daily = []
        base = datetime.fromtimestamp(time.time(), tz).date()
        for offset in range(days - 1, -1, -1):
            day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
            clicks, visitors = per_day.get(day, (0, 0))
            daily.append({"day": day, "clicks": clicks, "visitors": visitors, "today": day == today})
        return {
            "today_clicks": today_clicks, "today_visitors": today_visitors,
            "total_clicks": total_clicks, "total_visitors": total_visitors,
            "total_urls": total_urls,
            "top": top, "daily": daily, "recent": recent,
        }

    def reclassify_api_auth(self, is_public) -> int:
        """按路由的公共/需登录属性重写 api_stats.auth。

        auth 列是后来加的：加列前的历史记录被迁移默认成 1（需登录），
        导致公共接口（如 /api/public/stock）的老数据跑到「需登录」分组里。
        路由的公共属性是确定的，启动时按路由整体重算一遍即可自愈。
        is_public(route) -> bool 由调用方传入；返回修正的行数。"""
        fixed = 0
        with self._lock:
            routes = [r["route"] for r in self._conn.execute(
                "SELECT DISTINCT route FROM api_stats").fetchall()]
            for route in routes:
                auth = 0 if is_public(route) else 1
                cur = self._conn.execute(
                    "UPDATE api_stats SET auth = ? WHERE route = ? AND auth != ?",
                    (auth, route, auth))
                fixed += cur.rowcount
            self._conn.commit()
        return fixed

    def log_api_call(self, method: str, route: str, status: int,
                     dur_ms: float, ip: str = "", auth: int = 1) -> None:
        """记录一次 /api/* 调用（auth: 0=公共接口 1=需登录）。
        写入先进内存缓冲，攒够 64 条或距上次落盘超过 3 秒才批量提交：
        高并发轮询时避免每个请求都触发一次 fsync（统计允许极短延迟）。"""
        now = time.time()
        with self._lock:
            self._api_stats_buffer.append(
                (now, self._bj_day(now), (method or "GET").upper(),
                 (route or "").strip()[:200], int(status),
                 max(0.0, float(dur_ms)), (ip or "").strip()[:64], 1 if auth else 0))
            if len(self._api_stats_buffer) >= 64 or now - self._last_api_flush >= 3.0:
                self._flush_api_stats_locked(now)

    def _flush_api_stats_locked(self, now: Optional[float] = None) -> None:
        """把缓冲的接口统计批量落盘（须持锁调用）；顺带清理超过保留天数的旧记录。"""
        now = time.time() if now is None else now
        if self._api_stats_buffer:
            self._conn.executemany(
                """INSERT INTO api_stats (ts, day, method, route, status, dur_ms, ip, auth)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                self._api_stats_buffer,
            )
            self._api_stats_buffer.clear()
            self._conn.execute("DELETE FROM api_stats WHERE ts < ?",
                               (now - KEEP_API_STATS_DAYS * 86400,))
            self._conn.commit()
        self._last_api_flush = now

    def api_call_stats(self, days: int = 14, auth: Optional[int] = None) -> Dict[str, Any]:
        """接口调用统计：今日/累计请求与错误、按接口聚合、近 N 天趋势、最近调用。
        auth: None=全部 0=仅公共接口 1=仅需登录接口。"""
        tz = timezone(timedelta(hours=8))
        today = self._bj_day()
        where = "" if auth is None else f" WHERE auth = {1 if auth else 0}"
        where_day = f"{where} AND day = ?" if where else " WHERE day = ?"
        with self._lock:
            self._flush_api_stats_locked()   # 先落盘缓冲，保证统计读到最新记录
            row = self._conn.execute(
                f"""SELECT COUNT(*) AS req,
                           SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS err
                    FROM api_stats{where}"""
            ).fetchone()
            total_req, total_err = row["req"] or 0, row["err"] or 0
            row = self._conn.execute(
                f"""SELECT COUNT(*) AS req,
                           SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS err
                    FROM api_stats{where_day}""",
                (today,),
            ).fetchone()
            today_req, today_err = row["req"] or 0, row["err"] or 0
            endpoints = [dict(r) for r in self._conn.execute(
                f"""SELECT method, route,
                           COUNT(*) AS total,
                           SUM(CASE WHEN day = ? THEN 1 ELSE 0 END) AS today,
                           SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS err,
                           ROUND(AVG(dur_ms), 1) AS avg_ms,
                           ROUND(MAX(dur_ms), 1) AS max_ms,
                           MAX(ts) AS last_ts
                    FROM api_stats{where}
                    GROUP BY method, route
                    ORDER BY total DESC
                    LIMIT 60""",
                (today,),
            ).fetchall()]
            per_day = {r["day"]: (r["req"], r["err"]) for r in self._conn.execute(
                f"""SELECT day, COUNT(*) AS req,
                           SUM(CASE WHEN status >= 400 THEN 1 ELSE 0 END) AS err
                    FROM api_stats{where} GROUP BY day"""
            ).fetchall()}
            recent = [dict(r) for r in self._conn.execute(
                f"""SELECT ts, method, route, status, dur_ms, ip
                    FROM api_stats{where} ORDER BY id DESC LIMIT 50"""
            ).fetchall()]
        # 补齐最近 N 天（无调用的天也输出，前端画趋势不断档）
        daily = []
        base = datetime.fromtimestamp(time.time(), tz).date()
        for offset in range(days - 1, -1, -1):
            day = (base - timedelta(days=offset)).strftime("%Y-%m-%d")
            req, err = per_day.get(day, (0, 0))
            daily.append({"day": day, "req": req, "err": err, "today": day == today})
        return {
            "today_req": today_req, "today_err": today_err,
            "total_req": total_req, "total_err": total_err,
            "endpoints": endpoints, "daily": daily, "recent": recent,
        }

    # ------------------------------------------------------------------ #
    # 全局设置
    # ------------------------------------------------------------------ #

    def get_settings(self) -> Dict[str, Any]:
        """全局设置（带内存缓存：每次 API 请求的鉴权都会读设置，避免反复查库）。
        返回深拷贝，调用方就地修改不会污染缓存；save_settings 会使缓存失效。"""
        if self._settings_cache is not None:
            return json.loads(json.dumps(self._settings_cache, ensure_ascii=False))
        with self._lock:
            row = self._conn.execute("SELECT payload FROM settings WHERE id = 1").fetchone()
        if not row:
            merged = json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))
        else:
            try:
                payload = json.loads(row["payload"])
            except (ValueError, TypeError):
                payload = {}
            # 浅合并默认值，保证升级后新增字段可用
            merged = json.loads(json.dumps(DEFAULT_SETTINGS, ensure_ascii=False))
            merged.update(payload if isinstance(payload, dict) else {})
        self._settings_cache = merged
        return json.loads(json.dumps(merged, ensure_ascii=False))

    def save_settings(self, payload: Dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO settings (id, payload) VALUES (1, ?)
                   ON CONFLICT(id) DO UPDATE SET payload = excluded.payload""",
                (json.dumps(payload, ensure_ascii=False),),
            )
            self._conn.commit()
        self._settings_cache = None   # 使内存缓存失效，下次读取重新查库

    # ------------------------------------------------------------------ #
    # 管理员账号
    # ------------------------------------------------------------------ #

    def ensure_default_admin(self, username: str = DEFAULT_ADMIN_USERNAME,
                             password: str = DEFAULT_ADMIN_PASSWORD) -> bool:
        """没有任何账号时创建默认管理员；返回是否新建了账号。"""
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
            if row["n"]:
                return False
            now = time.time()
            self._conn.execute(
                """INSERT INTO users (username, password_hash, created_at, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (username, generate_password_hash(password), now, now),
            )
            self._conn.commit()
        return True

    def verify_user(self, username: str, password: str) -> Optional[Dict[str, Any]]:
        """校验用户名密码；通过返回用户行，否则 None。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        if row and check_password_hash(row["password_hash"], password or ""):
            return dict(row)
        return None

    def update_password(self, username: str, new_password: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """UPDATE users SET password_hash = ?, updated_at = ?
                   WHERE username = ?""",
                (generate_password_hash(new_password), time.time(), username),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def is_default_password(self, username: str) -> bool:
        """密码是否仍是出厂默认（用于前端提示尽快修改）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT password_hash FROM users WHERE username = ?", (username,)
            ).fetchone()
        return bool(row) and check_password_hash(row["password_hash"], DEFAULT_ADMIN_PASSWORD)
