"""cloak.py session 狀態機測試（不啟動真實 CloakBrowser）。

只驗證純邏輯層：TTL 沿用、single-flight、指數退避、force refresh 的
版本訊號語意、跨程序 lock 的 fail-closed 與匯出檔清理。真實瀏覽器
啟動流程（_launch_cloak / _refresh_impl 的內部等待）以 mock 取代。
"""

import errno
import fcntl
import hashlib
import json
import os
import shlex
import stat
import subprocess
import sys
import threading
import time
import types
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
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
def _reset_session_state(monkeypatch, tmp_path, request):
    # 本檔所有 launch 測試都以 FakeProc/FakeBrowser 驗證，先移除真實
    # Codex marker；專門的 preflight regression 會自行設回。
    monkeypatch.delenv("CODEX_SANDBOX", raising=False)
    if "launch_cloak" in request.node.name:
        browser_cache = tmp_path / "free-browser-cache"
        browser_binary = browser_cache / "chromium-test/verified-free-chromium"
        browser_binary.parent.mkdir(parents=True)
        browser_binary.write_bytes(b"verified free browser")
        monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(browser_cache))
        monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", str(browser_binary))
        monkeypatch.setenv(
            "PSQ_CLOAK_EXPECTED_SHA256",
            hashlib.sha256(browser_binary.read_bytes()).hexdigest(),
        )
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
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", tmp_path / "cookies.json")
    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", tmp_path / ".cloak-export.json")
    monkeypatch.setitem(cloak.CLOAK, "error_log_file", tmp_path / "cloak-launch.err.log")
    yield
    if cloak._browser_err_log is not None:
        cloak._browser_err_log.close()
    cloak._browser_err_log = None
    cloak._browser_proc = None
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


def test_refresh_reloads_cookie_published_while_waiting_for_process_lock(
    monkeypatch, tmp_path
) -> None:
    cookie_file = tmp_path / "cookies.json"
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)

    @contextmanager
    def publish_before_lock_is_acquired():
        cookie_file.write_text(json.dumps(NEW_COOKIES))
        os.utime(cookie_file, (time.time() - 1, time.time() - 1))
        yield True

    impl = mock.Mock()
    monkeypatch.setattr(cloak, "_process_refresh_lock", publish_before_lock_is_acquired)
    monkeypatch.setattr(cloak, "_refresh_impl", impl)

    assert cloak.refresh_session() == NEW_COOKIES
    impl.assert_not_called()


def test_singleflight_concurrent_calls_refresh_once(monkeypatch) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    real_sleep = time.sleep

    def impl():
        nonlocal calls
        calls += 1
        entered.set()
        release.wait(timeout=10)
        return NEW_COOKIES

    monkeypatch.setattr(cloak, "_refresh_impl", impl)
    # 保留排程讓出點，避免等待 flock 的 thread 忙迴圈餓死刷新 thread。
    monkeypatch.setattr(cloak.time, "sleep", lambda _seconds: real_sleep(0.001))

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

    assert all(not thread.is_alive() for thread in threads)
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


def test_reject_session_deletes_only_matching_persisted_cookie(monkeypatch, tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps(COOKIES))
    monkeypatch.setitem(cloak.CLOAK, "cookie_file", cookie_file)
    cloak._session_state.update({"cookies": COOKIES, "ok_ts": 10.0, "version": "v1"})

    cloak.reject_session("v1")

    assert cloak._session_state["cookies"] is None
    assert cloak._session_state["version"] is None
    assert "v1" in cloak._rejected_versions
    assert not cookie_file.exists()

    cookie_file.write_text(json.dumps(NEW_COOKIES))
    cloak._session_state.update({"cookies": NEW_COOKIES, "ok_ts": 20.0, "version": "v2"})

    cloak.reject_session("v1")

    assert cloak._session_state["cookies"] == NEW_COOKIES
    assert cloak._session_state["version"] == "v2"
    assert json.loads(cookie_file.read_text()) == NEW_COOKIES


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
    stop_owned_browser = mock.Mock()
    monkeypatch.setattr(cloak, "_stop_owned_browser", stop_owned_browser)

    assert cloak.refresh_session() == COOKIES
    assert saved == [COOKIES]
    assert not export.exists()
    assert stop_owned_browser.call_args_list == [mock.call(), mock.call(graceful=True)]


