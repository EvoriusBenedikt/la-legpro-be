"""Admin active-access overview endpoint."""
import uuid

import pytest
from conftest import bearer
from services import pg_service

pytestmark = pytest.mark.db


def test_admin_access_grants_returns_active_rows_and_display_fields(
        make_user, mint_token, client):
    admin_id = make_user("admin", role="admin")
    recipient_id = make_user("recipient", email="recipient@example.test")
    expired_recipient_id = make_user("expired-recipient")
    inactive_recipient_id = make_user("inactive-recipient", is_active=False)
    regulation = pg_service.execute_returning(
        "INSERT INTO regulations (judul, nomor, status, klasifikasi) "
        "VALUES ('Peraturan Akses', 'N-ACCESS', 'Berlaku', 'Rahasia') RETURNING id"
    )[0]["id"]
    active_id = str(uuid.uuid4())
    never_expires_id = str(uuid.uuid4())
    expired_id = str(uuid.uuid4())
    pg_service.execute(
        "INSERT INTO access_grants "
        "(id, doc_id, granted_by, granted_to, reason, expires_at) "
        "VALUES (%s, %s, %s, %s, 'Kebutuhan kerja', NOW() + interval '1 day'), "
        "       (%s, %s, %s, %s, 'Akses tetap', NULL), "
        "       (%s, %s, %s, %s, 'Sudah tidak berlaku', NOW() - interval '1 day')",
        (active_id, str(regulation), admin_id, recipient_id,
         never_expires_id, str(regulation), admin_id, recipient_id,
         expired_id, str(regulation), admin_id, expired_recipient_id),
    )
    pg_service.execute(
        "INSERT INTO access_grants "
        "(id, doc_id, granted_by, granted_to, reason, expires_at) "
        "VALUES (%s, %s, %s, %s, 'Tidak aktif', NOW() + interval '1 day')",
        (str(uuid.uuid4()), str(regulation), admin_id, inactive_recipient_id),
    )

    response = client.get("/api/admin/access-grants",
                          headers=bearer(mint_token(admin_id)))

    assert response.status_code == 200
    grants = response.json()["grants"]
    assert {grant["id"] for grant in grants} == {active_id, never_expires_id}
    assert all(grant["username"] != "inactive-recipient" for grant in grants)
    row = next(grant for grant in grants if grant["id"] == active_id)
    assert row["granted_to"] == recipient_id
    assert row["username"] == "recipient"
    assert row["email"] == "recipient@example.test"
    assert row["doc_id"] == str(regulation)
    assert row["judul"] == "Peraturan Akses"
    assert row["klasifikasi"] == "Rahasia"


def test_admin_access_grants_rejects_underprivileged_user(make_user, mint_token, client):
    user_id = make_user("ordinary")

    response = client.get("/api/admin/access-grants",
                          headers=bearer(mint_token(user_id)))

    assert response.status_code == 403
