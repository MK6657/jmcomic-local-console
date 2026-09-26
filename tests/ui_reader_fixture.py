"""Explicit offline UI fixture. Synthetic pages only; never loads the user's database.

Run with the project Python: python tests/ui_reader_fixture.py --port 5010
"""
import argparse
import tempfile
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5010)
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import core.path_utils as paths
    sandbox = Path(tempfile.mkdtemp(prefix="jm-reader-ui-"))
    paths.get_app_root = lambda: sandbox
    from app import create_app
    from core import database as db
    from flask import jsonify, request
    from PIL import Image, ImageDraw
    from waitress import serve
    app = create_app()
    output = sandbox / "downloads" / "Reader sample"
    output.mkdir(parents=True)
    for number in range(1, 46):
        img = Image.new("RGB", (600, 800), "#eeeae1")
        draw = ImageDraw.Draw(img)
        draw.rectangle((30, 30, 570, 770), outline="#686256", width=3)
        draw.text((240, 350), f"TEST PAGE {number}", fill="#2B2A27")
        img.save(output / f"{number:03d}.png")
    db.insert_job("job_ui", "900001", "Reader sample", [])
    db.update_job("job_ui", status="completed", output_path=str(output))
    calls = {"search": 0}
    def search():
        calls["search"] += 1
        page = request.args.get("page", 1, type=int)
        return jsonify(status="ok", total=120, page=page, items=[{
            "album_id": str(900001 + index), "title": f"Sample comic {index + 1}",
            "author": "UI fixture", "cover_url": "/static/images/no-cover.svg", "tags": ["sample"],
        } for index in range(12)])
    def detail(album_id):
        return jsonify(status="ok", data={"album_id": album_id, "title": "Sample detail", "author": "UI fixture",
            "cover": "/static/images/no-cover.svg", "photos": [], "tags": [], "chapter_count": 0})
    app.view_functions["api_search.search"] = search
    app.view_functions["api_album.album_detail"] = detail
    app.view_functions["api_library.album_tags_sync"] = lambda album_id: jsonify(status="ok", synced=0, total_tags=0)
    # 未下载的漫画点“阅读”会转到在线阅读：在线接口换成离线桩，从不访问上游
    app.view_functions["api_online.online_album"] = lambda album_id: (
        jsonify(status="error", message="在线加载失败，请稍后重试"), 502)
    app.add_url_rule("/test/metrics", "metrics", lambda: jsonify(calls))
    print(f"Fixture: http://127.0.0.1:{args.port}; data: {sandbox}", flush=True)
    serve(app, host="127.0.0.1", port=args.port, threads=8)


if __name__ == "__main__":
    main()
