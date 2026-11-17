"""Exercise actual converter documents and disk-backed PDF assembly."""
import io
import weakref
from pathlib import Path

import pytest
from PIL import Image

from core.export_budget import BudgetedWriter, ExportBudget, ExportLimit, ExportTimeout
from routes import api_export
from test_export_capacity import env, rejected  # shared disposable export fixture


def test_converter_never_retains_an_entire_book_before_assembly(env, monkeypatch):
    import img2pdf
    import pikepdf
    previous = []
    real_convert, real_job = img2pdf.convert_to_docobject, pikepdf.Job
    configs = []
    for number in range(2, 8):
        Image.new("RGB", (16 + number, 16), (number, 20, 30)).save(env.album / f"{number}.png")

    def single_page(*args, **kwargs):
        assert all(reference() is None for reference in previous)
        document = real_convert(*args, **kwargs)
        assert len(document.writer.pages) == 1
        previous.append(weakref.ref(document))
        return document

    def disk_sources(config):
        assert len(previous) == 7 and all(reference() is None for reference in previous)
        assert len(config["pages"]) == 7
        assert all(Path(source["file"]).is_file() for source in config["pages"])
        configs.append(config)
        return real_job(config)

    monkeypatch.setattr(img2pdf, "convert_to_docobject", single_page)
    monkeypatch.setattr(pikepdf, "Job", disk_sources)
    response = env.post()
    try:
        assert response.status_code == 200
        with pikepdf.Pdf.open(io.BytesIO(response.data)) as pdf:
            assert len(pdf.pages) == 7
            widths = [float(page.mediabox[2]) for page in pdf.pages]
            assert widths == sorted(widths) and len(set(widths)) == 7
        assert all(not Path(source["file"]).exists() for source in configs[0]["pages"])
    finally:
        response.close()


def test_intermediate_pdf_files_share_the_total_staging_budget(env):
    for number in range(2, 6):
        Image.new("RGB", (16, 16), "red").save(env.album / f"{number}.png")
    env.limit(staging_bytes=3000)
    rejected(env.post(), 413, "export_limit")
    assert list(env.exports.iterdir()) == []


@pytest.mark.parametrize("failure,status,reason", [
    ("disk", 500, None), ("limit", 413, "export_limit"),
    ("timeout", 503, "export_timeout"), ("fallback", 200, None),
])
def test_final_native_save_failure_releases_windows_source_handles(env, monkeypatch, failure, status, reason):
    import pikepdf
    for number in range(2, 4):
        Image.new("RGB", (16, 16), "red").save(env.album / f"{number}.png")
    budget, real = ExportBudget(), pikepdf.Pdf.save
    monkeypatch.setattr(api_export, "ExportBudget", lambda: budget)
    calls = []

    def fail_final(document, destination=None, **kwargs):
        if isinstance(destination, BudgetedWriter) and destination.staging_key is None:
            calls.append(1)
            if len(calls) == 1:
                destination.write(b"partial PDF")
                errors = {"disk": OSError(28, "disk full"), "limit": ExportLimit("output size"),
                          "timeout": ExportTimeout("deadline"), "fallback": ValueError("retry as JPEG")}
                raise errors[failure]
            assert destination.tell() == 0
        return real(document, destination, **kwargs)

    monkeypatch.setattr(pikepdf.Pdf, "save", fail_final)
    response = env.post()
    try:
        if status == 200:
            assert response.status_code == status and len(calls) == 2
            with pikepdf.Pdf.open(io.BytesIO(response.data)) as document:
                assert len(document.pages) == 3
        else:
            rejected(response, status, reason)
            assert len(calls) == 1
        assert budget.staging_bytes == 0
    finally:
        response.close()
    assert list(env.exports.iterdir()) == []
