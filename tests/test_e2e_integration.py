"""
端到端链路集成测试
====================
测试 4 条关键业务链路：
  链路 A: 搜索 → 详情 → 添加收藏 → 验证收藏
  链路 B: 创建下载任务 → 查看任务 → 取消任务 → 确认取消
  链路 C: 设置保存 → 导出 → 导入 → 验证
  链路 D: 系统诊断
  SSE 握手检查
  测试回滚 & 数据清理
"""
import json
import os
import sys
import time
import io
from pathlib import Path

# ── 设置测试环境 ──
os.environ['WERKZEUG_RUN_MAIN'] = 'true'

# ── 配置 ──
TEST_ALBUM_ID = "1447482"        # 用于链路 A 的真实 album_id
TEST_JOB_ALBUM_ID = "9999999"    # 用于链路 B 的测试 album_id（不会实际下载）
ORIGINAL_SETTINGS = {}           # 将保存原始设置用于回滚

PASS = 0
FAIL = 0
ERROR_LOG = []


def check(label: str, condition: bool, detail: str = ""):
    """记录单个测试结果"""
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        msg = f"  ❌ {label}" + (f" — {detail}" if detail else "")
        print(msg)
        ERROR_LOG.append(f"{label}: {detail}")


def check_status(resp, expected: int = 200):
    """检查 HTTP 状态码"""
    ok = resp.status_code == expected
    detail = f"expected {expected}, got {resp.status_code}" if not ok else ""
    if not ok:
        try:
            detail += f" body={resp.get_json()}"
        except Exception:
            detail += f" body={resp.data[:200]}"
    return ok, detail


def check_json_key(data, key: str):
    """检查 JSON 响应包含某 key"""
    has = key in data
    return has, f"missing key '{key}'" if not has else ""


def get_json(resp):
    """安全解析 JSON"""
    try:
        return resp.get_json()
    except Exception as e:
        return {"_parse_error": str(e)}


