"""
测试数据导出功能完整性
涵盖：ZIP 导出、PDF 导出、收藏导出/导入、设置导出/导入
"""
import io
import json
import os
import sys
import time
import re
from pathlib import Path
from datetime import datetime

sys.path.insert(0, '.')
os.environ['WERKZEUG_RUN_MAIN'] = 'true'

# ── 创建临时测试数据目录 ──
TEST_DATA_DIR = Path(__file__).resolve().parent / "_test_export_data"
TEST_DOWNLOAD_DIR = Path(__file__).resolve().parent / "downloads" / "test_export_album"
TEST_JOB_ID = "test_export_integrity_001"
TEST_ALBUM_ID = "99999999"
TEST_TITLE = "测试漫画中文标题_Test!"

results = []
errors = []
issues = []  # P0/P1/P2 issues


def report(label, passed, detail=""):
    icon = "✅" if passed else "❌"
    results.append(f"  {icon} {label}")
    if detail:
        results.append(f"      {detail}")
    return passed


def add_issue(severity: str, title: str, detail: str = ""):
    """P0=严重, P1=中等, P2=轻微"""
    issues.append({"severity": severity, "title": title, "detail": detail})


def setup_test_data():
    """创建测试需要的目录结构和图片文件"""
    TEST_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for i in range(1, 4):
        f = TEST_DOWNLOAD_DIR / f"page_{i:03d}.jpg"
        if not f.exists():
            # Use Pillow if available, otherwise write a minimal PNG
            try:
                from PIL import Image
                img = Image.new('RGB', (100, 100), color='white')
                img.save(f, 'JPEG')
            except ImportError:
                # Minimal valid JPEG (1x1 white pixel)
                f.write_bytes(
                    b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00'
                    b'\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08'
                    b'\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e'
                    b'\x1d\x1a\x1c\x1c $.\x27 ",#\x1c\x1c(7),01444\x1f\x27'
                    b'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00'
                    b'\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00'
                    b'\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b'
                    b'\xff\xc4\x00\xb5\x10\x00\x02\x01\x03\x03\x02\x04\x03\x05\x05\x04'
                    b'\x04\x00\x00\x00\x00\x00\x00\x01\x02\x03\x11\x04\x12!1\x06\x13Q'
                    b'\x07a\x08"q\x142\x81\x91\xa1\xb1\xc1\t#3R\x15\xf0\x16$Br\x82'
                    b'\x17\xd1%\x26\x27()*456789:CDEFGHIJSTUVWXYZcdefghijstuvwxyz'
                    b'\x83\x84\x85\x86\x87\x88\x89\x8a\x92\x93\x94\x95\x96\x97\x98\x99'
                    b'\x9a\xa2\xa3\xa4\xa5\xa6\xa7\xa8\xa9\xaa\xb2\xb3\xb4\xb5\xb6\xb7'
                    b'\xb8\xb9\xba\xc2\xc3\xc4\xc5\xc6\xc7\xc8\xc9\xca\xd2\xd3\xd4\xd5'
                    b'\xd6\xd7\xd8\xd9\xda\xe1\xe2\xe3\xe4\xe5\xe6\xe7\xe8\xe9\xea\xf1'
                    b'\xf2\xf3\xf4\xf5\xf6\xf7\xf8\xf9\xfa\xff\xc0\x00\x0f\x01\x01\x01'
                    b'\x01\x01\x01\x01\x01\x01\x00\xff\xc4\x00\x1f\x01\x01\x01\x01\x01'
                    b'\x01\x01\x01\x01\x01\x00\xff\xff\xd9'
                )
    # Create a non-image file to test ZIP includes everything
    (TEST_DOWNLOAD_DIR / "info.txt").write_text("测试信息文件")
    report("测试数据设置", True, f"在 {TEST_DOWNLOAD_DIR} 创建了 3 张图片 + 1 个文本文件")


