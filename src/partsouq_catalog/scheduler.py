"""單一排程入口：PartSouq 型錄與 NHTSA 同步共用同一個 MySQL 資料庫。"""

from __future__ import annotations

import argparse
import codecs
import fcntl
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

import pymysql

from .admission import (
    AdmissionLockBusy,
    acquire_catalog_writer_admission,
    catalog_writer_admission,
    release_catalog_writer_admission,
)
from .config import BASE_DIR, DB_CONFIG, LOG_DIR
from .migrations import (
    STALE_SCHEDULER_EXIT_CODE,
    CatalogMigrationRunner,
    MigrationError,
)
from .state_files import ensure_private_state_directory, open_private_state_file

MAX_OUTPUT_CHARS = 60_000
OUTPUT_CHUNK_CHARS = 8_192
CHILD_TERMINATE_GRACE_SECONDS = 5
CHILD_STALL_TIMEOUT_SECONDS = float(os.getenv("SCHEDULER_CHILD_STALL_TIMEOUT_SECONDS", "600"))
CHILD_PIPE_DRAIN_TIMEOUT_SECONDS = float(
    os.getenv("SCHEDULER_CHILD_PIPE_DRAIN_TIMEOUT_SECONDS", "5")
)
CHILD_STALL_EXIT_CODE = 124
INTERRUPTED_EXIT_CODE = STALE_SCHEDULER_EXIT_CODE
LOCK_BUSY_EXIT_CODE = 75
# scheduler 自身的 DB 紀錄失敗（_record_start/_record_finish/_recover）：
# 與子程序的站台失敗（exit=1）區分，daemon 的 MAX_CONSECUTIVE_FAILURES
# 只該偵測站台封鎖，DB 閃失會自癒、不該把排程靜默掉。
SCHEDULER_DB_ERROR_EXIT_CODE = 2
# 本機 flock 是正式部署的 owner lock；此年齡閘只保護 DB 中剛建立、
# 尚未能確認是否為舊版程序留下的 marker，不宣稱提供多主機 lease。
RECOVERY_MIN_AGE_SECONDS = 900
# 連續失敗上限：達到後停止指數重試，等下一次 interval 再檢查。
# 失敗通常是網站封鎖/驗證無法通過，每小時重試只會反覆重啟瀏覽器
# 錘站（2026-08-20 首次成功後連續 5 次 403 的失敗迴圈）。
MAX_CONSECUTIVE_FAILURES = 5
NHTSA_BULK_COMPLETED = "stage=bulk_completed"
NHTSA_API_COMPLETED = "stage=api_completed"
DAEMON_JOBS = ("catalog", "nhtsa", "pending", "vncs")
DEFAULT_INTERVAL_SECONDS = {
    "catalog": 30 * 24 * 60 * 60,
    "nhtsa": 24 * 60 * 60,
    "pending": 30,
    "vncs": 30 * 24 * 60 * 60,
}
_SHUTDOWN_EVENT: threading.Event | None = None
_JOB_CONTEXT = threading.local()


class ActiveDaemonRun(RuntimeError):
    """The database still contains a recent daemon marker for this family."""