def test_refresh_impl_stops_when_browser_exits_without_verified_export(
    monkeypatch, tmp_path
) -> None:
    export = tmp_path / "export.json"
    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)

    class FinishedProc:
        pid = 99999
        returncode = 1

        def poll(self) -> int:
            return self.returncode

    def fake_launch() -> bool:
        cloak._browser_proc = FinishedProc()
        return True

    sleep = mock.Mock()
    monkeypatch.setattr(cloak, "_launch_cloak", fake_launch)
    monkeypatch.setattr(cloak.time, "sleep", sleep)
    monkeypatch.setattr(cloak.os, "killpg", mock.Mock(side_effect=ProcessLookupError))

    assert cloak._refresh_impl() is None
    assert cloak._session_state["failures"] == 1
    sleep.assert_not_called()


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
        [{"name": "cf_clearance", "value": "v1", "domain": 123}],
        [{"name": "cf_clearance", "value": "v1", "path": False}],
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


def test_load_cookies_preserves_request_defaults_for_optional_fields(tmp_path) -> None:
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps([{"name": "cf_clearance", "value": "v1"}]))

    assert config.load_cookies(cookie_file) == [
        {
            "name": "cf_clearance",
            "value": "v1",
            "domain": "partsouq.com",
            "path": "/",
        }
    ]


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
    assert not cookie_file.exists()

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


def test_cookie_export_timeout_covers_all_child_deadlines() -> None:
    child_deadline = (
        cloak.PAGE_LOAD_TIMEOUT_SECONDS
        + cloak.CATALOG_VERIFY_TIMEOUT_SECONDS
        + cloak.COOKIE_SETTLE_SECONDS
    )

    assert child_deadline < cloak.COOKIE_EXPORT_TIMEOUT


def test_launch_cloak_refuses_macos_codex_sandbox_before_popen(monkeypatch) -> None:
    popen = mock.Mock()
    monkeypatch.setattr(cloak.sys, "platform", "darwin")
    monkeypatch.setenv("CODEX_SANDBOX", "seatbelt")
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False
    popen.assert_not_called()