def cleanup_test_data():
    """清理测试数据"""
    import shutil
    if TEST_DATA_DIR.exists():
        shutil.rmtree(TEST_DATA_DIR)
    if TEST_DOWNLOAD_DIR.exists():
        shutil.rmtree(TEST_DOWNLOAD_DIR)


def inject_test_job():
    """在数据库中插入一个测试任务（completed 状态），指向我们创建的测试输出目录"""
    from core import database as db
    now = datetime.now().isoformat()
    conn = db.get_db()
    try:
        # 先删除可能存在的同名 job
        conn.execute("DELETE FROM jobs WHERE job_id=?", (TEST_JOB_ID,))
        conn.execute(
            """INSERT INTO jobs (job_id, album_id, title, selected_photo_ids, status,
                                 total_pages, done_pages, output_path, created_at, updated_at, completed_at)
               VALUES (?, ?, ?, ?, 'completed',
                       3, 3, ?, ?, ?, ?)""",
            (TEST_JOB_ID, TEST_ALBUM_ID, TEST_TITLE, '["1","2","3"]',
             str(TEST_DOWNLOAD_DIR), now, now, now),
        )
        conn.commit()
        report("测试任务注入", True, f"job_id={TEST_JOB_ID} status=completed output_path={TEST_DOWNLOAD_DIR}")
    finally:
        conn.close()


def remove_test_job():
    from core import database as db
    conn = db.get_db()
    try:
        conn.execute("DELETE FROM jobs WHERE job_id=?", (TEST_JOB_ID,))
        conn.commit()
    finally:
        conn.close()


print("=" * 72)
print("  数据导出功能完整性测试")
print("=" * 72)

# ── 1. 初始化 App ──
t0 = time.time()
from app import create_app
app = create_app()
client = app.test_client()
t1 = time.time()
print(f"\n📦 Flask app 启动耗时: {(t1 - t0) * 1000:.0f}ms\n")

# ── 2. 准备测试数据 ──
print("## 准备测试数据")
setup_test_data()
inject_test_job()

# 先导入数据库模块
from core import database as db
from core.settings import DEFAULT_SETTINGS, get_settings, update_settings

# ===============================================================
# 1. ZIP 导出测试
# ===============================================================
print("\n## 1. ZIP 导出测试 (/api/export/<job_id>/zip)")

# 1a. 正常导出
resp = client.post(f'/api/export/{TEST_JOB_ID}/zip')
zip_ok = report(
    "ZIP 导出（正常任务）",
    resp.status_code == 200,
    f"HTTP {resp.status_code} Content-Type={resp.content_type} "
    f"Content-Disposition={resp.headers.get('Content-Disposition', 'N/A')}"
)
if resp.status_code == 200:
    assert resp.content_type == 'application/zip', f"Content-Type 应为 application/zip，实际为 {resp.content_type}"
    # 检查 ZIP 内容
    zip_data = resp.data
    import zipfile
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            names = zf.namelist()
            report("ZIP 内容验证", True, f"内含 {len(names)} 个文件: {names}")
            # 检查中文文件名是否完整保存
            has_info = any('info.txt' in n for n in names)
            has_jpg = any('.jpg' in n for n in names)
            report("ZIP 含 info.txt", has_info, f"文件列表: {[n for n in names if 'info' in n]}")
            report("ZIP 含图片文件", has_jpg, f"图片: {[n for n in names if '.jpg' in n]}")
            if not has_info:
                add_issue("P2", "ZIP 导出缺少 info.txt 等非图片文件",
                          "ZIP 使用 output_dir.parent 作为 arcname 基准，可能有路径问题")
    except zipfile.BadZipFile:
        report("ZIP 内容验证", False, "ZIP 文件损坏")
        add_issue("P1", "ZIP 导出生成的文件损坏", "无法解析 ZIP 内容")
else:
    add_issue("P1", f"ZIP 导出返回 {resp.status_code}", resp.get_data(as_text=True)[:200])
    # 检查具体的错误消息
    try:
        err_data = resp.get_json()
        if err_data:
            report("ZIP 错误详情", True, json.dumps(err_data, ensure_ascii=False))
    except Exception:
        pass

