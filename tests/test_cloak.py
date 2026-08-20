"""cloak.py session 狀態機測試（不啟動真實 CloakBrowser）。

只驗證純邏輯層：TTL 沿用、single-flight、指數退避、force refresh 的
版本訊號語意、跨程序 lock 的 fail-closed 與匯出檔清理。真實瀏覽器
啟動流程（_launch_cloak / _refresh_impl 的內部等待）以 mock 取代。
"""

import fcntl
import json
import os
import threading
import time
from unittest import mock

import pytest

import partsouq_catalog.cloak as cloak
import partsouq_catalog.config as config

COOKIES = [
    {"name": "cf_clearance", "value": "v1", "domain": "partsouq.com", "path": "/"},
    {"name": "PHPSESSID", "value": "p1", "domain": "partsouq.com", "path": "/"},
]
NEW_COOKIES = [
    {"name": "cf_clearance", "value": "v2", "domain": "partsouq.com", "path": "/"},
    {"name": "PHPSESSID", "value": "p2", "domain": "partsouq.com", "path": "/"},
]


@pytest.fixture(autouse=True)
def _reset_session_state(monkeypatch, tmp_path):
    cloak._session_state.update(
        {
            "cookies": None,
            "ok_ts": 0.0,
            "busy": False,
            "retry_after": 0.0,
            "failures": 0,
            "version": None,
        }
    )
    cloak._rejected_versions.clear()
    monkeypatch.setitem(cloak.CLOAK, "lock_file", tmp_path / "cloak.lock")
    yield
    cloak._rejected_versions.clear()
    cloak._session_state.update(
        {
            "cookies": None,
            "ok_ts": 0.0,
            "busy": False,
            "retry_after": 0.0,
            "failures": 0,
            "version": None,
        }
    )


def test_refresh_reuses_fresh_cookies_without_relaunch(monkeypatch) -> None:
    now = cloak.time.monotonic()
    cloak._session_state.update({"cookies": COOKIES, "ok_ts": now - 10, "version": "v1"})
    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.refresh_session() == COOKIES
    impl.assert_not_called()


def test_refresh_stale_ttl_triggers_refresh_and_updates_state(monkeypatch) -> None:
    now = cloak.time.monotonic()
    cloak._session_state.update(
        {"cookies": COOKIES, "ok_ts": now - cloak.COOKIE_TTL - 1, "version": "v1"}
    )
    impl = mock.Mock(return_value=NEW_COOKIES)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.refresh_session() == NEW_COOKIES
    impl.assert_called_once()
    assert cloak._session_state["cookies"] == NEW_COOKIES
    assert cloak._session_state["version"] == "v2"
    assert cloak._session_state["failures"] == 0