def test_default_cloak_files_share_project_private_state_directory(tmp_path) -> None:
    project_root = tmp_path / "checkout"
    environment = os.environ.copy()
    environment["PARTSOUQ_HOME"] = str(project_root)
    for name in (
        "PSQ_CLOAK_STATE_DIR",
        "PSQ_COOKIE_EXPORT_FILE",
        "PSQ_CLOAK_ERROR_LOG_FILE",
    ):
        environment.pop(name, None)
    script = (
        "import json\n"
        "from partsouq_catalog.config import CLOAK\n"
        "print(json.dumps({key: str(CLOAK[key]) for key in "
        "('state_dir', 'cookie_file', 'cookie_export_file', 'lock_file', "
        "'error_log_file')}))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    paths = {key: Path(value) for key, value in json.loads(result.stdout).items()}

    assert paths["state_dir"] == project_root / "data"
    assert all(
        path.parent == paths["state_dir"] for key, path in paths.items() if key != "state_dir"
    )
    assert paths["cookie_export_file"] != Path("/tmp/psq_cloak_cookies.json")
    assert paths["error_log_file"] != Path("/tmp/cloak_launch_err.log")


def test_private_state_overrides_keep_symlink_components_for_preflight(tmp_path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(external, target_is_directory=True)
    environment = os.environ.copy()
    environment["PSQ_CLOAK_STATE_DIR"] = str(alias / "cloak")
    environment["PSQ_RUNTIME_LOG_DIR"] = str(alias / "logs")
    script = (
        "import json\n"
        "from partsouq_catalog.config import CLOAK_STATE_DIR, LOG_DIR\n"
        "print(json.dumps({'cloak': str(CLOAK_STATE_DIR), 'logs': str(LOG_DIR)}))\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert json.loads(result.stdout) == {
        "cloak": str(alias / "cloak"),
        "logs": str(alias / "logs"),
    }


def test_state_directory_setup_never_chmods_existing_override_parent(tmp_path) -> None:
    shared_parent = tmp_path / "shared"
    shared_parent.mkdir(mode=0o755)
    shared_parent.chmod(0o755)

    cloak._ensure_state_directory(shared_parent)

    assert shared_parent.stat().st_mode & 0o777 == 0o755


def test_state_directory_rejects_symlinked_home_boundary_without_writes(
    monkeypatch, tmp_path
) -> None:
    external = tmp_path / "external-home"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    home_alias = tmp_path / "home-alias"
    home_alias.symlink_to(external, target_is_directory=True)
    monkeypatch.setenv("HOME", str(home_alias))

    with pytest.raises(OSError, match="refusing symlinked private state path"):
        cloak._ensure_state_directory(home_alias / "state")

    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_process_refresh_lock_rejects_symlinked_leaf_without_touching_target(
    monkeypatch, tmp_path
) -> None:
    external = tmp_path / "external-lock-target"
    external.write_text("preserve\n", encoding="utf-8")
    lock_path = tmp_path / ".cloak-refresh.lock"
    lock_path.symlink_to(external)
    monkeypatch.setitem(cloak.CLOAK, "lock_file", lock_path)

    with (
        pytest.raises(OSError, match="refusing symlinked state file"),
        cloak._process_refresh_lock(),
    ):
        pytest.fail("symlinked lock must not be acquired")

    assert external.read_text(encoding="utf-8") == "preserve\n"


def test_process_refresh_lock_rejects_symlinked_ancestor_without_writes(
    monkeypatch, tmp_path
) -> None:
    external = tmp_path / "external-state"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    alias = tmp_path / "state-alias"
    alias.symlink_to(external, target_is_directory=True)
    monkeypatch.setitem(cloak.CLOAK, "lock_file", alias / "state/.cloak-refresh.lock")

    with (
        pytest.raises(OSError, match="refusing symlinked private state path"),
        cloak._process_refresh_lock(),
    ):
        pytest.fail("symlinked lock ancestor must not be traversed")

    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert {path.relative_to(external) for path in external.rglob("*")} == {Path("preserve")}


def test_process_refresh_lock_closes_descriptor_when_fdopen_fails(monkeypatch, tmp_path) -> None:
    state_file = tmp_path / "refresh.lock"
    descriptor = os.open(state_file, os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setitem(cloak.CLOAK, "lock_file", state_file)
    monkeypatch.setattr(cloak, "_open_state_file_no_follow", lambda *_args: descriptor)
    monkeypatch.setattr(cloak.os, "fdopen", mock.Mock(side_effect=OSError("fdopen failed")))

    with pytest.raises(OSError, match="fdopen failed"), cloak._process_refresh_lock():
        pytest.fail("fdopen failure must abort before yielding")

    with pytest.raises(OSError) as error:
        os.fstat(descriptor)
    assert error.value.errno == errno.EBADF


def test_launch_cloak_closes_error_descriptor_when_fdopen_fails(monkeypatch, tmp_path) -> None:
    error_log = tmp_path / "cloak-launch.err.log"
    descriptor = os.open(error_log, os.O_WRONLY | os.O_CREAT, 0o600)
    popen = mock.Mock()
    monkeypatch.setitem(cloak.CLOAK, "error_log_file", error_log)
    monkeypatch.setattr(cloak, "_open_state_file_no_follow", lambda *_args: descriptor)
    monkeypatch.setattr(cloak.os, "fdopen", mock.Mock(side_effect=OSError("fdopen failed")))
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False

    popen.assert_not_called()
    with pytest.raises(OSError) as error:
        os.fstat(descriptor)
    assert error.value.errno == errno.EBADF


def test_stop_owned_browser_signals_group_even_after_wrapper_exits(monkeypatch) -> None:
    class ExitedWrapper:
        pid = 43210

        @staticmethod
        def poll() -> int:
            return 0

    killpg = mock.Mock(side_effect=(None, ProcessLookupError))
    err_log = StringIO()
    cloak._browser_proc = ExitedWrapper()
    cloak._browser_err_log = err_log
    monkeypatch.setattr(cloak.os, "killpg", killpg)

    cloak._stop_owned_browser()

    assert killpg.call_args_list == [
        mock.call(43210, cloak.signal.SIGTERM),
        mock.call(43210, 0),
    ]
    assert err_log.closed


def test_stop_owned_browser_escalates_when_exited_wrapper_group_survives(monkeypatch) -> None:
    class ExitedWrapper:
        pid = 43211

        @staticmethod
        def poll() -> int:
            return 0

    signals: list[int] = []

    def killpg(_pid: int, child_signal: int) -> None:
        signals.append(child_signal)

    cloak._browser_proc = ExitedWrapper()
    monkeypatch.setattr(cloak, "BROWSER_STOP_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(cloak.os, "killpg", killpg)

    cloak._stop_owned_browser()

    assert signals == [cloak.signal.SIGTERM, cloak.signal.SIGKILL]


def test_stop_owned_browser_allows_successful_process_group_to_exit_naturally(
    monkeypatch,
) -> None:
    class GracefulProc:
        pid = 43212

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            assert timeout > 0
            self.returncode = 0
            return self.returncode

    proc = GracefulProc()
    signals: list[int] = []

    def killpg(_pid: int, child_signal: int) -> None:
        signals.append(child_signal)
        if child_signal == 0 and proc.returncode is not None:
            raise ProcessLookupError

    err_log = StringIO()
    cloak._browser_proc = proc
    cloak._browser_err_log = err_log
    monkeypatch.setattr(cloak.os, "killpg", killpg)

    cloak._stop_owned_browser(graceful=True)

    assert signals == [0]
    assert err_log.closed


def test_stop_owned_browser_forces_shutdown_after_graceful_timeout(monkeypatch) -> None:
    class StuckProc:
        pid = 43213

        def __init__(self) -> None:
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float) -> int:
            events.append(("wait", timeout))
            if self.returncode is None:
                raise subprocess.TimeoutExpired("cloakbrowser", timeout)
            return self.returncode

    proc = StuckProc()
    now = {"value": 0.0}
    events: list[tuple[str, float | int]] = []

    def killpg(_pid: int, child_signal: int) -> None:
        events.append(("signal", child_signal))
        if child_signal == cloak.signal.SIGKILL:
            proc.returncode = -cloak.signal.SIGKILL

    def sleep(seconds: float) -> None:
        events.append(("sleep", seconds))
        now["value"] += seconds

    cloak._browser_proc = proc
    monkeypatch.setattr(cloak, "BROWSER_STOP_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(cloak.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr(cloak.time, "sleep", sleep)
    monkeypatch.setattr(cloak.os, "killpg", killpg)

    cloak._stop_owned_browser(graceful=True)

    terminating_signals = [
        value
        for event, value in events
        if event == "signal" and value in {cloak.signal.SIGTERM, cloak.signal.SIGKILL}
    ]
    first_term = events.index(("signal", cloak.signal.SIGTERM))
    assert any(event == "wait" for event, _value in events[:first_term])
    assert ("signal", 0) in events[:first_term]
    assert terminating_signals == [cloak.signal.SIGTERM, cloak.signal.SIGKILL]


def test_launch_cloak_keeps_server_args_as_single_argv(monkeypatch, tmp_path) -> None:
    """P0 review: xvfb-run 的 --server-args 含空白時必須保持為單一 argv
    元素，否則 shlex 會把 `0` 拆成 xvfb-run 要執行的 command，CloakBrowser
    根本不會啟動。直接驗證最終 Popen argv。"""
    captured: list[str] = []
    popen_options: dict[str, object] = {}
    export = tmp_path / "cookies.json"
    ready_file = export.with_name(f"{export.name}.ready")

    class FakeProc:
        pid = 99999
        returncode = None

        def poll(self):
            return None

    monkeypatch.setitem(
        cloak.CLOAK,
        "launcher",
        config.shlex.split("xvfb-run -a --server-args='-screen 0 1366x900x24'"),
    )
    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)

    def fake_popen(argv, **_kwargs):
        captured.extend(argv)
        popen_options.update(_kwargs)
        ready_file.write_text("ready")
        return FakeProc()

    monkeypatch.setenv("PARTSOUQ_DB_PASSWORD", "must-not-reach-browser")
    monkeypatch.setenv("PARTSOUQ_ADMIN_TOKEN", "must-not-reach-browser")
    browser_cache = tmp_path / "cloak-cache"
    browser_binary = browser_cache / "chromium-test/verified-free-chromium"
    browser_binary.parent.mkdir(parents=True)
    browser_binary.write_bytes(b"verified free browser")
    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(browser_cache))
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", str(browser_binary))
    monkeypatch.setenv(
        "PSQ_CLOAK_EXPECTED_SHA256",
        hashlib.sha256(browser_binary.read_bytes()).hexdigest(),
    )
    monkeypatch.setenv("CLOAKBROWSER_TOKEN", "must-not-reach-browser")
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-reach-browser.invalid")
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-reach-browser.invalid")
    monkeypatch.setenv("ALL_PROXY", "socks5://must-not-reach-browser.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "custom-ca.pem"))
    monkeypatch.setenv("SSL_CERT_DIR", str(tmp_path / "custom-ca"))
    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloak, "_stop_owned_browser", lambda: None)

    try:
        assert cloak._launch_cloak() is True
        assert captured[0:3] == [
            "xvfb-run",
            "-a",
            "--server-args=-screen 0 1366x900x24",
        ]
        assert captured[3] == cloak.CLOAK["venv_python"]
        assert captured[4:6] == ["-u", "-c"]
        assert "cloakbrowser.launch_async" in captured[6]
        assert "stealth_args=False" not in captured[6]
        assert "--remote-debugging-port" not in captured[6]
        child_environment = popen_options["env"]
        assert isinstance(child_environment, dict)
        assert "PARTSOUQ_DB_PASSWORD" not in child_environment
        assert "PARTSOUQ_ADMIN_TOKEN" not in child_environment
        assert child_environment["CLOAKBROWSER_CACHE_DIR"] == str(browser_cache)
        assert (
            child_environment["CLOAKBROWSER_BINARY_PATH"] == os.environ["CLOAKBROWSER_BINARY_PATH"]
        )
        assert (
            child_environment["PSQ_CLOAK_EXPECTED_SHA256"]
            == os.environ["PSQ_CLOAK_EXPECTED_SHA256"]
        )
        assert "CLOAKBROWSER_TOKEN" not in child_environment
        assert "HTTP_PROXY" not in child_environment
        assert "HTTPS_PROXY" not in child_environment
        assert "ALL_PROXY" not in child_environment
        assert "SSL_CERT_FILE" not in child_environment
        assert "SSL_CERT_DIR" not in child_environment
        assert child_environment["CLOAKBROWSER_AUTO_UPDATE"] == "false"
        assert child_environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert "verify_binary()" in captured[6]
    finally:
        cloak._browser_proc = None


def test_launch_cloak_keeps_non_launchd_entrypoint_without_binary_contract(
    monkeypatch, tmp_path
) -> None:
    export = tmp_path / "cookies.json"
    ready_file = export.with_name(f"{export.name}.ready")
    browser_environment: dict[str, str] = {}

    class FakeProc:
        pid = 99999
        returncode = None

        @staticmethod
        def poll():
            return None

    def fake_popen(_argv, **kwargs):
        browser_environment.update(kwargs["env"])
        ready_file.write_text("ready", encoding="utf-8")
        return FakeProc()

    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH")
    monkeypatch.delenv("PSQ_CLOAK_EXPECTED_SHA256")
    monkeypatch.delenv("PARTSOUQ_LAUNCHD_JOB", raising=False)
    home = tmp_path / "home"
    default_cache = home / ".cloakbrowser"
    default_cache.mkdir(parents=True)
    (default_cache / "license.key").write_text("must be ignored", encoding="utf-8")
    state_dir = tmp_path / "private-state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLOAKBROWSER_CACHE_DIR", raising=False)
    monkeypatch.setitem(cloak.CLOAK, "state_dir", state_dir)
    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)
    monkeypatch.setitem(cloak.CLOAK, "launcher", [])
    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloak, "_stop_owned_browser", lambda: None)

    try:
        assert cloak._launch_cloak() is True
        assert browser_environment["CLOAKBROWSER_CACHE_DIR"] == str(
            state_dir / "free-browser-cache"
        )
        assert browser_environment["HOME"] == str(state_dir / "browser-home")
    finally:
        cloak._browser_proc = None