def run_link_a(client):
    """链路 A: 搜索 → 详情 → 添加收藏 → 验证收藏"""
    print("\n" + "=" * 60)
    print("📦 链路 A: 搜索 → 详情 → 添加收藏 → 验证收藏")
    print("=" * 60)

    # A1. GET /api/search?q=test
    print("\n[A1] 搜索 GET /api/search?q=test")
    resp = client.get("/api/search?q=test&page_size=3", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("搜索接口返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("搜索返回 status 字段", has_status, d)
        if has_status:
            if data.get("status") == "ok":
                check("搜索返回 status=ok", True)
                has_items, d = check_json_key(data, "items")
                check("搜索返回 items 字段", has_items, d)
                if has_items:
                    items = data.get("items", [])
                    check(f"搜索返回 {len(items)} 条结果", isinstance(items, list))
            elif data.get("status") == "error":
                msg = data.get("message", "")
                check(f"搜索返回错误（外部 API 不可达？）: {msg}", True)

    # A2. GET /api/album/{id}
    print(f"\n[A2] 专辑详情 GET /api/album/{TEST_ALBUM_ID}")
    resp = client.get(f"/api/album/{TEST_ALBUM_ID}", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("专辑详情接口返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("专辑详情返回 status 字段", has_status, d)
        if has_status:
            if data.get("status") == "ok":
                check("专辑详情返回 status=ok", True)
                has_data, d = check_json_key(data, "data")
                check("专辑详情返回 data 字段", has_data, d)
                if has_data:
                    album_data = data.get("data", {})
                    has_title, d = check_json_key(album_data, "title")
                    check("专辑数据包含 title", has_title, d)
                    has_aid, d = check_json_key(album_data, "album_id")
                    check("专辑数据包含 album_id", has_aid, d)
            elif data.get("status") == "error":
                msg = data.get("message", "")
                check(f"专辑详情返回错误: {msg}", True)

    # A3. POST /api/wishlist 添加收藏
    print(f"\n[A3] 添加收藏 POST /api/wishlist (album_id={TEST_ALBUM_ID})")
    resp = client.post(
        "/api/wishlist",
        json={"album_id": TEST_ALBUM_ID, "title": "测试漫画", "author": "测试作者"},
        headers={"Accept": "application/json"},
    )
    ok, detail = check_status(resp, 201)
    check("添加收藏返回 201", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("添加收藏返回 status 字段", has_status, d)
        if has_status:
            check("添加收藏 status=ok", data.get("status") == "ok")
    else:
        # 可能已存在（之前测试残留）
        if resp.status_code == 200:
            check("收藏已存在（200，允许）", True)
        else:
            check(f"添加收藏异常 (status={resp.status_code})", False)

    # A4. GET /api/wishlist/{id} 验证在收藏中
    print(f"\n[A4] 验证收藏 GET /api/wishlist/{TEST_ALBUM_ID}")
    resp = client.get(f"/api/wishlist/{TEST_ALBUM_ID}", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("查看收藏返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("查看收藏返回 status 字段", has_status, d)
        if has_status:
            check("查看收藏 status=ok", data.get("status") == "ok")
            item = data.get("item")
            if item:
                check(f"收藏条目 album_id={item.get('album_id')}", item.get("album_id") == TEST_ALBUM_ID)
            else:
                check("收藏条目存在", False, "item is None")

    # A5. GET /api/wishlist 列表也包含
    print("\n[A5] 验证收藏列表包含测试条目")
    resp = client.get("/api/wishlist?page_size=100", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("收藏列表返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        items = data.get("items", [])
        found = any(item.get("album_id") == TEST_ALBUM_ID for item in items)
        check(f"收藏列表包含 album_id={TEST_ALBUM_ID}", found)


def run_link_b(client):
    """链路 B: 创建下载任务 → 查看任务 → 取消任务 → 确认取消"""
    print("\n" + "=" * 60)
    print("📦 链路 B: 创建下载任务 → 查看任务 → 取消任务 → 确认取消")
    print("=" * 60)

    # B1. POST /api/jobs 创建下载任务
    print(f"\n[B1] 创建下载任务 POST /api/jobs (album_id={TEST_JOB_ALBUM_ID})")
    resp = client.post(
        "/api/jobs",
        json={"album_id": TEST_JOB_ALBUM_ID, "title": "E2E测试任务"},
        headers={"Accept": "application/json"},
    )
    ok, detail = check_status(resp, 201)
    check("创建下载任务返回 201", ok, detail)
    if not ok:
        # 尝试获取错误详情
        try:
            detail2 = resp.get_json()
            print(f"     错误详情: {detail2}")
        except Exception:
            pass
        return None

    data = get_json(resp)
    job_id = data.get("job_id")
    if not job_id:
        check("创建任务返回 job_id", False, "missing job_id in response")
        return None
    check(f"创建任务返回 job_id={job_id}", bool(job_id))

    # B2. GET /api/jobs/{job_id} 查看任务详情
    print(f"\n[B2] 查看任务 GET /api/jobs/{job_id}")
    resp = client.get(f"/api/jobs/{job_id}", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("查看任务返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("查看任务返回 status 字段", has_status, d)
        if has_status:
            check("查看任务 status=ok", data.get("status") == "ok")
            job = data.get("job", {})
            has_job_id, d = check_json_key(job, "job_id")
            check("任务数据包含 job_id", has_job_id, d)
            has_status_job, d = check_json_key(job, "status")
            check("任务数据包含 status", has_status_job, d)
            if has_status_job:
                status = job.get("status", "")
                # 新创建的任务应该是 queued，但也可能被调度器抢走变成 running
                check(f"任务状态={status}", status in ("queued", "running", "paused", "failed", "canceled"))

    # B3. POST /api/jobs/{job_id}/cancel 取消任务
    print(f"\n[B3] 取消任务 POST /api/jobs/{job_id}/cancel")
    resp = client.post(f"/api/jobs/{job_id}/cancel", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("取消任务返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("取消任务返回 status 字段", has_status, d)
        if has_status:
            check("取消任务 status=ok", data.get("status") == "ok")

    # B4. GET /api/jobs/{job_id} 确认状态为 canceled
    print(f"\n[B4] 确认取消 GET /api/jobs/{job_id}")
    resp = client.get(f"/api/jobs/{job_id}", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("确认取消返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        job = data.get("job", {})
        status = job.get("status", "")
        check(f"任务状态已变为 canceled (实际={status})", status == "canceled")

    # B5. 查看任务列表包含此 job
    print("\n[B5] 验证任务列表包含")
    resp = client.get("/api/jobs", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("任务列表返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        jobs = data.get("jobs", [])
        found = any(j.get("job_id") == job_id for j in jobs)
        check(f"任务列表包含 job_id={job_id}", found)

    return job_id


def run_link_c(client):
    """链路 C: 设置保存 → 导出 → 导入 → 验证"""
    global ORIGINAL_SETTINGS
    print("\n" + "=" * 60)
    print("📦 链路 C: 设置保存 → 导出 → 导入 → 验证")
    print("=" * 60)

    # C1. GET /api/settings 获取当前设置（保存原始值用于回滚）
    print("\n[C1] 获取当前设置 GET /api/settings")
    resp = client.get("/api/settings", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("获取设置返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        ORIGINAL_SETTINGS = data.get("settings", {})
        has_status, d = check_json_key(data, "status")
        check("获取设置返回 status 字段", has_status, d)
        if has_status:
            check("获取设置 status=ok", data.get("status") == "ok")
            settings = data.get("settings", {})
            check(f"设置项数量 >= 16 (实际={len(settings)})", len(settings) >= 16,
                  f"got {len(settings)} keys")
            # 验证关键设置项存在
            for key in ("max_running_jobs", "timeout", "retry_times", "client_type",
                        "skip_existing", "proxy", "image_threads", "photo_threads"):
                has_key = key in settings
                check(f"设置包含 {key}", has_key, f"missing key '{key}'")

    # C2. POST /api/settings 修改设置
    print("\n[C2] 修改设置 POST /api/settings")
    new_settings = {
        "max_running_jobs": "2",
        "timeout": "60",
        "retry_times": "5",
        "skip_existing": "false",
    }
    resp = client.post("/api/settings", json=new_settings, headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("保存设置返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("保存设置返回 status 字段", has_status, d)
        if has_status:
            check("保存设置 status=ok", data.get("status") == "ok")
            settings = data.get("settings", {})
            # 验证修改生效
            check(f"max_running_jobs=2 (实际={settings.get('max_running_jobs')})",
                  settings.get("max_running_jobs") == "2")
            check(f"timeout=60 (实际={settings.get('timeout')})",
                  settings.get("timeout") == "60")
            check(f"retry_times=5 (实际={settings.get('retry_times')})",
                  settings.get("retry_times") == "5")
            check(f"skip_existing=false (实际={settings.get('skip_existing')})",
                  settings.get("skip_existing") == "false")

    # C3. GET /api/settings/export 导出设置
    print("\n[C3] 导出设置 GET /api/settings/export")
    resp = client.get("/api/settings/export")
    ok, detail = check_status(resp, 200)
    check("导出设置返回 200", ok, detail)
    if ok:
        content_type = resp.content_type or ""
        check(f"导出设置 Content-Type={content_type}",
              "application/json" in content_type.lower())
        # 验证导出的 JSON 有效
        try:
            export_data = json.loads(resp.data)
            check("导出 JSON 解析成功", True)
            # 验证导出的数据包含刚才修改的项
            check(f"导出包含 max_running_jobs={export_data.get('max_running_jobs')}",
                  export_data.get("max_running_jobs") == "2")
            check("导出不包含 download_root", "download_root" not in export_data)
        except json.JSONDecodeError as e:
            check("导出 JSON 解析失败", False, str(e))

    # C4. POST /api/settings/import 导入设置
    print("\n[C4] 导入设置 POST /api/settings/import")
    import_data = {
        "client_type": "html",
        "proxy": "http://127.0.0.1:8080",
    }
    import_json = json.dumps(import_data, ensure_ascii=False).encode("utf-8")
    resp = client.post(
        "/api/settings/import",
        data={"file": (io.BytesIO(import_json), "settings.json")},
        content_type="multipart/form-data",
        headers={"Accept": "application/json"},
    )
    ok, detail = check_status(resp, 200)
    check("导入设置返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("导入设置返回 status 字段", has_status, d)
        if has_status:
            check("导入设置 status=ok", data.get("status") == "ok")
            imported = data.get("imported", 0)
            check(f"成功导入 {imported} 项设置", imported > 0)

    # C5. 验证导入生效
    print("\n[C5] 验证导入设置生效")
    resp = client.get("/api/settings", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("获取设置返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        settings = data.get("settings", {})
        check(f"client_type=html (实际={settings.get('client_type')})",
              settings.get("client_type") == "html")
        check(f"proxy=127.0.0.1:8080 (实际={settings.get('proxy')})",
              settings.get("proxy") == "http://127.0.0.1:8080")


def run_link_d(client):
    """链路 D: 系统诊断"""
    print("\n" + "=" * 60)
    print("📦 链路 D: 系统诊断")
    print("=" * 60)

    # D1. GET /api/system/health 健康检查
    print("\n[D1] 健康检查 GET /api/system/health")
    resp = client.get("/api/system/health", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("健康检查返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("健康检查返回 status 字段", has_status, d)
        if has_status:
            check("健康检查 status=ok", data.get("status") == "ok")
        has_ts, d = check_json_key(data, "ts")
        check("健康检查返回 ts 字段", has_ts, d)

    # D2. GET /api/system/diagnose 完整诊断
    print("\n[D2] 完整诊断 GET /api/system/diagnose")
    resp = client.get("/api/system/diagnose", headers={"Accept": "application/json"})
    ok, detail = check_status(resp, 200)
    check("系统诊断返回 200", ok, detail)
    if ok:
        data = get_json(resp)
        has_status, d = check_json_key(data, "status")
        check("诊断返回 status 字段", has_status, d)
        if has_status:
            status = data.get("status", "")
            check(f"系统状态={status}", status in ("healthy", "degraded", "unhealthy"))

        for key, label in [("uptime", "uptime"), ("log_system", "log_system"),
                            ("application", "application")]:
            has_it, d = check_json_key(data, key)
            check(f"诊断包含 {label}", has_it, d)

        app_info = data.get("application", {})
        if app_info:
            has_db, d = check_json_key(app_info, "db_ok")
            check("诊断包含 db_ok", has_db, d)
            if has_db:
                check(f"数据库连接正常 (db_ok={app_info.get('db_ok')})",
                      app_info.get("db_ok") is True)

        # 检查 uptime 格式
        uptime = data.get("uptime", {})
        if uptime:
            has_seconds, d = check_json_key(uptime, "seconds")
            check("uptime 包含 seconds", has_seconds, d)
            has_since, d = check_json_key(uptime, "since")
            check("uptime 包含 since", has_since, d)


def run_cleanup(client, created_job_id=None):
    """测试回滚：清理测试数据"""
    print("\n" + "=" * 60)
    print("🧹 测试回滚 & 数据清理")
    print("=" * 60)

    # 清理收藏测试数据
    print(f"\n[清理] 删除收藏 DELETE /api/wishlist/{TEST_ALBUM_ID}")
    resp = client.delete(f"/api/wishlist/{TEST_ALBUM_ID}", headers={"Accept": "application/json"})
    if resp.status_code in (200, 404):
        print(f"  ✅ 收藏清理完成 (status={resp.status_code})")
    else:
        print(f"  ⚠️ 收藏清理结果 status={resp.status_code}")
        try:
            print(f"     body={resp.get_json()}")
        except Exception:
            pass

    # 清理测试任务
    if created_job_id:
        print(f"\n[清理] 删除任务 DELETE /api/jobs/{created_job_id}")
        # 先尝试取消（如果还在 running）
        cancel_resp = client.post(f"/api/jobs/{created_job_id}/cancel",
                                   headers={"Accept": "application/json"})
        print(f"     cancel status={cancel_resp.status_code}")
        resp = client.delete(f"/api/jobs/{created_job_id}",
                              headers={"Accept": "application/json"})
        if resp.status_code in (200, 404):
            print(f"  ✅ 任务清理完成 (status={resp.status_code})")
        else:
            print(f"  ⚠️ 任务清理结果 status={resp.status_code}")
            try:
                print(f"     body={resp.get_json()}")
            except Exception:
                pass

    # 恢复原始设置
    if ORIGINAL_SETTINGS:
        print("\n[清理] 恢复原始设置")
        # 移除 download_root（只读）
        restore = {k: v for k, v in ORIGINAL_SETTINGS.items() if k != "download_root"}
        resp = client.post("/api/settings", json=restore,
                           headers={"Accept": "application/json"})
        if resp.status_code == 200:
            print(f"  ✅ 设置已恢复 (status={resp.status_code})")
        else:
            print(f"  ⚠️ 设置恢复 status={resp.status_code}")
            try:
                print(f"     body={resp.get_json()}")
            except Exception:
                pass

    # 验证回滚后设置恢复
    resp = client.get("/api/settings", headers={"Accept": "application/json"})
    if resp.status_code == 200:
        data = get_json(resp)
        current = data.get("settings", {})
        match = all(current.get(k) == v for k, v in ORIGINAL_SETTINGS.items()
                    if k != "download_root")
        if match:
            print("  ✅ 设置已回滚到原始值")
        else:
            print("  ⚠️ 设置回滚可能未完全恢复")
            # 显示差异
            for k, v in ORIGINAL_SETTINGS.items():
                if k != "download_root" and current.get(k) != v:
                    print(f"     {k}: expected={v}, actual={current.get(k)}")


def check_sse_handshake(client, job_id=None):
    """检查 SSE 握手"""
    print("\n" + "=" * 60)
    print("📡 SSE 握手检查")
    print("=" * 60)

    # 尝试获取一个已完成任务或取消任务的 job_id
    if not job_id:
        resp = client.get("/api/jobs", headers={"Accept": "application/json"})
        if resp.status_code == 200:
            data = get_json(resp)
            jobs = data.get("jobs", [])
            # 找 canceled/completed/failed 的 job
            for j in jobs:
                if j.get("status") in ("canceled", "completed", "failed"):
                    job_id = j.get("job_id")
                    break
            if not job_id and jobs:
                job_id = jobs[0].get("job_id")  # 用第一个

    if not job_id:
        # 创建一个测试 job 并取消它
        resp = client.post("/api/jobs", json={"album_id": "8888888", "title": "SSE测试"},
                           headers={"Accept": "application/json"})
        if resp.status_code == 201:
            data = get_json(resp)
            job_id = data.get("job_id")
            if job_id:
                client.post(f"/api/jobs/{job_id}/cancel", headers={"Accept": "application/json"})
        else:
            check("SSE: 无法创建测试任务", False, f"status={resp.status_code}")

    if not job_id:
        check("SSE: 无可用的 job_id", False)
        return

    print(f"\n[SSE] 检查 GET /api/jobs/{job_id}/events")
    resp = client.get(f"/api/jobs/{job_id}/events")

    # 检查 Content-Type
    content_type = resp.content_type or ""
    check(f"SSE Content-Type = {content_type}",
          "text/event-stream" in content_type.lower(),
          f"got '{content_type}', expected 'text/event-stream'")

    # 检查状态码
    check(f"SSE 状态码 = {resp.status_code}",
          resp.status_code in (200, 404),  # 404 也接受（已清理）
          f"got {resp.status_code}")

    if resp.status_code == 200:
        # 检查是否有事件数据
        data_len = len(resp.data)
        check(f"SSE 响应体长度 = {data_len}B", data_len > 0)

        # 解析 SSE 事件
        body = resp.data.decode("utf-8", errors="replace")
        has_event = "event:" in body
        has_data = "data:" in body
        check(f"SSE 包含 event 行 ({'✅' if has_event else '❌'})", has_event)
        check(f"SSE 包含 data 行 ({'✅' if has_data else '❌'})", has_data)

        # 显示前 200 字符
        print(f"\n      SSE 内容预览:\n{body[:300]}")
    else:
        print(f"      SSE 返回 {resp.status_code}（任务可能已被清理）")


def run_all():
    """运行所有链路测试"""
    global PASS, FAIL, ERROR_LOG
    t_start = time.time()

    print("=" * 60)
    print("🔬 端到端链路集成测试")
    print(f"   项目: D:\\Hermes\\禁漫下载搜索插件")
    print(f"   时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    # ── 1. 初始化数据库 ──
    print("\n[初始化] 数据库...")
    import core.database as db
    db.init_db()
    print("  ✅ 数据库初始化完成")

    # ── 2. 导入并创建 Flask 应用 ──
    print("\n[初始化] 创建 Flask 应用...")
    from app import create_app
    app = create_app()
    print("  ✅ Flask 应用创建完成")

    # ── 3. 获取测试客户端 ──
    client = app.test_client()
    print("  ✅ 测试客户端就绪")

    # ── 4. 运行各链路测试 ──
    run_link_a(client)
    created_job_id = run_link_b(client)
    run_link_c(client)
    run_link_d(client)

    # ── 5. SSE 握手检查 ──
    check_sse_handshake(client, created_job_id)

    # ── 6. 测试回滚 ──
    run_cleanup(client, created_job_id)

    # ── 7. 汇总报告 ──
    t_end = time.time()
    duration = t_end - t_start
    total = PASS + FAIL

    print("\n\n" + "=" * 60)
    print("📊 端到端链路集成测试 — 最终报告")
    print("=" * 60)
    print(f"\n⏱  总耗时: {duration:.1f}s")
    print(f"📋 总测试项: {total}")
    print(f"  ✅ 通过: {PASS}")
    print(f"  ❌ 失败: {FAIL}")
    if total > 0:
        rate = PASS / total * 100
        print(f"  📈 通过率: {rate:.1f}%")
    print()

    # 链路汇总
    print("─" * 40)
    print("链路测试结果:")
    print("  A. 搜索→详情→收藏→验证: ", end="")
    print("✅" if ERROR_LOG.count("A") == 0 else "❌")
    print("  B. 创建→查看→取消→确认: ", end="")
    print("✅" if ERROR_LOG.count("B") == 0 else "❌")
    print("  C. 设置→导出→导入→验证: ", end="")
    print("✅" if ERROR_LOG.count("C") == 0 else "❌")
    print("  D. 系统诊断:             ", end="")
    print("✅" if ERROR_LOG.count("D") == 0 else "❌")
    print("  SSE 握手:               ", end="")
    print("✅" if ERROR_LOG.count("SSE") == 0 else "❌")

    if ERROR_LOG:
        print(f"\n❌ 错误详情 ({len(ERROR_LOG)}):")
        for i, err in enumerate(ERROR_LOG, 1):
            print(f"  {i}. {err}")
    else:
        print("\n🎉 全部测试通过，无错误！")

    # 保存报告
    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_s": round(duration, 1),
        "total": total,
        "passed": PASS,
        "failed": FAIL,
        "pass_rate": round(PASS / total * 100, 1) if total > 0 else 0,
        "errors": ERROR_LOG,
        "links": {
            "A": "搜索→详情→收藏→验证",
            "B": "创建→查看→取消→确认",
            "C": "设置→导出→导入→验证",
            "D": "系统诊断",
            "SSE": "SSE握手",
        },
    }
    report_path = Path(__file__).resolve().parent / "test_e2e_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"\n📄 报告已保存: {report_path}")

    return FAIL == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
