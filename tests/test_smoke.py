"""
集成冒烟测试 + 性能基准测定
涵盖：应用启动、页面路由、API 端点、错误页面、边界测试、性能基准
"""
import sys
import os
import time
import json

sys.path.insert(0, '.')
os.environ['WERKZEUG_RUN_MAIN'] = 'true'

print("=" * 72)
print("  集成冒烟测试 + 性能基准测定")
print("=" * 72)

# ── 1. 应用启动测试 ──────────────────────────────────────────
print("\n## 1. 应用启动测试")
t0 = time.time()
from app import create_app
app = create_app()
t1 = time.time()
boot_ms = (t1 - t0) * 1000
print(f"   启动耗时: {boot_ms:.0f}ms")
print()

# 提前注册测试路由（必须在第一次请求之前）
from flask import abort

@app.route('/test-500-smoke')
def trigger_500_handler():
    abort(500)

results = []
slow_routes = []
errors = []

# ── 2. 页面路由测试 ──────────────────────────────────────────
print("## 2. 页面路由测试")
page_routes = [
    '/',
    '/search',
    '/downloads',
    '/wishlist',
    '/library',
    '/settings',
    '/album/123456',
    '/preview/123456',
]

with app.test_client() as c:
    for path in page_routes:
        t = time.time()
        resp = c.get(path)
        dt = (time.time() - t) * 1000
        status = resp.status_code
        size = len(resp.data)
        results.append((path, 'GET', status, dt, size))
        status_str = '✅' if status == 200 else '❌'
        slow_mark = ' ⚠️ SLOW' if dt > 500 else ''
        print(f"   {status_str} {dt:6.0f}ms {status:3d} {size:6d}B {path}{slow_mark}")
        if status != 200:
            errors.append(f"[PAGE] {path} returned {status}")
        if dt > 500:
            slow_routes.append((path, 'GET', dt))

# ── 3. API 端点测试 ──────────────────────────────────────────
print("\n## 3. API 端点测试")
api_routes = [
    ('/api/search?q=test', 'search'),
    ('/api/album/123456', 'album detail'),
    ('/api/downloads', 'downloads (route does not exist → 404)'),
    ('/api/jobs', 'jobs'),
    ('/api/wishlist', 'wishlist'),
    ('/api/library', 'library'),
    ('/api/settings', 'settings'),
    ('/api/system/health', 'health'),
]

with app.test_client() as c:
    for path, label in api_routes:
        t = time.time()
        resp = c.get(path)
        dt = (time.time() - t) * 1000
        status = resp.status_code
        size = len(resp.data)
        is_json = resp.content_type and 'application/json' in resp.content_type
        results.append((path, 'GET', status, dt, size))

        if path == '/api/downloads':
            ok = (status == 404 and is_json)
            if not ok:
                errors.append(f"[API] {path} returned {status} (expected 404 JSON)")
        else:
            ok = (status in (200, 502) and is_json)
            if not ok:
                errors.append(f"[API] {path} returned {status} (expected 200 or 502 JSON)")
        if not is_json:
            errors.append(f"[API] {path} content-type is not JSON: {resp.content_type}")
        json_str = ' JSON' if is_json else ''
        status_str = '✅' if ok else '❌'
        slow_mark = ' ⚠️ SLOW' if dt > 500 else ''
        print(f"   {status_str} {dt:6.0f}ms {status:3d} {size:6d}B {path}{json_str}{slow_mark}")
        if dt > 500:
            slow_routes.append((path, 'GET', dt))