def test_launch_cloak_rejects_symlinked_browser_home_without_touching_target(
    monkeypatch, tmp_path
) -> None:
    free_cache = Path(os.environ["CLOAKBROWSER_CACHE_DIR"])
    external = tmp_path / "external-browser-home"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    (free_cache.parent / "browser-home").symlink_to(external, target_is_directory=True)
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False
    popen.assert_not_called()
    assert marker.read_text(encoding="utf-8") == "unchanged\n"


@pytest.mark.parametrize("entrypoint", ["default", "override"])
def test_launch_cloak_rejects_symlinked_private_state_ancestor_without_writes(
    monkeypatch, tmp_path, entrypoint: str
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    external = tmp_path / f"external-{entrypoint}-state"
    external.mkdir()
    marker = external / "preserve"
    marker.write_text("unchanged\n", encoding="utf-8")
    marker.chmod(0o640)
    alias_parent = home / f"{entrypoint}-alias-parent"
    alias_parent.symlink_to(external, target_is_directory=True)
    private_state = alias_parent / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH", raising=False)
    monkeypatch.delenv("PSQ_CLOAK_EXPECTED_SHA256", raising=False)
    monkeypatch.delenv("PARTSOUQ_LAUNCHD_JOB", raising=False)
    if entrypoint == "default":
        monkeypatch.delenv("CLOAKBROWSER_CACHE_DIR", raising=False)
        monkeypatch.setitem(cloak.CLOAK, "state_dir", private_state)
    else:
        monkeypatch.setenv(
            "CLOAKBROWSER_CACHE_DIR",
            str(private_state / "free-browser-cache"),
        )
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)
    external_before = {path.relative_to(external) for path in external.rglob("*")}

    assert cloak._launch_cloak() is False
    popen.assert_not_called()
    assert marker.read_text(encoding="utf-8") == "unchanged\n"
    assert stat.S_IMODE(marker.stat().st_mode) == 0o640
    assert {path.relative_to(external) for path in external.rglob("*")} == external_before


