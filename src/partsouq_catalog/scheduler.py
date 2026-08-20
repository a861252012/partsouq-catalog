"""單一排程入口：PartSouq 型錄與 NHTSA 同步共用同一個 MySQL 資料庫。"""

from __future__ import annotations

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import IO, TextIO

import pymysql

from .config import BASE_DIR, DB_CONFIG, LOG_DIR

MAX_OUTPUT_CHARS = 60_000
OUTPUT_CHUNK_CHARS = 8_192
CHILD_TERMINATE_GRACE_SECONDS = 5
INTERRUPTED_EXIT_CODE = 125
LOCK_BUSY_EXIT_CODE = 75
# 中斷回收的「最小年齡」：只有 started_at 早於此值的 running 排程才會
# 被自動標 failed/interrupted —— 剛在另一台主機啟動、還在正常執行的
# run 不會被誤殺（跨主機共享 MySQL 時本機 flock 擋不到）。
RECOVERY_MIN_AGE_SECONDS = 900
NHTSA_BULK_COMPLETED = "stage=bulk_completed"
NHTSA_API_COMPLETED = "stage=api_completed"
DAEMON_JOBS = ("catalog", "nhtsa", "pending")
DEFAULT_INTERVAL_SECONDS = {
    "catalog": 30 * 24 * 60 * 60,
    "nhtsa": 24 * 60 * 60,
    "pending": 30,
}
_ACTIVE_CHILD: subprocess.Popen[str] | None = None
_SHUTDOWN_EVENT: threading.Event | None = None
_JOB_CONTEXT = threading.local()


def _connect() -> pymysql.connections.Connection[pymysql.cursors.Cursor]:
    return pymysql.connect(
        host=str(DB_CONFIG["host"]),
        port=int(str(DB_CONFIG["port"])),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        database=str(DB_CONFIG["database"]),
        autocommit=True,
    )


def _dict_connect() -> pymysql.connections.Connection[pymysql.cursors.DictCursor]:
    return pymysql.connect(
        host=str(DB_CONFIG["host"]),
        port=int(str(DB_CONFIG["port"])),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        database=str(DB_CONFIG["database"]),
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def _record_start(job_name: str) -> int:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(job_name, trigger_mode, status, started_at) "
                "VALUES (%s, %s, 'running', UTC_TIMESTAMP())",
                (job_name, getattr(_JOB_CONTEXT, "trigger_mode", "manual")),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def _record_finish(
    run_id: int, return_code: int, output: str, success_codes: tuple[int, ...] = (0,)
) -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_job_runs "
                "SET status = %s, finished_at = UTC_TIMESTAMP(), exit_code = %s, output_text = %s "
                "WHERE id = %s",
                (
                    "completed" if return_code in success_codes else "failed",
                    return_code,
                    output[-MAX_OUTPUT_CHARS:],
                    run_id,
                ),
            )
    finally:
        connection.close()


def _record_progress(run_id: int, marker: str) -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_job_runs SET output_text = "
                "RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s), "
                "finished_at = CASE WHEN %s THEN UTC_TIMESTAMP() ELSE finished_at END "
                "WHERE id = %s AND status = 'running'",
                (f"{marker}\n", MAX_OUTPUT_CHARS, marker == NHTSA_API_COMPLETED, run_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"scheduled job run {run_id} is not running")
    finally:
        connection.close()


def _pending_requests() -> list[dict[str, object]]:
    connection = _dict_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id, job_name, requested_scope FROM admin_crawl_requests "
                "WHERE status = 'pending' ORDER BY requested_at, id"
            )
            return list(cursor.fetchall())
    finally:
        connection.close()


def _claim_request(request_id: int) -> bool:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_crawl_requests SET status = 'running', started_at = UTC_TIMESTAMP() "
                "WHERE id = %s AND status = 'pending'",
                (request_id,),
            )
            return bool(cursor.rowcount == 1)
    finally:
        connection.close()


def _finish_request(
    request_id: int,
    return_code: int,
    error_message: str | None = None,
) -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_crawl_requests SET status = %s, finished_at = UTC_TIMESTAMP(), "
                "error_message = %s, requested_scope = CASE WHEN job_name = 'nhtsa-vin' "
                "THEN CONCAT(LEFT(requested_scope, 3), '**********', RIGHT(requested_scope, 4)) "
                "ELSE requested_scope END WHERE id = %s",
                (
                    "completed" if return_code == 0 else "failed",
                    None
                    if return_code == 0
                    else error_message or f"scheduler exit code {return_code}",
                    request_id,
                ),
            )
    finally:
        connection.close()