# 1b. ZIP 导出 — 不存在的 job
resp = client.post('/api/export/nonexistent_job_id/zip')
report(
    "ZIP 导出（不存在的 job）→ 404",
    resp.status_code == 404,
    f"HTTP {resp.status_code} {resp.get_json()}"
)

# 1c. ZIP 导出 — 错误的方法（GET 而不是 POST）
resp = client.get(f'/api/export/{TEST_JOB_ID}/zip')
report(
    "ZIP 导出（GET 方法）→ 405",
    resp.status_code == 405,
    f"HTTP {resp.status_code}"
)

# ===============================================================
# 2. PDF 导出测试
# ===============================================================
print("\n## 2. PDF 导出测试 (/api/export/<job_id>/pdf)")

# 2a. 正常导出
resp = client.post(f'/api/export/{TEST_JOB_ID}/pdf')
pdf_ok = report(
    "PDF 导出（正常任务）",
    resp.status_code == 200,
    f"HTTP {resp.status_code} Content-Type={resp.content_type} "
    f"Content-Disposition={resp.headers.get('Content-Disposition', 'N/A')}"
)
if resp.status_code == 200:
    if 'application/pdf' in resp.content_type:
        report("PDF Content-Type 验证", True, f"正确: {resp.content_type}")
    else:
        report("PDF Content-Type 验证", False, f"期望 application/pdf，实际: {resp.content_type}")
        add_issue("P1", "PDF 导出 Content-Type 不正确", f"期望 application/pdf，实际 {resp.content_type}")
    # 验证 PDF 签名
    if resp.data[:5] == b'%PDF-':
        report("PDF 文件签名验证", True, f"有效 PDF: {resp.data[:8]}")
    else:
        report("PDF 文件签名验证", False, f"非 PDF 签名: {resp.data[:20]}")
        add_issue("P0", "PDF 导出生成的文件不是有效的 PDF",
                  f"文件头: {resp.data[:20]}，期望以 %PDF- 开头")
else:
    add_issue("P1", f"PDF 导出返回 {resp.status_code}", resp.get_data(as_text=True)[:200])

# 2b. PDF 导出 — 不存在的 job
resp = client.post('/api/export/nonexistent_job_id/pdf')
report(
    "PDF 导出（不存在的 job）→ 404",
    resp.status_code == 404,
    f"HTTP {resp.status_code} {resp.get_json()}"
)

# ===============================================================
# 3. Wishlist 导出/导入测试
# ===============================================================
print("\n## 3. 收藏清单 (Wishlist) 导出/导入测试")

# 3a. 先添加测试收藏
test_wishlist_ids = ["12345678", "23456789", "34567890"]
test_wishlist_zh_title = "中文漫画标题"
for aid in test_wishlist_ids:
    db.add_wishlist(aid, test_wishlist_zh_title, "测试作者", "")

# 3b. 导出
resp = client.get('/api/wishlist/export')
wishlist_export_ok = report(
    "收藏导出",
    resp.status_code == 200,
    f"HTTP {resp.status_code} Content-Type={resp.content_type}"
)
if resp.status_code == 200:
    # 检查 Content-Type
    if 'application/json' in resp.content_type or 'json' in resp.content_type:
        report("收藏导出 Content-Type 验证", True, f"正确: {resp.content_type}")
    else:
        report("收藏导出 Content-Type 验证", False, f"期望 application/json，实际: {resp.content_type}")
        add_issue("P2", "收藏导出 Content-Type 非标准",
                  f"期望 application/json; charset=utf-8，实际 {resp.content_type}")

    # 检查 Content-Disposition 含 attachment
    cd = resp.headers.get('Content-Disposition', '')
    if 'attachment' in cd and 'wishlist.json' in cd:
        report("收藏导出 Content-Disposition 验证", True, cd)
    else:
        report("收藏导出 Content-Disposition 验证", False, f"期望 attachment; filename=\"wishlist.json\"，实际 {cd}")
        add_issue("P2", "收藏导出缺少正确的 Content-Disposition", f"实际: {cd}")

    # 验证 JSON 内容
    try:
        data = json.loads(resp.data.decode('utf-8'))
        report("收藏导出 JSON 解析", True, f"导出 {len(data)} 条收藏")
        # 检查中文是否完好
        if data:
            sample_title = data[0].get('title', '')
            if test_wishlist_zh_title in str(data):
                report("收藏导出中文完整性", True, f"中文标题正确保留")
            else:
                report("收藏导出中文完整性", False, f"未找到预期中文标题")
                add_issue("P2", "收藏导出中文可能未正确处理", "JSON 中未找到预期中文标题")
    except json.JSONDecodeError as e:
        report("收藏导出 JSON 解析", False, str(e))
        add_issue("P1", "收藏导出返回的不是有效 JSON", str(e))

