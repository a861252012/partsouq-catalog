"""Short database admission lock shared by migrations and writer startup."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from .migrations import catalog_schema_lock_name

type AdmissionConnection = pymysql.connections.Connection[DictCursor]


class AdmissionLockBusy(RuntimeError):
    """A schema migration currently blocks new writer admission."""


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
