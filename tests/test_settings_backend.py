import io
import json
from unittest.mock import Mock

from core import database, settings
from routes import api_settings


def _import_settings(client, patch):
    return client.post("/api/settings/import", data={
        "file": (io.BytesIO(json.dumps(patch).encode()), "settings.json"),
    })


def test_disabling_auto_pack_clears_deletion_and_reenabling_does_not_restore_it(client):
    enabled = client.post("/api/settings", json={"auto_pack": True, "delete_originals": True})
    assert enabled.json["settings"]["delete_originals"] == "true"

    disabled = client.post("/api/settings", json={"delete_originals": True, "auto_pack": False})
    assert disabled.json["settings"]["delete_originals"] == "false"
    assert database.get_setting("delete_originals") == "false"

    reenabled = client.post("/api/settings", json={"auto_pack": True})
    assert reenabled.json["settings"]["delete_originals"] == "false"


def test_legacy_disabled_auto_pack_does_not_arm_deletion_when_reenabled(client):
    database.set_setting("auto_pack", "false")
    database.set_setting("delete_originals", "true")
    settings.invalidate_settings_cache()
    assert client.get("/api/settings").json["settings"]["delete_originals"] == "false"

    response = client.post("/api/settings", json={"auto_pack": True})
    assert response.status_code == 200 and database.get_setting("delete_originals") == "false"


def test_import_applies_valid_pack_settings_together_even_if_another_setting_is_invalid(client):
    response = _import_settings(client, {"delete_originals": True, "timeout": "invalid", "auto_pack": True})
    assert (response.status_code, response.json["imported"], response.json["skipped"]) == (200, 2, 1)
    assert database.get_setting("auto_pack") == database.get_setting("delete_originals") == "true"

    response = _import_settings(client, {"delete_originals": True, "auto_pack": False})
    assert response.json["imported"] == 2
    assert database.get_setting("auto_pack") == database.get_setting("delete_originals") == "false"


def test_only_changed_settings_apply_their_runtime_component(client, monkeypatch):
    network = Mock()
    scheduler = Mock()
    monkeypatch.setattr(api_settings, "invalidate_option_cache", network)
    monkeypatch.setattr(api_settings, "sync_scheduler", scheduler)

    response = client.post("/api/settings", json={
        "image_format": "png", "pack_format": "zip", "skip_existing": False,
        "organize_mode": "by_author", "timeout": " 30 ", "schedule_enabled": False,
    })
    assert response.status_code == 200 and response.json["warnings"] == []
    network.assert_not_called()
    scheduler.assert_not_called()

    assert client.post("/api/settings", json={"photo_threads": 2}).status_code == 200
    network.assert_called_once_with()
    scheduler.assert_not_called()

    assert _import_settings(client, {"timeout": 30, "schedule_start": 12}).status_code == 200
    network.assert_called_once_with()
    scheduler.assert_called_once_with()

    network.reset_mock()
    scheduler.reset_mock()
    assert client.post("/api/settings/reset", json={"scope": "all"}).status_code == 200
    network.assert_called_once_with()
    scheduler.assert_called_once_with()
    assert client.post("/api/settings/reset", json={"scope": "all"}).status_code == 200
    network.assert_called_once_with()
    scheduler.assert_called_once_with()


def test_settings_apply_warning_keeps_committed_change(client, monkeypatch):
    network = Mock()
    scheduler = Mock(side_effect=RuntimeError("fixture scheduler error"))
    monkeypatch.setattr(api_settings, "invalidate_option_cache", network)
    monkeypatch.setattr(api_settings, "sync_scheduler", scheduler)

    response = client.post("/api/settings", json={"schedule_start": 16})
    assert response.status_code == 200 and response.json["saved"] is True
    assert response.json["warnings"] == ["定时调度未能立即应用，请重启程序后检查"]
    assert database.get_setting("schedule_start") == "16"
    network.assert_not_called()
    scheduler.assert_called_once_with()


def test_build_jmcomic_option_accepts_snapshot_without_reading_database(monkeypatch):
    read = Mock(side_effect=AssertionError("explicit snapshot must not load persisted settings"))
    monkeypatch.setattr(settings, "get_settings", read)
    snapshot = {
        **settings.DEFAULT_SETTINGS, "client_type": "html", "proxy": "http://localhost:8080",
        "retry_times": "0", "timeout": "14", "image_threads": "8", "photo_threads": "3",
    }

    option = settings.build_jmcomic_option(settings=snapshot)
    client = option["client"]
    assert (client["impl"], client["retry_times"], client["timeout"]) == ("html", 0, 14)
    assert client["postman"]["meta_data"]["proxies"]["https"] == "http://localhost:8080"
    assert option["download"]["threading"] == {"image": 8, "photo": 3}
    read.assert_not_called()
