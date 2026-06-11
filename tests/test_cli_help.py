from __future__ import annotations

import subprocess
import sys


def _help(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "bemine.cli", *args, "--help"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def test_top_level_help_onboards_a_new_agent() -> None:
    text = _help()

    assert "New agent quickstart:" in text
    assert "uv run bemine prime" in text
    assert "Core concepts:" in text
    assert "capabilities  safe credential/capability manifest" in text
    assert "phantom       inspect/build/install" in text
    assert "permissions   explicit approval path" in text
    assert "workers       safe managed live sessions over tmux" in text
    assert "goal          start a managed worker and watcher" in text
    assert "health        deploy/services/jobs/watchers health packet" in text
    assert "continuity    line-cut recovery status packet" in text
    assert "help          self-documenting capability and config summary" in text
    assert (
        "discord       project channel routing, route inspection, and Discord sync"
        in text
    )
    assert "backlog       durable PA/SMM idea" in text
    assert "pa            plan daily focus" in text
    assert "smm           draft Ampixa-style social posts" in text
    assert "dream         condense recent history" in text
    assert "brief         generate PA/SMM brief" in text


def test_prime_help_explains_startup_packet() -> None:
    text = _help("prime")

    assert "Render the startup packet a new worker should read first." in text
    assert "Use --fleet when the worker needs cross-device status." in text
    assert "uv run bemine prime --fleet --limit 12" in text
    assert "Project root to describe" in text


def test_handoff_help_explains_shutdown_packet() -> None:
    text = _help("handoff")

    assert "Record the shutdown packet for substantial work." in text
    assert "Handoffs/ directory" in text
    assert "before ending a meaningful session" in text
    assert "Verification command/result" in text


def test_permissions_and_tmux_help_surface_safety_rules() -> None:
    permissions = _help("permissions")
    tmux = _help("tmux")
    workers = _help("workers")
    tmux_flat = " ".join(tmux.split())
    workers_flat = " ".join(workers.split())

    assert "ask" in permissions
    assert "notifies Discord by default" in permissions
    assert "TOTP" in permissions
    assert "safe live-control transport" in tmux
    assert "Do not inject text into arbitrary raw TTYs" in tmux_flat
    assert "adopt existing tmux sessions" in workers_flat
    assert "Raw Codex/Claude/Kimi terminals" in workers_flat


def test_goal_help_explains_worker_plus_watcher_flow() -> None:
    text = _help("goal")
    flat = " ".join(text.split())

    assert "Start a managed goal session." in text
    assert "starts a tmux-backed worker" in flat
    assert "registers a watcher" in flat
    assert "--target-device" in text
    assert "--list" in text
    assert "--status" in text
    assert "uv run bemine goal rai-speech-toolkit" in text


def test_backlog_help_explains_vault_storage() -> None:
    text = _help("backlog")
    flat = " ".join(text.split())

    assert "Capture or list durable PA/SMM backlog items." in text
    assert "Obsidian vault under Backlog/<owner>.md" in flat
    assert "uv run bemine backlog add" in text


def test_pa_help_explains_live_evidence_plan() -> None:
    text = _help("pa")
    flat = " ".join(text.split())

    assert "Plan daily focus from live PA evidence." in text
    assert "Bemine health" in flat
    assert "active watchers" in flat
    assert "uv run bemine pa plan" in text


def test_smm_help_explains_draft_inputs_and_assets() -> None:
    text = _help("smm")
    flat = " ".join(text.split())

    assert "Draft Ampixa-style social posts from existing SMM evidence." in text
    assert "SMM backlog items" in flat
    assert "assets/ampixa" in flat
    assert "uv run bemine smm draft" in text


def test_phantom_help_explains_phone_install_flow() -> None:
    text = _help("phantom")
    flat = " ".join(text.split())

    assert "Inspect and install the Phantom Android accessibility service app." in text
    assert "ADB authorization state" in flat
    assert "does not allow Bemine to silently enable" in flat
    assert "uv run bemine phantom install --dry-run --json" in text


def test_dream_help_explains_jsonl_storage() -> None:
    text = _help("dream")
    flat = " ".join(text.split())

    assert "Condense recent Bemine/Discord history" in text
    assert "Dreams/dreams.jsonl" in flat
    assert "uv run bemine dream status" in text
    assert "uv run bemine dream run" in text


def test_brief_help_explains_pa_smm_backlog_nudges() -> None:
    text = _help("brief")
    flat = " ".join(text.split())

    assert "Generate a deterministic PA/SMM brief" in text
    assert "Briefs/" in flat
    assert "non-duplicate PA/SMM backlog items" in flat
    assert "uv run bemine brief status" in text
    assert "uv run bemine brief run" in text


def test_discord_help_includes_local_transcript_commands() -> None:
    text = _help("discord")
    flat = " ".join(text.split())

    assert "threads" in text
    assert "transcript" in text
    assert "refresh-channel-names" in text
    assert "without requiring live Discord API credentials" in flat
    assert "uv run bemine discord transcript" in text


def test_systemd_help_includes_status() -> None:
    text = _help("systemd")

    assert "status" in text
    assert "Show systemd unit state" in text
    assert "uv run bemine systemd status" in text


def test_health_help_explains_aggregate_packet() -> None:
    text = _help("health")

    assert "Summarize Bemine deploy" in text
    assert "systemd services" in text
    assert "uv run bemine health --json" in text