def test_launch_cloak_rejects_symlinked_error_log_without_truncating_target(
    monkeypatch, tmp_path
) -> None:
    external = tmp_path / "external-error-log"
    external.write_text("must remain intact\n", encoding="utf-8")
    error_log = tmp_path / "cloak-launch.err.log"
    error_log.symlink_to(external)
    monkeypatch.setitem(cloak.CLOAK, "error_log_file", error_log)
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False
    popen.assert_not_called()
    assert external.read_text(encoding="utf-8") == "must remain intact\n"


def test_launch_cloak_launchd_requires_complete_binary_contract(monkeypatch) -> None:
    monkeypatch.delenv("CLOAKBROWSER_BINARY_PATH")
    monkeypatch.delenv("PSQ_CLOAK_EXPECTED_SHA256")
    monkeypatch.setenv("PARTSOUQ_LAUNCHD_JOB", "1")
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False
    popen.assert_not_called()


@pytest.mark.parametrize("location", ["outside", "pro"])
def test_launch_cloak_rejects_binary_contract_outside_free_version_directory(
    monkeypatch,
    tmp_path,
    location: str,
) -> None:
    cache = tmp_path / "free-browser-cache"
    cache.mkdir(exist_ok=True)
    binary = (
        tmp_path / "outside-browser"
        if location == "outside"
        else cache / "chromium-pro-test/browser"
    )
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"matching but forbidden browser")
    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(cache))
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", str(binary))
    monkeypatch.setenv(
        "PSQ_CLOAK_EXPECTED_SHA256",
        hashlib.sha256(binary.read_bytes()).hexdigest(),
    )
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False
    popen.assert_not_called()


