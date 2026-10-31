"""Run the whole suite against PostgreSQL by setting DATABASE_URL (a throwaway database!)."""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def clean_postgres():
    url = os.environ.get("DATABASE_URL")
    if url:
        import psycopg

        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
    yield