# 3c. 导入（文本 raw 方式）
resp_import = client.post('/api/wishlist/import', json={'raw': '123456,789012, 345678'})
report(
    "收藏导入（raw 方式）",
    resp_import.status_code == 200,
    f"HTTP {resp_import.status_code} {resp_import.get_json()}"
)
if resp_import.status_code == 200:
    import_result = resp_import.get_json()
    if import_result.get('status') == 'ok':
        report("收藏导入 status=ok", True, json.dumps(import_result, ensure_ascii=False))
    else:
        report("收藏导入 status=ok", False, json.dumps(import_result, ensure_ascii=False))
        add_issue("P1", "收藏导入返回非 ok 状态", json.dumps(import_result, ensure_ascii=False))

# 3d. 导入（文件上传方式）
import_file_data = json.dumps([
    {"album_id": "56789012", "title": "导入测试漫画", "author": "导入作者", "cover_url": ""}
], ensure_ascii=False)
resp_file_import = client.post(
    '/api/wishlist/import-file',
    data={'file': (io.BytesIO(import_file_data.encode('utf-8')), 'wishlist_import.json')},
    content_type='multipart/form-data'
)
report(
    "收藏导入（文件方式）",
    resp_file_import.status_code == 200,
    f"HTTP {resp_file_import.status_code} {resp_file_import.get_json()}"
)

# 3e. 导入验证 — 导入后检查条目是否存在
imported_item = db.get_wishlist("56789012")
report(
    "收藏导入验证（检查 DB）",
    imported_item is not None and imported_item.get('title') == "导入测试漫画",
    f"album_id=56789012 found={imported_item is not None} title={imported_item.get('title') if imported_item else 'N/A'}"
)

# 3f. 边界测试：导入空的 raw
resp = client.post('/api/wishlist/import', json={'raw': ''})
report(
    "收藏导入（空 raw）→ 400",
    resp.status_code == 400,
    f"HTTP {resp.status_code} {resp.get_json()}"
)

# 3g. 边界测试：导入非纯数字
resp = client.post('/api/wishlist/import', json={'raw': 'abc, 1.5, -123'})
report(
    "收藏导入（非法格式）",
    resp.status_code == 200,
    f"HTTP {resp.status_code} {resp.get_json()}"
)
# 验证非法 ID 被跳过
import_data = resp.get_json()
if import_data and import_data.get('added', 0) == 0:
    report("收藏导入非法格式跳过验证", True, f"added=0, failed_validation={import_data.get('failed_validation', [])}")
else:
    report("收藏导入非法格式跳过验证", False, json.dumps(import_data, ensure_ascii=False))

# 清理测试收藏
for aid in test_wishlist_ids + ["123456", "789012", "345678", "56789012"]:
    try:
        db.remove_wishlist(aid)
    except Exception:
        pass

# ===============================================================
# 4. 设置导出/导入测试
# ===============================================================
print("\n## 4. 设置 (Settings) 导出/导入测试")

