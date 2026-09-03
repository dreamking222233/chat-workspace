"""Regression coverage for the MySQL regeneration concurrency guard."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from threading import Event
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.dialects import mysql

from app.api.workspace import _active_regeneration_request_query


def test_active_regeneration_query_is_a_mysql_locking_current_read():
    statement = _active_regeneration_request_query("assistant-1")
    compiled = str(statement.compile(dialect=mysql.dialect()))

    assert "FOR UPDATE" in compiled.upper()
    assert "model_requests.message_id" in compiled
    assert "model_requests.status" in compiled


@pytest.mark.skipif(
    not os.environ.get("CHAT_MYSQL_CONCURRENCY_TEST_URL"),
    reason="set CHAT_MYSQL_CONCURRENCY_TEST_URL to run the MySQL/InnoDB lock regression",
)
def test_mysql_waiter_sees_root_request_committed_while_waiting_for_assistant_lock():
    """A pre-existing RR snapshot must not hide the winner's running request."""
    engine = create_engine(
        os.environ["CHAT_MYSQL_CONCURRENCY_TEST_URL"],
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS model_requests"))
        connection.execute(text("DROP TABLE IF EXISTS messages"))
        connection.execute(text("CREATE TABLE messages (id VARCHAR(36) PRIMARY KEY) ENGINE=InnoDB"))
        connection.execute(
            text(
                "CREATE TABLE model_requests ("
                "id VARCHAR(36) PRIMARY KEY, message_id VARCHAR(36), "
                "parent_request_id VARCHAR(36) NULL, status VARCHAR(20) NOT NULL, "
                "INDEX ix_model_requests_message_id (message_id)"
                ") ENGINE=InnoDB"
            )
        )
        connection.execute(text("INSERT INTO messages (id) VALUES ('assistant-1')"))

    winner = engine.connect()
    waiter = engine.connect()
    winner_transaction = winner.begin()
    waiter_transaction = waiter.begin()
    waiter_started = Event()

    try:
        winner.execute(text("SELECT id FROM messages WHERE id='assistant-1' FOR UPDATE"))
        # Establish an old consistent-read snapshot before the winner commits.
        assert waiter.execute(text("SELECT COUNT(*) FROM model_requests")).scalar_one() == 0

        def wait_then_read_current_request():
            waiter_started.set()
            waiter.execute(text("SELECT id FROM messages WHERE id='assistant-1' FOR UPDATE"))
            return waiter.execute(_active_regeneration_request_query("assistant-1")).scalar_one_or_none()

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(wait_then_read_current_request)
            assert waiter_started.wait(timeout=2)
            time.sleep(0.1)
            assert not future.done()
            winner.execute(
                text(
                    "INSERT INTO model_requests "
                    "(id, message_id, parent_request_id, status) "
                    "VALUES ('request-1', 'assistant-1', NULL, 'running')"
                )
            )
            winner_transaction.commit()
            assert future.result(timeout=5) == "request-1"
    finally:
        if winner_transaction.is_active:
            winner_transaction.rollback()
        if waiter_transaction.is_active:
            waiter_transaction.rollback()
        winner.close()
        waiter.close()
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE IF EXISTS model_requests"))
            connection.execute(text("DROP TABLE IF EXISTS messages"))
        engine.dispose()
