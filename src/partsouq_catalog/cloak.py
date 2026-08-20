"""CloakBrowser 工作階段管理員（基礎設施層）。

啟動隱匿版 Chromium（CloakBrowser），導向 partsouq.com 讓 Cloudflare
的 Turnstile 驗證自動通過，然後匯出 session 的 cookie
（cf_clearance + PHPSESSID）供高速 HTTP 爬蟲使用。

全程無人介入：碰到 Cloudflare 驗證時，瀏覽器本身會自動解決 ——
拜 CloakBrowser 對指紋的原始碼層修補所賜。

執行緒安全：所有刷新都走 single-flight 鎖，因此 N 個並行 worker
同時呼叫時，只會啟動一個 CloakBrowser；其餘 worker 等待結果並沿用
（見 get_session / refresh_session）。
"""

# ruff: noqa: UP031  -- 底下 `%` 格式是刻意保留：內嵌子程序腳本含有大量
# `{}`（dict literal），改用 .format()/f-string 會與腳本內容衝突。
import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.request

from .config import CLOAK, SITE, save_cookies

log = logging.getLogger("cloak")

CDP_START_TIMEOUT = 60.0
COOKIE_EXPORT_TIMEOUT = 180.0

# 只管理本程序親自啟動的 CloakBrowser process group。不得用 pkill 掃描
# 共用機器上的命令列，否則會誤殺其他專案的 CloakBrowser。
_BROWSER_LOCK = threading.Lock()
_browser_proc = None
_browser_err_log = None

