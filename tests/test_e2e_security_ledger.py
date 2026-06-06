from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from bemine.db import BemineDB
from bemine.security.credentials import PermissionGate
from bemine.security.notifications import notify_permission_request
from bemine.security.totp import hotp


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


def test_cli_credential_scan_and_totp_permission_flow(tmp_path: Path, monkeypatch) -> None:
    credential_home = tmp_path / "home"
    credential_home.mkdir()
    (credential_home / ".fmw").write_text("SECRET=redacted-test-value\n", encoding="utf-8")
    aws = credential_home / ".aws"
    aws.mkdir()
    (aws / "config").write_text("[default]\nregion=us-east-1\n", encoding="utf-8")
    state = tmp_path / "state"
    vault = tmp_path / "vault"
    secret = "JBSWY3DPEHPK3PXP"
    code = hotp(secret, int(time.time() // 30))
    monkeypatch.setenv("BEMINE_TOTP_SECRET", secret)

    scan = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "--credential-home",
            str(credential_home),
            "--device",
            "testbox",
            "credentials",
            "scan",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert json_loads(scan.stdout)["credential_profiles"] >= 9

    listing = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "credentials",
            "list",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    rows = json_loads(listing.stdout)
    fmw = next(row for row in rows if row["credential_id"] == "fmw:legacy")
    assert fmw["approval_policy"] == "discord_totp"
    assert fmw["totp_required"] == 1
    assert "redacted-test-value" not in listing.stdout

    capabilities = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "--credential-home",
            str(credential_home),
            "--device",
            "testbox",
            "capabilities",
            "--json",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    manifest = json_loads(capabilities.stdout)
    aws = next(row for row in manifest["credentials"] if row["credential_id"] == "aws:any")
    fmw_mbp = next(row for row in manifest["credentials"] if row["credential_id"] == "fmw:cdjk-mbp")
    github = next(row for row in manifest["credentials"] if row["credential_id"] == "github:personal")
    assert aws["access"] == "approval_required"
    assert "bemine permissions ask aws:any" in aws["request_command"]
    assert aws["proxy_command"] == "bemine proxy check <approved_request_id>"
    assert fmw_mbp["access"] == "approval_required"
    assert fmw_mbp["proxy_mode"] == "brokered_operations_only"
    assert github["access"] == "available"
    assert "redacted-test-value" not in capabilities.stdout

    request = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "--device",
            "testbox",
            "permissions",
            "request",
            "fmw:legacy",
            "--reason",
            "session needs legacy env for migration",
            "--runtime",
            "codex",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    request_id = json_loads(request.stdout)["request_id"]

    bad = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "permissions",
            "approve",
            request_id,
            "--totp",
            "000000",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert bad.returncode != 0
    assert "invalid TOTP" in bad.stderr

    blocked_proxy = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "proxy",
            "check",
            request_id,
            "--actor",
            "codex:test",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert blocked_proxy.returncode != 0
    assert "permission request is pending" in blocked_proxy.stderr

    approved = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "permissions",
            "approve",
            request_id,
            "--actor",
            "test",
            "--totp",
            code,
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    assert json_loads(approved.stdout)["status"] == "approved"

    proxy = subprocess.run(
        [
            sys.executable,
            "-m",
            "bemine.cli",
            "--state-dir",
            str(state),
            "--vault",
            str(vault),
            "proxy",
            "check",
            request_id,
            "--actor",
            "codex:test",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    proxy_result = json_loads(proxy.stdout)
    assert proxy_result["ok"] is True
    assert proxy_result["credential_id"] == "fmw:legacy"
    assert proxy_result["mode"] == "brokered_operations_only"

    conn = sqlite3.connect(state / "bemine.db")
    try:
        assert conn.execute("SELECT status FROM permission_requests").fetchone()[0] == "approved"
        events = [row[0] for row in conn.execute("SELECT event_type FROM audit_events")]
        assert "permission_requested" in events
        assert "permission_approved" in events
        assert "credential_proxy_authorized" in events
    finally:
        conn.close()


def test_permission_request_can_notify_discord_sender(tmp_path: Path) -> None:
    db = BemineDB(tmp_path / "bemine.db")
    db.upsert_credential_profile(
        credential_id="aws:any",
        name="AWS credentials",
        kind="cost_incurring",
        storage_ref="external://aws",
        approval_policy="discord_totp",
        totp_required=True,
    )
    request_id = PermissionGate(db).request(
        credential_id="aws:any",
        reason="Codex session needs AWS SSO for deploy smoke test",
        project_id="proj-alpha",
        session_id="codex:abc",
        device_id="testbox",
        runtime="codex",
    )
    sent_texts: list[str] = []

    result = notify_permission_request(
        db,
        request_id,
        sender=lambda text: sent_texts.append(text) or {"id": "discord-1", "channel_id": "home"},
    )

    assert result.message_id == "discord-1"
    assert result.channel_id == "home"
    assert sent_texts == [result.text]
    assert "AWS credentials" in result.text
    assert "Codex session needs AWS SSO" in result.text
    assert f"bemine approve {request_id} <totp>" in result.text
    assert f"bemine deny {request_id}" in result.text

    conn = sqlite3.connect(tmp_path / "bemine.db")
    try:
        assert (
            conn.execute("SELECT discord_message_id FROM permission_requests").fetchone()[0]
            == "discord-1"
        )
        events = [row[0] for row in conn.execute("SELECT event_type FROM audit_events")]
        assert "permission_requested" in events
        assert "permission_notification_sent" in events
    finally:
        conn.close()


def test_discord_approval_message_redacts_totp_before_storage() -> None:
    from bemine.discord.gateway import _parse_approval_command, _redact_sensitive_message

    content = "bemine approve perm_abc123 123456"
    assert _parse_approval_command(content) == ("perm_abc123", "123456")
    assert _redact_sensitive_message(content) == "bemine approve perm_abc123 <totp-redacted>"


def json_loads(value: str):
    import json

    return json.loads(value)