def test_singleflight_concurrent_calls_refresh_once(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def impl():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=10)
        return NEW_COOKIES

    monkeypatch.setattr(cloak, "_refresh_impl", impl)
    monkeypatch.setattr(cloak.time, "sleep", lambda _seconds: None)

    results: list[list | None] = []
    threads = [
        threading.Thread(target=lambda: results.append(cloak.refresh_session())) for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert entered.wait(timeout=10)
    release.set()
    for thread in threads:
        thread.join(timeout=10)

    assert calls == 1
    assert results == [NEW_COOKIES, NEW_COOKIES]


def test_backoff_grows_exponentially_and_caps(monkeypatch) -> None:
    now_box = {"t": 1000.0}
    monkeypatch.setattr(cloak.time, "monotonic", lambda: now_box["t"])

    cloak._mark_refresh_failed()
    assert cloak._session_state["failures"] == 1
    assert cloak._session_state["retry_after"] == 1060.0

    cloak._mark_refresh_failed()
    assert cloak._session_state["retry_after"] == 1120.0

    cloak._mark_refresh_failed()
    assert cloak._session_state["retry_after"] == 1240.0

    for _ in range(10):
        cloak._mark_refresh_failed()
    assert cloak._session_state["retry_after"] == 1000.0 + cloak.REFRESH_BACKOFF_MAX


def test_refresh_during_backoff_returns_none_without_refresh(monkeypatch) -> None:
    now_box = {"t": 1000.0}
    monkeypatch.setattr(cloak.time, "monotonic", lambda: now_box["t"])
    cloak._session_state.update({"retry_after": 1030.0})
    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.refresh_session() is None
    impl.assert_not_called()


def test_force_refresh_with_newer_session_version_reuses_cookies(monkeypatch) -> None:
    now = cloak.time.monotonic()
    cloak._session_state.update({"cookies": NEW_COOKIES, "ok_ts": now - 10, "version": "v2"})
    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.force_refresh_session("v1") == NEW_COOKIES
    impl.assert_not_called()
    assert cloak._session_state["cookies"] == NEW_COOKIES


def test_force_refresh_with_same_version_clears_and_refreshes(monkeypatch) -> None:
    now = cloak.time.monotonic()
    cloak._session_state.update({"cookies": COOKIES, "ok_ts": now - 10, "version": "v1"})
    impl = mock.Mock(return_value=NEW_COOKIES)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.force_refresh_session("v1") == NEW_COOKIES
    impl.assert_called_once()
    assert cloak._session_state["version"] == "v2"


def test_force_refresh_failure_keeps_backoff_counters(monkeypatch) -> None:
    now_box = {"t": 1000.0}
    monkeypatch.setattr(cloak.time, "monotonic", lambda: now_box["t"])
    cloak._mark_refresh_failed()
    cloak._session_state.update({"cookies": COOKIES, "ok_ts": 990.0, "version": "v1"})

    def impl():
        cloak._mark_refresh_failed()
        return None

    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    now_box["t"] = 1061.0  # 退避窗口已過，這次 force 才會真正重刷
    assert cloak.force_refresh_session("v1") is None
    assert cloak._session_state["failures"] == 2
    assert cloak._session_state["retry_after"] == 1181.0


def test_refresh_impl_restricts_and_unlinks_export_file(monkeypatch, tmp_path) -> None:
    export = tmp_path / "export.json"
    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)
    saved: list[list] = []
    monkeypatch.setattr(cloak, "save_cookies", lambda cookies: saved.append(cookies))

    def fake_launch():
        export.write_text(json.dumps(COOKIES))
        return True

    monkeypatch.setattr(cloak, "_launch_cloak", fake_launch)
    monkeypatch.setattr(cloak, "_stop_owned_browser", lambda: None)

    assert cloak.refresh_session() == COOKIES
    assert saved == [COOKIES]
    assert not export.exists()


def test_refresh_skipped_when_another_process_holds_lock(monkeypatch, tmp_path) -> None:
    lock_path = tmp_path / "cloak.lock"
    monkeypatch.setitem(cloak.CLOAK, "lock_file", lock_path)
    holder = open(lock_path, "a+")
    fcntl.flock(holder, fcntl.LOCK_EX)
    now_box = {"t": 0.0}
    monkeypatch.setattr(cloak.time, "monotonic", lambda: now_box["t"])
    monkeypatch.setattr(
        cloak.time, "sleep", lambda _seconds: now_box.__setitem__("t", now_box["t"] + 4)
    )
    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    try:
        assert cloak.refresh_session() is None
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
    impl.assert_not_called()


def test_cf_value_extracts_clearance() -> None:
    assert cloak._cf_value(COOKIES) == "v1"
    assert cloak._cf_value([{"name": "PHPSESSID", "value": "x"}]) == ""
    assert cloak._cf_value(None) == ""
    assert cloak._cf_value([]) == ""


def test_get_session_seeds_from_fresh_persisted_cookies(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(COOKIES))
    os.utime(cookie_file, (time.time() - 10, time.time() - 10))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() == COOKIES
    impl.assert_not_called()
    assert cloak._session_state["version"] == "v1"