# ---------------------------------------------------------------------------
# 全域 single-flight 的 session 狀態
# ---------------------------------------------------------------------------
_SESSION_LOCK = threading.Lock()
_SESSION_COND = threading.Condition(_SESSION_LOCK)
_session_state = {
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


def _cf_value(cookies) -> str:
    """從 cookie 列表取出 cf_clearance 的值（無則回傳空字串）。

    cf_clearance 每次刷新必然改變，是可靠的 session 版本訊號
    （與 http_client 的 _cf_value 同義；這裡各自定義避免循環 import）。
    """
    for c in cookies or []:
        if c.get("name") == "cf_clearance":
            return c.get("value", "")
    return ""


@contextlib.contextmanager
def _process_refresh_lock():
    """跨程序 refresh 互斥鎖（single-flight 只管得到同程序內執行緒）。

    CloakBrowser 的 CDP port 是全域資源：兩個 crawler 程序同時啟動
    瀏覽器會互搶 port、互相誤殺。用 flock 鎖檔串列化 refresh；鎖被
    佔用超過期限時放棄（fail-closed），呼叫端拿到 acquired=False。
    """
    lock_path = CLOAK["lock_file"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, "a+")
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


def _stop_owned_browser():
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
        running = proc.poll() is None
        if running:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                running = False
            except PermissionError:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    running = False
        if running:
            try:
                proc.wait(timeout=5)
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
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    log.error("owned CloakBrowser process group did not exit after SIGKILL")
    finally:
        if err_log is not None:
            err_log.close()


def _cdp_alive() -> bool:
    """檢查 CDP 端點是否回應（瀏覽器是否已就緒）。"""
    try:
        with urllib.request.urlopen(f"{CLOAK['cdp_host']}/json/version", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def _launch_cloak() -> bool:
    """啟動本程序擁有的 CloakBrowser process group 並等待 CDP。

    子程序腳本負責：驅動 CloakBrowser 前往目標頁面、等待 Turnstile
    驗證自動通過、最後把 session cookie 匯出成 JSON 檔案。
    不再仰賴 agent-browser 的常駐 daemon（其長時間運行曾造成
    cookie 匯出不穩定的問題）。
    """
    global _browser_err_log, _browser_proc

    if _cdp_alive():
        log.error("CDP port %s is occupied by an unowned browser", CLOAK["cdp_port"])
        return False
    script = (
        "import asyncio, json, os, time, cloakbrowser\n"
        "OUT = %r\n"
        "SITE = %r\n"
        "async def main():\n"
        "    b = await cloakbrowser.launch_async(\n"
        "        headless=False, stealth_args=False,\n"
        "        args=['--remote-debugging-port=%d'])\n"
        "    ctx = await b.new_context(viewport={'width': 1366, 'height': 900})\n"
        "    page = await ctx.new_page()\n"
        "    await page.goto(SITE, wait_until='domcontentloaded', timeout=60000)\n"
        "    deadline = time.time() + 150\n"
        "    while time.time() < deadline:\n"
        "        try:\n"
        "            body = await page.evaluate('document.body ? document.body.innerText.length : 0')\n"
        "            title = await page.title()\n"
        "            if body > 20000 or 'CATALOGS' in title:\n"
        "                break\n"
        "        except Exception:\n"
        "            pass\n"
        "        await asyncio.sleep(3)\n"
        "    await asyncio.sleep(2)\n"
        "    cookies = await ctx.cookies()\n"
        "    data = [{'name': c['name'], 'value': c['value'],\n"
        "             'domain': c.get('domain', ''), 'path': c.get('path', '/')}\n"
        "            for c in cookies]\n"
        "    fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)\n"
        "    with os.fdopen(fd, 'w') as f:\n"
        "        json.dump(data, f)\n"
        "    print('COOKIES_EXPORTED', len(data), flush=True)\n"
        "    await b.close()\n"
        "asyncio.run(main())\n"
    ) % (str(CLOAK["cookie_export_file"]), SITE["genuine"], CLOAK["cdp_port"])  # noqa: UP031
    err_log = None
    try:
        # 只保留最後一次的 stderr，並在寫入前設為 owner-only。
        err_fd = os.open(
            "/tmp/cloak_launch_err.log",
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            0o600,
        )
        os.fchmod(err_fd, 0o600)
        err_log = os.fdopen(err_fd, "w")
        proc = subprocess.Popen(
            [CLOAK["venv_python"], "-u", "-c", script],
            stdout=subprocess.DEVNULL,
            stderr=err_log,
            start_new_session=True,
        )
    except OSError as e:
        if err_log is not None:
            err_log.close()
        log.error("failed to start CloakBrowser via %s: %s", CLOAK["venv_python"], e)
        return False
    with _BROWSER_LOCK:
        _browser_proc = proc
        _browser_err_log = err_log
    log.info("CloakBrowser launching (pid=%s), waiting for CDP...", proc.pid)
    deadline = time.time() + CDP_START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            log.error("CloakBrowser exited before CDP became ready (rc=%s)", proc.returncode)
            _stop_owned_browser()
            return False
        if _cdp_alive():
            log.info("CloakBrowser CDP ready on :%s", CLOAK["cdp_port"])
            return True
        time.sleep(2)
    log.error("CloakBrowser CDP never became ready")
    _stop_owned_browser()
    return False


def refresh_session() -> list | None:
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


def force_refresh_session(rejected_version: str | None = None) -> list | None:
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
        if rejected:
            _rejected_versions.add(rejected)
        _session_state["cookies"] = None
        _session_state["ok_ts"] = 0.0
        _session_state["version"] = None
    return refresh_session()


def _refresh_impl() -> list | None:
    """實際刷新；離開時一律回收本次擁有的 browser process group。"""
    export_file = CLOAK["cookie_export_file"]
    try:
        export_file.unlink(missing_ok=True)
    except OSError:
        pass

    _stop_owned_browser()
    try:
        if not _launch_cloak():
            _mark_refresh_failed()
            return None

        # 等待瀏覽器內部的腳本匯出 cookie（它會自動解決 Turnstile 驗證）
        deadline = time.time() + COOKIE_EXPORT_TIMEOUT
        last_progress_log = 0.0
        while time.time() < deadline:
            if time.time() - last_progress_log >= 30:
                last_progress_log = time.time()
                log.info("waiting for cookie export... %ds elapsed", int(deadline - time.time()))
            if export_file.exists():
                # 子程序以預設 umask 寫出，先限縮成 owner-only 再讀，
                # 避免 cf_clearance 短暫暴露給本機其他使用者。
                try:
                    export_file.chmod(0o600)
                except OSError:
                    pass
                try:
                    cookies = json.loads(export_file.read_text())
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
                        return cookies
                except (json.JSONDecodeError, OSError) as e:
                    log.warning("cookie export not ready yet: %s", e)
            time.sleep(3)
        log.error("no cookies exported within %ss", COOKIE_EXPORT_TIMEOUT)
        _mark_refresh_failed()
        return None
    finally:
        _stop_owned_browser()
        # 無論成功與否都清掉暫存匯出檔（cookie 已在 save_cookies 落地）。
        try:
            export_file.unlink(missing_ok=True)
        except OSError:
            pass


def _mark_refresh_failed():
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


def _seed_from_disk() -> None:
    """程序啟動時把上次持久化的 cookie 載入記憶體（只做一次）。

    每個新程序（daemon 每趟 run 都是新 process）不該為了「不知道
    磁碟上有沒有新鮮 cookie」就重啟 CloakBrowser、重解一次 Turnstile：
    短時間內連續重解會觸發 Cloudflare 標記，正是 2026-08-20 首次
    成功後連續 5 次 403 的原因。以檔案 mtime 當成功時間：TTL 內的
    話直接沿用，到期/被拒時才由 refresh_session 重啟瀏覽器。
    版本已在本程序被伺服器拒絕過的（_rejected_versions）跳過不載入，
    否則 force_refresh 白清快取、403 迴圈重演。
    """
    if _session_state["cookies"] is not None:
        return
    try:
        if not CLOAK["cookie_file"].exists():
            return
        mtime = CLOAK["cookie_file"].stat().st_mtime
        age = time.time() - mtime
        if age < 0 or age >= COOKIE_TTL:
            return
        cookies = json.loads(CLOAK["cookie_file"].read_text())
        if (
            not isinstance(cookies, list)
            or not cookies
            or not all(
                isinstance(cookie, dict)
                and isinstance(cookie.get("name"), str)
                and isinstance(cookie.get("value"), str)
                for cookie in cookies
            )
            or not _cf_value(cookies)
        ):
            return
    except (OSError, json.JSONDecodeError):
        return
    version = _cf_value(cookies)
    if version in _rejected_versions:
        log.info("skipping persisted session cookies previously rejected by server")
        return
    with _SESSION_COND:
        if _session_state["cookies"] is not None:
            return
        _session_state["cookies"] = cookies
        _session_state["ok_ts"] = time.monotonic() - age
        _session_state["version"] = version
        log.info(
            "reusing persisted session cookies (%s, %ds old)",
            "cf_clearance" if version else "no cf_clearance",
            int(age),
        )


def get_session() -> list | None:
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
