"""單一排程入口：PartSouq 型錄與 NHTSA 同步共用同一個 MySQL 資料庫。"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import UTC, datetime

import pymysql

from .config import BASE_DIR, DB_CONFIG

MAX_OUTPUT_CHARS = 60_000


def _record_start(job_name: str) -> int:
    connection = pymysql.connect(**DB_CONFIG, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO scheduled_job_runs (job_name, status, started_at) "
                "VALUES (%s, 'running', UTC_TIMESTAMP())",
                (job_name,),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def _record_finish(run_id: int, return_code: int, output: str) -> None:
    connection = pymysql.connect(**DB_CONFIG, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE scheduled_job_runs "
                "SET status = %s, finished_at = UTC_TIMESTAMP(), exit_code = %s, output_text = %s "
                "WHERE id = %s",
                (
                    "completed" if return_code == 0 else "failed",
                    return_code,
                    output[-MAX_OUTPUT_CHARS:],
                    run_id,
                ),
            )
    finally:
        connection.close()


def _pending_requests() -> list[dict]:
    connection = pymysql.connect(
        **DB_CONFIG, cursorclass=pymysql.cursors.DictCursor, autocommit=True
    )
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
    connection = pymysql.connect(**DB_CONFIG, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_crawl_requests SET status = 'running', started_at = UTC_TIMESTAMP() "
                "WHERE id = %s AND status = 'pending'",
                (request_id,),
            )
            return cursor.rowcount == 1
    finally:
        connection.close()


def _finish_request(request_id: int, return_code: int) -> None:
    connection = pymysql.connect(**DB_CONFIG, autocommit=True)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE admin_crawl_requests SET status = %s, finished_at = UTC_TIMESTAMP(), "
                "error_message = %s, requested_scope = CASE WHEN job_name = 'nhtsa-vin' "
                "THEN CONCAT(LEFT(requested_scope, 3), '**********', RIGHT(requested_scope, 4)) "
                "ELSE requested_scope END WHERE id = %s",
                (
                    "completed" if return_code == 0 else "failed",
                    None if return_code == 0 else f"scheduler exit code {return_code}",
                    request_id,
                ),
            )
    finally:
        connection.close()


def _run(job_name: str, command: list[str]) -> int:
    try:
        run_id = _record_start(job_name)
    except pymysql.MySQLError as error:
        print(f"無法記錄 {job_name} 排程：{error}", file=sys.stderr)
        return 1

    result = subprocess.run(
        command,
        cwd=BASE_DIR,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    output = result.stdout or ""
    if job_name == "nhtsa-vin" and len(command) > 4:
        vin = command[4].strip().upper()
        output = re.sub(
            re.escape(vin),
            f"{vin[:3]}**********{vin[-4:]}",
            output,
            flags=re.IGNORECASE,
        )
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    try:
        _record_finish(run_id, result.returncode, output)
    except pymysql.MySQLError as error:
        print(f"無法完成 {job_name} 的排程紀錄：{error}", file=sys.stderr)
        return 1
    return result.returncode


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
    return parser


def dispatch(job: str, scope: str) -> int:
    if job == "pending":
        try:
            requests = _pending_requests()
        except pymysql.MySQLError as error:
            print(f"無法讀取後台爬取要求：{error}", file=sys.stderr)
            return 1

        exit_code = 0
        for request in requests:
            request_id = int(request["id"])
            try:
                if not _claim_request(request_id):
                    continue
                return_code = dispatch(str(request["job_name"]), str(request["requested_scope"]))
                _finish_request(request_id, return_code)
                exit_code = max(exit_code, return_code)
            except pymysql.MySQLError as error:
                print(f"無法更新後台爬取要求 {request_id}：{error}", file=sys.stderr)
                return 1
        return exit_code

    if job == "catalog":
        workers = os.getenv("PSQ_WORKERS", "1")
        return _run(
            "catalog",
            [sys.executable, "-m", "partsouq_catalog.run_crawl", "--workers", workers],
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

    bulk_result = dispatch("nhtsa-bulk", scope)
    if bulk_result != 0:
        return bulk_result
    return dispatch("nhtsa-api", scope)


def main() -> int:
    args = build_parser().parse_args()
    return dispatch(args.job, args.scope)


if __name__ == "__main__":
    raise SystemExit(main())
