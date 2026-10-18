"""T9: migration 011 on scratch databases (CREATE DATABASE per test)."""
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from conftest import MIGRATIONS, TEST_DB, apply_migrations

pytestmark = pytest.mark.db

M011 = MIGRATIONS / "011_auth_hardening.sql"


@pytest.fixture
def scratch():
    """A brand-new empty database; dropped afterwards."""
    name = "scratch_" + uuid.uuid4().hex[:10]
    with psycopg.connect(TEST_DB, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(TEST_DB, dbname=name)
    try:
        yield dsn
    finally:
        with psycopg.connect(TEST_DB, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def run_011(dsn):
    notices = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.add_notice_handler(lambda d: notices.append(d.message_primary))
        conn.execute(M011.read_text(encoding="utf-8"))
    return notices


def schema_state(dsn):
    with psycopg.connect(dsn, autocommit=True) as conn:
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")}
        idx = {r[0] for r in conn.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'users'")}
        has_table = conn.execute("SELECT to_regclass('public.login_attempts') IS NOT NULL").fetchone()[0]
    return cols, idx, has_table


NEW_COLS = {"failed_attempts", "locked_until", "last_failed_at", "token_version"}


def test_fresh_database_applies_001_to_011_cleanly(scratch):
    apply_migrations(scratch)
    cols, idx, has_table = schema_state(scratch)
    assert NEW_COLS <= cols and has_table
    assert {"users_username_lower_uidx", "users_email_lower_uidx"} <= idx


def test_011_is_idempotent(scratch):
    apply_migrations(scratch)
    run_011(scratch)
    run_011(scratch)
    cols, idx, has_table = schema_state(scratch)
    assert NEW_COLS <= cols and has_table
    assert {"users_username_lower_uidx", "users_email_lower_uidx"} <= idx
    with psycopg.connect(scratch) as conn:
        row = conn.execute(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'users' AND column_name = 'token_version'").fetchone()
        assert row[0] == "0"


def test_case_variant_duplicates_do_not_abort_and_skip_the_indexes(scratch):
    apply_migrations(scratch, upto=10)
    with psycopg.connect(scratch, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id, username, password_hash, email) VALUES "
                     "('1','Bob','h','bob@example.test'),('2','bob','h','BOB@example.test')")
    notices = run_011(scratch)  # must not raise
    cols, idx, has_table = schema_state(scratch)
    assert NEW_COLS <= cols and has_table
    assert "users_username_lower_uidx" not in idx
    assert "users_email_lower_uidx" not in idx
    assert any("skipping users_username_lower_uidx" in n for n in notices)
    assert any("skipping users_email_lower_uidx" in n for n in notices)
    # existing rows got the defaults
    with psycopg.connect(scratch) as conn:
        rows = conn.execute("SELECT failed_attempts, token_version FROM users").fetchall()
        assert rows == [(0, 0), (0, 0)]


def test_only_username_duplicates_skip_only_that_index(scratch):
    apply_migrations(scratch, upto=10)
    with psycopg.connect(scratch, autocommit=True) as conn:
        conn.execute("INSERT INTO users (id, username, password_hash, email) VALUES "
                     "('1','Bob','h','a@example.test'),('2','bob','h','b@example.test')")
    run_011(scratch)
    _, idx, _ = schema_state(scratch)
    assert "users_username_lower_uidx" not in idx
    assert "users_email_lower_uidx" in idx


def test_no_duplicates_creates_both_unique_indexes_and_enforces_them(scratch):
    apply_migrations(scratch, upto=10)
    with psycopg.connect(scratch, autocommit=True) as conn:
        # blank / NULL emails may repeat: the email index is partial
        conn.execute("INSERT INTO users (id, username, password_hash, email) VALUES "
                     "('1','alice','h','alice@example.test'),('2','carol','h',''),"
                     "('3','dave','h',''),('4','erin','h',NULL),('5','frank','h',NULL)")
    run_011(scratch)
    _, idx, _ = schema_state(scratch)
    assert {"users_username_lower_uidx", "users_email_lower_uidx"} <= idx
    with psycopg.connect(scratch, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO users (id, username, password_hash, email) "
                         "VALUES ('9','ALICE','h','z@example.test')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("INSERT INTO users (id, username, password_hash, email) "
                         "VALUES ('9','zed','h','ALICE@example.test')")
