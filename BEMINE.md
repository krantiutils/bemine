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
- "what can you do / help" → `help` or `uv run bemine help`; this prints the live
  capability/config summary and the main operator surfaces.
- "line cuts / continuity on cdjk" → `continuity` / `uv run bemine continuity`
  (or ask `wake sessions after reboot?`; use `health` if you want the whole
  deploy/service/watchers packet).
- "what Bemine-managed workers are running / duplicates?" → `workers_status` native tool
  or `uv run bemine workers list` / `uv run bemine workers duplicates`.
- "send/tell the session …" → `tmux_list` → `tmux_send` (it appends Enter; afterwards
  `tmux_capture` to VERIFY the input registered — if the pane didn't change, send Enter again).
- "what did it say / is it stuck" → `tmux_capture` (remote devices reachable; recovery and
  watcher already poll every ~30s, so don't build ad-hoc loops).
- "add X to shopping/checklist" → `checklist.add` route (the gateway's local tool router).
- "store SMM/PA idea X" → `uv run bemine backlog add "<idea>" --owner SMM|PA`
  (Discord also accepts `SMM idea: ...` / `store idea for PA: ...`).
- "create/sync project channels" → inspect `channel_routes` first, or use
  `/discord routes`, `discord routes`, or `uv run bemine discord routes` for the
  read-only route view. Then dry-run with `/discord sync-plan`, `channel_sync_plan`,
  `discord sync plan`, or
  `uv run bemine discord sync-project-channels --dry-run`. Only after that use
  `channel_sync_apply` with `confirm=true` to mutate
  Discord. Use `shell_run` + Discord REST only for channel operations outside that
  project-sync path.
- "remind me / schedule X" → `uv run bemine jobs add-reminder --help`.
- "start/run a goal for project X" → `goal_start` native tool or
  `uv run bemine goal <project> "<goal brief>"`; this starts a managed tmux worker,
  sends the brief, and registers a watcher. Inspect goal workers/watchers with
  `goal_status` or `uv run bemine goal --list`.
- "dream / learn from recent history" → `dream status` / `when did you last dream?`
  to inspect freshness; `dream_run` native tool, `dream now` in Discord, or
  `uv run bemine dream run` to append one JSONL data point to `Dreams/dreams.jsonl`.
  The daemon also runs the `dream` job periodically.
- config questions → `bemine_config` tool or `uv run bemine config show`; NEVER guess
  runtime/model identity.
- phone/ADB questions → `capabilities_status` native tool or `uv run bemine capabilities`;
  the tool section reports ADB availability, attached device authorization state, and
  Android SDK path. Phantom-specific work → `phantom_status` /
  `uv run bemine phantom status`; install with `phantom_install` /
  `uv run bemine phantom install` once ADB shows an authorized device.

## Watching & recovering sessions

- Goal that should run itself from CLI/Discord → `goal_start` native tool or
  `uv run bemine goal <project> "<brief>"` (optional `--cwd`, `--runtime`,
  `--worker`, `--interval`). This is the preferred path over manually starting a
  terminal and then adding a watcher. If the project is only visible in fleet status,
  `goal` can start the worker on that device; use `target_device`/`--target-device
  <host>` to force a device. Inspect with `goal_status`, `uv run bemine goal --list`,
  or `uv run bemine goal --status <project-or-worker> --json`.
- cdjk line cuts / power loss: systemd restarts `bemine-daemon`; the daemon's
  `[continuity]` pass restarts Bemine-managed local tmux workers that were recorded as
  `running` but whose tmux session disappeared. This only applies to managed workers
  (especially `bemine goal` sessions), not arbitrary/adopted terminal sessions.
- Session watcher updates create Discord project channels on first active sighting, then
  route started/done/needs-input updates there; non-project sessions use
  `[jobs].session_fallback_channel`.
- Goal-driven babysitting of a project session → `watch_control {"action":"start","brief":
  "<the user's full context verbatim>","interval_secs":600}`. Stop: `{"action":"stop"}`.
  Inspect all active watchers with `watchers_status`.
- Always-on auto-recovery (rate-limits resent with Up+Enter, usage limits held until
  their reset clock then nudged with `continue`, trivial confirms auto-yes'd, questions
  answered by an LLM tier) runs in the daemon — check what it did via
  `audit_list {"category":"recovery"}`. Tune in `bemine.toml [recovery]`.
- A session idle > `[recovery].max_session_age_secs` (4h) is deliberately left alone.

## Memory & records

- Durable facts/decisions → Obsidian vault (`~/Documents/Mayor`): project pages auto-refresh
  daily; decisions in `Decisions/`; checklists in `Checklists/`.
- PA/SMM ideas and tasks → `uv run bemine backlog add ...`; files live under
  `Backlog/PA.md` and `Backlog/SMM.md`.
- PA/SMM daily brief → `brief status` / `when was the last PA/SMM brief?` to
  inspect freshness; `brief run` / `generate PA/SMM brief` in Discord, or
  `uv run bemine brief run` to write `Briefs/<date>-pa-smm.md` and add
  non-duplicate generated PA/SMM backlog nudges.
- "what should I focus on today?" / PA planning → `pa_plan` native tool or
  `uv run bemine pa plan`; this prioritizes health issues, failed jobs, waiting
  sessions, active watchers, recovery escalations, and PA backlog with evidence.
- SMM post ideas/copy → `smm status` / `when was the last SMM draft?` to
  inspect freshness; `smm_drafts` native tool or `uv run bemine smm draft` to
  turn SMM backlog/brief signals into Ampixa-style draft posts with proof,
  target channels, and image prompts using `subagents/style.md` + `assets/ampixa/`.
- Local Discord history → `discord_threads` / `discord_transcript` native tools, or
  `discord_refresh_channels` / `uv run bemine discord refresh-channel-names` to
  backfill readable server channel names, then `uv run bemine discord threads` and
  `uv run bemine discord transcript --query "<term>"`; transcript reads work even when
  live Discord API credentials are unavailable.
- Periodic self-improvement memory → `dream_run` native tool or
  `uv run bemine dream run`; data points append to `Dreams/dreams.jsonl` and are
  audit-logged as `dream/dream_recorded`.
- End substantial work with `uv run bemine handoff "<what changed>" --next "<next>"
  --test "uv run pytest -q"` — this also writes the `Daily/` note.
- Operational history → `audit_list` / `uv run bemine audit list` (categories: deploy,
  recovery, watcher, discord, jobs).

## Scheduled jobs

- `health_status` native tool or `uv run bemine health` — one packet for deploy phase,
  daemon/gateway units, heartbeats, job failures, and active watchers. Use this before
  drilling down.
- `uv run bemine systemd status` — daemon/gateway unit state plus heartbeat SHA.
- `uv run bemine jobs list` — what runs when (session watcher every 2 min,
  dream at 03:15, PA/SMM brief at 08:45, daily digest at 18:00 by default).
- Run one now: `job_run` native tool or `uv run bemine jobs run <job_id>`. Job configs
  live in SQLite (`jobs` table); `channel_id` in a job's config overrides where its
  output is posted.

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