def _requeue_interrupted_requests() -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_crawl_requests "
                "SET status = 'pending', started_at = NULL, finished_at = NULL, "
                "error_message = 'scheduler interrupted; request recovered automatically' "
                "WHERE status = 'running'"
            )
    finally:
        connection.close()


def _defer_request(request_id: int) -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_crawl_requests "
                "SET status = 'pending', started_at = NULL, finished_at = NULL, "
                "error_message = 'job deferred or interrupted; scheduler will retry' "
                "WHERE id = %s AND status = 'running'",
                (request_id,),
            )
    finally:
        connection.close()


def _stream_chunks(stream: IO[str], vin: str | None) -> Iterator[str]:
    if not vin:
        while chunk := stream.readline(OUTPUT_CHUNK_CHARS):
            yield chunk
        return

    masked_vin = f"{vin[:3]}**********{vin[-4:]}"
    candidate = ""
    while chunk := stream.readline(OUTPUT_CHUNK_CHARS):
        output: list[str] = []
        for character in chunk:
            candidate += character
            while candidate and not vin.startswith(candidate.upper()):
                output.append(candidate[0])
                candidate = candidate[1:]
            if len(candidate) == len(vin):
                output.append(masked_vin)
                candidate = ""
        if output:
            yield "".join(output)
    if candidate:
        yield candidate


def _terminate_child(child: subprocess.Popen[str]) -> None:
    try:
        child.send_signal(signal.SIGINT)
    except OSError:
        return

    def kill_if_needed() -> None:
        if child.poll() is None:
            try:
                child.kill()
            except OSError:
                pass

    timer = threading.Timer(CHILD_TERMINATE_GRACE_SECONDS, kill_if_needed)
    timer.daemon = True
    timer.start()


def _shutdown_requested() -> bool:
    return _SHUTDOWN_EVENT is not None and _SHUTDOWN_EVENT.is_set()


def _run(job_name: str, command: list[str], success_codes: tuple[int, ...] = (0,)) -> int:
    global _ACTIVE_CHILD

    try:
        run_id = _record_start(job_name)
    except pymysql.MySQLError as error:
        print(f"無法記錄 {job_name} 排程：{error}", file=sys.stderr)
        return 1

    child_environment = os.environ.copy()
    child_environment["SCHEDULED_JOB_RUN_ID"] = str(run_id)
    if _shutdown_requested():
        output = f"{job_name} 尚未啟動，scheduler 已收到停止訊號\n"
        _record_finish(run_id, INTERRUPTED_EXIT_CODE, output)
        return INTERRUPTED_EXIT_CODE

    vin = command[4].strip().upper() if job_name == "nhtsa-vin" and len(command) > 4 else None
    output_parts: deque[str] = deque()
    output_chars = 0
    output_was_streamed = False
    process: subprocess.Popen[str] | None = None
    try:
        lock_fd = getattr(_JOB_CONTEXT, "lock_fd", None)
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            env=child_environment,
            text=True,
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=(() if lock_fd is None else (int(lock_fd),)),
        )
        _ACTIVE_CHILD = process
        if _shutdown_requested():
            _terminate_child(process)
        if process.stdout is None:
            raise RuntimeError(f"{job_name} 沒有可讀取的 stdout")
        for chunk in _stream_chunks(process.stdout, vin):
            print(chunk, end="", flush=True)
            output_was_streamed = True
            output_parts.append(chunk)
            output_chars += len(chunk)
            while output_chars > MAX_OUTPUT_CHARS:
                overflow = output_chars - MAX_OUTPUT_CHARS
                first = output_parts[0]
                if len(first) <= overflow:
                    output_parts.popleft()
                    output_chars -= len(first)
                else:
                    output_parts[0] = first[overflow:]
                    output_chars -= overflow
        process.wait()
        return_code = int(process.returncode or 0)
    except (OSError, RuntimeError) as error:
        output_parts.append(f"無法啟動 {job_name}：{error}\n")
        return_code = 127
    finally:
        if process is not None and process.poll() is None:
            _terminate_child(process)
            try:
                process.wait(timeout=CHILD_TERMINATE_GRACE_SECONDS + 1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        _ACTIVE_CHILD = None
    output = "".join(output_parts)
    if output and not output_was_streamed:
        print(output, end="" if output.endswith("\n") else "\n")
    try:
        _record_finish(run_id, return_code, output, success_codes)
    except pymysql.MySQLError as error:
        print(f"無法完成 {job_name} 的排程紀錄：{error}", file=sys.stderr)
        return 1
    # 成功碼（含 catalog 的 sample 預期停止）視為完成，回傳 0 給呼叫端，
    # 避免 daemon 把「已完成的樣本跑」當失敗而無限重試。
    return 0 if return_code in success_codes else return_code


def _job_family(job: str) -> str:
    if job.startswith("nhtsa"):
        return "nhtsa"
    return job


def _try_lock(prefix: str, job: str) -> TextIO | None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOG_DIR / f"{prefix}-{_job_family(job)}.lock", "a")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    return lock_file