@pytest.mark.parametrize(
    ("artifact", "directory"),
    [
        ("license.key", False),
        (".license_cache", False),
        ("latest_pro_version_test", False),
        ("chromium-pro-test", True),
    ],
)
def test_launch_cloak_rejects_pro_artifacts_in_dedicated_cache(
    monkeypatch,
    tmp_path,
    artifact: str,
    directory: bool,
) -> None:
    cache = tmp_path / "free-browser-cache"
    target = cache / artifact
    if directory:
        target.mkdir(parents=True)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("paid artifact", encoding="utf-8")
    monkeypatch.setenv("CLOAKBROWSER_CACHE_DIR", str(cache))
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak._launch_cloak() is False
    popen.assert_not_called()


def test_launch_cloak_only_exports_verified_catalog_page(monkeypatch, tmp_path) -> None:
    """challenge cookie 不可發布；型錄結構通過後才原子發布。"""
    captured: list[str] = []
    launch_options: dict[str, object] = {}
    export = tmp_path / "cookies.json"

    class FakeProc:
        pid = 99999
        returncode = None

        def poll(self):
            return None

    class FakeLocator:
        count_value = 0

        async def count(self) -> int:
            return self.count_value

    class FakePage:
        url = cloak.SITE["genuine"]

        async def goto(self, *_args, **_kwargs) -> None:
            return None

        async def title(self) -> str:
            return "Just a moment..."

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator()

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

        async def cookies(self) -> list[dict]:
            return [{"name": "cf_clearance", "value": "not-verified"}]

    class FakeBrowser:
        async def new_context(self, **_kwargs) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            return None

    async def launch_async(**kwargs) -> FakeBrowser:
        launch_options.update(kwargs)
        return FakeBrowser()

    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)
    ready_file = export.with_name(f"{export.name}.ready")

    def fake_popen(argv, **_kwargs):
        captured.extend(argv)
        ready_file.write_text("ready")
        return FakeProc()

    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloak, "_stop_owned_browser", lambda: None)
    monkeypatch.setitem(
        sys.modules, "cloakbrowser", types.SimpleNamespace(launch_async=launch_async)
    )

    try:
        assert cloak._launch_cloak() is True
        script = captured[-1]
        assert f"timeout={int(cloak.PAGE_LOAD_TIMEOUT_SECONDS * 1000)}" in script
        assert f"deadline = time.monotonic() + {cloak.CATALOG_VERIFY_TIMEOUT_SECONDS!r}" in script
        assert f"await asyncio.sleep({cloak.COOKIE_SETTLE_SECONDS!r})" in script
        assert script.index("await b.close()") < script.index("os.replace(tmp, OUT)")
        expired_script = script.replace(
            f"deadline = time.monotonic() + {cloak.CATALOG_VERIFY_TIMEOUT_SECONDS!r}",
            "deadline = time.monotonic() - 1",
        )
        assert expired_script != script
        with pytest.raises(RuntimeError, match="verified catalog page"):
            exec(compile(expired_script, "<cloak-test>", "exec"), {})
        assert not export.exists()

        FakeLocator.count_value = int(cloak.CRAWL["min_brands"])
        exec(compile(script, "<cloak-test>", "exec"), {})
        assert json.loads(export.read_text()) == [
            {"name": "cf_clearance", "value": "not-verified", "domain": "", "path": "/"}
        ]
        assert not export.with_name(f"{export.name}.tmp").exists()
        assert launch_options["headless"] is False
        assert "stealth_args" not in launch_options
        assert "args" not in launch_options
    finally:
        cloak._browser_proc = None


