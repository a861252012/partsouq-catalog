"""CloakBrowser 工作階段管理員（基礎設施層）。

啟動 CloakBrowser 並導向 partsouq.com。只有實際型錄頁通過結構驗證後，
才匯出 session cookie 供 HTTP 爬蟲使用；仍停在 Cloudflare challenge 頁
或逾時時一律失敗，不把 cookie 存在誤判為驗證成功。

執行緒安全：所有刷新都走 single-flight 鎖，因此 N 個並行 worker
同時呼叫時，只會啟動一個 CloakBrowser；其餘 worker 等待結果並沿用
（見 get_session / refresh_session）。
"""

# ruff: noqa: UP031  -- 底下 `%` 格式是刻意保留：內嵌子程序腳本含有大量
# `{}`（dict literal），改用 .format()/f-string 會與腳本內容衝突。
import contextlib
import fcntl
import hashlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO, TypedDict

from .config import CLOAK, CRAWL, SITE, Cookies, load_cookies, save_cookies
from .state_files import (
    ensure_private_state_directory,
    open_private_state_file,
    private_path_has_symlink,
)

log = logging.getLogger("cloak")

BROWSER_START_TIMEOUT = 60.0
BROWSER_STOP_GRACE_SECONDS = 5.0
# _stop_owned_browser(graceful=True) 最壞會先等一次自然關閉，再等
# process-group TERM 與 wrapper reap；父程序的強制 KILL 必須晚於此預算。
OWNED_BROWSER_CLEANUP_BUDGET_SECONDS = BROWSER_STOP_GRACE_SECONDS * 3 + 2
PAGE_LOAD_TIMEOUT_SECONDS = 60.0
CATALOG_VERIFY_TIMEOUT_SECONDS = 150.0
COOKIE_SETTLE_SECONDS = 2.0
PAGE_FETCH_CHALLENGE_TIMEOUT_SECONDS = 60.0
PAGE_FETCH_TIMEOUT_SECONDS = (
    PAGE_LOAD_TIMEOUT_SECONDS
    + PAGE_FETCH_CHALLENGE_TIMEOUT_SECONDS
    + BROWSER_STOP_GRACE_SECONDS
    + 30.0
)
# ready marker 在 page.goto 之前發布；父程序等待時間必須完整涵蓋子程序
# 的 navigation、型錄驗證與 cookie settle，再留 30 秒寫檔/排程餘裕。
COOKIE_EXPORT_TIMEOUT = (
    PAGE_LOAD_TIMEOUT_SECONDS + CATALOG_VERIFY_TIMEOUT_SECONDS + COOKIE_SETTLE_SECONDS + 30.0
)

# 只管理本程序親自啟動的 CloakBrowser process group。不得用 pkill 掃描
# 共用機器上的命令列，否則會誤殺其他專案的 CloakBrowser。
_BROWSER_LOCK = threading.Lock()
_BROWSER_SHUTDOWN = threading.Event()
_browser_proc: subprocess.Popen[bytes] | None = None
_browser_err_log: TextIO | None = None


class SessionState(TypedDict):
    cookies: Cookies | None
    ok_ts: float
    busy: bool
    retry_after: float
    failures: int
    version: str | None


# ---------------------------------------------------------------------------
# 全域 single-flight 的 session 狀態
# ---------------------------------------------------------------------------
_SESSION_LOCK = threading.Lock()
_SESSION_COND = threading.Condition(_SESSION_LOCK)
_session_state: SessionState = {
    "cookies": None,  # 最近一次成功匯出的 cookie
    "ok_ts": 0.0,  # 上次成功的單調時鐘（monotonic）時間
    "busy": False,  # 目前是否正在刷新（single-flight 旗標）
    "retry_after": 0.0,  # 在此單調時鐘時間之前不得重試（退避）
    "failures": 0,  # 連續失敗次數（退避指數成長用）
    "version": None,  # 目前 cookie 的 cf_clearance 值（session 版本訊號，SOL review P2）
}
# 被伺服器拒絕過的 cf_clearance 版本：force_refresh_session 清掉記憶體
# 快取後，_seed_from_disk 不得再把同一份被拒 cookie 從磁碟撈回來
# （否則 403 迴圈只會重演，等於白清了快取）。
_rejected_versions: set[str] = set()
# Cookie 的有效期限：每 25 分鐘主動刷新一次
COOKIE_TTL = 25 * 60.0
# 刷新失敗後的退避基礎：連續失敗會指數成長（60s → 120s → 240s ...）
REFRESH_RETRY_BACKOFF = 60.0
# 退避上限：避免長時間封鎖時越等越久
REFRESH_BACKOFF_MAX = 20 * 60.0


def _cf_value(cookies: Cookies | None) -> str:
    """從 cookie 列表取出 cf_clearance 的值（無則回傳空字串）。

    cf_clearance 每次刷新必然改變，是可靠的 session 版本訊號
    （與 http_client 的 _cf_value 同義；這裡各自定義避免循環 import）。
    """
    for c in cookies or []:
        if c.get("name") == "cf_clearance":
            return c.get("value", "")
    return ""


def _ensure_state_directory(path: Path) -> None:
    ensure_private_state_directory(path)


def _open_state_file_no_follow(path: Path, flags: int) -> int:
    return open_private_state_file(path, flags)


def _free_cache_has_pro_artifacts(free_cache: Path) -> bool:
    return any(
        path.exists()
        for path in (
            free_cache / "license.key",
            free_cache / ".license_cache",
            free_cache / ".last_pro_version_check",
            free_cache / ".last_pro_update_check",
            *free_cache.glob("latest_pro_version_*"),
            *(
                path
                for path in free_cache.iterdir()
                if path.is_dir() and "pro" in path.name.lower()
            ),
        )
    )


