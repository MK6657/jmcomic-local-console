"""Offline fixtures which run a worker directly can tick the real maintenance queue too."""
import threading


def maintenance_tick():
    from core import album_maintenance
    work = album_maintenance.claim()
    if work:
        album_maintenance.run(work, threading.Event())
