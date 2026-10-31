"""One database API for SQLite (default, a local file) and PostgreSQL (when DATABASE_URL is set).

The app writes SQLite-style SQL ("?" placeholders, AUTOINCREMENT, REAL). For PostgreSQL this
module translates those few differences, returns rows that work both as dicts and by index
(like sqlite3.Row), and closes every connection when the `with` block ends.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

try:
    import psycopg
except ImportError:  # pragma: no cover - only needed with DATABASE_URL
    psycopg = None


class Row(dict):
    """Dict row that also supports row[0], like sqlite3.Row."""

    def __init__(self, names: Sequence[str], values: Sequence[Any]) -> None:
        super().__init__(zip(names, values))
        self._values = tuple(values)

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


def _row_factory(cursor: Any):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(names, values)


def translate_sql(sql: str) -> str:
    """SQLite dialect -> PostgreSQL for the subset of SQL this app uses."""
    sql = sql.replace("%", "%%").replace("?", "%s")
    sql = re.sub(r"INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql)
    return sql


class Cursor:
    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self) -> Any:
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        return self._cursor.fetchall()

    def __iter__(self) -> Iterator[Any]:
        return iter(self._cursor)


class Connection:
    def __init__(self, url: str | None, sqlite_path: Path) -> None:
        self.is_postgres = bool(url)
        if self.is_postgres:
            if psycopg is None:
                raise RuntimeError("DATABASE_URL is set but psycopg is not installed (pip install 'psycopg[binary]').")
            self._conn = psycopg.connect(url, row_factory=_row_factory)
        else:
            sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(sqlite_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")

    # -- statements --------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        if self.is_postgres:
            return Cursor(self._conn.execute(translate_sql(sql), tuple(params)))
        return Cursor(self._conn.execute(sql, params))

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        if self.is_postgres:
            with self._conn.cursor() as cursor:
                cursor.executemany(translate_sql(sql), [tuple(row) for row in rows])
        else:
            self._conn.executemany(sql, rows)

    def executescript(self, script: str) -> None:
        if not self.is_postgres:
            self._conn.executescript(script)
            return
        for statement in script.split(";"):
            if statement.strip():
                self._conn.execute(translate_sql(statement), ())

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """INSERT a row and return its new id."""
        if self.is_postgres:
            return int(self._conn.execute(translate_sql(sql.rstrip().rstrip(";")) + " RETURNING id", tuple(params)).fetchone()[0])
        return int(self._conn.execute(sql, params).lastrowid)

    def upsert(self, table: str, key_column: str, values: dict[str, Any]) -> None:
        columns = list(values)
        placeholders = ", ".join("?" for _ in columns)
        updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != key_column)
        self.execute(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) ON CONFLICT ({key_column}) DO UPDATE SET {updates}",
            [values[column] for column in columns],
        )

    def table_columns(self, table: str) -> set[str]:
        if self.is_postgres:
            rows = self.execute("SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)).fetchall()
            return {row[0] for row in rows}
        return {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}

    # -- transaction ---------------------------------------------------------------------
    def __enter__(self) -> "Connection":
        return self

    def __exit__(self, exc_type: Any, *_: Any) -> None:
        try:
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
        finally:
            self._conn.close()


def connect(url: str | None, sqlite_path: Path) -> Connection:
    return Connection(url, sqlite_path)


def describe(url: str | None) -> str:
    """Where the data lives, without leaking the password."""
    if not url:
        return "SQLite"
    return "PostgreSQL @ " + re.sub(r"^[a-z]+://(?:[^@/]*@)?", "", url).split("/")[0]
