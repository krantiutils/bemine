from __future__ import annotations

import sqlite3
from pathlib import Path

from bemine.db import BemineDB


def test_permission_and_usage_ledgers_are_queryable(tmp_path: Path) -> None:
    db = BemineDB(tmp_path / "bemine.db")

    db.upsert_credential_profile(
        credential_id="google-api",
        name="Google API",
        kind="cost-incurring",
        storage_ref="op://bemine/google-api/key",
        approval_policy="discord_totp",
        totp_required=True,
    )
    db.create_permission_request(
        request_id="req-1",
        credential_id="google-api",
        project_id="proj-alpha",
        session_id="codex:abc",
        device_id="testbox",
        runtime="codex",
        reason="Need to call a paid Google API endpoint for alpha.",
    )
    db.record_usage_sample(
        provider="codex",
        runtime="codex",
        session_id="codex:abc",
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        limit_name="daily",
        limit_remaining=42,
        raw_json='{"ok": true}',
    )
    db.record_audit_event(
        category="credential",
        event_type="permission_requested",
        actor="test",
        project_id="proj-alpha",
        session_id="codex:abc",
        payload_json='{"request_id": "req-1"}',
    )

    conn = sqlite3.connect(tmp_path / "bemine.db")
    try:
        row = conn.execute(
            "SELECT approval_policy, totp_required FROM credential_profiles WHERE credential_id = 'google-api'"
        ).fetchone()
        assert row == ("discord_totp", 1)
        assert conn.execute("SELECT status FROM permission_requests").fetchone()[0] == "pending"
        assert conn.execute("SELECT total_tokens FROM usage_samples").fetchone()[0] == 150
        assert conn.execute("SELECT event_type FROM audit_events").fetchone()[0] == "permission_requested"
    finally:
        conn.close()