# ── 4. 404/403/500 渲染检查 ──────────────────────────────────
print("\n## 4. 错误页面渲染检查")
with app.test_client() as c:
    # 4a. 自定义 404 页面
    t = time.time()
    resp = c.get('/nonexistent')
    dt = (time.time() - t) * 1000
    data = resp.data.decode('utf-8')
    has_nav = ('nav' in data.lower() or '导航' in data or 'navbar' in data.lower())
    has_back = ('返回' in data or 'javascript:history.back' in data or 'javascript:history.go' in data)
    status_icon = '✅' if resp.status_code == 404 else '❌'
    nav_icon = '✅' if has_nav else '❌'
    back_icon = '✅' if has_back else '❌'
    print(f"   {status_icon} 404 | {resp.status_code} | {dt:6.0f}ms | {len(resp.data):6d}B | nav={nav_icon} back={back_icon}")
    if resp.status_code != 404:
        errors.append(f"[404] /nonexistent returned {resp.status_code} (expected 404)")
    if not has_nav:
        errors.append("[404] custom 404 page missing navigation bar")
    if not has_back:
        errors.append("[404] custom 404 page missing back button")

    # 4b. API 404
    t = time.time()
    resp = c.get('/api/nonexistent')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    status_icon = '✅' if (resp.status_code == 404 and is_json) else '❌'
    print(f"   {status_icon} API 404 | {resp.status_code} | {dt:6.0f}ms | {len(resp.data):6d}B | json={'✅' if is_json else '❌'}")
    if resp.status_code != 404 or not is_json:
        errors.append(f"[API 404] /api/nonexistent returned {resp.status_code} (expected 404 JSON)")

    # 4c. 500 错误页面
    t = time.time()
    resp = c.get('/test-500-smoke')
    dt = (time.time() - t) * 1000
    data_500 = resp.data.decode('utf-8')
    has_nav_500 = ('nav' in data_500.lower() or '导航' in data_500 or 'navbar' in data_500.lower())
    status_icon = '✅' if resp.status_code == 500 else '❌'
    print(f"   {status_icon} 500 | {resp.status_code} | {dt:6.0f}ms | {len(resp.data):6d}B | nav={'✅' if has_nav_500 else '❌'}")
    if resp.status_code != 500:
        errors.append(f"[500] /test-500-smoke returned {resp.status_code} (expected 500)")
    if not has_nav_500:
        errors.append("[500] custom 500 page missing navigation bar")

    # 4d. API 500 错误
    t = time.time()
    resp = c.get('/api/test-500-smoke')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    print(f"   API 500 | {resp.status_code} | {dt:6.0f}ms | {len(resp.data):6d}B | json={'✅' if is_json else '❌'}")

# ── 5. 边界测试 ──────────────────────────────────────────────
print("\n## 5. 边界测试")
with app.test_client() as c:
    # 5a. 空关键词搜索
    t = time.time()
    resp = c.get('/api/search?q=')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    ok = resp.status_code in (200, 422, 400)
    status_str = '✅' if (ok and is_json) else '❌'
    print(f"   {status_str} {dt:6.0f}ms {resp.status_code:3d} {len(resp.data):6d}B 空关键词 /api/search?q=")
    if resp.status_code == 500:
        errors.append("[BOUNDARY] Empty keyword search returned 500")
    if not is_json:
        errors.append("[BOUNDARY] Empty keyword search didn't return JSON")

    # 5b. 超大 page 号
    t = time.time()
    resp = c.get('/api/search?q=test&page=999999')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    ok = resp.status_code in (200, 400, 422)
    status_str = '✅' if (ok and is_json) else '❌'
    print(f"   {status_str} {dt:6.0f}ms {resp.status_code:3d} {len(resp.data):6d}B 超大page=999999")
    if resp.status_code == 500:
        errors.append("[BOUNDARY] Large page number returned 500")

    # 5c. 无效 album_id
    t = time.time()
    resp = c.get('/api/album/abc')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    ok = resp.status_code in (200, 400, 404, 422)
    status_str = '✅' if (ok and is_json) else '❌'
    print(f"   {status_str} {dt:6.0f}ms {resp.status_code:3d} {len(resp.data):6d}B 无效album_id=abc")
    if resp.status_code == 500:
        errors.append("[BOUNDARY] Invalid album_id 'abc' returned 500")

    # 5d. 超大 page_size
    t = time.time()
    resp = c.get('/api/search?q=test&page_size=99999')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    ok = resp.status_code in (200, 400, 422)
    status_str = '✅' if (ok and is_json) else '❌'
    print(f"   {status_str} {dt:6.0f}ms {resp.status_code:3d} {len(resp.data):6d}B 超大page_size=99999")
    if resp.status_code == 500:
        errors.append("[BOUNDARY] Large page_size returned 500")

    # 5e. 超长搜索词 (DoS 保护测试)
    t = time.time()
    long_q = 'a' * 300
    resp = c.get(f'/api/search?q={long_q}')
    dt = (time.time() - t) * 1000
    is_json = resp.content_type and 'application/json' in resp.content_type
    ok = resp.status_code in (200, 400)
    status_str = '✅' if (ok and is_json) else '❌'
    print(f"   {status_str} {dt:6.0f}ms {resp.status_code:3d} {len(resp.data):6d}B 超长搜索词(300chars)")
    if resp.status_code == 500:
        errors.append("[BOUNDARY] Long keyword search returned 500")

    # 5f. 空 album_id 页面路由
    t = time.time()
    resp = c.get('/album/')
    dt = (time.time() - t) * 1000
    ok = resp.status_code in (200, 404)
    print(f"   {'✅' if ok else '❌'} {dt:6.0f}ms {resp.status_code:3d} {len(resp.data):6d}B 空album_id /album/")