# 4a. 导出
resp = client.get('/api/settings/export')
settings_export_ok = report(
    "设置导出",
    resp.status_code == 200,
    f"HTTP {resp.status_code} Content-Type={resp.content_type}"
)
if resp.status_code == 200:
    if 'application/json' in resp.content_type or resp.content_type == 'application/json':
        report("设置导出 Content-Type 验证", True, f"正确: {resp.content_type}")
    else:
        report("设置导出 Content-Type 验证", False, f"期望 application/json，实际: {resp.content_type}")
        add_issue("P2", "设置导出 Content-Type 非标准", f"期望 application/json，实际 {resp.content_type}")

    # 检查 Content-Disposition 含 attachment
    cd = resp.headers.get('Content-Disposition', '')
    if 'attachment' in cd and 'settings.json' in cd:
        report("设置导出 Content-Disposition 验证", True, cd)
    else:
        report("设置导出 Content-Disposition 验证", False, f"期望 attachment; filename=\"settings.json\"，实际 {cd}")
        add_issue("P2", "设置导出缺少正确的 Content-Disposition", f"实际: {cd}")

    # 验证 JSON 内容
    try:
        exported = json.loads(resp.data.decode('utf-8'))
        # 不应包含 download_root
        if 'download_root' not in exported:
            report("设置导出不含 download_root", True, "download_root 已被排除")
        else:
            report("设置导出不含 download_root", False, "download_root 不应出现在导出中")
            add_issue("P1", "设置导出包含只读字段 download_root", "download_root 是只读的，不应被导出")

        # 检查 key 数量: 预期全部默认设置减去 download_root = 15
        expected_keys = [k for k in DEFAULT_SETTINGS if k != 'download_root']
        missing_keys = [k for k in expected_keys if k not in exported]
        if not missing_keys:
            report("设置导出字段完整性", True, f"正确导出 {len(exported)} 个字段")
        else:
            report("设置导出字段完整性", False, f"缺失 {len(missing_keys)} 个字段: {missing_keys}")
            add_issue("P1", "设置导出缺少预期字段", f"缺失: {missing_keys}")
    except json.JSONDecodeError as e:
        report("设置导出 JSON 解析", False, str(e))
        add_issue("P1", "设置导出返回的不是有效 JSON", str(e))

# 4b. 导入（文件上传）
import_settings = {
    'timeout': '45',
    'retry_times': '5',
    'proxy': 'http://test-proxy:8080',
    'schedule_enabled': 'true',
    'max_running_jobs': '2',
}
settings_bytes = json.dumps(import_settings, ensure_ascii=False).encode('utf-8')
resp_import = client.post(
    '/api/settings/import',
    data={'file': (io.BytesIO(settings_bytes), 'settings.json')},
    content_type='multipart/form-data'
)
report(
    "设置导入",
    resp_import.status_code == 200,
    f"HTTP {resp_import.status_code} {resp_import.get_json()}"
)
if resp_import.status_code == 200:
    import_res = resp_import.get_json()
    if import_res.get('status') == 'ok':
        report("设置导入 status=ok", True, json.dumps(import_res, ensure_ascii=False))
    else:
        report("设置导入 status=ok", False, json.dumps(import_res, ensure_ascii=False))

# 4c. 验证设置导入是否生效
updated = get_settings()
verifications = []
for key, expected_val in import_settings.items():
    actual = updated.get(key)
    if str(actual) == str(expected_val):
        verifications.append(f"{key}={actual} ✓")
    else:
        verifications.append(f"{key}=期望{expected_val}实际{actual} ✗")
        add_issue("P1", f"设置导入后 {key} 值不匹配", f"期望 {expected_val}，实际 {actual}")

report(
    "设置导入持久化验证",
    all('✗' not in v for v in verifications),
    "; ".join(verifications)
)