def _recover_interrupted_job_runs(job: str) -> bool:
    family = _job_family(job)
    if family == "pending":
        return False
    connection = _connect()
    try:
        connection.begin()
        with connection.cursor() as cursor:
            recovered_complete = False
            if family == "catalog":
                # 對帳不依賴排程器自身的 PSQ_BOUNDED_PARTS 環境變數：
                # daemon 重啟時若環境變數遺失，仍能認出「已發布的
                # bounded run」（target/parts_ok/snapshot 三者一致）。
                cursor.execute(
                    "UPDATE scheduled_job_runs AS jobs "
                    "JOIN crawl_runs AS runs ON runs.scheduled_job_run_id = jobs.id "
                    "JOIN (SELECT crawl_run_id, COUNT(*) AS snapshot_rows "
                    "FROM bounded_parts GROUP BY crawl_run_id) AS snapshots "
                    "ON snapshots.crawl_run_id = runs.id "
                    "SET jobs.status = 'completed', jobs.finished_at = runs.finished_at, "
                    "jobs.exit_code = 0, jobs.output_text = "
                    "RIGHT(CONCAT(COALESCE(jobs.output_text, ''), %s), %s) "
                    "WHERE jobs.status = 'running' AND jobs.job_name = 'catalog' "
                    "AND jobs.trigger_mode = 'daemon' "
                    "AND runs.dataset_kind = 'bounded' "
                    "AND runs.status = 'bounded_success' "
                    "AND runs.finished_at IS NOT NULL "
                    "AND runs.target_parts > 0 "
                    "AND runs.parts_ok = runs.target_parts "
                    "AND snapshots.snapshot_rows = runs.target_parts",
                    (
                        "\nbounded publish committed before scheduler interruption; "
                        "completion reconciled automatically\n",
                        MAX_OUTPUT_CHARS,
                    ),
                )
                recovered_complete = cursor.rowcount > 0

            if family == "nhtsa":
                cursor.execute(
                    "UPDATE scheduled_job_runs SET status = 'completed', "
                    "finished_at = COALESCE(finished_at, started_at), exit_code = 0, "
                    "output_text = RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s) "
                    "WHERE status = 'running' AND job_name = 'nhtsa' "
                    "AND trigger_mode = 'daemon' AND output_text LIKE %s",
                    (
                        "\nNHTSA API completed before scheduler interruption; "
                        "completion reconciled automatically\n",
                        MAX_OUTPUT_CHARS,
                        f"%{NHTSA_API_COMPLETED}%",
                    ),
                )
                recovered_complete = cursor.rowcount > 0
                cursor.execute(
                    "UPDATE nhtsa_sync_runs SET status = 'interrupted', "
                    "updated_at = UTC_TIMESTAMP(6), ended_at = UTC_TIMESTAMP(6), "
                    "error_message = 'scheduler interrupted; recovered automatically' "
                    "WHERE status = 'running' AND run_key REGEXP "
                    "'^nhtsa-(bulk|api|vin)-[0-9]{8}T[0-9]{6}Z$'"
                )
                cursor.execute(
                    "UPDATE scheduled_job_runs SET status = 'failed', "
                    "finished_at = UTC_TIMESTAMP(), exit_code = %s, "
                    "output_text = RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s) "
                    "WHERE status = 'running' AND job_name LIKE 'nhtsa%%'",
                    (
                        INTERRUPTED_EXIT_CODE,
                        "\nprevious scheduler interrupted; recovered automatically\n",
                        MAX_OUTPUT_CHARS,
                    ),
                )
            else:
                cursor.execute(
                    "UPDATE scheduled_job_runs SET status = 'failed', "
                    "finished_at = UTC_TIMESTAMP(), exit_code = %s, "
                    "output_text = RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s) "
                    "WHERE status = 'running' AND job_name = %s "
                    "AND started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                    (
                        INTERRUPTED_EXIT_CODE,
                        "\nprevious scheduler interrupted; recovered automatically\n",
                        MAX_OUTPUT_CHARS,
                        family,
                        RECOVERY_MIN_AGE_SECONDS,
                    ),
                )
            if family == "catalog":
                cursor.execute(
                    "UPDATE crawl_runs AS runs "
                    "JOIN scheduled_job_runs AS jobs ON jobs.id = runs.scheduled_job_run_id "
                    "SET runs.status = 'interrupted', "
                    "runs.finished_at = COALESCE(runs.finished_at, UTC_TIMESTAMP()) "
                    "WHERE runs.status = 'running' AND jobs.status = 'failed' "
                    "AND jobs.job_name = 'catalog' "
                    "AND jobs.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                    (RECOVERY_MIN_AGE_SECONDS,),
                )
        connection.commit()
        return recovered_complete
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def dispatch_locked(job: str, scope: str) -> int:
    lock_file = _try_lock("scheduler-job", job)
    if lock_file is None:
        print(f"{job} 已由另一個排程執行，保留工作並稍後重試", file=sys.stderr)
        return LOCK_BUSY_EXIT_CODE
    previous_lock_fd = getattr(_JOB_CONTEXT, "lock_fd", None)
    _JOB_CONTEXT.lock_fd = lock_file.fileno()
    try:
        try:
            recovered_complete = _recover_interrupted_job_runs(job)
        except pymysql.MySQLError as error:
            print(f"無法回收 {job} 的中斷排程：{error}", file=sys.stderr)
            return 1
        if (
            recovered_complete
            and job in ("catalog", "nhtsa")
            and getattr(_JOB_CONTEXT, "trigger_mode", "manual") == "daemon"
        ):
            print(f"{job} 已從完整資料對帳完成；略過重跑", flush=True)
            return 0
        return dispatch(job, scope)
    finally:
        if previous_lock_fd is None:
            del _JOB_CONTEXT.lock_fd
        else:
            _JOB_CONTEXT.lock_fd = previous_lock_fd
        lock_file.close()