# ── 6. 结果汇总 ──────────────────────────────────────────────
print("\n" + "=" * 72)
print("  测试结果汇总")
print("=" * 72)

print(f"\n📊 启动耗时: {boot_ms:.0f}ms {'✅' if boot_ms < 2000 else '⚠️ 超过 2s'}")
print(f"📊 路由测试数: {len(results)}")
print(f"📊 错误数量: {len(errors)}")
print(f"📊 慢路由数 (>500ms): {len(slow_routes)}")

print(f"\n{'─' * 72}")
print(f"{'路径':42s} {'方法':6s} {'状态':5s} {'耗时':8s} {'大小':8s}")
print(f"{'─' * 72}")
for path, method, status, dt, size in results:
    slow = ' ⚠️' if dt > 500 else ''
    print(f"{path:42s} {method:6s} {status:5d} {dt:6.0f}ms {size:7d}B{slow}")

if slow_routes:
    print(f"\n{'─' * 72}")
    print("🐌 慢路由分析 (>500ms)")
    print(f"{'─' * 72}")
    for path, method, dt in sorted(slow_routes, key=lambda x: -x[2]):
        level = '🔴 CRITICAL' if dt > 2000 else '🟡 WARN' if dt > 1000 else '🔵 SLOW'
        print(f"   {level} {dt:6.0f}ms | {method:6s} {path}")

if errors:
    print(f"\n{'─' * 72}")
    print("❌ 发现问题清单")
    print(f"{'─' * 72}")
    for i, err in enumerate(errors, 1):
        print(f"   {i}. {err}")

print(f"\n{'─' * 72}")
total_tests = len(results) + 1  # +1 for boot
passed = total_tests - len(errors)
pct = round(passed / total_tests * 100, 1) if total_tests > 0 else 0
print(f"   总计: {total_tests} 项测试 | ✅ 通过: {passed} | ❌ 失败: {len(errors)} | 通过率: {pct}%")
print(f"{'─' * 72}")

# Save JSON report
report_path = os.path.join(os.path.dirname(__file__) or '.', 'test_smoke_report.json')
report = {
    'boot_ms': round(boot_ms, 1),
    'routes': [{'path': p, 'method': m, 'status': s, 'duration_ms': round(d, 1), 'size': sz}
                for p, m, s, d, sz in results],
    'errors': errors,
    'slow_routes': [{'path': p, 'method': m, 'duration_ms': round(d, 1)}
                     for p, m, d in slow_routes],
    'total_tests': total_tests,
    'passed': passed,
    'failed': len(errors),
    'pass_rate': pct,
}
with open(report_path, 'w', encoding='utf-8') as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"\n📄 报告已保存: {report_path}")