def _connect() -> pymysql.connections.Connection[pymysql.cursors.DictCursor]:
    return pymysql.connect(
        host=str(DB_CONFIG["host"]),
        port=int(str(DB_CONFIG["port"])),
        user=str(DB_CONFIG["user"]),
        password=str(DB_CONFIG["password"]),
        database=str(DB_CONFIG["database"]),
        cursorclass=pymysql.cursors.DictCursor,
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


def _audit_catalog_evidence(
    crawl_run_id: int,
    target_parts: int,
    *,
    allow_running_scheduler: bool = False,
    allow_failed_scheduler: bool = False,
) -> bool:
    from .db import Database
    from .repositories import CrawlRepository

    database = Database().connect()
    try:
        CrawlRepository(database, "scheduler-evidence-audit").audit_run_evidence(
            crawl_run_id,
            target_parts,
            allow_running_scheduler=allow_running_scheduler,
            allow_failed_scheduler=allow_failed_scheduler,
        )
        return True
    except (RuntimeError, ValueError) as error:
        print(
            f"crawl run {crawl_run_id} 的 HTTP evidence 驗證失敗：{type(error).__name__}: {error}",
            file=sys.stderr,
            flush=True,
        )
        return False
    finally:
        database.rollback()
        database.close()


def _record_start(job_name: str, parent_scheduled_job_run_id: int | None = None) -> int:
    connection = _connect()
    try:
        with catalog_writer_admission(connection), connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs "
                "(parent_scheduled_job_run_id, job_name, trigger_mode, status, started_at) "
                "VALUES (%s, %s, %s, 'running', UTC_TIMESTAMP())",
                (
                    parent_scheduled_job_run_id,
                    job_name,
                    getattr(_JOB_CONTEXT, "trigger_mode", "manual"),
                ),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def _record_finish(
    run_id: int, return_code: int, output: str, success_codes: tuple[int, ...] = (0,)
) -> int:
    connection = _connect()
    try:
        connection.begin()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT job_name FROM scheduled_job_runs WHERE id = %s",
                (run_id,),
            )
            scheduled_run = cursor.fetchone()
            if scheduled_run is None:
                raise RuntimeError(f"scheduled job run {run_id} does not exist")
            job_name = str(scheduled_run["job_name"])
            effective_return_code = return_code
            effective_output = output
            if job_name in {"nhtsa-bulk", "nhtsa-api", "nhtsa-vin"}:
                cursor.execute(
                    "SELECT id, status, ended_at, lease_slot, lease_token, lease_expires_at "
                    "FROM nhtsa_sync_runs WHERE scheduled_job_run_id = %s FOR UPDATE",
                    (run_id,),
                )
                domain_run = cursor.fetchone()
                cursor.execute(
                    "SELECT status, finished_at, exit_code FROM scheduled_job_runs "
                    "WHERE id = %s FOR UPDATE",
                    (run_id,),
                )
                child_state = cursor.fetchone()
                if child_state is None:
                    raise RuntimeError(f"scheduled job run {run_id} disappeared")
                exact_atomic_completion = (
                    child_state["status"] == "completed"
                    and child_state["finished_at"] is not None
                    and child_state["exit_code"] == 0
                    and domain_run is not None
                    and domain_run["status"] == "completed"
                    and domain_run["ended_at"] is not None
                    and domain_run["lease_slot"] is None
                    and domain_run["lease_token"] is None
                    and domain_run["lease_expires_at"] is None
                )
                if exact_atomic_completion:
                    effective_return_code = 0
                    if return_code != 0:
                        effective_output += (
                            f"\nNHTSA child process exit {return_code} was observed after "
                            "exact atomic completion; completed state preserved\n"
                        )
                    cursor.execute(
                        "UPDATE scheduled_job_runs SET output_text = "
                        "RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s) WHERE id = %s",
                        (effective_output, MAX_OUTPUT_CHARS, run_id),
                    )
                else:
                    if return_code in success_codes:
                        effective_return_code = 1
                        effective_output += (
                            "\nNHTSA child exited successfully without an exact atomic "
                            "completed tuple\n"
                        )
                    cursor.execute(
                        "UPDATE nhtsa_sync_runs SET status = 'interrupted', "
                        "updated_at = UTC_TIMESTAMP(6), ended_at = UTC_TIMESTAMP(6), "
                        "lease_slot = NULL, lease_token = NULL, lease_expires_at = NULL, "
                        "error_message = 'linked scheduler child failed' "
                        "WHERE scheduled_job_run_id = %s AND status = 'running'",
                        (run_id,),
                    )
                    cursor.execute(
                        "UPDATE scheduled_job_runs "
                        "SET status = 'failed', finished_at = UTC_TIMESTAMP(), "
                        "exit_code = %s, output_text = %s WHERE id = %s",
                        (
                            effective_return_code,
                            effective_output[-MAX_OUTPUT_CHARS:],
                            run_id,
                        ),
                    )
            else:
                cursor.execute(
                    "UPDATE scheduled_job_runs "
                    "SET status = %s, finished_at = UTC_TIMESTAMP(), exit_code = %s, "
                    "output_text = %s WHERE id = %s",
                    (
                        "completed" if effective_return_code in success_codes else "failed",
                        effective_return_code,
                        effective_output[-MAX_OUTPUT_CHARS:],
                        run_id,
                    ),
                )
            if effective_return_code == INTERRUPTED_EXIT_CODE or effective_return_code < 0:
                cursor.execute(
                    "UPDATE crawl_runs SET status = 'interrupted', "
                    "finished_at = COALESCE(finished_at, UTC_TIMESTAMP()) "
                    "WHERE scheduled_job_run_id = %s AND status = 'running'",
                    (run_id,),
                )
        connection.commit()
        return effective_return_code
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _record_progress(run_id: int, marker: str) -> None:
    connection = _connect()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_job_runs SET output_text = "
                "RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s) "
                "WHERE id = %s AND status = 'running'",
                (f"{marker}\n", MAX_OUTPUT_CHARS, run_id),
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
        with catalog_writer_admission(connection), connection.cursor() as cursor:
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
        with catalog_writer_admission(connection), connection.cursor() as cursor:
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


def _signal_child_group(child: subprocess.Popen[bytes], child_signal: signal.Signals) -> None:
    try:
        os.killpg(child.pid, child_signal)
        return
    except ProcessLookupError:
        pass
    except PermissionError:
        pass
    if child.poll() is None:
        try:
            child.send_signal(child_signal)
        except OSError:
            pass


def _terminate_child(child: subprocess.Popen[bytes]) -> threading.Timer:
    _signal_child_group(child, signal.SIGINT)

    def kill_if_needed() -> None:
        # wrapper 可能已退出，但孫程序仍持有 stdout；不能只看 child.poll()。
        _signal_child_group(child, signal.SIGKILL)

    timer = threading.Timer(CHILD_TERMINATE_GRACE_SECONDS, kill_if_needed)
    timer.daemon = True
    timer.start()
    return timer


def _shutdown_requested() -> bool:
    return _SHUTDOWN_EVENT is not None and _SHUTDOWN_EVENT.is_set()