def _seconds_until_next_run(job: str, interval_seconds: int) -> float:
    if job == "pending":
        return 0.0
    connection = _dict_connect()
    try:
        with connection.cursor() as cursor:
            if job == "catalog":
                cursor.execute(
                    "SELECT jobs.job_name, jobs.status, "
                    "TIMESTAMPDIFF(SECOND, jobs.finished_at, UTC_TIMESTAMP()) AS age_seconds, "
                    "runs.dataset_kind, runs.status AS crawl_status, runs.target_parts, "
                    "runs.parts_ok, (SELECT COUNT(*) FROM bounded_parts AS bounded "
                    "WHERE bounded.crawl_run_id = runs.id) AS snapshot_rows "
                    "FROM scheduled_job_runs AS jobs "
                    "LEFT JOIN crawl_runs AS runs ON runs.scheduled_job_run_id = jobs.id "
                    "WHERE jobs.job_name = 'catalog' AND jobs.trigger_mode = 'daemon' "
                    "ORDER BY jobs.started_at DESC, jobs.id DESC LIMIT 1"
                )
            else:
                cursor.execute(
                    "SELECT job_name, status, "
                    "TIMESTAMPDIFF(SECOND, finished_at, UTC_TIMESTAMP()) AS age_seconds "
                    "FROM scheduled_job_runs WHERE job_name = %s "
                    "AND trigger_mode = 'daemon' "
                    "ORDER BY started_at DESC, id DESC LIMIT 1",
                    (job,),
                )
            row = cursor.fetchone()
    finally:
        connection.close()
    expected_success_job = job
    if (
        not row
        or row.get("job_name") != expected_success_job
        or row.get("status") != "completed"
        or row.get("age_seconds") is None
    ):
        return 0.0
    if job == "catalog":
        try:
            bounded_target = int(os.getenv("PSQ_BOUNDED_PARTS", "0"))
        except ValueError:
            bounded_target = 0
        if bounded_target > 0:
            if (
                row.get("dataset_kind") != "bounded"
                or row.get("crawl_status") != "bounded_success"
                or int(row.get("target_parts") or 0) != bounded_target
                or int(row.get("parts_ok") or 0) != bounded_target
                or int(row.get("snapshot_rows") or 0) != bounded_target
            ):
                return 0.0
        elif row.get("crawl_status") not in ("success", "bounded_success", "sample"):
            # 無 bounded 設定時，已完成的全站跑或樣本跑都算完成，
            # 依 interval 等待下一次排程，避免立即重跑。
            return 0.0
    age_seconds = row["age_seconds"]
    return max(0.0, interval_seconds - max(0, int(age_seconds)))


