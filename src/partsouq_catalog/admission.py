"""Short database admission lock shared by migrations and writer startup."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from .migrations import catalog_schema_lock_name

type AdmissionConnection = pymysql.connections.Connection[DictCursor]
CATALOG_RUNTIME_SHUTDOWN_SECONDS = 5.0


class AdmissionLockBusy(RuntimeError):
    """A schema migration currently blocks new writer admission."""


class CatalogRuntimeLockBusy(RuntimeError):
    """Another crawler process owns the catalog mutation lease."""


class CatalogRuntimeLease:
    """Keep a MySQL named lock alive and fail closed when ownership is lost."""

    def __init__(
        self,
        connection: AdmissionConnection,
        lock_name: str,
        owner_connection_id: int,
        *,
        heartbeat_seconds: float = 30.0,
        shutdown_seconds: float = CATALOG_RUNTIME_SHUTDOWN_SECONDS,
    ) -> None:
        self.connection = connection
        self.lock_name = lock_name
        self.owner_connection_id = owner_connection_id
        self.heartbeat_seconds = heartbeat_seconds
        self.shutdown_seconds = shutdown_seconds
        self._connection_lock = threading.Lock()
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(
            target=self._heartbeat,
            name="catalog-runtime-lock",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def assert_owned(self) -> None:
        if self._lost.is_set():
            raise RuntimeError("catalog runtime lock ownership was lost")
        try:
            with self._connection_lock, self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT IS_USED_LOCK(%s) AS owner_connection_id, "
                    "CONNECTION_ID() AS current_connection_id",
                    (self.lock_name,),
                )
                row = cursor.fetchone()
        except Exception as error:
            self._lost.set()
            raise RuntimeError("catalog runtime lock ownership check failed") from error
        if not row or (
            row.get("owner_connection_id") != self.owner_connection_id
            or row.get("current_connection_id") != self.owner_connection_id
        ):
            self._lost.set()
            raise RuntimeError("catalog runtime lock ownership was lost")

    def close(self) -> None:
        self._stop.set()
        if not self._thread.is_alive():
            return
        self._thread.join(timeout=self.shutdown_seconds)
        if not self._thread.is_alive():
            return

        # PyMySQL 沒有 heartbeat query 的 read timeout；若 execute 卡住，
        # 必須關閉 owner connection 才能中斷 socket read。close() 不可在
        # heartbeat thread 還活著時返回，否則呼叫端會與它競態清理連線。
        self._lost.set()
        close_error: Exception | None = None
        try:
            self.connection.close()
        except Exception as error:
            close_error = error
        self._thread.join(timeout=self.shutdown_seconds)
        if self._thread.is_alive():
            raise RuntimeError("catalog runtime lock heartbeat did not stop") from close_error
        if close_error is not None:
            raise RuntimeError(
                "failed to interrupt catalog runtime lock heartbeat"
            ) from close_error

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                self.assert_owned()
            except RuntimeError:
                return


@contextmanager
def catalog_writer_admission(connection: AdmissionConnection) -> Iterator[None]:
    """Hold the migration mutex until a new running marker is durable."""
    lock_name = acquire_catalog_writer_admission(connection)
    try:
        yield
    finally:
        release_catalog_writer_admission(connection, lock_name)


def acquire_catalog_writer_admission(connection: AdmissionConnection) -> str:
    """Acquire the non-blocking admission mutex and return its exact name."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT DATABASE() AS database_name")
        row = cursor.fetchone()
        database = row.get("database_name") if row else None
        if not isinstance(database, str) or not database:
            raise RuntimeError("writer connection must select an explicit database")
        lock_name = catalog_schema_lock_name(database)
        cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
        acquired = cursor.fetchone()
    if not acquired or acquired.get("acquired") != 1:
        raise AdmissionLockBusy("catalog schema migration is in progress")
    return lock_name


def release_catalog_writer_admission(connection: AdmissionConnection, lock_name: str) -> None:
    """Release on the acquiring connection; discard it if ownership was lost."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT RELEASE_LOCK(%s) AS released", (lock_name,))
            released = cursor.fetchone()
        if not released or released.get("released") != 1:
            raise RuntimeError("failed to release catalog writer admission lock")
    except Exception:
        # GET_LOCK 是 session-scoped。連 RELEASE_LOCK 查詢本身都失敗時，
        # 關閉 owner connection 是唯一能保證 MySQL 釋放鎖的做法。
        try:
            connection.close()
        except Exception:
            pass
        raise


def catalog_runtime_lock_name(database: str) -> str:
    digest = hashlib.sha256(database.encode()).hexdigest()[:32]
    return f"partsouq:catalog-runtime:{digest}"


def acquire_catalog_runtime_lock(connection: AdmissionConnection) -> CatalogRuntimeLease:
    """取得跨 checkout／容器的 catalog 全程互斥鎖。

    呼叫端必須讓這條專用連線存活到 crawler 完整結束；關閉連線時
    MySQL 會自動釋放 named lock，程序 crash 也不會留下殭屍鎖。
    """
    with connection.cursor() as cursor:
        cursor.execute("SET SESSION wait_timeout = 31536000")
        cursor.execute("SELECT @@SESSION.wait_timeout AS wait_timeout")
        timeout_row = cursor.fetchone()
        if not timeout_row or timeout_row.get("wait_timeout") != 31_536_000:
            raise RuntimeError("catalog runtime connection wait_timeout was not applied")
        cursor.execute("SELECT DATABASE() AS database_name")
        row = cursor.fetchone()
        database = row.get("database_name") if row else None
        if not isinstance(database, str) or not database:
            raise RuntimeError("catalog runtime connection must select an explicit database")
        lock_name = catalog_runtime_lock_name(database)
        cursor.execute("SELECT GET_LOCK(%s,0) AS acquired", (lock_name,))
        acquired = cursor.fetchone()
        cursor.execute("SELECT CONNECTION_ID() AS connection_id")
        connection_row = cursor.fetchone()
    if not acquired or acquired.get("acquired") != 1:
        raise CatalogRuntimeLockBusy("another catalog crawler owns the database runtime lock")
    if not connection_row or not isinstance(connection_row.get("connection_id"), int):
        raise RuntimeError("catalog runtime connection has no connection id")
    lease = CatalogRuntimeLease(
        connection,
        lock_name,
        connection_row["connection_id"],
    )
    lease.start()
    return lease
