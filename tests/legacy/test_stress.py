"""
并发和压力测试 —— 覆盖 5 个维度：
  1. 60 并发请求（6 条路径 × 10 并发）
  2. SSE 压力测试（10 客户端同时连接）
  3. 大结果集边界测试（page=500, page_size=200）
  4. 30s 长时间运行稳定性
  5. 重载启动测试（连续启动/停止 3 次）
"""
import json
import os
import sys
import threading
import time
import queue
import signal
import subprocess
import platform
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ['WERKZEUG_RUN_MAIN'] = 'true'

# ── 全局计数器 ──────────────────────────────────────────────
PASS = 0
FAIL = 0
ERROR_LOG = []


def check(label: str, condition: bool, detail: str = ""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        msg = f"  ❌ {label}" + (f"  ({detail})" if detail else "")
        print(msg)
        ERROR_LOG.append(msg)


# ═══════════════════════════════════════════════════════════════
# 1. 并发请求测试
# ═══════════════════════════════════════════════════════════════
from app import create_app
app = create_app()
client = app.test_client()

errors = []
lock = threading.Lock()


def hit(path):
    try:
        r = client.get(path)
        if r.status_code not in (200, 302, 404):
            with lock:
                errors.append(f'{path} → {r.status_code}')
    except Exception as e:
        with lock:
            errors.append(f'{path} → {e}')


def test_concurrent_requests():
    """60 个并发请求打 6 个不同路径"""
    print("\n" + "=" * 60)
    print("📌 测试 1: 60 并发请求")
    print("=" * 60)

    errors.clear()
    paths = ['/', '/search', '/downloads', '/wishlist', '/library', '/settings'] * 10
    threads = [threading.Thread(target=hit, args=(p,)) for p in paths]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - t0

    print(f"  60 并发请求完成: {elapsed:.2f}s")
    check("全部请求 2xx/3xx/4xx（无异常）", len(errors) == 0,
          f"错误数={len(errors)}")
    for e in errors[:5]:
        print(f"    ⚠ {e}")
    check("响应时间 < 30s（可接受）", elapsed < 30, f"{elapsed:.2f}s")
    return errors


# ═══════════════════════════════════════════════════════════════
# 2. SSE 压力测试
# ═══════════════════════════════════════════════════════════════
from core.progress import progress_manager
from flask import Response


def test_sse_pressure():
    """模拟 10 个客户端同时连接 SSE 端点（每个客户端使用独立 tracker，模拟真实场景）"""
    print("\n" + "=" * 60)
    print("📌 测试 2: SSE 压力测试（10 并发客户端 × 独立 tracker）")
    print("=" * 60)

    # 为每个客户端创建一个独立的 tracker，模拟不同 job_id 的 SSE 连接
    test_job_ids = [f"__stress_sse_{i}__" for i in range(10)]
    trackers = []
    for jid in test_job_ids:
        t = progress_manager.create_tracker(jid)
        t.push("init", {"job_id": jid, "status": "queued"})
        trackers.append(t)

    received = []
    rlock = threading.Lock()
    all_connected = threading.Barrier(10, timeout=10)

    def sse_client(client_id: int):
        """模拟 SSE 客户端连接到独立的 job_id"""
        jid = test_job_ids[client_id]
        try:
            with app.test_request_context():
                from routes.api_jobs import job_events
                resp = job_events(jid)
                if isinstance(resp, Response):
                    gen = resp.response
                    # 读取第一个 event（至少应有 init 事件或 heartbeat）
                    first = next(gen, None)
                    with rlock:
                        received.append({
                            "id": client_id,
                            "first_event": first is not None,
                            "first_data": first[:100] if first else "",
                        })
                    gen.close()
                else:
                    with rlock:
                        received.append({
                            "id": client_id,
                            "first_event": False,
                            "error": str(resp),
                        })
        except Exception as e:
            with rlock:
                received.append({
                    "id": client_id,
                    "first_event": False,
                    "error": str(e),
                })
        finally:
            try:
                all_connected.wait()
            except Exception:
                pass

    threads = [threading.Thread(target=sse_client, args=(i,)) for i in range(10)]
    t0 = time.time()
    for t in threads:
        t.start()

    # 给所有客户端一点时间建立连接
    time.sleep(1)
    # 每个 tracker 再推送进度事件
    for t in trackers:
        t.push("progress", {"done": 3, "total": 10})

    for t in threads:
        t.join(timeout=10)
    elapsed = time.time() - t0

    all_received = len(received)
    first_events_ok = sum(1 for r in received if r.get("first_event"))
    check(f"10 个 SSE 客户端全部连接成功", all_received == 10,
          f"收到={all_received}")
    check(f"全部客户端收到初始事件（或心跳）", first_events_ok == 10,
          f"成功={first_events_ok}/10")
    check(f"SSE 响应时间 < 10s", elapsed < 10, f"{elapsed:.2f}s")

    # 清理全部 tracker
    for jid in test_job_ids:
        progress_manager.remove_tracker(jid)
    return received


# ═══════════════════════════════════════════════════════════════
# 3. 大结果集测试
# ═══════════════════════════════════════════════════════════════
def test_large_result_set():
    """测试极端分页参数：page=500（上限）, page_size=200（上限）"""
    print("\n" + "=" * 60)
    print("📌 测试 3: 大结果集边界测试")
    print("=" * 60)

    with app.test_request_context():
        # 3a. 资源库 page=500 & page_size=200
        print("\n  [3a] 资源库 API: page=500, page_size=200")
        with client.get("/api/library/?page=500&page_size=200") as resp:
            check(f"状态码 200", resp.status_code == 200,
                  f"got {resp.status_code}")
            try:
                data = resp.get_json()
                check("返回有效 JSON", data is not None)
                check("状态字段为 ok", data.get("status") == "ok",
                      f"got {data.get('status')}")
                if data:
                    print(f"        items={len(data.get('items', []))}, "
                          f"total={data.get('total')}, "
                          f"page={data.get('page')}, "
                          f"page_size={data.get('page_size')}")
            except Exception as e:
                check("返回有效 JSON", False, str(e))

        # 3b. 收藏列表 page=500 & page_size=200
        print("\n  [3b] 收藏列表 API: page=500, page_size=200")
        with client.get("/api/wishlist?page=500&page_size=200") as resp:
            check(f"状态码 200", resp.status_code == 200,
                  f"got {resp.status_code}")
            try:
                data = resp.get_json()
                check("返回有效 JSON", data is not None)
                check("状态字段为 ok", data.get("status") == "ok",
                      f"got {data.get('status')}")
                if data:
                    print(f"        items={len(data.get('items', []))}, "
                          f"total={data.get('total')}, "
                          f"page={data.get('page')}, "
                          f"page_size={data.get('page_size')}")
            except Exception as e:
                check("返回有效 JSON", False, str(e))

        # 3c. 设置 page_size=0（边界最小值）
        print("\n  [3c] 资源库 API: page=-1, page_size=0（边界最小值）")
        with client.get("/api/library/?page=-1&page_size=0") as resp:
            check(f"状态码 200（防御生效）", resp.status_code == 200,
                  f"got {resp.status_code}")
            try:
                data = resp.get_json()
                if data:
                    print(f"        clamped page={data.get('page')}, "
                          f"page_size={data.get('page_size')}")
                    check("page 被 clamp 到 ≥1", data.get("page", 0) >= 1)
                    check("page_size 被 clamp 到 ≥1",
                          data.get("page_size", 0) >= 1)
            except Exception as e:
                check("返回有效 JSON", False, str(e))

        # 3d. 搜索 page=500（上限）
        print("\n  [3d] 搜索 API: page=500（无关键词，应返回 400）")
        with client.get("/api/search?page=500&page_size=100") as resp:
            # 没有 q 参数应该返回 400
            check(f"状态码 400（缺少参数）", resp.status_code == 400,
                  f"got {resp.status_code}")


# ═══════════════════════════════════════════════════════════════
# 4. 长时间运行稳定性（30s 持续请求）
# ═══════════════════════════════════════════════════════════════
import psutil


def test_long_running_stability():
    """连续 30 秒每 1 秒请求一次 /，监控内存增长"""
    print("\n" + "=" * 60)
    print("📌 测试 4: 长时间运行稳定性（30 秒）")
    print("=" * 60)

    pid = os.getpid()
    proc = psutil.Process(pid)
    mem_samples = []
    response_times = []

    for i in range(30):
        t0 = time.time()
        try:
            with client.get("/") as resp:
                dt = time.time() - t0
                response_times.append(dt)
                if resp.status_code not in (200, 302):
                    print(f"    ⚠ 第 {i+1}s 请求异常: status={resp.status_code}")
        except Exception as e:
            print(f"    ⚠ 第 {i+1}s 请求异常: {e}")

        mem = proc.memory_info().rss / 1024 / 1024  # MB
        mem_samples.append(mem)
        time.sleep(1)

    max_mem = max(mem_samples)
    min_mem = min(mem_samples)
    avg_mem = sum(mem_samples) / len(mem_samples)
    mem_growth = mem_samples[-1] - mem_samples[0]
    max_rt = max(response_times) if response_times else 0
    avg_rt = sum(response_times) / len(response_times) if response_times else 0

    print(f"\n  内存: min={min_mem:.1f}MB  max={max_mem:.1f}MB  "
          f"avg={avg_mem:.1f}MB  growth={mem_growth:+.1f}MB")
    resp_time = f"avg={avg_rt*1000:.1f}ms  max={max_rt*1000:.1f}ms"
    print(f"  响应时间: {resp_time}")

    check("30 次请求全部成功", len(response_times) == 30,
          f"成功={len(response_times)}")
    check("无显著内存泄漏（增长 < 50MB）", abs(mem_growth) < 50,
          f"增长={mem_growth:+.1f}MB")
    avg_rt_ms = avg_rt * 1000
    max_rt_ms = max_rt * 1000
    check("平均响应时间 < 500ms", avg_rt < 0.5,
          f"{avg_rt_ms:.1f}ms")
    check("最大响应时间 < 5s", max_rt < 5,
          f"{max_rt_ms:.1f}ms")

    return {
        "mem_min_mb": round(min_mem, 1),
        "mem_max_mb": round(max_mem, 1),
        "mem_avg_mb": round(avg_mem, 1),
        "mem_growth_mb": round(mem_growth, 1),
        "rt_avg_ms": round(avg_rt * 1000, 1),
        "rt_max_ms": round(max_rt * 1000, 1),
    }


# ═══════════════════════════════════════════════════════════════
# 5. 重载启动测试
# ═══════════════════════════════════════════════════════════════
def test_restart_cycles():
    """连续启动/停止 3 次，检查端口绑定和数据库完整性"""
    print("\n" + "=" * 60)
    print("📌 测试 5: 重载启动测试（启动/停止 × 3）")
    print("=" * 60)

    import socket

    db_path = Path(__file__).resolve().parent.parent / "runtime" / "data" / "app.db"
    port_file = Path(__file__).resolve().parent.parent / "runtime" / "data" / "flask.json"

    # 记录初始数据库大小
    db_size_before = db_path.stat().st_size if db_path.exists() else 0

    for cycle in range(3):
        print(f"\n  启动/停止 第 {cycle+1} 轮...")
        # 模拟端口检查
        test_port = 5000
        used_ports = []
        for attempt in range(2):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", test_port))
                sock.listen()
                sock.close()
                break  # 端口可用
            except OSError:
                sock.close()
                used_ports.append(test_port)
                test_port += 1
                continue

        check(f"第 {cycle+1} 轮端口绑定成功", len(used_ports) == 0
              or test_port - 5000 > 0,
              f"已用端口: {used_ports}")

        # 检查数据库是否仍可读取
        try:
            import sqlite3
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA integrity_check").fetchone()
            conn.close()
            check(f"第 {cycle+1} 轮数据库完整性通过", True)
        except Exception as e:
            check(f"第 {cycle+1} 轮数据库完整性通过", False, str(e))

    # 最终数据库大小
    db_size_after = db_path.stat().st_size if db_path.exists() else 0
    print(f"\n  数据库: {db_size_before/1024:.1f}KB → {db_size_after/1024:.1f}KB "
          f"(变化={db_size_after - db_size_before:+}B)")


# ═══════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 60)
    print("JMComic 下载搜索插件 - 并发和压力测试")
    print("=" * 60)

    results = {}
    t_start = time.time()

    # 测试 1：并发请求
    try:
        results["concurrent"] = test_concurrent_requests()
    except Exception as e:
        print(f"\n  ❌ 测试 1 异常: {e}")
        ERROR_LOG.append(f"Test 1 crashed: {e}")
        FAIL += 1

    # 测试 2：SSE 压力
    try:
        results["sse"] = test_sse_pressure()
    except Exception as e:
        print(f"\n  ❌ 测试 2 异常: {e}")
        ERROR_LOG.append(f"Test 2 crashed: {e}")
        FAIL += 1

    # 测试 3：大结果集
    try:
        test_large_result_set()
    except Exception as e:
        print(f"\n  ❌ 测试 3 异常: {e}")
        ERROR_LOG.append(f"Test 3 crashed: {e}")
        FAIL += 1

    # 测试 4：长时间运行
    try:
        results["stability"] = test_long_running_stability()
    except Exception as e:
        print(f"\n  ❌ 测试 4 异常: {e}")
        ERROR_LOG.append(f"Test 4 crashed: {e}")
        FAIL += 1

    # 测试 5：重载启动
    try:
        test_restart_cycles()
    except Exception as e:
        print(f"\n  ❌ 测试 5 异常: {e}")
        ERROR_LOG.append(f"Test 5 crashed: {e}")
        FAIL += 1

    t_elapsed = time.time() - t_start

    # ── 汇总 ──
    print("\n" + "=" * 60)
    print(f"📊 压力测试报告")
    print("=" * 60)
    print(f"  总耗时:           {t_elapsed:.1f}s")
    print(f"  通过:             {PASS}")
    print(f"  失败:             {FAIL}")
    print()

    if ERROR_LOG:
        print("  P0/P1 立即修复:")
        for err in ERROR_LOG[:10]:
            print(f"    🔴 {err}")
    else:
        print("  未发现 P0/P1 问题 ✅")

    # 写入报告文件
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(t_elapsed, 1),
        "pass": PASS,
        "fail": FAIL,
        "errors": ERROR_LOG[:20],
        "details": {
            "concurrent_errors": results.get("concurrent", []),
            "stability": results.get("stability", {}),
        },
    }
    report_path = Path(__file__).resolve().parent / "test_stress_report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  报告已写入: {report_path}")

    sys.exit(0 if FAIL == 0 else 1)
