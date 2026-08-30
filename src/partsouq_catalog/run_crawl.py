"""CLI 進入點：python3 -m partsouq_catalog.run_crawl [--brand TOYOTA] [--fresh]

執行整趟 PartSouq 爬取。可續爬：先前完成的型號/車型會自動跳過。
若爬取途中 Cloudflare 的 cookie 過期，HTTP 層會自動透過 CloakBrowser
刷新 session。

本模組是「組合根」（composition root）：組裝資料庫連線、Repository、
HTTP 工作階段與爬蟲服務，然後交給服務層執行 —— 本身不含業務邏輯。
"""

import argparse
import fcntl
import logging
import os
import signal
import sys
from collections.abc import Callable
from functools import partial
from pathlib import Path

from .admission import (
    AdmissionLockBusy,
    CatalogRuntimeLockBusy,
    acquire_catalog_runtime_lock,
)
from .cloak import _begin_browser_shutdown, _finish_browser_shutdown
from .config import CRAWL, LOG_DIR, load_cookies
from .crawler import Crawler
from .db import Database
from .governor import RequestGovernor
from .http_client import SessionManager
from .state_files import (
    PrivateRotatingFileHandler,
    ensure_private_state_directory,
    open_private_state_file,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="PartSouq 全站爬蟲")
    parser.add_argument("--brand", default=None, help="只爬這個品牌（例如 Toyota）")
    parser.add_argument("--fresh", action="store_true", help="執行前先清除爬取進度（從頭開始）")
    parser.add_argument(
        "--no-browser", action="store_true", help="只用已存 cookie，碰到驗證就直接失敗（除錯用）"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=int(CRAWL.get("workers", 4)),
        help="並行車型 worker 數（預設取自 PSQ_WORKERS 或 4）",
    )
    parser.add_argument(
        "--recover-only",
        action="store_true",
        help="只重抓 fetched_status IS NULL 的孤兒組並結束，不跑整趟爬取；"
        "與正式爬取共用 crawler.lock，不可和線上 daemon 並行",
    )
    args = parser.parse_args()

    ensure_private_state_directory(LOG_DIR)
    # 由 launchd 啟動時 stdout 會寫入無上限的 launchd.out.log：
    # 此時只寫輪替檔，不重複寫 stdout。
    handlers: list[logging.Handler] = [
        # 20 MB x 5 輪替：跑好幾天的爬蟲不能讓日誌無限長大
        PrivateRotatingFileHandler(
            LOG_DIR / "crawl.log",
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
        ),
    ]
    if "LAUNCHD_JOB" not in os.environ:
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    log = logging.getLogger("main")

    if args.brand:
        CRAWL["start_brand"] = args.brand

    scheduled_job_run_id = int(CRAWL["scheduled_job_run_id"])
    scheduled_scope_invalid = scheduled_job_run_id > 0 and (
        int(CRAWL["limit_parts"]) != 0
        or int(CRAWL["bounded_parts"]) != 10_000
        or not str(CRAWL["bounded_brand"]).strip()
        or not str(CRAWL["bounded_model"]).strip()
        or int(CRAWL["vehicle_year_window"]) <= 0
    )
    if scheduled_scope_invalid:
        log.error(
            "scheduled catalog crawl requires PSQ_LIMIT_PARTS=0, "
            "PSQ_BOUNDED_PARTS=10000, non-empty PSQ_BOUNDED_BRAND/PSQ_BOUNDED_MODEL, "
            "and positive PSQ_VEHICLE_YEAR_WINDOW"
        )
        root_logger = logging.getLogger()
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()
        return 64

    # supervisor 的 flock 只能防兩個 supervisor；直接 CLI（尤其
    # --fresh）也必須共用 crawler lock，否則兩趟 run 會同時重設 state
    # 與發布 snapshot。
    configured_state_dir = os.getenv("PSQ_SCHEDULER_STATE_DIR", "").strip()
    lock_dir = (
        Path(configured_state_dir).expanduser().absolute() if configured_state_dir else LOG_DIR
    )
    # recover 也會改 group receipt 與 part membership，必須和 full crawl
    # 共用鎖；兩者併行會讓正式 run 的發布來源被 recover run 拆走。
    lock_path = lock_dir / "crawler.lock"
    try:
        ensure_private_state_directory(lock_dir)
        lock_descriptor = open_private_state_file(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_APPEND,
        )
        try:
            lock_fd = os.fdopen(lock_descriptor, "a")
        except BaseException:
            os.close(lock_descriptor)
            raise
    except BaseException:
        root_logger = logging.getLogger()
        for handler in handlers:
            root_logger.removeHandler(handler)
            handler.close()
        raise
    previous_sigterm = signal.getsignal(signal.SIGTERM)
    sigterm_installed = False

    def terminate(signum: int, _frame: object) -> None:
        # Supervisor._kill_current() 送 SIGTERM；轉成可展開 finally 的
        # SystemExit，讓 run_crawl 先回收 owned browser process group。
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise SystemExit(128 + signum)

    try:
        signal.signal(signal.SIGTERM, terminate)
        sigterm_installed = True
    except ValueError:
        # 測試可能從非 main thread 呼叫；正式 CLI 一定在 main thread。
        pass
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.error("another crawler holds the lock; exiting")
            return 2

        # 組合根：組裝各層元件。--fresh 的 reset 已移到 Crawler.run，
        # 與 start_run 在同一交易，不會在 cookie 初始化失敗時先毀進度。
        db = Database().connect()
        runtime_lock_connection = db.open_owner_connection()
        try:
            runtime_lease = acquire_catalog_runtime_lock(runtime_lock_connection)
        except CatalogRuntimeLockBusy:
            log.error("another catalog crawler owns the database runtime lock; exiting")
            return 2
        crawler: Crawler | None = None
        # 只讀既有 cookie；此處不得呼叫 get_session()。第一次真正的 HTTP
        # 請求才由 SessionManager.ensure_fresh() 視需要啟動瀏覽器，確保
        # Crawler.run 已先取得 migration admission lock 並提交 running
        # marker。schema migration 進行中時，CLI 因此能在零瀏覽器啟動下
        # 以 exit 75 延後。
        cookies = load_cookies()
        if cookies is None:
            log.warning(
                "no cookies available%s",
                " (no-browser mode; crawling without cookies)"
                if args.no_browser
                else "; challenge will auto-refresh",
            )

        # 全站共用的 request governor：主 session（_brands() 等直發請求）
        # 與 Crawler 的 worker session 共用同一實例，每個 wire request
        # 都受全域限流（SOL P1）。
        governor = RequestGovernor(CRAWL["request_rate"], CRAWL["request_burst"])
        http = SessionManager(cookies, no_browser=args.no_browser, gov=governor)
        crawler = Crawler(
            http,
            db,
            workers=args.workers,
            governor=governor,
            fresh=args.fresh,
            runtime_guard=runtime_lease.assert_owned,
        )
        if args.recover_only:
            if crawler.sample_mode or crawler.bounded_mode or crawler.part_limit:
                log.error("recover-only requires PSQ_LIMIT_PARTS=0 and PSQ_BOUNDED_PARTS=0")
                return 64
            # 跳過整趟 run()，直接收斂孤兒組；部分失敗要回傳非零狀態。
            try:
                recovered = crawler.recover_null_groups()
            except AdmissionLockBusy:
                log.warning("schema migration in progress; recover-only deferred before writing")
                return 75
            except Exception:
                log.exception("recover-only 未完整收斂")
                return 1
            log.info("recover-only 完成：收斂 %d 組", recovered)
            return 0
        try:
            counts = crawler.run()
        except AdmissionLockBusy:
            log.warning("schema migration in progress; crawler deferred before writing")
            return 75
        log.info("crawl complete: %s (status=%s)", counts, crawler.last_status)
        # 全站與正式 bounded dataset 成功均是 exit 0；sample 是
        # 未發布的預期停止，仍保留獨立 exit 3。
        if crawler.last_status in ("success", "bounded_success"):
            return 0
        if crawler.last_status == "sample":
            return 3
        if crawler.last_status == "bounded_under_target":
            return 4
        return 1
    finally:
        active_error = sys.exception()
        cleanup_errors: list[Exception] = []

        def cleanup(label: str, action: Callable[[], object]) -> None:
            try:
                action()
            except Exception as error:
                cleanup_errors.append(error)
                log.exception("%s failed during crawler shutdown", label)

        # scheduler 的 SIGINT 會讓主執行緒先進入這裡；必須在等待 worker
        # pool 前回收目前 browser process group，否則 worker 可能卡在
        # fetch_page，而已脫離 scheduler 子群組的 Chromium 會變成孤兒。
        cleanup("owned browser shutdown", _begin_browser_shutdown)
        if "crawler" in locals() and crawler is not None:
            cleanup("crawler worker pool close", crawler.close)
        cleanup("browser shutdown state reset", _finish_browser_shutdown)
        if "runtime_lease" in locals():
            cleanup("catalog runtime lease close", runtime_lease.close)
        if "runtime_lock_connection" in locals() and runtime_lock_connection.open:
            cleanup("catalog runtime owner connection close", runtime_lock_connection.close)
        if "db" in locals():
            cleanup("database close", db.close)
        cleanup("crawler lock close", lock_fd.close)
        if sigterm_installed:
            cleanup(
                "SIGTERM handler restore",
                lambda: signal.signal(signal.SIGTERM, previous_sigterm),
            )
        root_logger = logging.getLogger()
        for handler in handlers:
            cleanup(
                "log handler detach",
                partial(root_logger.removeHandler, handler),
            )
            cleanup("log handler close", handler.close)
        if active_error is None and cleanup_errors:
            raise cleanup_errors[0]


if __name__ == "__main__":
    sys.exit(main())