def test_launch_cloak_closes_browser_when_navigation_raises(monkeypatch, tmp_path) -> None:
    captured: list[str] = []
    export = tmp_path / "cookies.json"

    class FakeProc:
        pid = 99999
        returncode = None

        @staticmethod
        def poll():
            return None

    class FakePage:
        async def goto(self, *_args, **_kwargs) -> None:
            raise RuntimeError("navigation failed")

    class FakeContext:
        async def new_page(self) -> FakePage:
            return FakePage()

    class FakeBrowser:
        closed = False

        async def new_context(self, **_kwargs) -> FakeContext:
            return FakeContext()

        async def close(self) -> None:
            self.closed = True

    browser = FakeBrowser()

    async def launch_async(**_kwargs) -> FakeBrowser:
        return browser

    def fake_popen(argv, **_kwargs):
        captured.extend(argv)
        export.with_name(f"{export.name}.ready").write_text("ready")
        return FakeProc()

    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)
    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cloak, "_stop_owned_browser", lambda: None)
    monkeypatch.setitem(
        sys.modules, "cloakbrowser", types.SimpleNamespace(launch_async=launch_async)
    )

    try:
        assert cloak._launch_cloak() is True
        with pytest.raises(RuntimeError, match="navigation failed"):
            exec(compile(captured[-1], "<cloak-test>", "exec"), {})
        assert browser.closed is True
    finally:
        cloak._browser_proc = None


def test_launch_cloak_rejects_stale_ready_marker(monkeypatch, tmp_path) -> None:
    """前一次異常中斷留下的 marker 不可被當成本次啟動成功。"""
    export = tmp_path / "cookies.json"
    ready_file = export.with_name(f"{export.name}.ready")
    ready_file.write_text("stale")

    class FinishedProc:
        pid = 99999
        returncode = 17

        def poll(self):
            return self.returncode

    monkeypatch.setitem(cloak.CLOAK, "cookie_export_file", export)
    monkeypatch.setitem(cloak.CLOAK, "launcher", [])
    monkeypatch.setattr(cloak.subprocess, "Popen", lambda *_args, **_kwargs: FinishedProc())
    monkeypatch.setattr(cloak.os, "killpg", mock.Mock(side_effect=ProcessLookupError))

    assert cloak._launch_cloak() is False
    assert not ready_file.exists()


def test_psq_cloak_launcher_env_keeps_server_args_single_argv(monkeypatch) -> None:
    """P0 review 的組態端防護：compose.yml 的 scheduler env
    PSQ_CLOAK_LAUNCHER（含內層引號）經 config 的 shlex.split 後，
    --server-args 必須是**單一** argv 元素。舊的未引號寫法會把 `0`
    拆成 xvfb-run 要執行的 command。

    直接讀 compose.yml（不硬編碼字串）：日後 compose.yml 回退成舊
    寫法時此測試會失敗。"""
    import importlib
    import re
    from pathlib import Path

    compose_text = (Path(__file__).resolve().parents[1] / "compose.yml").read_text(encoding="utf-8")
    match = re.search(r'PSQ_CLOAK_LAUNCHER:\s*"([^"]*)"', compose_text)
    assert match is not None, "compose.yml must define PSQ_CLOAK_LAUNCHER"
    compose_value = match.group(1)
    parsed = shlex.split(compose_value)
    assert parsed == [
        "xvfb-run",
        "-a",
        "--server-args=-screen 0 1366x900x24",
    ], "compose.yml PSQ_CLOAK_LAUNCHER must quote --server-args as one argv element"

    monkeypatch.setenv("PSQ_CLOAK_LAUNCHER", compose_value)
    importlib.reload(config)
    assert config.CLOAK["launcher"] == parsed

    broken = "xvfb-run -a --server-args=-screen 0 1366x900x24"
    monkeypatch.setenv("PSQ_CLOAK_LAUNCHER", broken)
    importlib.reload(config)
    assert config.CLOAK["launcher"][2] == "--server-args=-screen"
    assert config.CLOAK["launcher"][3] == "0"


