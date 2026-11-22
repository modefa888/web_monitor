#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网页监控平台 · 代码执行器 + 后台调度器

监控代码约定（与项目里 check_stock_once.py 的输出规范保持一致）
----------------------------------------------------------------
1. 每次执行前，平台把用户代码写入临时脚本，用独立的 python 子进程运行；
2. 子进程里可直接使用 PARAMS 变量（监控项里配置的 JSON 参数），
   以及 emit(ok, message, data=None) 函数用于上报结果；
3. stdout 中 __RESULT__ 开头的行会被解析为执行结果；没调用 emit 时，
   退出码 0 视为成功（message 取 stdout 末行），非 0 视为失败；
4. 执行超时、代码抛异常、非零退出码都会被记为一次失败，不会拖垮平台。

调度器为单线程循环（1 秒一轮），到期的监控项丢进线程池并行执行；
同一路径手动执行（立即执行 / 测试运行）复用 run_code。
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from notifier import Notifier
from store import Store

log = logging.getLogger("web-monitor")

RESULT_PREFIX = "__RESULT__"

# 注入到用户代码之前的小前导：提供 PARAMS 与 emit()
# （注意：这里不能走 str.format，用户可见的 "{}" 字面量会破坏占位符解析）
PRELUDE = '''# ---- 平台注入的执行环境 ----
import json as _json, os as _os, sys as _sys

PARAMS = _json.loads(_os.environ.get("MONITOR_PARAMS") or "{}")

def emit(ok, message="", data=None):
    """上报本次监控结果；ok=是否正常，message=一句话结果，data=任意可 JSON 化的附加数据。"""
    print("__RESULT__" + _json.dumps(
        {"ok": bool(ok), "message": str(message), "data": data}, ensure_ascii=False
    ), flush=True)

# ---- 以下是监控代码 ----
'''

assert RESULT_PREFIX in PRELUDE, "PRELUDE 里的结果标记与 RESULT_PREFIX 不一致"


