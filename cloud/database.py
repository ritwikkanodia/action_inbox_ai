"""Explicit service-owned transactions; never imports the local SQLite app."""
from contextlib import contextmanager
import psycopg
from psycopg import sql
from psycopg.rows import dict_row


class Database:
    def __init__(self, dsn: str, schema: str):
        self.dsn, self.schema = dsn, schema

    @contextmanager
    def transaction(self):
        with psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row, connect_timeout=5) as conn:
            with conn.transaction():
                conn.execute(sql.SQL('SET LOCAL search_path TO {}').format(sql.Identifier(self.schema)))
                conn.execute("SET LOCAL lock_timeout = '2s'")
                conn.execute("SET LOCAL statement_timeout = '10s'")
                yield conn

    def read(self, query, params=()):
        with self.transaction() as tx:
            return tx.execute(query, params).fetchall()