def test_is_cloudflare_challenge_markers() -> None:
    assert cloak._is_cloudflare_challenge("Just a moment...")
    assert cloak._is_cloudflare_challenge("<title>Verify you are human</title>")
    assert cloak._is_cloudflare_challenge("請稍候...")
    assert cloak._is_cloudflare_challenge("cf-mitigated: challenge")
    assert not cloak._is_cloudflare_challenge("<html><body>real catalog</body></html>")
    assert not cloak._is_cloudflare_challenge("")


@contextmanager
def _fake_browser_lock():
    yield True


def _fake_prepare(_state):
    return ({}, _state / "cache", _state / "home", "", "")


def test_fetch_page_returns_html_when_browser_produces_page(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(cloak.CLOAK, "state_dir", tmp_path)
    out_file = tmp_path / ".cloak-page-fetch.html"
    real_html = "<html><body>real catalog page</body></html>"

    monkeypatch.setattr(cloak, "_process_refresh_lock", _fake_browser_lock)
    monkeypatch.setattr(cloak, "_prepare_browser_launch", lambda: _fake_prepare(tmp_path))
    monkeypatch.setattr(cloak.os, "killpg", lambda *_a, **_k: None)

    class FakeProc:
        pid = 12345

        def poll(self) -> int:
            return 0

        def wait(self, _timeout: float) -> int:
            return 0

    def fake_popen(_argv, **_kwargs):
        out_file.write_text(real_html, encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)

    assert (
        cloak.fetch_page("https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1") == real_html
    )
    assert not out_file.exists()


def test_fetch_page_returns_none_on_challenge_page(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(cloak.CLOAK, "state_dir", tmp_path)
    out_file = tmp_path / ".cloak-page-fetch.html"
    challenge_html = "<html><title>Just a moment...</title></html>"

    monkeypatch.setattr(cloak, "_process_refresh_lock", _fake_browser_lock)
    monkeypatch.setattr(cloak, "_prepare_browser_launch", lambda: _fake_prepare(tmp_path))
    monkeypatch.setattr(cloak.os, "killpg", lambda *_a, **_k: None)

    class FakeProc:
        pid = 12345

        def poll(self) -> int:
            return 0

        def wait(self, _timeout: float) -> int:
            return 0

    def fake_popen(_argv, **_kwargs):
        out_file.write_text(challenge_html, encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)

    assert cloak.fetch_page("https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1") is None
    assert not out_file.exists()


def test_fetch_page_skips_when_lock_unavailable(monkeypatch, tmp_path) -> None:
    @contextmanager
    def unavailable():
        yield False

    monkeypatch.setattr(cloak, "_process_refresh_lock", unavailable)
    monkeypatch.setattr(cloak, "_prepare_browser_launch", lambda: _fake_prepare(tmp_path))
    popen = mock.Mock()
    monkeypatch.setattr(cloak.subprocess, "Popen", popen)

    assert cloak.fetch_page("https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1") is None
    popen.assert_not_called()


def test_fetch_page_raises_non_unit_when_browser_lands_off_unit(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(cloak.CLOAK, "state_dir", tmp_path)
    out_file = tmp_path / ".cloak-page-fetch.html"
    marker = tmp_path / ".cloak-page-fetch.html.offunit"

    monkeypatch.setattr(cloak, "_process_refresh_lock", _fake_browser_lock)
    monkeypatch.setattr(cloak, "_prepare_browser_launch", lambda: _fake_prepare(tmp_path))
    monkeypatch.setattr(cloak.os, "killpg", lambda *_a, **_k: None)

    class FakeProc:
        pid = 12345

        def poll(self) -> int:
            return 0

        def wait(self, _timeout: float) -> int:
            return 0

    def fake_popen(_argv, **_kwargs):
        # 模擬瀏覽器被轉址到 /locate 首頁：只寫 sidecar marker，不寫 HTML。
        marker.write_text(
            "https://partsouq.com/en/catalog/genuine/locate?c=Toyota", encoding="utf-8"
        )
        return FakeProc()

    monkeypatch.setattr(cloak.subprocess, "Popen", fake_popen)

    with pytest.raises(cloak.NonUnitPageError):
        cloak.fetch_page("https://partsouq.com/en/catalog/genuine/unit?c=Toyota&uid=1&vid=0")
    assert not out_file.exists()
    assert not marker.exists()