def _wait_for_daemon_lock(job: str, stop_event: threading.Event) -> TextIO | None:
    while not stop_event.is_set():
        lock_file = _try_lock("scheduler-daemon", job)
        if lock_file is not None:
            return lock_file
        print(f"另一個 {job} daemon 已在執行；等待接手", file=sys.stderr)
        stop_event.wait(5)
    return None


def run_daemon(
    job: str,
    scope: str,
    interval_seconds: int,
    retry_base_seconds: int,
    retry_max_seconds: int,
    *,
    stop_event: threading.Event,
) -> int:
    daemon_lock = _wait_for_daemon_lock(job, stop_event)
    if daemon_lock is None:
        return 0
    previous_trigger_mode = getattr(_JOB_CONTEXT, "trigger_mode", None)
    _JOB_CONTEXT.trigger_mode = "daemon"
    failures = 0
    schedule_read_failures = 0
    completion_check_failures = 0
    wait_seconds = 0.0
    needs_schedule_check = job != "pending"
    announce_completion = False
    try:
        while not stop_event.wait(wait_seconds):
            if needs_schedule_check:
                try:
                    wait_seconds = _seconds_until_next_run(job, interval_seconds)
                except pymysql.MySQLError as error:
                    schedule_read_failures += 1
                    wait_seconds = float(
                        min(
                            retry_max_seconds,
                            retry_base_seconds * (2 ** min(schedule_read_failures - 1, 20)),
                        )
                    )
                    print(
                        f"無法讀取 {job} 排程進度：{error}；{int(wait_seconds)} 秒後重新讀取",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue
                schedule_read_failures = 0
                needs_schedule_check = False
                if wait_seconds > 0:
                    if announce_completion:
                        print(
                            f"{job} 排程完成；{int(wait_seconds)} 秒後再執行",
                            flush=True,
                        )
                    announce_completion = False
                    needs_schedule_check = True
                    continue
                if announce_completion:
                    # 子程序回報成功但 DB 驗證未過（例如環境變數不一致）：
                    # 用獨立的指數退避重查，而不是每 retry_base 就重發一次
                    # 新的完整爬取。
                    completion_check_failures += 1
                    wait_seconds = float(
                        min(
                            retry_max_seconds,
                            retry_base_seconds * (2 ** min(completion_check_failures - 1, 20)),
                        )
                    )
                    announce_completion = False
                    needs_schedule_check = True
                    print(
                        f"{job} 子程序成功，但完成狀態尚未通過資料庫驗證；"
                        f"{int(wait_seconds)} 秒後重新檢查",
                        file=sys.stderr,
                        flush=True,
                    )
                    continue

            return_code = dispatch_locked(job, scope)
            if stop_event.is_set():
                break
            if return_code == 0:
                failures = 0
                completion_check_failures = 0
                if job == "pending":
                    wait_seconds = float(interval_seconds)
                    print(f"{job} 排程完成；{int(wait_seconds)} 秒後再執行", flush=True)
                else:
                    wait_seconds = 0.0
                    needs_schedule_check = True
                    announce_completion = True
                continue

            failures += 1
            retry_seconds = min(
                retry_max_seconds,
                retry_base_seconds * (2 ** min(failures - 1, 20)),
            )
            wait_seconds = float(retry_seconds)
            if job != "pending":
                needs_schedule_check = True
            print(
                f"{job} 排程失敗（exit={return_code}）；{int(wait_seconds)} 秒後自動重試",
                file=sys.stderr,
                flush=True,
            )
        return 0
    finally:
        if previous_trigger_mode is None:
            del _JOB_CONTEXT.trigger_mode
        else:
            _JOB_CONTEXT.trigger_mode = previous_trigger_mode
        daemon_lock.close()


def _env_seconds(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as error:
        raise ValueError(f"{name} 必須是正整數") from error
    if value <= 0:
        raise ValueError(f"{name} 必須是正整數")
    return value


def _install_signal_handlers(stop_event: threading.Event) -> None:
    global _SHUTDOWN_EVENT
    _SHUTDOWN_EVENT = stop_event

    def stop(_signum: int, _frame: object) -> None:
        stop_event.set()
        child = _ACTIVE_CHILD
        if child is not None and child.poll() is None:
            _terminate_child(child)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _nhtsa_bulk_completed_for_retry() -> bool:
    if getattr(_JOB_CONTEXT, "trigger_mode", "manual") != "daemon":
        return False
    connection = _dict_connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, output_text FROM scheduled_job_runs "
                "WHERE job_name = 'nhtsa' AND trigger_mode = 'daemon' "
                "AND started_at >= UTC_TIMESTAMP() - INTERVAL 1 DAY "
                "ORDER BY started_at DESC, id DESC LIMIT 1"
            )
            row = cursor.fetchone()
    finally:
        connection.close()
    return bool(
        row
        and row.get("status") == "failed"
        and NHTSA_BULK_COMPLETED in str(row.get("output_text") or "")
        and NHTSA_API_COMPLETED not in str(row.get("output_text") or "")
    )


def _run_nhtsa(scope: str) -> int:
    if scope != "all":
        print("nhtsa composite 只支援 scope=all；個別 scope 請使用子工作", file=sys.stderr)
        return 2
    try:
        resume_api = _nhtsa_bulk_completed_for_retry()
    except pymysql.MySQLError as error:
        print(f"無法讀取 nhtsa stage：{error}", file=sys.stderr)
        return 1
    try:
        parent_run_id = _record_start("nhtsa")
    except pymysql.MySQLError as error:
        print(f"無法記錄 nhtsa 排程：{error}", file=sys.stderr)
        return 1

    completed_stages: list[str] = []
    bulk_result = 0 if resume_api else dispatch("nhtsa-bulk", scope)
    if bulk_result == 0:
        completed_stages.append(NHTSA_BULK_COMPLETED)
        try:
            _record_progress(parent_run_id, NHTSA_BULK_COMPLETED)
        except (pymysql.MySQLError, RuntimeError) as error:
            print(f"無法記錄 nhtsa bulk stage：{error}", file=sys.stderr)
            return_code = 1
        else:
            return_code = dispatch("nhtsa-api", scope)
            if return_code == 0:
                completed_stages.append(NHTSA_API_COMPLETED)
                try:
                    _record_progress(parent_run_id, NHTSA_API_COMPLETED)
                except (pymysql.MySQLError, RuntimeError) as error:
                    print(f"無法記錄 nhtsa API stage：{error}", file=sys.stderr)
    else:
        return_code = bulk_result
    output = (
        "".join(f"{stage}\n" for stage in completed_stages)
        + f"nhtsa composite finished with exit code {return_code}; see child job rows\n"
    )
    try:
        _record_finish(parent_run_id, return_code, output)
    except pymysql.MySQLError as error:
        print(f"無法完成 nhtsa 的排程紀錄：{error}", file=sys.stderr)
        return 1
    return return_code


def _nhtsa_run_id(kind: str) -> str:
    return f"nhtsa-{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PartSouq / NHTSA 統一排程入口")
    parser.add_argument(
        "--job",
        required=True,
        choices=("catalog", "nhtsa-bulk", "nhtsa-api", "nhtsa-vin", "nhtsa", "pending"),
        help="catalog 僅跑型錄；nhtsa 依序執行 bulk 與 API；pending 消費後台要求。",
    )
    parser.add_argument("--scope", default="all", help="NHTSA 同步範圍，預設 all。")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="常駐並依 interval 自動執行；僅支援 catalog、nhtsa、pending。",
    )
    parser.add_argument("--interval-seconds", type=int, default=None)
    parser.add_argument("--retry-base-seconds", type=int, default=None)
    parser.add_argument("--retry-max-seconds", type=int, default=None)
    return parser