def _run(
    job_name: str,
    command: list[str],
    success_codes: tuple[int, ...] = (0,),
    *,
    parent_scheduled_job_run_id: int | None = None,
) -> int:
    try:
        run_id = _record_start(job_name, parent_scheduled_job_run_id)
    except AdmissionLockBusy:
        print(f"{job_name} 遇到 schema migration；保留工作並稍後重試", file=sys.stderr)
        return LOCK_BUSY_EXIT_CODE
    except (pymysql.MySQLError, RuntimeError) as error:
        print(f"無法記錄 {job_name} 排程：{error}", file=sys.stderr)
        return SCHEDULER_DB_ERROR_EXIT_CODE

    child_environment = os.environ.copy()
    child_environment.pop("LAUNCHD_JOB", None)
    child_environment["SCHEDULED_JOB_RUN_ID"] = str(run_id)
    if job_name in {"nhtsa", "nhtsa-bulk", "nhtsa-api", "nhtsa-vin"}:
        child_environment["NHTSA_HEARTBEAT_INTERVAL_SECONDS"] = str(
            max(0.01, min(60.0, CHILD_STALL_TIMEOUT_SECONDS / 3))
        )
    if _shutdown_requested():
        output = f"{job_name} 尚未啟動，scheduler 已收到停止訊號\n"
        try:
            _record_finish(run_id, INTERRUPTED_EXIT_CODE, output)
        except (pymysql.MySQLError, RuntimeError) as error:
            print(f"無法完成 {job_name} 的排程紀錄：{error}", file=sys.stderr)
            return SCHEDULER_DB_ERROR_EXIT_CODE
        return INTERRUPTED_EXIT_CODE

    vin = command[4].strip().upper() if job_name == "nhtsa-vin" and len(command) > 4 else None
    output_parts: deque[str] = deque()
    output_chars = 0
    output_was_streamed = False
    emit_stdout = "LAUNCHD_JOB" not in os.environ
    process: subprocess.Popen[bytes] | None = None
    stdout_selector: selectors.BaseSelector | None = None
    termination_timer: threading.Timer | None = None
    return_code = 127
    try:
        lock_fd = getattr(_JOB_CONTEXT, "lock_fd", None)
        process = subprocess.Popen(
            command,
            cwd=BASE_DIR,
            env=child_environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            pass_fds=(() if lock_fd is None else (int(lock_fd),)),
            start_new_session=True,
        )
        if process.stdout is None:
            raise RuntimeError(f"{job_name} 沒有可讀取的 stdout")
        stdout_fd = process.stdout.fileno()
        os.set_blocking(stdout_fd, False)
        stdout_selector = selectors.DefaultSelector()
        stdout_selector.register(stdout_fd, selectors.EVENT_READ)
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        vin_candidate = ""
        masked_vin = f"{vin[:3]}**********{vin[-4:]}" if vin else ""
        last_output_at = time.monotonic()
        process_exited_at: float | None = None
        stdout_eof_at: float | None = None
        poll_seconds = min(
            0.5,
            max(
                0.01,
                min(CHILD_STALL_TIMEOUT_SECONDS, CHILD_PIPE_DRAIN_TIMEOUT_SECONDS) / 4,
            ),
        )
        forced_exit_code: int | None = None
        while True:
            if _shutdown_requested():
                forced_exit_code = INTERRUPTED_EXIT_CODE
                output_parts.append(f"{job_name} 收到停止訊號；正在回收子程序\n")
                termination_timer = _terminate_child(process)
                break
            try:
                events = stdout_selector.select(timeout=poll_seconds)
            except OSError as error:
                raise RuntimeError(f"{job_name} stdout selector failed: {error}") from error

            now = time.monotonic()
            if process.poll() is None:
                process_exited_at = None
            elif process_exited_at is None:
                process_exited_at = now

            if events:
                try:
                    raw_chunk = os.read(stdout_fd, OUTPUT_CHUNK_CHARS)
                except BlockingIOError:
                    raw_chunk = None
                except OSError as error:
                    raise RuntimeError(f"{job_name} stdout read failed: {error}") from error

                if raw_chunk is None:
                    chunk = ""
                elif raw_chunk:
                    last_output_at = now
                    chunk = decoder.decode(raw_chunk)
                else:
                    chunk = decoder.decode(b"", final=True)
                    stdout_selector.unregister(stdout_fd)
                    stdout_eof_at = now

                if vin:
                    masked_output: list[str] = []
                    for character in chunk:
                        vin_candidate += character
                        while vin_candidate and not vin.startswith(vin_candidate.upper()):
                            masked_output.append(vin_candidate[0])
                            vin_candidate = vin_candidate[1:]
                        if len(vin_candidate) == len(vin):
                            masked_output.append(masked_vin)
                            vin_candidate = ""
                    if stdout_eof_at is not None and vin_candidate:
                        masked_output.append(vin_candidate)
                        vin_candidate = ""
                    chunk = "".join(masked_output)

                if chunk:
                    if emit_stdout:
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

            if stdout_eof_at is not None:
                if process.poll() is not None:
                    break
                if now - stdout_eof_at >= CHILD_PIPE_DRAIN_TIMEOUT_SECONDS:
                    forced_exit_code = CHILD_STALL_EXIT_CODE
                    output_parts.append(
                        f"{job_name} closed stdout but remained alive; "
                        "terminating owned process group\n"
                    )
                    termination_timer = _terminate_child(process)
                    break
            elif (
                process_exited_at is not None
                and now - process_exited_at >= CHILD_PIPE_DRAIN_TIMEOUT_SECONDS
            ):
                forced_exit_code = CHILD_STALL_EXIT_CODE
                output_parts.append(
                    f"{job_name} wrapper exited but stdout remained open; "
                    "terminating owned process group\n"
                )
                termination_timer = _terminate_child(process)
                break

            if now - last_output_at >= CHILD_STALL_TIMEOUT_SECONDS:
                forced_exit_code = CHILD_STALL_EXIT_CODE
                output_parts.append(
                    f"{job_name} produced no output for "
                    f"{CHILD_STALL_TIMEOUT_SECONDS:g}s; terminating owned process group\n"
                )
                termination_timer = _terminate_child(process)
                break

            if stdout_eof_at is not None:
                remaining_drain_seconds = CHILD_PIPE_DRAIN_TIMEOUT_SECONDS - (now - stdout_eof_at)
                if remaining_drain_seconds > 0:
                    try:
                        process.wait(timeout=min(poll_seconds, remaining_drain_seconds))
                    except subprocess.TimeoutExpired:
                        pass
        if forced_exit_code is None:
            process.wait()
            return_code = int(process.returncode or 0)
        else:
            return_code = forced_exit_code
    except (OSError, RuntimeError) as error:
        output_parts.append(f"{job_name} 執行失敗：{error}\n")
        return_code = 127
    finally:
        if stdout_selector is not None:
            stdout_selector.close()
        if process is not None:
            if process.poll() is None and termination_timer is None:
                termination_timer = _terminate_child(process)
            if process.poll() is None:
                try:
                    process.wait(timeout=CHILD_TERMINATE_GRACE_SECONDS + 1)
                except subprocess.TimeoutExpired:
                    _signal_child_group(process, signal.SIGKILL)
                    process.wait()
            else:
                process.wait()
            if process.stdout is not None and not process.stdout.closed:
                process.stdout.close()
        if termination_timer is not None:
            child_group_alive = False
            if process is not None:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    child_group_alive = True
                else:
                    child_group_alive = True
            if not child_group_alive:
                termination_timer.cancel()
            termination_timer.join()
    output = "".join(output_parts)
    if output and not output_was_streamed and emit_stdout:
        print(output, end="" if output.endswith("\n") else "\n")
    try:
        recorded_return_code = _record_finish(run_id, return_code, output, success_codes)
        if recorded_return_code is not None:
            return_code = recorded_return_code
    except (pymysql.MySQLError, RuntimeError) as error:
        print(f"無法完成 {job_name} 的排程紀錄：{error}", file=sys.stderr)
        return SCHEDULER_DB_ERROR_EXIT_CODE
    # 只有呼叫端明確列出的成功碼會轉成 0；正式 catalog 只接受 0。
    return 0 if return_code in success_codes else return_code


