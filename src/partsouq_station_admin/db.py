from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pymysql
from pymysql.connections import Connection
from pymysql.constants import CLIENT
from pymysql.cursors import DictCursor

from partsouq_station_admin.config import AdminConfig
from partsouq_station_admin.query_trace import QueryTrace

_TAG_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
SqlParams = Sequence[object] | Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    lastrowid: int
    rowcount: int


class Database(Protocol):
    def fetch_one(self, tag: str, sql: str, params: SqlParams = None) -> dict[str, Any] | None: ...

    def fetch_all(self, tag: str, sql: str, params: SqlParams = None) -> list[dict[str, Any]]: ...

    def execute(self, tag: str, sql: str, params: SqlParams = None) -> ExecutionResult: ...

    def transaction(self) -> AbstractContextManager[Database]: ...


class RequestDatabase(Database, Protocol):
    def close(self) -> None: ...


class AdminDatabase:
    def __init__(self, connection: Connection[DictCursor], trace: QueryTrace) -> None:
        self.connection = connection
        self.trace = trace

    @classmethod
    def connect(cls, config: AdminConfig, trace: QueryTrace) -> AdminDatabase:
        connection = pymysql.connect(
            host=config.mysql_host,
            port=config.mysql_port,
            user=config.mysql_user,
            password=config.mysql_password,
            database=config.mysql_database,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=DictCursor,
            read_timeout=30,
            write_timeout=30,
        )
        return cls(connection, trace)

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[AdminDatabase]:
        self.connection.begin()
        try:
            yield self
        except BaseException:
            with suppress(pymysql.MySQLError):
                self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def fetch_one(
        self,
        tag: str,
        sql: str,
        params: SqlParams = None,
    ) -> dict[str, Any] | None:
        tagged_sql = self._tagged_sql(tag, sql)
        started = time.perf_counter()
        with self.connection.cursor() as cursor:
            cursor.execute(tagged_sql, params)
            row = cursor.fetchone()
            row_count = cursor.rowcount
        self._record(tag, tagged_sql, started, row_count)
        return dict(row) if row else None

    def fetch_all(
        self,
        tag: str,
        sql: str,
        params: SqlParams = None,
    ) -> list[dict[str, Any]]:
        tagged_sql = self._tagged_sql(tag, sql)
        started = time.perf_counter()
        with self.connection.cursor() as cursor:
            cursor.execute(tagged_sql, params)
            rows = cursor.fetchall()
            row_count = cursor.rowcount
        self._record(tag, tagged_sql, started, row_count)
        return [dict(row) for row in rows]

    def execute(
        self,
        tag: str,
        sql: str,
        params: SqlParams = None,
    ) -> ExecutionResult:
        tagged_sql = self._tagged_sql(tag, sql)
        started = time.perf_counter()
        with self.connection.cursor() as cursor:
            cursor.execute(tagged_sql, params)
            result = ExecutionResult(lastrowid=int(cursor.lastrowid or 0), rowcount=cursor.rowcount)
        self._record(tag, tagged_sql, started, result.rowcount)
        return result

    @staticmethod
    def _tagged_sql(tag: str, sql: str) -> str:
        if not _TAG_RE.fullmatch(tag):
            raise ValueError(f"invalid SQL tag: {tag!r}")
        return f"/* admin:{tag} */ {sql.strip()}"

    def _record(self, tag: str, sql: str, started: float, row_count: int) -> None:
        self.trace.record(
            tag=tag,
            sql=sql,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            row_count=row_count,
        )


def apply_admin_schema(config: AdminConfig) -> None:
    project_root = Path(os.getenv("PARTSOUQ_HOME", Path(__file__).resolve().parents[2]))
    schema = (project_root / "db" / "station_admin.sql").read_text(encoding="utf-8")
    connection = pymysql.connect(
        host=config.mysql_host,
        port=config.mysql_port,
        user=config.mysql_user,
        password=config.mysql_password,
        database=config.mysql_database,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=DictCursor,
        client_flag=CLIENT.MULTI_STATEMENTS,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(schema)
            while cursor.nextset():
                pass
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