# 4d. 边界测试：导入空文件
resp = client.post(
    '/api/settings/import',
    data={'file': (io.BytesIO(b'{}'), 'empty.json')},
    content_type='multipart/form-data'
)
report(
    "设置导入（空 JSON）",
    resp.status_code == 200,
    f"HTTP {resp.status_code} {resp.get_json()}"
)

# 4e. 边界测试：导入非法 JSON
resp = client.post(
    '/api/settings/import',
    data={'file': (io.BytesIO(b'not json'), 'bad.json')},
    content_type='multipart/form-data'
)
report(
    "设置导入（非法 JSON）→ 400",
    resp.status_code == 400,
    f"HTTP {resp.status_code} {resp.get_json()}"
)

# 4f. 边界测试：导入非 dict JSON
resp = client.post(
    '/api/settings/import',
    data={'file': (io.BytesIO(b'["a","b"]'), 'array.json')},
    content_type='multipart/form-data'
)
report(
    "设置导入（数组而非对象）→ 400",
    resp.status_code == 400,
    f"HTTP {resp.status_code} {resp.get_json()}"
)

# 4g. 边界测试：导入含未知字段
settings_unknown = {'unknown_key': 'value', 'timeout': '30'}
resp = client.post(
    '/api/settings/import',
    data={'file': (io.BytesIO(json.dumps(settings_unknown).encode()), 'unknown.json')},
    content_type='multipart/form-data'
)
report(
    "设置导入（含未知字段）",
    resp.status_code == 200,
    f"HTTP {resp.status_code} {resp.get_json()}"
)
if resp.status_code == 200:
    data = resp.get_json()
    if data and data.get('skipped', 0) >= 1:
        report("设置导入跳过未知字段", True, f"skipped={data.get('skipped', 0)}")
    else:
        report("设置导入跳过未知字段", False, "期望跳过未知字段但未跳过")
        add_issue("P2", "设置导入未正确报告跳过的未知字段", json.dumps(data, ensure_ascii=False))

# 恢复设置
update_settings({'timeout': '30', 'retry_times': '3', 'proxy': '', 'schedule_enabled': 'false', 'max_running_jobs': '1'})

# ===============================================================
# 5. 清理测试数据
# ===============================================================
print("\n## 清理测试数据")
remove_test_job()
cleanup_test_data()
report("测试数据清理", True)

# ===============================================================
# 总结
# ===============================================================
print("\n" + "=" * 72)
print("  测试结果汇总")
print("=" * 72)
for r in results:
    print(r)

print(f"\n{'='*72}")
total = len(results)
passed = sum(1 for r in results if r.startswith("  ✅"))
warnings = sum(1 for r in results if r.startswith("  ⚠️") or r.startswith("  ℹ️"))
skipped_or_detail = sum(1 for r in results if r.startswith("      "))
failed = total - passed - warnings - skipped_or_detail
print(f"总计: {total}  测试项: {total - skipped_or_detail}  通过: {passed}  警告: {warnings}  失败: {failed}")

if issues:
    print(f"\n{'='*72}")
    print(f"  发现 {len(issues)} 个问题")
    print("=" * 72)
    for iss in issues:
        sev = iss['severity']
        sev_icon = {"P0": "🔴", "P1": "🟠", "P2": "🟡"}.get(sev, "⚪")
        print(f"  {sev_icon} [{sev}] {iss['title']}")
        if iss['detail']:
            print(f"          {iss['detail']}")
else:
    print("\n🎉 未发现问题!")

# 输出 JSON 报告
report_data = {
    "test": "数据导出功能完整性测试",
    "timestamp": datetime.now().isoformat(),
    "total": total,
    "passed": passed,
    "failed": failed,
    "issues": issues,
    "results": [r for r in results if r.startswith("  ❌") or r.startswith("  ⚠️") or r.startswith("  ℹ️")],
}
report_path = Path(__file__).resolve().parent / "test_export_report.json"
report_path.write_text(json.dumps(report_data, ensure_ascii=False, indent=2))
print(f"\n📄 详细报告已保存: {report_path}")

sys.exit(1 if failed > 0 else 0)