def dispatch(job: str, scope: str) -> int:
    if job == "pending":
        try:
            _requeue_interrupted_requests()
            requests = _pending_requests()
        except pymysql.MySQLError as error:
            print(f"無法讀取後台爬取要求：{error}", file=sys.stderr)
            return 1

        exit_code = 0
        for request in requests:
            request_id = int(str(request["id"]))
            previous_trigger_mode = getattr(_JOB_CONTEXT, "trigger_mode", None)
            _JOB_CONTEXT.trigger_mode = "queue"
            try:
                if not _claim_request(request_id):
                    continue
                if str(request["job_name"]) == "catalog":
                    _finish_request(
                        request_id,
                        2,
                        "catalog is handled by the dedicated catalog daemon",
                    )
                    continue
                return_code = dispatch_locked(
                    str(request["job_name"]), str(request["requested_scope"])
                )
                if return_code in (LOCK_BUSY_EXIT_CODE, INTERRUPTED_EXIT_CODE) or return_code < 0:
                    _defer_request(request_id)
                    if return_code != LOCK_BUSY_EXIT_CODE:
                        exit_code = 1
                    continue
                _finish_request(request_id, return_code)
                if return_code != 0:
                    exit_code = 1
            except pymysql.MySQLError as error:
                print(f"無法更新後台爬取要求 {request_id}：{error}", file=sys.stderr)
                return 1
            finally:
                if previous_trigger_mode is None:
                    del _JOB_CONTEXT.trigger_mode
                else:
                    _JOB_CONTEXT.trigger_mode = previous_trigger_mode
        return exit_code

    if job == "catalog":
        workers = os.getenv("PSQ_WORKERS", "1")
        return _run(
            "catalog",
            [sys.executable, "-m", "partsouq_catalog.run_crawl", "--workers", workers],
            # run_crawl 對「達上限的樣本跑」回傳 exit 3（預期停止，資料已
            # 寫入）；排程情境下視為完成，不應無窮重試。
            success_codes=(0, 3),
        )

    if job == "nhtsa-bulk":
        return _run(
            "nhtsa-bulk",
            [
                sys.executable,
                "-m",
                "partsouq_crawler",
                "nhtsa-sync-bulk",
                "--scope",
                scope,
                "--run-id",
                _nhtsa_run_id("bulk"),
            ],
        )

    if job == "nhtsa-api":
        return _run(
            "nhtsa-api",
            [
                sys.executable,
                "-m",
                "partsouq_crawler",
                "nhtsa-sync-api",
                "--scope",
                scope,
                "--run-id",
                _nhtsa_run_id("api"),
            ],
        )

    if job == "nhtsa-vin":
        return _run(
            "nhtsa-vin",
            [
                sys.executable,
                "-m",
                "partsouq_crawler",
                "nhtsa-decode-vin",
                scope,
                "--run-id",
                _nhtsa_run_id("vin"),
            ],
        )

    return _run_nhtsa(scope)