def test_persisted_cookie_keeps_its_original_age(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(COOKIES))
    wall_now = 10_000.0
    monotonic_now = 2_000.0
    age = cloak.COOKIE_TTL - 10
    os.utime(cookie_file, (wall_now - age, wall_now - age))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    monkeypatch.setattr(cloak.time, "time", lambda: wall_now)
    monkeypatch.setattr(cloak.time, "monotonic", lambda: monotonic_now)

    cloak._seed_from_disk()

    assert cloak._session_state["ok_ts"] == monotonic_now - age


@pytest.mark.parametrize(
    "payload",
    (
        [{"name": "PHPSESSID", "value": "p1"}],
        {"name": "cf_clearance", "value": "v1"},
        [{"name": "cf_clearance"}],
    ),
)
def test_get_session_rejects_invalid_persisted_cookie_shape(monkeypatch, tmp_path, payload) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(payload))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock(return_value=None)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() is None
    impl.assert_called_once()


def test_get_session_rejects_future_dated_cookie_file(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(COOKIES))
    os.utime(cookie_file, (time.time() + 60, time.time() + 60))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock(return_value=None)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() is None
    impl.assert_called_once()


def test_save_cookies_is_owner_only_and_leaves_no_temporary_file(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    monkeypatch.setattr(config, "COOKIE_FILE", cookie_file)

    config.save_cookies(COOKIES)

    assert json.loads(cookie_file.read_text()) == COOKIES
    assert cookie_file.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [cookie_file]


def test_get_session_ignores_stale_persisted_cookies(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(COOKIES))
    os.utime(
        cookie_file,
        (time.time() - cloak.COOKIE_TTL - 60, time.time() - cloak.COOKIE_TTL - 60),
    )
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock(return_value=None)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() is None
    impl.assert_called_once()


def test_get_session_ignores_unparseable_persisted_cookies(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text("not-json")
    os.utime(cookie_file, (time.time() - 5, time.time() - 5))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock(return_value=None)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() is None
    impl.assert_called_once()


def test_get_session_does_not_overwrite_in_memory_cookies(monkeypatch, tmp_path) -> None:
    now = cloak.time.monotonic()
    cloak._session_state.update({"cookies": COOKIES, "ok_ts": now - 10, "version": "v1"})
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(NEW_COOKIES))
    os.utime(cookie_file, (time.time() - 5, time.time() - 5))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() == COOKIES
    impl.assert_not_called()
    assert cloak._session_state["version"] == "v1"


def test_force_refresh_rejected_version_is_not_reseeded_from_disk(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(COOKIES))
    os.utime(cookie_file, (time.time() - 10, time.time() - 10))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    impl = mock.Mock(return_value=None)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.get_session() == COOKIES
    impl.assert_not_called()

    assert cloak.force_refresh_session("v1") is None
    assert cloak._session_state["cookies"] is None

    # 退避期結束後：同一份被拒的 v1 不得再從磁碟 seed 回來，
    # 必須真的走一次瀏覽器刷新。
    cloak._session_state["retry_after"] = 0.0
    assert cloak.get_session() is None
    assert cloak._session_state["cookies"] is None
    assert impl.call_count == 2

    # 換成全新版本的檔案（v2）→ 正常沿用，不再啟動瀏覽器。
    cookie_file.write_text(json.dumps(NEW_COOKIES))
    os.utime(cookie_file, (time.time() - 5, time.time() - 5))
    assert cloak.get_session() == NEW_COOKIES
    assert cloak._session_state["version"] == "v2"
    assert impl.call_count == 2


def test_rejected_versions_are_forgotten_after_successful_refresh(monkeypatch) -> None:
    now = cloak.time.monotonic()
    cloak._session_state.update({"cookies": COOKIES, "ok_ts": now - 10, "version": "v1"})
    impl = mock.Mock(return_value=NEW_COOKIES)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.force_refresh_session("v1") == NEW_COOKIES
    assert cloak._session_state["version"] == "v2"
    assert cloak._rejected_versions == set()