def _job_family(job: str) -> str:
    if job.startswith("nhtsa"):
        return "nhtsa"
    return job


def _try_lock(prefix: str, job: str) -> TextIO | None:
    configured_state_dir = os.getenv("PSQ_SCHEDULER_STATE_DIR", "").strip()
    state_dir = (
        Path(configured_state_dir).expanduser().absolute() if configured_state_dir else LOG_DIR
    )
    ensure_private_state_directory(state_dir)
    lock_path = state_dir / f"{prefix}-{_job_family(job)}.lock"
    lock_fd = open_private_state_file(
        lock_path,
        os.O_RDWR | os.O_CREAT | os.O_APPEND,
    )
    try:
        lock_file = os.fdopen(lock_fd, "a")
    except BaseException:
        os.close(lock_fd)
        raise
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    except OSError:
        lock_file.close()
        raise
    return lock_file


def _recover_interrupted_job_runs(job: str) -> bool:
    family = _job_family(job)
    if family == "pending":
        return False
    connection = _connect()
    admission_lock: str | None = None
    try:
        admission_lock = acquire_catalog_writer_admission(connection)
        connection.begin()
        with connection.cursor() as cursor:
            recovered_complete = False
            if family == "catalog":
                cursor.execute(
                    "SELECT jobs.id AS scheduled_job_run_id, runs.id AS crawl_run_id, "
                    "runs.target_parts FROM scheduled_job_runs AS jobs "
                    "JOIN crawl_runs AS runs ON runs.scheduled_job_run_id = jobs.id "
                    "JOIN (SELECT crawl_run_id, COUNT(*) AS snapshot_rows "
                    "FROM bounded_parts GROUP BY crawl_run_id) AS snapshots "
                    "ON snapshots.crawl_run_id = runs.id "
                    "WHERE (jobs.status = 'running' OR (jobs.status = 'failed' "
                    "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
                    "AND jobs.exit_code <> 0)) AND jobs.job_name = 'catalog' "
                    "AND jobs.trigger_mode = 'daemon' "
                    "AND runs.dataset_kind = 'bounded' "
                    "AND runs.status = 'bounded_success' "
                    "AND runs.finished_at IS NOT NULL "
                    "AND runs.target_parts = 10000 "
                    "AND runs.parts_ok = runs.target_parts "
                    "AND snapshots.snapshot_rows = runs.target_parts "
                    "AND runs.evidence_status = 'verified' "
                    "AND runs.evidence_manifest_sha256 IS NOT NULL "
                    "AND runs.evidence_dataset_sha256 IS NOT NULL "
                    "AND runs.evidence_verified_at IS NOT NULL "
                    "AND runs.evidence_artifact_count > 0 "
                    "AND runs.evidence_record_count = runs.target_parts"
                )
                verified_job_ids = [
                    int(row["scheduled_job_run_id"])
                    for row in cursor.fetchall()
                    if _audit_catalog_evidence(
                        int(row["crawl_run_id"]),
                        int(row["target_parts"]),
                        allow_running_scheduler=True,
                        allow_failed_scheduler=True,
                    )
                ]
                if verified_job_ids:
                    placeholders = ",".join("%s" for _job_id in verified_job_ids)
                    cursor.execute(
                        "UPDATE scheduled_job_runs AS jobs "
                        "JOIN crawl_runs AS runs ON runs.scheduled_job_run_id = jobs.id "
                        "SET jobs.status = 'completed', jobs.finished_at = runs.finished_at, "
                        "jobs.exit_code = 0, jobs.output_text = "
                        "RIGHT(CONCAT(COALESCE(jobs.output_text, ''), %s), %s) "
                        f"WHERE jobs.id IN ({placeholders}) "
                        "AND (jobs.status = 'running' OR (jobs.status = 'failed' "
                        "AND jobs.finished_at IS NOT NULL AND jobs.exit_code IS NOT NULL "
                        "AND jobs.exit_code <> 0)) "
                        "AND runs.status = 'bounded_success' "
                        "AND runs.evidence_status = 'verified'",
                        (
                            "\nbounded publish committed before scheduler interruption; "
                            "completion reconciled automatically\n",
                            MAX_OUTPUT_CHARS,
                            *verified_job_ids,
                        ),
                    )
                    recovered_complete = cursor.rowcount > 0

            if family == "nhtsa":
                cursor.execute(
                    "UPDATE scheduled_job_runs AS parent "
                    "JOIN scheduled_job_runs AS bulk_child "
                    "ON bulk_child.parent_scheduled_job_run_id = parent.id "
                    "AND bulk_child.job_name = 'nhtsa-bulk' "
                    "AND bulk_child.status = 'completed' AND bulk_child.finished_at IS NOT NULL "
                    "AND bulk_child.exit_code = 0 "
                    "JOIN nhtsa_sync_runs AS bulk_run "
                    "ON bulk_run.scheduled_job_run_id = bulk_child.id "
                    "AND bulk_run.status = 'completed' AND bulk_run.ended_at IS NOT NULL "
                    "AND bulk_run.lease_slot IS NULL AND bulk_run.lease_token IS NULL "
                    "AND bulk_run.lease_expires_at IS NULL "
                    "JOIN scheduled_job_runs AS api_child "
                    "ON api_child.parent_scheduled_job_run_id = parent.id "
                    "AND api_child.job_name = 'nhtsa-api' "
                    "AND api_child.status = 'completed' AND api_child.finished_at IS NOT NULL "
                    "AND api_child.exit_code = 0 "
                    "JOIN nhtsa_sync_runs AS api_run "
                    "ON api_run.scheduled_job_run_id = api_child.id "
                    "AND api_run.status = 'completed' AND api_run.ended_at IS NOT NULL "
                    "AND api_run.lease_slot IS NULL AND api_run.lease_token IS NULL "
                    "AND api_run.lease_expires_at IS NULL "
                    "SET parent.status = 'completed', "
                    "parent.finished_at = GREATEST(bulk_child.finished_at, api_child.finished_at), "
                    "parent.exit_code = 0, parent.output_text = "
                    "RIGHT(CONCAT(COALESCE(parent.output_text, ''), %s), %s) "
                    "WHERE parent.job_name = 'nhtsa' "
                    "AND parent.trigger_mode = 'daemon' "
                    "AND (parent.status = 'running' OR (parent.status = 'failed' "
                    "AND parent.exit_code IS NOT NULL AND parent.exit_code <> 0)) "
                    "AND EXISTS (SELECT 1 FROM nhtsa_current_artifacts AS current_bulk "
                    "WHERE current_bulk.published_run_id = bulk_run.id) "
                    "AND EXISTS (SELECT 1 FROM nhtsa_current_artifacts AS current_api "
                    "WHERE current_api.published_run_id = api_run.id)",
                    (
                        "\nNHTSA bulk and API publishes reconciled from exact lineage\n",
                        MAX_OUTPUT_CHARS,
                    ),
                )
                recovered_complete = cursor.rowcount > 0
                cursor.execute(
                    "SELECT jobs.id FROM scheduled_job_runs AS jobs "
                    "LEFT JOIN nhtsa_sync_runs AS own_run "
                    "ON own_run.scheduled_job_run_id = jobs.id "
                    "WHERE jobs.status = 'running' "
                    "AND jobs.trigger_mode IN ('daemon', 'queue') "
                    "AND jobs.job_name IN ('nhtsa', 'nhtsa-bulk', 'nhtsa-api', 'nhtsa-vin') "
                    "AND (jobs.started_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND "
                    "OR (own_run.status = 'running' "
                    "AND own_run.lease_expires_at >= UTC_TIMESTAMP(6)) "
                    "OR EXISTS (SELECT 1 FROM scheduled_job_runs AS active_child "
                    "JOIN nhtsa_sync_runs AS active_run "
                    "ON active_run.scheduled_job_run_id = active_child.id "
                    "WHERE active_child.parent_scheduled_job_run_id = jobs.id "
                    "AND active_child.status = 'running' "
                    "AND active_run.status = 'running' "
                    "AND active_run.lease_expires_at >= UTC_TIMESTAMP(6))) LIMIT 1",
                    (RECOVERY_MIN_AGE_SECONDS,),
                )
            else:
                cursor.execute(
                    "SELECT id FROM scheduled_job_runs WHERE status = 'running' "
                    "AND trigger_mode = 'daemon' AND job_name = %s "
                    "AND started_at >= UTC_TIMESTAMP() - INTERVAL %s SECOND LIMIT 1",
                    (family, RECOVERY_MIN_AGE_SECONDS),
                )
            if cursor.fetchone():
                raise ActiveDaemonRun(f"recent {family} daemon marker is still present")

            if family == "nhtsa":
                cursor.execute(
                    "UPDATE nhtsa_sync_runs AS runs "
                    "JOIN scheduled_job_runs AS child "
                    "ON child.id = runs.scheduled_job_run_id "
                    "SET runs.status = 'interrupted', runs.updated_at = UTC_TIMESTAMP(6), "
                    "runs.ended_at = UTC_TIMESTAMP(6), runs.lease_slot = NULL, "
                    "runs.lease_token = NULL, runs.lease_expires_at = NULL, "
                    "runs.error_message = 'scheduler recovered expired linked lease' "
                    "WHERE runs.status = 'running' "
                    "AND runs.lease_expires_at < UTC_TIMESTAMP(6) "
                    "AND child.status = 'running' "
                    "AND child.trigger_mode IN ('daemon', 'queue') "
                    "AND child.job_name IN ('nhtsa-bulk', 'nhtsa-api', 'nhtsa-vin') "
                    "AND child.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                    (RECOVERY_MIN_AGE_SECONDS,),
                )
                cursor.execute(
                    "UPDATE scheduled_job_runs AS child "
                    "JOIN nhtsa_sync_runs AS runs "
                    "ON runs.scheduled_job_run_id = child.id "
                    "SET child.status = 'failed', child.finished_at = UTC_TIMESTAMP(), "
                    "child.exit_code = %s, child.output_text = "
                    "RIGHT(CONCAT(COALESCE(child.output_text, ''), %s), %s) "
                    "WHERE child.status = 'running' AND runs.status = 'interrupted' "
                    "AND child.trigger_mode IN ('daemon', 'queue') "
                    "AND child.job_name IN ('nhtsa-bulk', 'nhtsa-api', 'nhtsa-vin') "
                    "AND child.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                    (
                        INTERRUPTED_EXIT_CODE,
                        "\nexpired linked NHTSA lease recovered automatically\n",
                        MAX_OUTPUT_CHARS,
                        RECOVERY_MIN_AGE_SECONDS,
                    ),
                )
                cursor.execute(
                    "UPDATE scheduled_job_runs AS child "
                    "LEFT JOIN nhtsa_sync_runs AS runs "
                    "ON runs.scheduled_job_run_id = child.id "
                    "SET child.status = 'failed', child.finished_at = UTC_TIMESTAMP(), "
                    "child.exit_code = %s, child.output_text = "
                    "RIGHT(CONCAT(COALESCE(child.output_text, ''), %s), %s) "
                    "WHERE child.status = 'running' AND runs.id IS NULL "
                    "AND child.trigger_mode IN ('daemon', 'queue') "
                    "AND child.job_name IN ('nhtsa-bulk', 'nhtsa-api', 'nhtsa-vin') "
                    "AND child.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                    (
                        INTERRUPTED_EXIT_CODE,
                        "\nstale NHTSA child stopped before domain lease claim\n",
                        MAX_OUTPUT_CHARS,
                        RECOVERY_MIN_AGE_SECONDS,
                    ),
                )
                cursor.execute(
                    "UPDATE scheduled_job_runs AS parent "
                    "LEFT JOIN scheduled_job_runs AS active_child "
                    "ON active_child.parent_scheduled_job_run_id = parent.id "
                    "AND active_child.status = 'running' "
                    "SET parent.status = 'failed', parent.finished_at = UTC_TIMESTAMP(), "
                    "parent.exit_code = %s, parent.output_text = "
                    "RIGHT(CONCAT(COALESCE(parent.output_text, ''), %s), %s) "
                    "WHERE parent.status = 'running' AND parent.job_name = 'nhtsa' "
                    "AND parent.trigger_mode = 'daemon' "
                    "AND parent.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND "
                    "AND active_child.id IS NULL",
                    (
                        INTERRUPTED_EXIT_CODE,
                        "\nprevious NHTSA composite interrupted; recovered automatically\n",
                        MAX_OUTPUT_CHARS,
                        RECOVERY_MIN_AGE_SECONDS,
                    ),
                )
            else:
                cursor.execute(
                    "UPDATE scheduled_job_runs SET status = 'failed', "
                    "finished_at = UTC_TIMESTAMP(), exit_code = %s, "
                    "output_text = RIGHT(CONCAT(COALESCE(output_text, ''), %s), %s) "
                    "WHERE status = 'running' AND job_name = %s "
                    "AND trigger_mode = 'daemon' "
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
                    "AND jobs.job_name = 'catalog' AND jobs.trigger_mode = 'daemon' "
                    "AND jobs.started_at < UTC_TIMESTAMP() - INTERVAL %s SECOND",
                    (RECOVERY_MIN_AGE_SECONDS,),
                )
        connection.commit()
        return recovered_complete
    except Exception:
        connection.rollback()
        raise
    finally:
        try:
            if admission_lock is not None:
                release_catalog_writer_admission(connection, admission_lock)
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
        except ActiveDaemonRun:
            print(f"{job} 的 DB 仍有近期執行中 marker；保留工作並稍後重試", file=sys.stderr)
            return LOCK_BUSY_EXIT_CODE
        except AdmissionLockBusy:
            print(f"{job} 遇到 schema migration；保留工作並稍後重試", file=sys.stderr)
            return LOCK_BUSY_EXIT_CODE
        except (pymysql.MySQLError, RuntimeError) as error:
            print(f"無法回收 {job} 的中斷排程：{type(error).__name__}: {error}", file=sys.stderr)
            return SCHEDULER_DB_ERROR_EXIT_CODE
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
                    "runs.id AS crawl_run_id, runs.dataset_kind, "
                    "runs.status AS crawl_status, runs.target_parts, runs.parts_ok, "
                    "runs.evidence_status, runs.evidence_manifest_sha256, "
                    "runs.evidence_dataset_sha256, runs.evidence_artifact_count, "
                    "runs.evidence_record_count, runs.evidence_verified_at, "
                    "(SELECT COUNT(*) FROM bounded_parts AS bounded "
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
                or row.get("evidence_status") != "verified"
                or not row.get("evidence_manifest_sha256")
                or not row.get("evidence_dataset_sha256")
                or int(row.get("evidence_artifact_count") or 0) <= 0
                or int(row.get("evidence_record_count") or 0) != bounded_target
                or row.get("evidence_verified_at") is None
            ):
                return 0.0
            if not _audit_catalog_evidence(int(row.get("crawl_run_id") or 0), bounded_target):
                return 0.0
        elif row.get("crawl_status") not in ("success", "bounded_success"):
            # Sample 是隔離測試資料，不能延後正式 catalog 排程。
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