def main() -> int:
    args = build_parser().parse_args()
    if not args.daemon:
        return dispatch_locked(args.job, args.scope)
    if args.job not in DAEMON_JOBS:
        print("daemon 僅支援 catalog、nhtsa、pending", file=sys.stderr)
        return 2
    try:
        interval_seconds = (
            args.interval_seconds
            if args.interval_seconds is not None
            else _env_seconds(
                f"SCHEDULER_{args.job.replace('-', '_').upper()}_INTERVAL_SECONDS",
                DEFAULT_INTERVAL_SECONDS[args.job],
            )
        )
        retry_base_seconds = (
            args.retry_base_seconds
            if args.retry_base_seconds is not None
            else _env_seconds("SCHEDULER_RETRY_BASE_SECONDS", 60)
        )
        retry_max_seconds = (
            args.retry_max_seconds
            if args.retry_max_seconds is not None
            else _env_seconds("SCHEDULER_RETRY_MAX_SECONDS", 3600)
        )
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    if min(interval_seconds, retry_base_seconds, retry_max_seconds) <= 0:
        print("排程間隔與重試秒數都必須是正整數", file=sys.stderr)
        return 2
    if retry_base_seconds > retry_max_seconds:
        print("retry-base-seconds 不可大於 retry-max-seconds", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    _install_signal_handlers(stop_event)
    return run_daemon(
        args.job,
        args.scope,
        interval_seconds,
        retry_base_seconds,
        retry_max_seconds,
        stop_event=stop_event,
    )


if __name__ == "__main__":
    raise SystemExit(main())
