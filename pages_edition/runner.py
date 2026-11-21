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

ROOT = Path(__file__).resolve().parent.parent          # 仓库根目录
BASE = Path(__file__).resolve().parent                 # pages_edition/
DOCS_DATA = ROOT / "docs" / "data"                     # Pages 静态数据目录
STATE_FILE = BASE / "state.json"

sys.path.insert(0, str(ROOT))                          # 复用主项目的模板与执行器
from code_templates import TEMPLATES                    # noqa: E402
from scheduler import run_code                          # noqa: E402

RESULT_PREFIX = "__RESULT__"

# 与主项目 PUBLIC_HIDDEN_DATA_KEYS 保持一致（url/site_url 保留：前往站点按钮要用）
HIDDEN_DATA_KEYS = {"goods", "checked_at", "current_session", "next_session", "sessions"}


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
        fails = 0 if ok else int(prev.get("fails") or 0) + 1
        history = (prev.get("history") or [])
        history.append(1 if ok else 0)
        history = history[-50:]                      # 最近 50 次用于成功率
        state[name] = {"fails": fails, "history": history, "last_ts": now}

        data = result.get("data") or {}
        items.append({
            "id": len(items),
            "name": name,
            "group_name": m.get("group") or "",
            "status": "ok" if ok else "fail",
            "running": False,
            "message": result.get("message") or "",
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
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"采集完成：{len(items)} 个监控项 -> docs/data/stock.json")
    for it in items:
        flag = "✅" if it["status"] == "ok" else ("⚪" if it["status"] == "pending" else "❌")
        print(f"  {flag} {it['name']}：{it['message']}")


if __name__ == "__main__":
    main()
