# Session Auto-Recovery — Design

Date: 2026-06-09
Status: Approved (design); pending implementation plan

## Problem

Two recurring failures stall the user's tmux-hosted agent sessions, and today nothing
recovers them automatically:

1. **Transient provider rate-limits** — e.g. `API Error: Server is temporarily limiting
   requests (not your usage limit) · Rate limited`. The request fails and the session just
   sits there until the user notices.
2. **Trivial confirmation prompts** — Claude (esp. 4.7/4.8) constantly asks obvious
   yes/continue questions; setting up a goal-driven watcher for these is overkill.

Separately, when the bot is asked to relay input into a session it sometimes **sends the
text but not Enter**, so nothing is submitted — and there is no verification that any
action actually took effect.

## Goal

An always-on auto-recovery loop (in the daemon poll) that, for every *recent* active
session, detects stuck/limited/trivial states and recovers them immediately, **verifying
every action worked**. It sits beneath the existing goal-driven watcher (which stays for
"drive toward a brief") and reuses the same RemoteTmux + status + decision machinery.
Plus a foundational fix so tmux actuation reliably submits and is verified.

## Locked decisions (from brainstorming)

- **Recovery = back off + resubmit in place** for known transient rate-limits (never kill
  the session — keep its context).
- **Tiered classification:** known signatures handled mechanically (no LLM); ambiguous
  states fall back to an LLM sub-agent decision.
- **Verification loop on every action:** observe the effect (re-capture), retry/escalate if
  the state didn't advance — never assume success.
- **Recency is configurable** (`[recovery].max_session_age_secs`, default 1800s). Never act
  on sessions idle longer than that — no reviving a session from 10 days ago.
- **Always-on, no per-project setup** required.

## Architecture

New module `src/bemine/jobs/auto_recovery.py`. Wired into `BemineDaemon` poll (alongside the
existing `_deploy_tick` and `_tick_watchers`). Reuses:
- `bemine.index.status` — fleet status, `asks_user`/`ask_summary`, transcript tails,
  `age_seconds`.
- `bemine.jobs.remote_tmux.RemoteTmux` — local/SSH tmux send + capture.
- `bemine.jobs.project_watcher` — pane resolver (`resolve_pane`), the decision sub-agent
  (`decide`) for the LLM tier, and `echo_to_channel` for escalation.
- A per-session recovery state store (new, small).

All side-effects (status, tmux, decide, echo, clock) are injected via a deps bundle so the
core is unit-testable without real tmux/SSH/LLM (same pattern as the watcher/deploy).

### Components

- **Signatures (`auto_recovery.py`)** — `classify(pane_text, status) -> str` returning one of
  `rate_limit | trivial_confirm | ambiguous | healthy`. `rate_limit` matches a configurable
  list of case-insensitive substrings (defaults below). `trivial_confirm` matches obvious
  proceed prompts when `status.asks_user`. Else if `status.asks_user` → `ambiguous`; else
  `healthy`.
- **Recovery state store** — per-session record keyed by `device:runtime:session_id`:
  `attempts`, `next_retry_at`, `backoff_secs`, `last_action`, `last_seen_at`. JSON file
  `state_dir/recovery_state.json` (mirrors `session_watcher_state.json`). Stale records
  (older than the recency window) are pruned.
- **Tick driver** — `tick_recovery(settings, deps, *, now)`: for each fleet session within
  the recency window, classify and act (see Data flow), persisting recovery state.
- **Actuation fix** — `tmux_send` tool + `RemoteTmux.send_text`: literal text → small delay
  → `Enter` → caller re-captures to confirm submission; resend `Enter` once if unchanged.

## Data flow (one recovery tick)

For each session from `collect_fleet_status` with `age_seconds <= max_session_age_secs`:

1. Resolve the tmux pane (`resolve_pane`); no pane → skip (can't act).
2. `kind = classify(capture(pane), status)`.
3. If `healthy` → reset that session's recovery record (attempts=0) and continue.
4. If a record exists with `now < next_retry_at` → still in backoff, continue.
5. Act by tier:
   - **rate_limit** (mechanical): resubmit in place — `send_keys(Enter)` (or re-send the last
     user input if available). No LLM.
   - **trivial_confirm** (mechanical): `send_text("yes")` / `Enter`.
   - **ambiguous** (LLM): `decide(...)` with a generic brief ("keep this session unstuck;
     never take destructive/irreversible actions or use gated credentials — escalate
     instead"). Apply its `send_text`/`send_keys`; `escalate`/`done`/`wait` handled as in the
     watcher.
6. **Verify:** sleep `verify_delay_secs`, re-capture. If the pane changed AND the triggering
   signature is gone → success: reset attempts/backoff, echo a brief note. If not advanced →
   `attempts += 1`, grow backoff (`backoff_secs *= 2`), set `next_retry_at`. When
   `attempts >= max_attempts` → escalate to the session's Discord channel (project channel via
   binding, else the catch-all) and stop retrying until the state changes.
7. Audit every action/escalation (`audit_events`, category `recovery`).

The existing **watcher** gains the same verify-after-act step on its sends (closing the
deferred loop-protection gap): after a send, re-capture; if unchanged for K ticks, back off
and escalate rather than re-sending blindly.

## Configuration (`[recovery]` in bemine.toml; sensible defaults)

- `enabled` (bool, default `true`)
- `max_session_age_secs` (int, default `1800`) — ignore sessions idle longer than this.
- `backoff_secs` (int, default `30`) — initial backoff; doubles per failed attempt.
- `max_attempts` (int, default `5`) — failed verifications before escalating.
- `verify_delay_secs` (int, default `4`) — wait before re-capturing to verify.
- `rate_limit_signatures` (list[str]) — default:
  `["server is temporarily limiting requests", "rate limited", "api error",
    "overloaded", "529", "please try again", "too many requests"]`.

## Error handling / edge cases

- No pane / SSH failure → skip that session this tick (logged, not fatal).
- Destructive/credential prompts → only reachable via the `ambiguous` LLM tier, which is
  instructed to escalate, never act.
- A session that keeps failing verification → escalates once, then is left alone (record
  carries `attempts >= max_attempts`) until its pane state changes, so it can't spam.
- Recency window prevents acting on long-idle sessions; stale records pruned each tick.

## Testing

Core is deps-injected — no real tmux/SSH/LLM/sleeps:
- `classify`: rate-limit string → `rate_limit`; "Proceed? (y/n)" + asks_user → `trivial_confirm`;
  other asks_user → `ambiguous`; quiet → `healthy`.
- rate-limit tick: resubmits (Enter), verify shows error gone → attempts reset.
- backoff: verify shows unchanged → attempts++, `next_retry_at` set; within backoff → skipped.
- escalation: after `max_attempts` unchanged → channel echo + audit; then no further sends.
- recency: session with `age_seconds > max_session_age_secs` → never touched.
- trivial_confirm tick: sends "yes", verify advanced.
- ambiguous tick: injected `decide` returns send/escalate → applied + verified.
- actuation: `RemoteTmux.send_text` issues literal-text then Enter; verify-resend path sends a
  second Enter when the pane is unchanged.

## Out of scope (separate follow-ups)

- Launching Claude workers non-interactively (`--permission-mode bypassPermissions`) so they
  prompt in plain text "like codex" — a worker-spawn config change.
- A verification pass inside the general dispatch agent-loop (this spec covers actuation,
  auto-recovery, and the watcher's sends).
