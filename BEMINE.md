# BEMINE.md — how to operate this harness (the playbook)

AGENTS.md tells you WHAT tools exist; this file tells you HOW to do the recurring jobs
with them — from updating Bemine itself down to trivial asks. Read it with `file_read`
(native) or directly (codex) whenever you're about to operate the harness.

## Updating Bemine itself (self-modification)

The ONLY correct ship path — never restart services by hand, never edit on a dirty tree:

1. `git status` must be clean; work on a branch: `git checkout -b <fix|feat>/<slug>`.
2. Make the change. Every behavior change gets a test first (`uv run pytest <file> -v`).
3. Gate locally: `uv run pytest -q` (all green; `tests/test_workers.py::test_workers_transfer_…`
   is a known real-tmux flake — rerun once before believing it).
4. `git checkout main && git merge --no-ff <branch> && git branch -d <branch> && git push origin main`.
5. Deploy: `deploy_control {"action":"request"}` (native tool) or `uv run bemine deploy` (CLI).
   The pipeline re-runs the suite, restarts the gateway, verifies its heartbeat on the new
   commit, refreshes the daemon, and AUTO-ROLLS-BACK if anything is unhealthy.
6. Verify: `uv run bemine deploy status` → `phase=confirmed target=<new sha>`; heartbeats in
   `.bemine/deploy/heartbeat-*.json` must show that sha.

If the deploy fails its gate, fix the tests — do not bypass. If a deploy wedges, the daemon
reaps stale state automatically (~30 min); `deploy status` shows the phase.

## Trivial asks (do them, don't narrate them)

- "what's running / status of X" → `status_show` (or `uv run bemine status --limit 12`);
  for a project channel, X = THAT project's session (match cwd basename).
- "send/tell the session …" → `tmux_list` → `tmux_send` (it appends Enter; afterwards
  `tmux_capture` to VERIFY the input registered — if the pane didn't change, send Enter again).
- "what did it say / is it stuck" → `tmux_capture` (remote devices reachable; recovery and
  watcher already poll every ~30s, so don't build ad-hoc loops).
- "add X to shopping/checklist" → `checklist.add` route (the gateway's local tool router).
- "create a channel / post to a channel" → Discord REST via `shell_run` with
  `$MAYOR_DISCORD_BOT_TOKEN`; find the guild via `GET /users/@me/guilds`.
- "remind me / schedule X" → `uv run bemine jobs add-reminder --help`.
- config questions → `bemine_config` tool or `uv run bemine config show`; NEVER guess
  runtime/model identity.

## Watching & recovering sessions

- Goal-driven babysitting of a project session → `watch_control {"action":"start","brief":
  "<the user's full context verbatim>","interval_secs":600}`. Stop: `{"action":"stop"}`.
- Always-on auto-recovery (rate-limits resent with Up+Enter, trivial confirms auto-yes'd,
  questions answered by an LLM tier) runs in the daemon — check what it did via
  `audit_list {"category":"recovery"}`. Tune in `bemine.toml [recovery]`.
- A session idle > `[recovery].max_session_age_secs` (4h) is deliberately left alone.

## Memory & records

- Durable facts/decisions → Obsidian vault (`~/Documents/Mayor`): project pages auto-refresh
  daily; decisions in `Decisions/`; checklists in `Checklists/`.
- End substantial work with `uv run bemine handoff "<what changed>" --next "<next>"
  --test "uv run pytest -q"` — this also writes the `Daily/` note.
- Operational history → `audit_list` / `uv run bemine audit list` (categories: deploy,
  recovery, watcher, discord, jobs).

## Scheduled jobs

- `uv run bemine jobs list` — what runs when (daily digest 18:00 and morning plan 08:30 go
  to the #daily-digest channel; session watcher every 2 min).
- Run one now: `uv run bemine jobs run <job_id>`. Job configs live in SQLite (`jobs` table);
  `channel_id` in a job's config overrides where its output is posted.

## Environment gotchas (learned the hard way — don't relearn them)

- systemd services have a MINIMAL PATH: use absolute binaries in config
  (`[codex].bin = /home/cdjk/.npm-global/bin/codex`).
- Remote tmux needs a login-interactive shell: `ssh <dev> 'zsh -lic "tmux …"'`.
- codex `workspace-write` sandbox (bubblewrap) is broken under systemd on this box —
  keep `[codex].sandbox = "danger-full-access"`.
- The claude runtime rides the meridian proxy (127.0.0.1:3456, Claude Max plan); meridian's
  passthrough caps internal turns at 3 — a 500 "maximum number of turns" is meridian, not you.
- tmux text input: literal text first, brief pause, then Enter; always re-capture to verify.
- A "stuck" session's last-activity is OLD by definition — never filter stuck things by
  recent activity.

## When blocked

Say exactly what is blocked and why (credential gate, destructive step, missing access) —
after you have actually tried. "Run this command yourself" is never an acceptable reply.