def _write_daemon_ready_marker() -> None:
    configured_path = os.getenv("PARTSOUQ_SCHEDULER_READY_MARKER", "").strip()
    if not configured_path:
        return
    marker = Path(configured_path).expanduser().absolute()
    ensure_private_state_directory(marker.parent)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{marker.name}.{os.getpid()}.",
        dir=marker.parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            stream = os.fdopen(descriptor, "w", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(f"{os.getpid()}\n")
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


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
    try:
        if job == "catalog" and os.getenv("PARTSOUQ_APPLY_MIGRATIONS_ON_START") == "1":
            job_lock = _try_lock("scheduler-job", job)
            if job_lock is None:
                print("catalog 工作仍由另一個程序持有；不可進行啟動升級", file=sys.stderr)
                return LOCK_BUSY_EXIT_CODE
            try:
                runner = CatalogMigrationRunner()
                runner.apply(
                    recover_stale_catalog_daemon_seconds=RECOVERY_MIN_AGE_SECONDS,
                    recover_stale_nhtsa_daemon_seconds=RECOVERY_MIN_AGE_SECONDS,
                )
                runner.check()
                _recover_interrupted_job_runs(job)
            except (
                MigrationError,
                ActiveDaemonRun,
                AdmissionLockBusy,
                pymysql.MySQLError,
                RuntimeError,
            ) as error:
                print(f"catalog 啟動升級失敗：{type(error).__name__}: {error}", file=sys.stderr)
                return SCHEDULER_DB_ERROR_EXIT_CODE
            finally:
                job_lock.close()

        _write_daemon_ready_marker()
        failures = 0
        non_site_failures = 0
        schedule_read_failures = 0
        completion_check_failures = 0
        wait_seconds = 0.0
        needs_schedule_check = job != "pending"
        announce_completion = False
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

            if stop_event.is_set():
                break
            return_code = dispatch_locked(job, scope)
            if stop_event.is_set():
                break
            if return_code == 0:
                failures = 0
                non_site_failures = 0
                completion_check_failures = 0
                if job == "pending":
                    wait_seconds = float(interval_seconds)
                    print(f"{job} 排程完成；{int(wait_seconds)} 秒後再執行", flush=True)
                else:
                    wait_seconds = 0.0
                    needs_schedule_check = True
                    announce_completion = True
                continue

            if job == "catalog" and return_code == 3:
                # Sample 代表排程環境誤設 PSQ_LIMIT_PARTS；記為失敗，
                # 但不連續重跑同一批測試資料。
                failures += 1
                wait_seconds = float(interval_seconds)
                needs_schedule_check = True
                print(
                    "catalog 排程收到 sample exit=3；請修正 PSQ_LIMIT_PARTS，"
                    f"{int(wait_seconds)} 秒後重新檢查",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            if return_code in (
                LOCK_BUSY_EXIT_CODE,
                SCHEDULER_DB_ERROR_EXIT_CODE,
                127,
            ):
                # 非站台失敗（鎖衝突／DB 紀錄閃失／子程序無法啟動）：
                # 指數退避但不計入 MAX_CONSECUTIVE_FAILURES —— 上限是
                # 為了偵測站台封鎖；這些原因會自癒，計入只會讓
                # catalog 在無辜的狀況下靜默 30 天。
                non_site_failures += 1
                wait_seconds = float(
                    min(
                        retry_max_seconds,
                        retry_base_seconds * (2 ** min(non_site_failures - 1, 20)),
                    )
                )
                if job != "pending":
                    needs_schedule_check = True
                print(
                    f"{job} 排程非站台失敗（exit={return_code}）；{int(wait_seconds)} 秒後自動重試",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            failures += 1
            if failures >= MAX_CONSECUTIVE_FAILURES:
                # 連續失敗 = 封鎖/驗證無法通過的徵兆：停止指數重試，
                # 等下一次 interval 再檢查，而不是每小時重啟瀏覽器。
                wait_seconds = float(interval_seconds)
                # 長時間等待後必須重新查 DB；等待期間可能已由另一個
                # daemon 完成，不能醒來就直接重跑整個 catalog/NHTSA。
                needs_schedule_check = job != "pending"
                print(
                    f"{job} 排程連續失敗 {failures} 次；停止重試，{int(wait_seconds)} 秒後再執行",
                    file=sys.stderr,
                    flush=True,
                )
                continue
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

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)


def _run_nhtsa(scope: str) -> int:
    if scope != "all":
        print("nhtsa composite 只支援 scope=all；個別 scope 請使用子工作", file=sys.stderr)
        return 2
    try:
        parent_run_id = _record_start("nhtsa", None)
    except AdmissionLockBusy:
        print("nhtsa 遇到 schema migration；保留工作並稍後重試", file=sys.stderr)
        return LOCK_BUSY_EXIT_CODE
    except (pymysql.MySQLError, RuntimeError) as error:
        print(f"無法記錄 nhtsa 排程：{error}", file=sys.stderr)
        return SCHEDULER_DB_ERROR_EXIT_CODE

    completed_stages: list[str] = []
    bulk_result = dispatch(
        "nhtsa-bulk",
        scope,
        parent_scheduled_job_run_id=parent_run_id,
    )
    if bulk_result == 0:
        completed_stages.append(NHTSA_BULK_COMPLETED)
        try:
            _record_progress(parent_run_id, NHTSA_BULK_COMPLETED)
        except (pymysql.MySQLError, RuntimeError) as error:
            print(f"無法記錄 nhtsa bulk stage：{error}", file=sys.stderr)
        return_code = dispatch(
            "nhtsa-api",
            scope,
            parent_scheduled_job_run_id=parent_run_id,
        )
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
        recorded_return_code = _record_finish(parent_run_id, return_code, output)
        if recorded_return_code is not None:
            return_code = recorded_return_code
    except (pymysql.MySQLError, RuntimeError) as error:
        print(f"無法完成 nhtsa 的排程紀錄：{error}", file=sys.stderr)
        return SCHEDULER_DB_ERROR_EXIT_CODE
    return return_code


def _nhtsa_run_id(kind: str) -> str:
    return f"nhtsa-{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _vncs_run_id() -> str:
    return f"vncs-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="PartSouq / NHTSA 統一排程入口")
    parser.add_argument(
        "--job",
        required=True,
        choices=("catalog", "nhtsa-bulk", "nhtsa-api", "nhtsa-vin", "nhtsa", "pending", "vncs"),
        help=(
            "catalog 僅跑型錄；nhtsa 依序執行 bulk 與 API；pending 消費後台要求；"
            "vncs 同步台灣 MOENV VNCS 汽油/柴油車輛。"
        ),
    )
    parser.add_argument("--scope", default="all", help="NHTSA 同步範圍，預設 all。")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="常駐並依 interval 自動執行；僅支援 catalog、nhtsa、pending、vncs。",
    )
    parser.add_argument("--interval-seconds", type=int, default=None)
    parser.add_argument("--retry-base-seconds", type=int, default=None)
    parser.add_argument("--retry-max-seconds", type=int, default=None)
    return parser


def dispatch(
    job: str,
    scope: str,
    *,
    parent_scheduled_job_run_id: int | None = None,
) -> int:
    if job == "pending":
        try:
            _requeue_interrupted_requests()
            requests = _pending_requests()
        except AdmissionLockBusy:
            print("pending queue 遇到 schema migration；稍後重試", file=sys.stderr)
            return LOCK_BUSY_EXIT_CODE
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
            except AdmissionLockBusy:
                print(
                    f"後台爬取要求 {request_id} 遇到 schema migration；稍後重試",
                    file=sys.stderr,
                )
                return LOCK_BUSY_EXIT_CODE
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
            parent_scheduled_job_run_id=parent_scheduled_job_run_id,
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
            parent_scheduled_job_run_id=parent_scheduled_job_run_id,
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
            parent_scheduled_job_run_id=parent_scheduled_job_run_id,
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
            parent_scheduled_job_run_id=parent_scheduled_job_run_id,
        )

    if job == "vncs":
        return _run(
            "vncs",
            [
                sys.executable,
                "-m",
                "partsouq_crawler",
                "vncs-sync",
                "--run-id",
                _vncs_run_id(),
            ],
            parent_scheduled_job_run_id=parent_scheduled_job_run_id,
        )

    return _run_nhtsa(scope)


def main() -> int:
    args = build_parser().parse_args()
    if not args.daemon:
        return dispatch_locked(args.job, args.scope)
    if args.job not in DAEMON_JOBS:
        print("daemon 僅支援 catalog、nhtsa、pending、vncs", file=sys.stderr)
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