def run_code(code: str, params: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """在独立子进程里执行一段监控代码，返回统一格式的结果。

    返回字段：ok / message / data / output / duration_ms / timed_out
    """
    workdir = Path(tempfile.mkdtemp(prefix="webmon_"))
    script = workdir / "monitor_task.py"
    started = time.time()
    try:
        script.write_text(PRELUDE + (code or ""), encoding="utf-8")
        env = dict(os.environ)
        env["MONITOR_PARAMS"] = json.dumps(params or {}, ensure_ascii=False)
        try:
            proc = subprocess.run(
                [sys.executable, str(script)],
                cwd=str(workdir), capture_output=True, text=True,
                timeout=max(1.0, float(timeout)), env=env,
            )
            stdout, stderr, returncode = proc.stdout or "", proc.stderr or "", proc.returncode
        except subprocess.TimeoutExpired as exc:
            return {
                "ok": False, "timed_out": True,
                "message": f"执行超时（上限 {timeout:g} 秒），已强制终止",
                "data": None,
                "output": _as_text(exc.stdout) + _as_text(exc.stderr),
                "duration_ms": (time.time() - started) * 1000,
            }

        ok, message, data = _parse_result(stdout, stderr, returncode)
        return {
            "ok": ok, "timed_out": False, "message": message, "data": data,
            "output": (stdout + ("\n" + stderr if stderr.strip() else ""))[-8000:],
            "duration_ms": (time.time() - started) * 1000,
        }
    except Exception as exc:  # 兜底：执行器自身出错也记为一次失败
        log.exception("执行监控代码失败")
        return {
            "ok": False, "timed_out": False,
            "message": f"执行器错误：{exc}", "data": None,
            "output": "", "duration_ms": (time.time() - started) * 1000,
        }
    finally:
        try:
            script.unlink(missing_ok=True)
            workdir.rmdir()
        except OSError:
            pass


def _as_text(chunk: Any) -> str:
    """subprocess 在超时分支可能返回 bytes，统一转成文本。"""
    if not chunk:
        return ""
    if isinstance(chunk, bytes):
        return chunk.decode("utf-8", errors="replace")
    return str(chunk)


def _parse_result(stdout: str, stderr: str, returncode: int) -> Tuple[bool, str, Any]:
    """从子进程输出里解析结果：__RESULT__ 行 > 末行 JSON > 退出码。"""
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    for line in reversed(lines):
        if line.startswith(RESULT_PREFIX):
            try:
                payload = json.loads(line[len(RESULT_PREFIX):])
                return bool(payload.get("ok")), str(payload.get("message") or ""), payload.get("data")
            except ValueError:
                break  # 标记行损坏，退回退出码逻辑

    # 没调用 emit：尝试把末行当 JSON（兼容老式“末行输出单行 JSON”脚本）
    if lines:
        try:
            payload = json.loads(lines[-1])
            if isinstance(payload, dict):
                ok = bool(payload.get("ok", returncode == 0))
                message = str(payload.get("detail") or payload.get("message") or lines[-1])
                return ok, message[:500], payload
        except ValueError:
            pass

    if returncode == 0:
        return True, (lines[-1] if lines else "执行完成")[:500], None
    reason = ""
    err_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if err_lines:
        reason = err_lines[-1][:400]
    elif lines:
        reason = lines[-1][:400]
    return False, f"退出码 {returncode}{('：' + reason) if reason else ''}", None


# 公开看板的“技术异常”识别标记：采集脚本自身出的问题（堆栈 / 连接错误 /
# 超时 / 执行器错误等）不对外展示原文，统一替换成一句友好提示。
# 中英文都要覆盖——执行器产出的超时、执行器错误消息是中文，只配英文标记会漏掉，
# 导致容忍窗口失效、原始错误直接亮给访客（曾因此出现“无货 + 剩余 131 台”的矛盾展示）。
_PUBLIC_ERROR_MARKERS = (
    "Traceback", "Error:", "Exception", "NewConnectionError", "ConnectionError",
    "HTTPSConnectionPool", "HTTPConnectionPool", "Max retries exceeded",
    "NewConnectionError", "Connection refused", "Network is unreachable",
    "timed out", "Timeout", "SSLError", "JSONDecodeError", "KeyError",
    "TypeError", "ValueError", "IndexError", "AttributeError", "Errno",
    "Caused by", "Failed to establish", "unreachable", "not known",
    "requests.exceptions", "Connection aborted", "RemoteDisconnected",
    "执行超时", "已强制终止", "执行器错误", "退出码",
)

# 慢执行备注是管理员信息（scheduler._record 追加到消息尾部），公开看板一并去除
_SLOW_NOTE_RE = re.compile(r"\s*⚠\s*耗时\s*\d+ms\s*超过阈值\s*\d+ms")


def is_technical_error(text: str) -> bool:
    """是否为采集脚本抛出的技术异常（堆栈 / 连接错误 / 超时 / 执行器错误等）。"""
    return bool(text) and any(
        marker.lower() in text.lower() for marker in _PUBLIC_ERROR_MARKERS
    )


def public_message(raw: Any) -> str:
    """公开结果消息的脱敏：技术异常只显示一句友好提示，慢执行备注一并去除。"""
    text = str(raw or "")
    if is_technical_error(text):
        return "数据获取异常，管理员正在处理"
    return _SLOW_NOTE_RE.sub("", text).strip()


CN_TZ = timezone(timedelta(hours=8))     # 活动场次均按北京时间


def _parse_windows(raw: Any) -> List[Tuple[int, int]]:
    """解析 [{start: HH:MM, end: HH:MM}]，返回分钟数区间；空=全天监控。"""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    result: List[Tuple[int, int]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            sh, sm = (int(x) for x in str(item.get("start", "")).split(":"))
            eh, em = (int(x) for x in str(item.get("end", "")).split(":"))
        except (ValueError, TypeError):
            continue
        if 0 <= sh <= 23 and 0 <= eh <= 23 and 0 <= sm <= 59 and 0 <= em <= 59:
            result.append((sh * 60 + sm, eh * 60 + em))
    return result


def _window_status(raw: Any, now: float) -> Tuple[bool, Optional[float]]:
    """返回 (当前是否应执行, 下一场开始时间戳)；全天监控时第二项为 None。"""
    windows = _parse_windows(raw)
    if not windows:
        return True, None
    local = datetime.fromtimestamp(now, CN_TZ)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    candidates: List[float] = []
    for start_min, end_min in windows:
        for offset in (0, -86400):   # 兼顾跨零点的场次（凌晨属于前一天）
            start_ts = day_start + start_min * 60 + offset
            end_ts = day_start + end_min * 60 + offset
            if end_min <= start_min:
                end_ts += 86400
            if start_ts <= now < end_ts:
                return True, None
            if start_ts > now:
                candidates.append(start_ts)
    if candidates:
        return False, min(candidates)
    return False, day_start + 86400 + min(s for s, _ in windows) * 60


class Scheduler:
    """后台调度：轮询数据库里已启用的监控项，到期的丢进线程池执行。"""

    def __init__(self, store: Store, poll_seconds: float = 1.0, max_workers: int = 8) -> None:
        self.store = store
        self.poll_seconds = poll_seconds
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="mon-run")
        self._next_run: Dict[int, float] = {}      # monitor_id -> 下次调度时间
        self._running: set = set()                 # 正在执行的 monitor_id
        self._lock = threading.Lock()
        self._notifier: Optional[Notifier] = None
        self._notifier_loaded_at: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- 生命周期 ------------------------------------------------------------ #

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="mon-scheduler", daemon=True)
        self._thread.start()
        log.info("调度器已启动（%.0f 秒轮询，最多 %d 个并行任务）", self.poll_seconds, self.pool._max_workers)

    def stop(self) -> None:
        self._stop.set()

    def is_running(self, monitor_id: int) -> bool:
        with self._lock:
            return monitor_id in self._running

    def next_run_at(self, monitor_id: int) -> Optional[float]:
        with self._lock:
            return self._next_run.get(monitor_id)

    # -- 主循环 ---------------------------------------------------------------- #

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("调度循环异常，下一轮重试")
            self._stop.wait(self.poll_seconds)

    def _tick(self) -> None:
        now = time.time()
        monitors = self.store.list_monitors(enabled_only=True)
        active_ids = {m["id"] for m in monitors}

        with self._lock:
            # 清掉已删除 / 已停用的调度状态
            for mid in list(self._next_run):
                if mid not in active_ids:
                    self._next_run.pop(mid, None)

        for monitor in monitors:
            mid = monitor["id"]
            in_window, next_start = _window_status(monitor.get("active_windows"), now)
            if not in_window:
                # 非监控时段完全不执行，并把下次运行指向下一场开始时刻
                with self._lock:
                    self._next_run[mid] = next_start or (now + self.poll_seconds)
                continue
            with self._lock:
                if mid in self._running:
                    continue
                if mid not in self._next_run:
                    # 新监控项 / 服务重启后：延迟一个间隔再跑，避免启动风暴
                    self._next_run[mid] = now + float(monitor["interval_seconds"])
                due = now >= self._next_run[mid]

            if not due:
                continue
            self._dispatch(monitor)

    def _dispatch(self, monitor: Dict[str, Any]) -> None:
        mid = monitor["id"]
        with self._lock:
            if mid in self._running:
                return
            self._running.add(mid)
            # 立刻排下一次，避免执行耗时导致间隔漂移
            self._next_run[mid] = time.time() + float(monitor["interval_seconds"])
        future = self.pool.submit(self._run_and_record, monitor, "schedule")
        future.add_done_callback(lambda _f: self._finish(mid))

    def _finish(self, monitor_id: int) -> None:
        with self._lock:
            self._running.discard(monitor_id)

    # -- 执行 + 记录 + 通知 ----------------------------------------------------- #

    def _run_and_record(self, monitor: Dict[str, Any], trigger: str) -> None:
        monitor_id = monitor["id"]
        result = run_code(
            monitor["code"], self._safe_params(monitor["params"]), monitor["timeout_seconds"]
        )
        # 定时任务失败自动重试：手动执行不重试（保持“立即反馈”的语义）
        retries = int(monitor.get("retry_count") or 0)
        if trigger == "schedule" and not result["ok"] and retries > 0:
            raw_gap = monitor.get("retry_interval")
            gap = max(0.0, float(5 if raw_gap is None else raw_gap))
            for attempt in range(2, retries + 2):
                log.info("监控「%s」第 %d/%d 次失败，%g 秒后重试：%s",
                         monitor["name"], attempt - 1, retries, gap, result["message"])
                if gap > 0:
                    time.sleep(gap)
                result = run_code(
                    monitor["code"], self._safe_params(monitor["params"]),
                    monitor["timeout_seconds"],
                )
                result["attempt"] = attempt      # 无论成败，记录真实尝试次数
                if result["ok"]:
                    result["message"] = f"{result['message']}（重试 {attempt - 1} 次后成功）"
                    log.info("监控「%s」重试第 %d 次成功", monitor["name"], attempt - 1)
                    break
        self._record(monitor, result, trigger)
        self._maybe_notify(monitor, result)

    def _record(self, monitor: Dict[str, Any], result: Dict[str, Any], trigger: str) -> None:
        monitor_id = monitor["id"]
        ok = bool(result["ok"])
        prev_ok = monitor.get("last_ok")

        # 慢执行检测：耗时超过阈值记为 slow（不算失败，仅标记与提醒）
        threshold = float(monitor.get("slow_threshold_ms") or 0)
        slow = bool(threshold > 0 and result["duration_ms"] > threshold)
        if slow:
            result["slow"] = True
            result["message"] = (f"{result['message']} ⚠ 耗时 {int(result['duration_ms'])}ms"
                                 f" 超过阈值 {int(threshold)}ms")

        # 连续失败计数：成功清零，失败累加
        fail_count = 0 if ok else (monitor.get("fail_count") or 0) + 1
        prev_run = self.store.get_last_run(monitor_id)   # 本次插入前的最近记录
        result["_prev_slow"] = bool(prev_run and prev_run.get("slow"))
        self.store.add_run(
            monitor_id, time.time() - result["duration_ms"] / 1000,
            result["duration_ms"], ok, result["message"],
            result.get("output") or "",
            json.dumps(result["data"], ensure_ascii=False) if result.get("data") is not None else None,
            trigger, slow=slow, attempt=int(result.get("attempt") or 1),
        )
        # 失败时保留上一次成功的数据快照：公开看板据此展示最近一次有效的库存，
        # 不因偶发网络抖动被清空；详细错误仍完整写入历史，供管理员排查。
        snapshot = json.dumps(result["data"], ensure_ascii=False) if result.get("data") is not None else None
        runtime_fields = {
            "last_ok": 1 if ok else 0,
            "last_message": result["message"],
            "last_run_at": time.time(),
            "fail_count": fail_count,
        }
        if snapshot is not None:
            runtime_fields["last_data"] = snapshot
        self.store.set_runtime(monitor_id, **runtime_fields)
        transition = prev_ok is not None and bool(prev_ok) != ok
        if transition:
            log.info("监控「%s」状态变化：%s", monitor["name"],
                     "异常 -> 恢复" if ok else "正常 -> 异常")
        if slow:
            log.info("监控「%s」执行缓慢：%dms > 阈值 %gms",
                     monitor["name"], int(result["duration_ms"]), threshold)
        log.debug("监控「%s」%s：%s", monitor["name"],
                  "手动" if trigger == "manual" else "定时",
                  result["message"])

    @staticmethod
    def _remaining_of(raw: Any) -> Tuple[Optional[str], Optional[float]]:
        """从结果 data 里取库存数量字段（remaining / stock_left）。"""
        if not raw:
            return None, None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        for key in ("remaining", "stock_left"):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return key, float(value)
        return None, None

    @staticmethod
    def _in_stock(raw: Any) -> bool:
        """判断当前是否有库存：优先用 data.in_stock，其次看数量/进度字段。"""
        data = raw.get("data") if isinstance(raw, dict) else raw
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except (ValueError, TypeError):
                data = None
        if isinstance(data, dict):
            for key in ("in_stock", "has_stock", "available_stock"):
                if key in data:
                    return bool(data[key])
            for key in ("remaining", "stock_left", "available"):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return value > 0
            for key in ("percent", "usage"):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    return value < 100
        # 没有库存字段的老式监控：成功即视为“有”，失败视为“无”
        return bool(raw.get("ok")) if isinstance(raw, dict) else False

    def _quantity_change(self, monitor: Dict[str, Any], result: Dict[str, Any]):
        """对比上次与新结果里的剩余数量，变化时返回 (字段名, 旧值, 新值)。"""
        prev_key, prev_val = self._remaining_of(monitor.get("last_data"))
        now_key, now_val = self._remaining_of(result.get("data"))
        if prev_key and now_key and prev_key == now_key and prev_val != now_val:
            return prev_key, prev_val, now_val
        return None, None, None

    def _maybe_notify(self, monitor: Dict[str, Any], result: Dict[str, Any]) -> None:
        """按监控项的 notify_on 策略 + 全局冷却时间决定是否推送。

        推送时机：
        - change：状态翻转 / 库存数量变化 / 慢执行翻转；
        - fail：仅失败与需要知道的问题；
        - in_stock：只在“无货 -> 有货”时推送，剩余数量变化或有货期间都不再打扰；
        冷却时间内不重复打扰。
        """
        ok = bool(result["ok"])
        slow = bool(result.get("slow"))
        notify_on = monitor.get("notify_on") or "change"
        if notify_on == "never":
            return

        prev_ok = monitor.get("last_ok")            # 本次运行前的状态
        has_prev = prev_ok is not None
        status_change = has_prev and bool(prev_ok) != ok
        slow_change = has_prev and slow != bool(result.get("_prev_slow"))
        qty_key, qty_prev, qty_now = self._quantity_change(monitor, result)
        qty_change = qty_key is not None

        if notify_on == "fail":
            # 仅失败推送；慢执行翻转也一并提醒（同属“需要知道的问题”）
            if ok and not slow_change:
                return
        elif notify_on == "in_stock":
            # 只在从无货变有货时提醒；有货期间的每次刷新、数量变化都不再推送
            prev_in_stock = self._in_stock({
                "data": monitor.get("last_data"), "ok": monitor.get("last_ok"),
            })
            now_in_stock = self._in_stock(result)
            log.debug("监控「%s」库存推送判定：上次有货=%s，本次有货=%s",
                      monitor["name"], prev_in_stock, now_in_stock)
            if not (now_in_stock and not prev_in_stock):
                return
        else:   # change：状态翻转、数量变化或慢状态翻转任一发生即推送（首次运行不推）
            if not (status_change or slow_change or qty_change):
                return

        settings = self.store.get_settings()
        cooldown = float(settings.get("notify", {}).get("cooldown_seconds") or 300)
        now = time.time()
        if now - float(monitor.get("last_notify_at") or 0) < cooldown:
            log.debug("监控「%s」处于推送冷却期，跳过", monitor["name"])
            return

        name = monitor["name"]
        if notify_on == "in_stock":
            status_text = "有货提醒"
        elif ok and qty_change and not status_change and not slow_change:
            status_text = "库存变化"
        elif ok and slow_change and not status_change:
            status_text = "执行缓慢" if slow else "速度恢复"
        else:
            status_text = "恢复正常" if ok else "监控异常"
        threshold_note = (f"（阈值 {int(float(monitor.get('slow_threshold_ms') or 0))}ms）"
                          if slow and monitor.get("slow_threshold_ms") else "")
        result_text = ("✅ " if ok else "❌ ") + result["message"]
        qty_text = (f"{'剩余' if qty_key == 'remaining' else '库存'}：{qty_prev:g} → {qty_now:g}"
                    if qty_change else "")
        now_text = time.strftime("%Y-%m-%d %H:%M:%S")
        group_name = monitor.get("group_name") or ""
        # 模板变量上下文：供各渠道自定义推送标题/正文使用（中文主键 + 英文别名）
        site_name = self.store.site_info().get("name") or "网页监控平台"
        ctx = {
            "平台名": site_name, "site": site_name,
            "监控名": name, "name": name,
            "分组": group_name, "group": group_name,
            "状态": status_text, "status": status_text,
            "结果": result_text, "message": result_text,
            "库存变化": qty_text, "qty_change": qty_text,
            "连续失败": str(monitor.get("fail_count") or 0),
            "fail_count": str(monitor.get("fail_count") or 0),
            "耗时": f"{int(result['duration_ms'])}ms{threshold_note}",
            "duration": f"{int(result['duration_ms'])}ms{threshold_note}",
            "时间": now_text, "time": now_text,
            "厂商": monitor.get("vendor") or "", "vendor": monitor.get("vendor") or "",
            "地区": monitor.get("region") or "", "region": monitor.get("region") or "",
        }
        default_title = f"【网页监控】{name} {status_text}"
        default_content = "\n".join(line for line in [
            f"监控项：{name}",
            f"状态：{result_text}",
            qty_text,
            f"连续失败：{monitor.get('fail_count') or 0} 次" if not ok else "",
            f"耗时：{int(result['duration_ms'])}ms{threshold_note}" if ok else "",
            f"时间：{now_text}",
        ] if line)
        notifier = self._get_notifier()
        if not notifier:
            return
        # 逐渠道结果写入通知日志，供「通知设置 → 通用」页展示
        monitor_id, monitor_name = monitor["id"], name
        sent = notifier.send(
            default_title, default_content,
            on_result=lambda kind, ok, detail, cname:
                self.store.log_notify(monitor_id, monitor_name, kind, ok, detail, cname),
            ctx=ctx,
        )
        if sent:
            self.store.set_runtime(monitor["id"], last_notify_at=now)

    def _get_notifier(self) -> Optional[Notifier]:
        """设置里没配渠道时返回 None；配置有变化时重建。"""
        settings = self.store.get_settings()
        signature = json.dumps(settings.get("channels"), ensure_ascii=False, sort_keys=True)
        if self._notifier is None or signature != self._notifier_loaded_at:
            self._notifier = Notifier(settings.get("channels") or [])
            self._notifier_loaded_at = signature
        return self._notifier if self._notifier.channels else None

    @staticmethod
    def _safe_params(raw: Any) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            return {}
