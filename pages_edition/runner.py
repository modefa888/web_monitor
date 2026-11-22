#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GitHub Actions 采集器：执行 monitors.json 里的监控项，产出静态看板数据。

用法（在仓库根目录执行）：
    python pages_edition/runner.py

产出：
    docs/data/stock.json    公开看板数据（与主项目 /api/public/stock 同构）
    pages_edition/state.json  连续失败计数 / 成功率等跨次状态（随仓库提交）

监控项来源 monitors.json：
    - "template": 复用主项目 code_templates.py 里的内置模板（推荐）
    - "script":   相对 pages_edition/ 的自定义脚本，脚本内用 emit() 上报结果
两者都通过主项目 scheduler.run_code 的独立子进程执行，约定完全一致。
"""

import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent          # 仓库根目录
BASE = Path(__file__).resolve().parent                 # pages_edition/
DOCS_DATA = ROOT / "docs" / "data"                     # Pages 静态数据目录
STATE_FILE = BASE / "state.json"

sys.path.insert(0, str(ROOT))                          # 复用主项目的模板与执行器
from code_templates import TEMPLATES                    # noqa: E402
from scheduler import is_technical_error, public_message, run_code  # noqa: E402

RESULT_PREFIX = "__RESULT__"

# 与主项目 PUBLIC_HIDDEN_DATA_KEYS 保持一致（url/site_url 保留：前往站点按钮要用）
HIDDEN_DATA_KEYS = {"goods", "checked_at", "current_session", "next_session", "sessions"}

# 曲线采样：与主项目 /api/public/stock/<id>/runs 接口的行同构，看板直接复用画图逻辑。
# 曲线只展示当天，但保留 26 小时（跨零点后仍能看到昨天尾部的对照）。
TREND_DATA_KEYS = ("remaining", "in_stock", "percent", "progress")
TREND_KEEP_SECONDS = 26 * 3600
TREND_MAX_SAMPLES = 400            # 双保险：约 33 小时 @5 分钟间隔


def _trend_sample(now: float, ok: bool, message: str, data: Any) -> dict:
    """把一次执行结果压缩成曲线采样点（只留画图需要的字段，控制仓库体积）。"""
    data = data if isinstance(data, dict) else {}
    return {
        "started_at": now,
        "ok": ok,
        "message": message[:120],
        "data": {k: data[k] for k in TREND_DATA_KEYS
                 if isinstance(data.get(k), (int, float, bool))},
    }


def _prune_trend(samples: list, now: float) -> list:
    """按时间窗口与条数上限裁剪采样序列。"""
    kept = [s for s in samples if s.get("started_at", 0) >= now - TREND_KEEP_SECONDS]
    return kept[-TREND_MAX_SAMPLES:]


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def resolve_code(entry: dict) -> str:
    """监控项 -> 可执行代码：优先内置模板，其次自定义脚本文件。"""
    template = str(entry.get("template") or "").strip()
    if template:
        for tpl in TEMPLATES:
            if tpl["id"] == template:
                return tpl["code"]
        raise SystemExit(f"monitors.json 里引用了未知模板：{template}（可选："
                         f"{', '.join(t['id'] for t in TEMPLATES)}）")
    script = str(entry.get("script") or "").strip()
    if script:
        return (BASE / script).read_text(encoding="utf-8")
    raise SystemExit(f"监控项「{entry.get('name')}」缺少 template 或 script 字段")


def main() -> None:
    config = load_json(BASE / "monitors.json", {})
    state = load_json(STATE_FILE, {})
    monitors = [m for m in (config.get("monitors") or []) if m.get("enabled", True)]
    now = time.time()

    items = []
    for m in monitors:
        name = m.get("name") or "未命名监控"
        prev = state.get(name) or {}
        try:
            code = resolve_code(m)
            result = run_code(code, m.get("params") or {}, float(m.get("timeout") or 30))
        except Exception as exc:  # 执行器自身出错也记为一次失败
            result = {"ok": False, "message": f"执行器错误：{exc}", "data": None}

        ok = bool(result["ok"])
        raw_message = str(result.get("message") or "")
        pub_message = public_message(raw_message)
        # error = 采集脚本自身的技术异常（超时 / 连接失败等），与 fail（采集成功、
        # 结论就是无货这类有意义结果）区分：看板对 error 显示"异常"，绝不显示"无货"
        status = "ok" if ok else ("error" if is_technical_error(raw_message) else "fail")
        fails = 0 if ok else int(prev.get("fails") or 0) + 1
        history = (prev.get("history") or [])
        history.append(1 if ok else 0)
        history = history[-50:]                      # 最近 50 次用于成功率
        # 曲线采样随 state.json 跨次累积，发布成 docs/data/trend/<id>.json 供看板画图
        trend = _prune_trend((prev.get("trend") or []) + [
            _trend_sample(now, ok, pub_message, result.get("data"))], now)
        state[name] = {"fails": fails, "history": history, "last_ts": now, "trend": trend}

        data = result.get("data") or {}
        items.append({
            "id": len(items),
            "name": name,
            "group_name": m.get("group") or "",
            "status": status,
            "running": False,
            "message": pub_message,
            "data": {k: v for k, v in data.items()
                     if k not in HIDDEN_DATA_KEYS and v is not None and v != ""},
            "last_run_at": now,
            "interval_seconds": 300,
            "fail_count": fails,
            "success_rate": (round(sum(history) / len(history) * 100, 1)
                             if history else None),
            "vendor": m.get("vendor") or "",
            "region": m.get("region") or "",
            "price_type": m.get("price_type") or "",
            "purchase_limit": m.get("purchase_limit") or 0,
            "realname": m.get("realname") or "",
        })

    payload = {
        "items": items,
        "groups": config.get("group_order") or sorted({m.get("group") or ""
                                                       for m in monitors}),
        "group_icons": config.get("group_icons") or {},
        "all_icon": config.get("all_icon") or "",
        "site": config.get("site") or {},
        "generated_at": time.time(),
    }

    DOCS_DATA.mkdir(parents=True, exist_ok=True)
    (DOCS_DATA / "stock.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    # 每个监控项一条曲线采样文件（单文件小，看板点开「曲线」才拉对应那份）
    trend_dir = DOCS_DATA / "trend"
    trend_dir.mkdir(parents=True, exist_ok=True)
    keep_ids = set()
    for i, m in enumerate(monitors):
        name = m.get("name") or "未命名监控"
        keep_ids.add(str(i))
        (trend_dir / f"{i}.json").write_text(
            json.dumps({"generated_at": now,
                        "items": (state.get(name) or {}).get("trend") or []},
                       ensure_ascii=False), encoding="utf-8")
    for old in trend_dir.glob("*.json"):       # 清掉已删除/停用监控的残留文件
        if old.stem not in keep_ids:
            old.unlink(missing_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"采集完成：{len(items)} 个监控项 -> docs/data/stock.json")
    for it in items:
        flag = "✅" if it["status"] == "ok" else ("⚪" if it["status"] == "pending" else "❌")
        print(f"  {flag} {it['name']}：{it['message']}")


if __name__ == "__main__":
    main()