@contextlib.contextmanager
def _process_refresh_lock() -> Iterator[bool]:
    """跨程序 refresh 互斥鎖（single-flight 只管得到同程序內執行緒）。

    兩個 crawler 程序同時啟動瀏覽器會互相覆寫 cookie 匯出檔。用
    flock 鎖檔串列化 refresh；鎖被佔用超過期限時放棄（fail-closed），
    呼叫端拿到 acquired=False。
    """
    lock_path = CLOAK["lock_file"]
    _ensure_state_directory(lock_path.parent)
    lock_fd = _open_state_file_no_follow(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_APPEND,
    )
    try:
        lock_file = os.fdopen(lock_fd, "a+")
    except BaseException:
        os.close(lock_fd)
        raise
    acquired = False
    deadline = time.monotonic() + COOKIE_EXPORT_TIMEOUT + 120
    try:
        while True:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(3)
        yield acquired
    finally:
        if acquired:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except OSError:
                pass
        lock_file.close()


def _stop_owned_browser(*, graceful: bool = False) -> None:
    """冪等停止本程序啟動的 CloakBrowser process group。"""
    global _browser_err_log, _browser_proc

    with _BROWSER_LOCK:
        proc = _browser_proc
        err_log = _browser_err_log
        _browser_proc = None
        _browser_err_log = None

    try:
        if proc is None:
            return
        if graceful:
            deadline = time.monotonic() + BROWSER_STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    proc.wait(timeout=min(0.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    return
                except PermissionError:
                    break
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
        wrapper_running = proc.poll() is None
        group_signaled = False
        try:
            # wrapper 可能已先退出，但 start_new_session 建立的 child process
            # group 仍存活；無論 wrapper 狀態都必須對 owned PGID 送訊號。
            os.killpg(proc.pid, signal.SIGTERM)
            group_signaled = True
        except ProcessLookupError:
            wrapper_running = False
        except PermissionError:
            if wrapper_running:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    wrapper_running = False
        if group_signaled:
            deadline = time.monotonic() + BROWSER_STOP_GRACE_SECONDS
            while time.monotonic() < deadline:
                try:
                    os.killpg(proc.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (PermissionError, ProcessLookupError):
                    pass
        if wrapper_running:
            try:
                proc.wait(timeout=BROWSER_STOP_GRACE_SECONDS + 1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                try:
                    proc.wait(timeout=BROWSER_STOP_GRACE_SECONDS + 1)
                except subprocess.TimeoutExpired:
                    log.error("owned CloakBrowser process group did not exit after SIGKILL")
    finally:
        if err_log is not None:
            err_log.close()
        ready_file = CLOAK["cookie_export_file"].with_name(
            f"{CLOAK['cookie_export_file'].name}.ready"
        )
        ready_file.unlink(missing_ok=True)
        ready_file.with_name(f"{ready_file.name}.tmp").unlink(missing_ok=True)


def _begin_browser_shutdown() -> None:
    """禁止註冊新瀏覽器，並回收目前已註冊的 owned process group。"""
    _BROWSER_SHUTDOWN.set()
    _stop_owned_browser()


def _finish_browser_shutdown() -> None:
    """結束 run_crawl 的 shutdown 臨界區（主要供同程序測試重入）。"""
    _BROWSER_SHUTDOWN.clear()


def _launch_cloak() -> bool:
    """啟動本程序擁有的 CloakBrowser process group 並等待就緒訊號。

    子程序腳本負責：驅動 CloakBrowser 前往目標頁面、驗證型錄結構，
    最後把已驗證頁面的 session cookie 匯出成 JSON 檔案。
    不再仰賴 agent-browser 的常駐 daemon（其長時間運行曾造成
    cookie 匯出不穩定的問題）。
    """
    global _browser_err_log, _browser_proc

    # Codex 的 macOS seatbelt 無法安全啟動 headed Chromium；強行啟動時
    # Chromium 會被系統終止並跳出「未預期的結束」視窗。正式 Aqua
    # LaunchAgent 不會帶這個環境變數，因此正常 host 排程不受影響。
    if sys.platform == "darwin" and os.environ.get("CODEX_SANDBOX"):
        log.error(
            "refusing to launch headed CloakBrowser inside the macOS Codex sandbox; "
            "use the Aqua LaunchAgent"
        )
        return False

    cache_override = os.environ.get("CLOAKBROWSER_CACHE_DIR")
    if cache_override:
        free_cache = Path(cache_override).expanduser()
        private_state_root = free_cache.parent
    else:
        private_state_root = Path(
            os.environ.get("PSQ_CLOAK_STATE_DIR", CLOAK["state_dir"])
        ).expanduser()
        free_cache = private_state_root / "free-browser-cache"
    browser_home = free_cache.parent / "browser-home"
    if any(
        private_path_has_symlink(private_path)
        for private_path in (private_state_root, free_cache, browser_home)
    ):
        log.error("refusing symlinked CloakBrowser private state path")
        return False
    _ensure_state_directory(free_cache)
    if _free_cache_has_pro_artifacts(free_cache):
        log.error("refusing CloakBrowser Pro artifacts in the dedicated free cache")
        return False
    _ensure_state_directory(browser_home)

    expected_binary = os.environ.get("CLOAKBROWSER_BINARY_PATH", "")
    expected_sha256 = os.environ.get("PSQ_CLOAK_EXPECTED_SHA256", "")
    binary_contract_required = os.environ.get("PARTSOUQ_LAUNCHD_JOB") == "1"
    binary_contract_enabled = bool(expected_binary or expected_sha256)
    if (binary_contract_required or binary_contract_enabled) and (
        not expected_binary
        or not expected_sha256
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        log.error("incomplete verified CloakBrowser free binary contract")
        return False
    if binary_contract_enabled:
        try:
            resolved_binary = Path(expected_binary).expanduser().resolve(strict=True)
            relative_binary = resolved_binary.relative_to(free_cache.resolve(strict=True))
        except (OSError, ValueError) as error:
            log.error("verified CloakBrowser binary is outside the dedicated free cache: %s", error)
            return False
        if (
            len(relative_binary.parts) < 2
            or not relative_binary.parts[0].startswith("chromium-")
            or "pro" in relative_binary.parts[0].lower()
        ):
            log.error("verified CloakBrowser binary is not in a free Chromium version directory")
            return False
        expected_binary = str(resolved_binary)
        try:
            with resolved_binary.open("rb") as binary_file:
                binary_sha256 = hashlib.file_digest(binary_file, "sha256").hexdigest()
        except OSError as error:
            log.error("could not read verified CloakBrowser binary: %s", error)
            return False
        if binary_sha256 != expected_sha256:
            log.error("verified CloakBrowser binary changed before launch")
            return False

    ready_file = CLOAK["cookie_export_file"].with_name(f"{CLOAK['cookie_export_file'].name}.ready")
    ready_tmp = ready_file.with_name(f"{ready_file.name}.tmp")
    _ensure_state_directory(ready_file.parent)
    ready_file.unlink(missing_ok=True)
    ready_tmp.unlink(missing_ok=True)
    script = (
        "import asyncio, hashlib, json, os, time, cloakbrowser\n"
        "OUT = %r\n"
        "READY = %r\n"
        "SITE = %r\n"
        "BINARY = %r\n"
        "BINARY_SHA256 = %r\n"
        "def verify_binary():\n"
        "    if not BINARY:\n"
        "        return\n"
        "    with open(BINARY, 'rb') as binary_file:\n"
        "        actual = hashlib.file_digest(binary_file, 'sha256').hexdigest()\n"
        "    if actual != BINARY_SHA256:\n"
        "        raise RuntimeError('verified CloakBrowser binary changed before launch')\n"
        "async def main():\n"
        "    b = None\n"
        "    try:\n"
        "        verify_binary()\n"
        "        b = await cloakbrowser.launch_async(\n"
        "            headless=False)\n"
        "        ready_tmp = READY + '.tmp'\n"
        "        fd = os.open(ready_tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "        with os.fdopen(fd, 'w') as f:\n"
        "            f.write(str(os.getpid()))\n"
        "        os.replace(ready_tmp, READY)\n"
        "        ctx = await b.new_context(viewport={'width': 1366, 'height': 900})\n"
        "        page = await ctx.new_page()\n"
        "        await page.goto(SITE, wait_until='domcontentloaded', timeout=%d)\n"
        "        verified = False\n"
        "        title = ''\n"
        "        brand_links = 0\n"
        "        page_url = ''\n"
        "        deadline = time.monotonic() + %r\n"
        "        while time.monotonic() < deadline:\n"
        "            try:\n"
        "                title = await page.title()\n"
        "                page_url = page.url\n"
        "                brand_links = await page.locator(\n"
        "                    'li a[href*=\"/en/catalog/genuine/locate?c=\"]'\n"
        "                ).count()\n"
        "                catalog_url = page_url.split('#', 1)[0].split('?', 1)[0].rstrip('/')\n"
        "                if catalog_url == SITE.rstrip('/') and brand_links >= %d:\n"
        "                    verified = True\n"
        "                    break\n"
        "            except Exception:\n"
        "                pass\n"
        "            await asyncio.sleep(3)\n"
        "        if not verified:\n"
        "            raise RuntimeError(\n"
        "                f'verified catalog page not reached: url={page_url[:120]!r} '\n"
        "                f'title={title[:80]!r} brand_links={brand_links}'\n"
        "            )\n"
        "        await asyncio.sleep(%r)\n"
        "        cookies = await ctx.cookies()\n"
        "        data = [{'name': c['name'], 'value': c['value'],\n"
        "                 'domain': c.get('domain', ''), 'path': c.get('path', '/')}\n"
        "                for c in cookies]\n"
        # OUT 是父程序的 completion marker。必須先讓 Chromium 正常關閉，
        # 再發布檔案；否則父程序一看到 OUT 就會在 finally 對仍在關閉中
        # 的 browser process group 送 TERM/KILL，macOS 便顯示未預期結束。
        "        await b.close()\n"
        "        b = None\n"
        "        tmp = OUT + '.tmp'\n"
        "        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "        with os.fdopen(fd, 'w') as f:\n"
        "            json.dump(data, f)\n"
        "        os.replace(tmp, OUT)\n"
        "        print('COOKIES_EXPORTED', len(data), flush=True)\n"
        "    finally:\n"
        "        if b is not None:\n"
        "            await b.close()\n"
        "asyncio.run(main())\n"
    ) % (
        str(CLOAK["cookie_export_file"]),
        str(ready_file),
        SITE["genuine"],
        expected_binary,
        expected_sha256,
        int(PAGE_LOAD_TIMEOUT_SECONDS * 1000),
        CATALOG_VERIFY_TIMEOUT_SECONDS,
        CRAWL["min_brands"],
        COOKIE_SETTLE_SECONDS,
    )  # noqa: UP031
    err_log = None
    try:
        # 只保留最後一次的 stderr，並在寫入前設為 owner-only。
        error_log_file = CLOAK["error_log_file"]
        _ensure_state_directory(error_log_file.parent)
        err_fd = _open_state_file_no_follow(
            error_log_file,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
        )
        try:
            err_log = os.fdopen(err_fd, "w")
        except BaseException:
            os.close(err_fd)
            raise
        browser_environment = {
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "CLOAKBROWSER_CACHE_DIR",
                "CLOAKBROWSER_BINARY_PATH",
                "DISPLAY",
                "HOME",
                "LANG",
                "LC_ALL",
                "LOGNAME",
                "PATH",
                "PLAYWRIGHT_NODEJS_PATH",
                "PSQ_CLOAK_EXPECTED_SHA256",
                "SHELL",
                "TEMP",
                "TMP",
                "TMPDIR",
                "USER",
                "XAUTHORITY",
            }
        }
        # Browser binary 與 HTTP UA 必須維持同一個已驗證版本；也避免
        # runtime 背景下載。DB 密碼、後台 token 等非必要環境不會傳入。
        browser_environment["CLOAKBROWSER_CACHE_DIR"] = str(free_cache)
        browser_environment["CLOAKBROWSER_AUTO_UPDATE"] = "false"
        browser_environment["HOME"] = str(browser_home)
        browser_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if binary_contract_enabled:
            browser_environment["CLOAKBROWSER_BINARY_PATH"] = expected_binary
        proc = subprocess.Popen(
            [*CLOAK["launcher"], CLOAK["venv_python"], "-u", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=err_log,
            env=browser_environment,
            start_new_session=True,
        )
    except OSError as e:
        if err_log is not None:
            err_log.close()
        log.error("failed to start CloakBrowser via %s: %s", CLOAK["venv_python"], e)
        return False
    with _BROWSER_LOCK:
        shutdown_requested = _BROWSER_SHUTDOWN.is_set()
        if not shutdown_requested:
            _browser_proc = proc
            _browser_err_log = err_log
    if shutdown_requested:
        _terminate_process_group(proc)
        if err_log is not None:
            err_log.close()
        ready_file.unlink(missing_ok=True)
        ready_tmp.unlink(missing_ok=True)
        return False
    log.info("CloakBrowser launching (pid=%s), waiting for browser readiness...", proc.pid)
    deadline = time.monotonic() + BROWSER_START_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            log.error(
                "CloakBrowser exited before becoming ready (rc=%s); see %s",
                proc.returncode,
                CLOAK["error_log_file"],
            )
            _stop_owned_browser()
            return False
        if ready_file.exists():
            if _free_cache_has_pro_artifacts(free_cache):
                log.error("CloakBrowser created Pro artifacts in the dedicated free cache")
                _stop_owned_browser()
                return False
            log.info("CloakBrowser ready (pid=%s)", proc.pid)
            return True
        time.sleep(2)
    log.error(
        "CloakBrowser did not become ready within %.0fs; see %s",
        BROWSER_START_TIMEOUT,
        CLOAK["error_log_file"],
    )
    _stop_owned_browser()
    return False


def _process_group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 群組仍存在，只是目前無權送訊號；不可誤判成已回收。
        return True
    return True


def _terminate_process_group(proc: "subprocess.Popen[bytes]") -> None:
    """終止以 start_new_session 啟動的瀏覽器子程序 process group。"""

    if not _process_group_alive(proc.pid):
        if proc.poll() is None:
            try:
                proc.wait(timeout=BROWSER_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                log.error("browser wrapper did not exit after its process group disappeared")
        return

    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        if proc.poll() is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass

    deadline = time.monotonic() + BROWSER_STOP_GRACE_SECONDS
    while _process_group_alive(proc.pid) and time.monotonic() < deadline:
        if proc.poll() is None:
            try:
                proc.wait(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                pass
        if _process_group_alive(proc.pid):
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    if _process_group_alive(proc.pid):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            if proc.poll() is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass

    if proc.poll() is None:
        try:
            proc.wait(timeout=BROWSER_STOP_GRACE_SECONDS + 1)
        except subprocess.TimeoutExpired:
            log.error("browser wrapper did not exit after process-group SIGKILL")


def _is_cloudflare_challenge(text: str) -> bool:
    """判斷頁面 HTML 是否為 Cloudflare 挑戰頁（managed challenge）。

    同一份邏輯也內嵌在 fetch_page 的子程序腳本（獨立 venv，無法匯入
    本模組），兩處必須保持同步：任何一邊放寬都等於放寬 fail-closed。
    """
    lowered = text.lower()
    return (
        "just a moment" in lowered
        or "verify you are human" in lowered
        or "請稍候" in text
        or "cf-mitigated" in lowered
    )


class NonUnitPageError(Exception):
    """瀏覽器最後落點不是原本請求的 catalog 頁。

    fetch_page 的子程序腳本跑在獨立 venv，無法匯入本例外類別，故改用
    sidecar marker 檔通報；父程序讀到 marker 再轉拋成此例外。HTTP 層只
    會把 unit 頁轉成 NotFoundError，其他 catalog 頁仍維持 fail-closed。"""


def _prepare_browser_launch() -> tuple[dict[str, str], Path, Path, str, str] | None:
    """解析 CloakBrowser 啟動環境並完成 free binary contract 檢查。

    供 fetch_page（頁面抓取）共用安全關鍵的 binary contract 邏輯；
    refresh_session 的 cookie 匯出路徑另有一份相同檢查（保持各自獨立，
    避免改動關鍵刷新路徑）。失敗回傳 None。
    """
    cache_override = os.environ.get("CLOAKBROWSER_CACHE_DIR")
    if cache_override:
        free_cache = Path(cache_override).expanduser()
        private_state_root = free_cache.parent
    else:
        private_state_root = Path(
            os.environ.get("PSQ_CLOAK_STATE_DIR", CLOAK["state_dir"])
        ).expanduser()
        free_cache = private_state_root / "free-browser-cache"
    browser_home = free_cache.parent / "browser-home"
    if any(
        private_path_has_symlink(private_path)
        for private_path in (private_state_root, free_cache, browser_home)
    ):
        log.error("refusing symlinked CloakBrowser private state path")
        return None
    _ensure_state_directory(free_cache)
    if _free_cache_has_pro_artifacts(free_cache):
        log.error("refusing CloakBrowser Pro artifacts in the dedicated free cache")
        return None
    _ensure_state_directory(browser_home)

    expected_binary = os.environ.get("CLOAKBROWSER_BINARY_PATH", "")
    expected_sha256 = os.environ.get("PSQ_CLOAK_EXPECTED_SHA256", "")
    binary_contract_required = os.environ.get("PARTSOUQ_LAUNCHD_JOB") == "1"
    binary_contract_enabled = bool(expected_binary or expected_sha256)
    if (binary_contract_required or binary_contract_enabled) and (
        not expected_binary
        or not expected_sha256
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        log.error("incomplete verified CloakBrowser free binary contract")
        return None
    if binary_contract_enabled:
        try:
            resolved_binary = Path(expected_binary).expanduser().resolve(strict=True)
            relative_binary = resolved_binary.relative_to(free_cache.resolve(strict=True))
        except (OSError, ValueError) as error:
            log.error("verified CloakBrowser binary is outside the dedicated free cache: %s", error)
            return None
        if (
            len(relative_binary.parts) < 2
            or not relative_binary.parts[0].startswith("chromium-")
            or "pro" in relative_binary.parts[0].lower()
        ):
            log.error("verified CloakBrowser binary is not in a free Chromium version directory")
            return None
        expected_binary = str(resolved_binary)
        try:
            with resolved_binary.open("rb") as binary_file:
                binary_sha256 = hashlib.file_digest(binary_file, "sha256").hexdigest()
        except OSError as error:
            log.error("could not read verified CloakBrowser binary: %s", error)
            return None
        if binary_sha256 != expected_sha256:
            log.error("verified CloakBrowser binary SHA256 mismatch")
            return None

    allowed_env = {
        "CLOAKBROWSER_CACHE_DIR",
        "CLOAKBROWSER_BINARY_PATH",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOGNAME",
        "PATH",
        "PLAYWRIGHT_NODEJS_PATH",
        "PSQ_CLOAK_EXPECTED_SHA256",
        "SHELL",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USER",
        "XAUTHORITY",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed_env}
    env["CLOAKBROWSER_CACHE_DIR"] = str(free_cache)
    env["HOME"] = str(browser_home)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if expected_binary:
        env["CLOAKBROWSER_BINARY_PATH"] = expected_binary
    if expected_sha256:
        env["PSQ_CLOAK_EXPECTED_SHA256"] = expected_sha256
    return env, free_cache, browser_home, expected_binary, expected_sha256


def fetch_page(url: str, *, timeout_seconds: float = PAGE_FETCH_TIMEOUT_SECONDS) -> str | None:
    """透過真實 CloakBrowser 抓取單一 catalog 頁面 HTML。

    requests 傳輸即使帶有效 cf_clearance，仍會被 Cloudflare 以 TLS
    fingerprint 擋下（403 managed challenge 或轉址挑戰）。真實瀏覽器具備
    真實 fingerprint，載入同一份 cookie 即可通過 —— 這是 requests 被挑戰
    時的最後手段。

    回傳頁面 HTML；若瀏覽器仍拿到挑戰頁、或任何失敗，回傳 None（保留
    fail-closed：HTTP 層會依此決定是否放棄該請求，絕不把挑戰頁當成
    資料寫入）。"""
    global _browser_err_log, _browser_proc

    with _process_refresh_lock() as acquired:
        if not acquired:
            log.error("browser fetch lock held by another process; skip %s", url[:80])
            return None
        prepared = _prepare_browser_launch()
        if prepared is None:
            return None
        env, free_cache, browser_home, _binary, _sha = prepared
        cookie_file = CLOAK["cookie_file"]
        out_file = CLOAK["state_dir"] / ".cloak-page-fetch.html"
        marker_file = out_file.with_name(out_file.name + ".offunit")
        out_tmp_file = out_file.with_name(out_file.name + ".tmp")
        marker_tmp_file = marker_file.with_name(marker_file.name + ".tmp")
        script = (
            "import asyncio, json, os, time\n"
            "from urllib.parse import urlsplit, parse_qs\n"
            "import cloakbrowser\n"
            f"COOKIE_FILE = {str(cookie_file)!r}\n"
            f"URL = {url!r}\n"
            f"OUT = {str(out_file)!r}\n"
            f"MARKER = {str(marker_file)!r}\n"
            "IDENTITY_KEYS = {\n"
            "    '/en/catalog/genuine': (),\n"
            "    '/en/catalog/genuine/locate': ('c',),\n"
            "    '/en/catalog/genuine/pick': ('c', 'model'),\n"
            "    '/en/catalog/genuine/vehicle': ('c', 'vid'),\n"
            "    '/en/catalog/genuine/unit': ('uid',),\n"
            "}\n"
            "UNIT_CONTEXT_KEYS = ('c', 'vid', 'cid')\n"
            "def _same_catalog_page(requested, final):\n"
            "    requested_parts = urlsplit(requested)\n"
            "    final_parts = urlsplit(final)\n"
            "    requested_path = requested_parts.path.rstrip('/')\n"
            "    final_path = final_parts.path.rstrip('/')\n"
            "    keys = IDENTITY_KEYS.get(requested_path)\n"
            "    if (keys is None or final_path != requested_path\n"
            "            or requested_parts.scheme.lower() != 'https'\n"
            "            or final_parts.scheme.lower() != 'https'\n"
            "            or (requested_parts.hostname or '').lower()\n"
            "               not in ('partsouq.com', 'www.partsouq.com')\n"
            "            or (final_parts.hostname or '').lower()\n"
            "               not in ('partsouq.com', 'www.partsouq.com')\n"
            "            or requested_parts.port not in (None, 443)\n"
            "            or final_parts.port not in (None, 443)):\n"
            "        return False\n"
            "    requested_query = parse_qs(requested_parts.query)\n"
            "    final_query = parse_qs(final_parts.query)\n"
            "    if requested_path == '/en/catalog/genuine/unit':\n"
            "        keys += tuple(key for key in UNIT_CONTEXT_KEYS\n"
            "                      if requested_query.get(key, [None])[0] is not None)\n"
            "    return all(requested_query.get(key, [None])[0] is not None\n"
            "               and requested_query.get(key, [None])[0]\n"
            "               == final_query.get(key, [None])[0] for key in keys)\n"
            "async def main():\n"
            "    b = await cloakbrowser.launch_async(headless=False)\n"
            "    try:\n"
            "        ctx = await b.new_context(viewport={'width':1366,'height':900})\n"
            "        try:\n"
            "            raw = json.load(open(COOKIE_FILE))\n"
            "            await ctx.add_cookies(\n"
            "                [{'name': c['name'], 'value': c['value'],\n"
            "                  'domain': c.get('domain', 'partsouq.com'),\n"
            "                  'path': c.get('path', '/')} for c in raw]\n"
            "            )\n"
            "        except Exception:\n"
            "            pass\n"
            "        page = await ctx.new_page()\n"
            f"        await page.goto(URL, wait_until='domcontentloaded', "
            f"timeout={int(PAGE_LOAD_TIMEOUT_SECONDS * 1000)})\n"
            f"        deadline = time.monotonic() + {PAGE_FETCH_CHALLENGE_TIMEOUT_SECONDS!r}\n"
            "        html = ''\n"
            "        while time.monotonic() < deadline:\n"
            "            html = await page.content()\n"
            "            low = html.lower()\n"
            "            if ('just a moment' not in low and 'verify you are human' not in low\n"
            "                    and '\\u8acb\\u6682\\u5019' not in html and 'cf-mitigated' not in low):\n"
            "                break\n"
            "            await asyncio.sleep(3)\n"
            "        # 落點不是原本請求的 catalog 頁：\n"
            "        # 寫 sidecar marker 而非 HTML，父程序據此拒絕（fail-closed）。\n"
            "        final_url = page.url\n"
            "        out_tmp = OUT + '.tmp'\n"
            "        marker_tmp = MARKER + '.tmp'\n"
            "        if not _same_catalog_page(URL, final_url):\n"
            "            with open(marker_tmp, 'w') as f:\n"
            "                f.write(final_url)\n"
            "            os.replace(marker_tmp, MARKER)\n"
            "        else:\n"
            "            with open(out_tmp, 'w') as f:\n"
            "                f.write(html)\n"
            "            os.replace(out_tmp, OUT)\n"
            "    finally:\n"
            "        await b.close()\n"
            "asyncio.run(main())\n"
        )
        err_fd = _open_state_file_no_follow(
            CLOAK["error_log_file"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        )
        proc: subprocess.Popen[bytes] | None = None
        try:
            err_log = os.fdopen(err_fd, "w")
        except BaseException:
            os.close(err_fd)
            raise
        fd, tmp_script = tempfile.mkstemp(suffix=".py", text=True)
        with os.fdopen(fd, "w") as f:
            f.write(script)
        try:
            for stale_file in (out_file, marker_file, out_tmp_file, marker_tmp_file):
                stale_file.unlink(missing_ok=True)
            proc = subprocess.Popen(
                [*CLOAK["launcher"], CLOAK["venv_python"], "-u", tmp_script],
                stdout=subprocess.DEVNULL,
                stderr=err_log,
                env=env,
                start_new_session=True,
            )
            shutdown_requested = False
            with _BROWSER_LOCK:
                shutdown_requested = _BROWSER_SHUTDOWN.is_set()
                if not shutdown_requested:
                    _browser_proc = proc
                    _browser_err_log = err_log
            if shutdown_requested:
                _terminate_process_group(proc)
                return None
            deadline = time.monotonic() + timeout_seconds
            output_ready = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                if out_file.exists() or marker_file.exists():
                    output_ready = True
                    break
                time.sleep(2)
            if output_ready and proc.poll() is None:
                try:
                    proc.wait(timeout=BROWSER_STOP_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    log.error(
                        "browser fetch child did not exit after producing output for %s; "
                        "terminating",
                        url[:80],
                    )
                    _terminate_process_group(proc)
            elif proc.poll() is None:
                log.error("browser fetch timed out for %s; terminating", url[:80])
            # wrapper 可能已自行結束，但它啟動的 Chromium 仍留在相同
            # process group；只看 poll()/wait() 會留下孤兒瀏覽器。
            if _process_group_alive(proc.pid):
                _terminate_process_group(proc)
            if not out_file.exists() and not marker_file.exists():
                log.error("browser fetch produced no output for %s", url[:80])
                return None
            if marker_file.exists():
                final = marker_file.read_text(encoding="utf-8", errors="replace").strip()
                marker_file.unlink(missing_ok=True)
                out_file.unlink(missing_ok=True)
                log.warning("browser fetch landed off-unit (%s); refusing %s", final, url[:80])
                raise NonUnitPageError(final)
            try:
                html = out_file.read_text(encoding="utf-8", errors="replace")
            finally:
                out_file.unlink(missing_ok=True)
        finally:
            with _BROWSER_LOCK:
                if proc is not None and _browser_proc is proc:
                    _browser_proc = None
                    _browser_err_log = None
            err_log.close()
            os.unlink(tmp_script)
            out_tmp_file.unlink(missing_ok=True)
            marker_tmp_file.unlink(missing_ok=True)
        if _is_cloudflare_challenge(html):
            log.warning("browser fetch returned a challenge page for %s; refusing", url[:80])
            return None
        return html


def refresh_session() -> Cookies | None:
    """完整刷新流程：啟動 CloakBrowser、解決驗證、匯出 cookie。

    跨程序互斥：先取得 process lock，再走同程序內的 single-flight。
    Single-flight：並行呼叫者會等同一場刷新完成，而不是各自啟動瀏覽器。
    成功回傳 cookie 列表；失敗回傳 None。失敗的退避時間隨連續失敗
    次數指數成長（60s → 120s → 240s，上限 20 分鐘），避免封鎖期間
    反覆啟動瀏覽器造成「刷新失敗風暴」。
    """
    with _process_refresh_lock() as lock_acquired:
        if not lock_acquired:
            log.error("refresh lock held by another process; refresh skipped")
            return None
        # 等待跨程序鎖時，另一個 crawler 可能已完成刷新並發布 cookie。
        # 取得鎖後重新讀磁碟，避免本程序立刻再開一個瀏覽器。
        _seed_from_disk(replace_stale=True)
        with _SESSION_COND:
            while _session_state["busy"]:
                _SESSION_COND.wait()
            # 若剛才刷新成功且尚未過期，直接沿用，不需動瀏覽器
            if (
                _session_state["cookies"]
                and time.monotonic() - _session_state["ok_ts"] < COOKIE_TTL
            ):
                return _session_state["cookies"]
            # 退避期間（上次刷新失敗）共用失敗結果（P1 修復）：等待中的
            # worker 直接拿到 None，而不是輪流成為 leader 各自再刷一次。
            # 退避期過後的下一次呼叫才會真正再刷。
            if time.monotonic() < _session_state["retry_after"]:
                return None
            _session_state["busy"] = True

        try:
            cookies = _refresh_impl()
            with _SESSION_COND:
                if cookies:
                    _session_state["cookies"] = cookies
                    _session_state["ok_ts"] = time.monotonic()
                    _session_state["retry_after"] = 0.0
                    _session_state["failures"] = 0
                    _session_state["version"] = _cf_value(cookies)
                    # 新版本已生效；舊的被拒版本不再需要防重播。
                    _rejected_versions.clear()
            return cookies
        finally:
            with _SESSION_COND:
                _session_state["busy"] = False
                _SESSION_COND.notify_all()


def session_backoff_remaining() -> float:
    """回傳距離下次允許刷新剩餘的秒數（0 = 現在就可以刷新）。

    HTTP 層在 challenge 重試的等待中呼叫：舊碼固定睡 max(65, 15*n)
    秒，完全無視 cloak 的指數退避（60s→120s→…→1200s），結果退避
    窗口還沒走完就放棄了（P2 修復）。對齊後重試節奏與退避一致。
    """
    with _SESSION_COND:
        remaining = _session_state["retry_after"] - time.monotonic()
    return max(0.0, remaining)


def reject_session(rejected_version: str | None = None) -> None:
    """淘汰被站方拒絕的 session，且只刪除磁碟上的同一版本。"""
    with _SESSION_COND:
        rejected = rejected_version or _session_state.get("version")
        if not rejected:
            return
        _rejected_versions.add(rejected)
        if _session_state.get("version") == rejected:
            _session_state["cookies"] = None
            _session_state["ok_ts"] = 0.0
            _session_state["version"] = None

    with _process_refresh_lock() as lock_acquired:
        if not lock_acquired:
            log.warning("could not lock persisted session while rejecting version")
            return
        cookie_file = CLOAK["cookie_file"]
        try:
            persisted = load_cookies(cookie_file)
            if _cf_value(persisted) == rejected:
                cookie_file.unlink(missing_ok=True)
        except OSError as error:
            log.warning("failed to remove rejected persisted session: %s", error)


def force_refresh_session(rejected_version: str | None = None) -> Cookies | None:
    """強制刷新 cookie：無視 TTL，即使快取仍新鮮也重新解決驗證。

    用於 HTTP 層收到 challenge（403/429/驗證頁）的情境 —— 此時快取
    中的 cookie 已被伺服器拒絕，繼續沿用只會重複踩雷。仍保留
    single-flight，併發呼叫者共用同一場刷新。

    rejected_version（SOL review P2）：呼叫端持有的 cookie 的
    cf_clearance 值（被伺服器拒絕的版本）。若全域 session 已由其他
    worker 刷新成**不同版本**（持舊 cookie 的延遲 challenge 在新
    cookie 產生後才返回），直接沿用新 cookie、不啟動瀏覽器 ——
    舊碼無條件清掉目前 cookie，會把別人剛刷好的新 cookie 清掉並
    再啟動一次瀏覽器。沿用只限「仍在 TTL 內」的快取；已過期則照常
    清掉重刷（否則會拿過期 session 白打一輪再被 challenge）。

    注意：**不清** failures／retry_after（P1 修復）—— 強制刷新代表
    主動重試，但若這次又失敗，退避計數必須繼續累積（否則連續失敗
    永遠停在 60 秒第一階，變成固定的 60/60/60 打點）。退避由
    refresh_session 在發起前檢查，force 只是清掉「可用 cookie」。

    被拒版本記入 _rejected_versions：清掉快取後，_seed_from_disk 就
    不會把同一份被拒 cookie 從磁碟重新載入（SOL review P3 修復）。
    """
    with _SESSION_COND:
        if (
            rejected_version is not None
            and _session_state["cookies"]
            and _session_state["version"] != rejected_version
            and time.monotonic() - _session_state["ok_ts"] < COOKIE_TTL
        ):
            # 全域 session 已是更新版本（且仍在 TTL 內）：直接沿用，
            # 不再清掉重刷
            return _session_state["cookies"]
        rejected = rejected_version or _session_state.get("version")
    reject_session(rejected)
    return refresh_session()


def _refresh_impl() -> Cookies | None:
    """實際刷新；離開時一律回收本次擁有的 browser process group。"""
    export_file = CLOAK["cookie_export_file"]
    export_temp_file = export_file.with_name(f"{export_file.name}.tmp")
    _ensure_state_directory(export_file.parent)
    for path in (export_file, export_temp_file):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    _stop_owned_browser()
    exported = False
    try:
        if not _launch_cloak():
            _mark_refresh_failed()
            return None

        # 等待瀏覽器內部腳本驗證實際型錄頁後匯出 cookie。
        deadline = time.monotonic() + COOKIE_EXPORT_TIMEOUT
        last_progress_log = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() - last_progress_log >= 30:
                last_progress_log = time.monotonic()
                log.info(
                    "waiting for cookie export... %ds remaining",
                    int(deadline - time.monotonic()),
                )
            if export_file.exists():
                # 子程序以預設 umask 寫出，先限縮成 owner-only 再讀，
                # 避免 cf_clearance 短暫暴露給本機其他使用者。
                try:
                    export_file.chmod(0o600)
                except OSError:
                    pass
                cookies = load_cookies(export_file)
                if cookies:
                    names = {c["name"] for c in cookies}
                    if "cf_clearance" not in names:
                        log.error(
                            "exported cookies missing cf_clearance (%s); refresh failed",
                            sorted(names),
                        )
                        _mark_refresh_failed()
                        return None
                    save_cookies(cookies)
                    log.info(
                        "session cookies exported: %s (has cf_clearance=%s)",
                        sorted(names),
                        "cf_clearance" in names,
                    )
                    exported = True
                    return cookies
                log.warning("cookie export is empty or invalid")
            with _BROWSER_LOCK:
                proc = _browser_proc
            if proc is not None and proc.poll() is not None:
                log.error(
                    "CloakBrowser exited before verified catalog cookie export (rc=%s)",
                    proc.returncode,
                )
                _mark_refresh_failed()
                return None
            time.sleep(3)
        log.error("no cookies exported within %ss", COOKIE_EXPORT_TIMEOUT)
        _mark_refresh_failed()
        return None
    finally:
        _stop_owned_browser(graceful=exported)
        # 無論成功與否都清掉暫存匯出檔（cookie 已在 save_cookies 落地）。
        for path in (export_file, export_temp_file):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def _mark_refresh_failed() -> None:
    """刷新失敗：退避時間隨連續失敗次數指數成長。

    1 次失敗 60s、2 次 120s、3 次 240s…… 上限 20 分鐘。
    連續失敗代表 Cloudflare 可能正在封鎖我們，退避要拉長而不是
    固定 60 秒就重試一次（那會造成反覆啟動瀏覽器的風暴）。
    """
    with _SESSION_COND:
        _session_state["failures"] = _session_state.get("failures", 0) + 1
        n = _session_state["failures"]
        delay = min(REFRESH_RETRY_BACKOFF * (2 ** (n - 1)), REFRESH_BACKOFF_MAX)
        _session_state["retry_after"] = time.monotonic() + delay
        log.warning("refresh failed (%d consecutive); backing off %.0fs", n, delay)


def _seed_from_disk(*, replace_stale: bool = False) -> None:
    """程序啟動時把上次持久化的 cookie 載入記憶體（只做一次）。

    每個新程序（daemon 每趟 run 都是新 process）不該為了「不知道
    磁碟上有沒有新鮮 cookie」就重啟 CloakBrowser、重解一次 Turnstile：
    短時間內連續重解會觸發 Cloudflare 標記，正是 2026-08-20 首次
    成功後連續 5 次 403 的原因。以檔案 mtime 當成功時間：TTL 內的
    話直接沿用，到期/被拒時才由 refresh_session 重啟瀏覽器。
    版本已在本程序被伺服器拒絕過的（_rejected_versions）跳過不載入，
    否則 force_refresh 白清快取、403 迴圈重演。
    """
    with _SESSION_COND:
        if _session_state["cookies"] is not None and not replace_stale:
            return
    try:
        if not CLOAK["cookie_file"].exists():
            return
        mtime = CLOAK["cookie_file"].stat().st_mtime
        age = time.time() - mtime
        if age < 0 or age >= COOKIE_TTL:
            return
        cookies = load_cookies(CLOAK["cookie_file"])
        if not cookies or not _cf_value(cookies):
            return
    except OSError:
        return
    version = _cf_value(cookies)
    if version in _rejected_versions:
        log.info("skipping persisted session cookies previously rejected by server")
        return
    with _SESSION_COND:
        if (
            _session_state["cookies"] is not None
            and time.monotonic() - _session_state["ok_ts"] < COOKIE_TTL
        ):
            return
        _session_state["cookies"] = cookies
        _session_state["ok_ts"] = time.monotonic() - age
        _session_state["version"] = version
        log.info(
            "reusing persisted session cookies (%s, %ds old)",
            "cf_clearance" if version else "no cf_clearance",
            int(age),
        )


def get_session() -> Cookies | None:
    """取得可用的 cookie：快取仍新鮮就直接回傳，否則執行刷新。

    可在多執行緒下安全呼叫：底層的刷新是 single-flight。
    新程序第一次呼叫時會先嘗試沿用磁碟上的新鮮 cookie（_seed_from_disk），
    避免每次 run 都重啟瀏覽器。
    """
    _seed_from_disk()
    with _SESSION_COND:
        if _session_state["cookies"] and time.monotonic() - _session_state["ok_ts"] < COOKIE_TTL:
            return _session_state["cookies"]
    # TTL 已過期（或完全沒有快取）：執行一次刷新
    return refresh_session()
