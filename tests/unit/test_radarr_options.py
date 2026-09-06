"""Review step 7 unit test: the options cache survives being saved from several threads.

`load_all_radarr_options` fetches every connection at once through `asyncio.gather`, and
each coroutine now awaits its save on its own worker thread. `save` is a read-modify-write
over one shared file — load every connection, replace one, write them all back — and
`filestore.write_yaml` only guards the write half. Without a lock around the whole of it,
the last writer's document is built from a read taken before its siblings wrote, and their
connections vanish from the file.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from app.services.radarr_options import RadarrOptions, RadarrOptionsCache

CONNECTION_IDS = ("app-attic", "app-lounge", "app-shed", "app-loft")


# Long enough that every thread is unambiguously past the read before any write lands,
# short enough that these two tests stay under a second.
READ_WINDOW_SECONDS = 0.05


def _slow_reader(cache: RadarrOptionsCache) -> object:
    """`_load_all`, held open, so unserialized callers all read the same stale document.

    A real gather opens this window by itself; widening it makes the test decide the same
    way on every run instead of on thread scheduling. Under the lock the sleeps simply
    queue up, because no two threads can be in here at once.
    """
    original = cache._load_all

    def load_all_slowly() -> dict[str, RadarrOptions]:
        stored = original()
        time.sleep(READ_WINDOW_SECONDS)
        return stored

    return load_all_slowly


def test_saving_every_connection_at_once_keeps_them_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = RadarrOptionsCache(tmp_path)
    monkeypatch.setattr(cache, "_load_all", _slow_reader(cache))

    threads = [
        threading.Thread(target=cache.save, args=(app_id, RadarrOptions()))
        for app_id in CONNECTION_IDS
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5.0)

    assert sorted(cache.load_all()) == sorted(CONNECTION_IDS)


def test_forgetting_one_connection_leaves_the_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delete route runs in FastAPI's threadpool and calls `forget` there, so it races
    the same saves. It is the same read-modify-write and takes the same lock."""
    cache = RadarrOptionsCache(tmp_path)
    for app_id in CONNECTION_IDS:
        cache.save(app_id, RadarrOptions())
    monkeypatch.setattr(cache, "_load_all", _slow_reader(cache))

    forgetting = threading.Thread(target=cache.forget, args=(CONNECTION_IDS[0],))
    saving = threading.Thread(target=cache.save, args=("app-new", RadarrOptions()))
    forgetting.start()
    saving.start()
    forgetting.join(timeout=5.0)
    saving.join(timeout=5.0)

    stored = cache.load_all()
    assert CONNECTION_IDS[0] not in stored
    assert "app-new" in stored
    assert set(CONNECTION_IDS[1:]) <= set(stored)
