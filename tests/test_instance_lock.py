"""Maintenance and serving use the same OS ownership primitive."""
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows instance ownership")


def test_maintenance_excludes_other_owners_and_borrowing_keeps_handle_alive():
    from core.instance_lock import acquire_instance_lock, maintenance_lock, InstanceBusyError
    name = "Local\\JMComic_MaintenanceTest_" + uuid.uuid4().hex
    owner = acquire_instance_lock(name)
    try:
        with maintenance_lock(name):
            owner.close()
            owner.close()  # Repeated close must not release the borrowed maintenance lease.
            with pytest.raises(InstanceBusyError):
                acquire_instance_lock(name)
        replacement = acquire_instance_lock(name)
        replacement.close()
    finally:
        owner.close()


def test_maintenance_releases_its_ownership_after_failure():
    from core.instance_lock import acquire_instance_lock, maintenance_lock, InstanceBusyError
    name = "Local\\JMComic_MaintenanceTest_" + uuid.uuid4().hex
    with pytest.raises(ValueError):
        with maintenance_lock(name):
            with pytest.raises(InstanceBusyError):
                acquire_instance_lock(name)
            raise ValueError("synthetic operation failure")
    replacement = acquire_instance_lock(name)
    replacement.close()


def test_another_process_cannot_enter_maintenance_while_mutex_is_held():
    from core.instance_lock import acquire_instance_lock
    name = "Local\\JMComic_MaintenanceTest_" + uuid.uuid4().hex
    lease = acquire_instance_lock(name)
    script = """
import sys
from core.instance_lock import acquire_instance_lock, InstanceBusyError
try:
    lease=acquire_instance_lock(sys.argv[1])
except InstanceBusyError:
    print('blocked')
else:
    lease.close()
    raise SystemExit(2)
"""
    try:
        result = subprocess.run([sys.executable, "-c", script, name],
                                cwd=Path(__file__).resolve().parent.parent,
                                capture_output=True, text=True, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "blocked"
    finally:
        lease.close()
