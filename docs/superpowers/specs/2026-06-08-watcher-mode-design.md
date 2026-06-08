# Watcher Mode — Design

Date: 2026-06-08
Status: Approved (design); pending implementation plan

## Problem

The projects the user actually works on (e.g. `rai-speech-toolkit`, `magar-toolkit`,
`gurung-language-toolkit`) run as agent sessions **on remote devices** (mbp). The
Discord gateway and its native worker run on `cdjk` and execute tools locally, so a
Discord request to "improve project X" has no files to touch on the gateway host — it
cannot wire anything up, and the user sees nothing happen. Today the user must SSH into
the device and talk to the session directly.

Separately, sessions frequently **stall waiting on input** (a question, a permission
prompt, a select/checkbox menu) and sit idle until the user notices.

## Goal

A per-project **watcher** that keeps a tmux-hosted agent session moving toward a goal:
it polls the session at an interval, and when the session is waiting on input it decides
what to send (guided by a rich context brief the user supplies), sends it via tmux —
over SSH for remote devices — and only messages the user when genuinely blocked. Every
action is echoed to the project's Discord channel so the user always sees what happened.

## Decisions (locked during brainstorming)

- **Session host:** sessions run **inside tmux** on each device. Drive via
  `tmux send-keys` / `capture-pane`.
- **Topology:** the watcher runs on the **gateway host (cdjk)** and reaches remote panes
  over **SSH** — the same SSH path already used by `collect_fleet_status`. No new daemon
  is deployed to other devices.
- **Decision model:** **autonomous, escalate when blocked.** A decision sub-agent decides
  the keystrokes each tick; it escalates to the user only when it needs a credential, a
  real human decision, or hits something destructive/ambiguous.
- **Control surface:** **plain language in the project channel** (reusing the
  channel→project binding), plus a `bemine watch` CLI.
- **Context brief:** the user supplies a **lengthy free-form brief** at enable time. It is
  stored verbatim and injected into every decision. This is the watcher's own goal,
  additive to whatever goal the session itself holds.

## Architecture

New module `src/bemine/jobs/project_watcher.py`, distinct from the notify-only
`src/bemine/jobs/session_watcher.py`. It reuses:

- `bemine.index.status` — fleet status, `asks_user` / `ask_summary` inference, transcript
  tails (already cross-device via the SSH status script).
- An SSH-aware tmux runner (a thin wrapper that runs `tmux` locally for the gateway host
  and `ssh <device> tmux …` for remote devices). The runner is **injectable** so tests can
  substitute a fake.
- A **decision sub-agent** (the existing `AgentLoop` / native client path) that, given the
  brief + live pane capture + transcript tail + recent channel messages, returns a
  structured decision.

Wired into the existing `BemineDaemon` poll loop (~30s). Watcher config and state are
persisted (new `project_watchers` table — see Data).

### Components and responsibilities

- **Watcher store** — persist/read watcher config + state. One row per project channel.
- **Tmux/SSH runner** — `list_panes(device)`, `capture(device, pane)`,
  `send_keys(device, pane, keys, enter)`. Local when `device` is the gateway, else SSH.
- **Pane resolver** — map a session (device, cwd, runtime) to a tmux pane: list panes on
  the device and match `pane_current_path == session.cwd` (and `pane_current_command` is
  `claude`/`codex`/`kimi`). Ambiguous/no match → escalate.
- **Decision sub-agent** — input: brief, pane capture, transcript tail, recent channel
  messages, ask_summary. Output (structured): one of
  - `send` with `keys` (free-text, or menu navigation: arrows/space/number) and `enter`,
  - `escalate` with a `question` for the user,
  - `done` with a short rationale (goal judged achieved),
  - `wait` (still working / nothing to do this tick).
- **Tick driver** — orchestrates one watcher tick (see Data flow).
- **Channel echo** — post each send/escalation/done to the project channel and write an
  `audit_events` row.
- **CLI** `bemine watch` — `start <project> --goal/--brief --interval`, `stop`, `status`,
  `list`.

## Data flow (one tick)

1. Daemon poll: for each **active** watcher whose interval has elapsed, fire a tick.
2. Resolve the project's live session via fleet status → device, cwd, session_id,
   transcript path. No live session → reschedule (and, after N empty ticks, pause+notify).
3. Pane resolver finds the tmux pane on that device.
4. Capture pane + transcript tail.
5. If the session is **actively working** (not `asks_user`, not idle past threshold) →
   `wait`, reschedule.
6. If **waiting on input** → call the decision sub-agent.
7. Apply the decision:
   - `send` → `send_keys` via the runner; echo "▶ sent: …" to the channel; record tick.
   - `escalate` → post the question to the channel; set `awaiting_reply`; pause ticking.
   - `done` → echo, set status `done`, stop ticking.
   - `wait` → reschedule.
8. Loop-protection: if the pane capture is unchanged after a `send` for K consecutive
   ticks, back off (longer interval) and eventually escalate ("session not responding to
   input").

### Escalation ↔ user reply

When a watcher is `awaiting_reply`, the **next user message in that project channel** is
consumed as the answer: it is relayed into the session (via the pane) and the watcher
resumes, instead of spawning the normal channel agent. When no watcher is awaiting in the
channel, channel messages behave normally (the bound channel agent).

## Control (plain language in the channel)

A new local tool `watch_control` is added to the channel agent's tool registry, with
actions `start` (args: `brief`, `interval`), `stop`, `status`. Because the bound channel
agent already runs on every channel message, "watch this, goal: … <long brief>, check
every 10m" routes to `watch_control(start, …)` with the whole brief captured verbatim.
"stop watching" → `watch_control(stop)`. "watcher status" → `watch_control(status)`. The
agent resolves the target project from the channel binding, so the user never names it.

The `bemine watch` CLI provides the same operations from a terminal.

## Safety and visibility

- **Visibility:** every send, escalation, and `done` is echoed to the project channel and
  written to `audit_events`. This directly fixes "it shows nothing."
- **No autonomous destructive actions:** the sub-agent must `escalate` for anything
  destructive/irreversible or requiring a gated credential (consistent with existing
  credential/permission rules). The prompt states this explicitly.
- **Stop conditions:** user stop; `done`; session gone/idle for N consecutive ticks;
  repeated send failures or non-responsive pane → pause + notify.
- **Bounds:** configurable `max_ticks` and `max_wall_clock`; default interval 5m; back-off
  on no-change.

## Data

New table `project_watchers`:

| column          | type    | notes                                            |
|-----------------|---------|--------------------------------------------------|
| project_id      | TEXT PK | the bound project / channel-derived id           |
| channel_id      | TEXT    | Discord channel to echo to and consume replies   |
| brief           | TEXT    | verbatim user context brief (the goal)           |
| interval_secs   | INTEGER | poll interval                                    |
| status          | TEXT    | active / paused / awaiting_reply / done / stopped|
| last_tick_at    | REAL    | epoch of last tick                               |
| awaiting_reply  | INTEGER | 0/1                                              |
| empty_ticks     | INTEGER | consecutive ticks with no live session           |
| started_at      | REAL    |                                                  |
| created_at      | REAL    |                                                  |

## Testing

- **End-to-end (local tmux):** start a real local tmux pane running a script that prints a
  "stuck" prompt and reads a line; assert the watcher detects waiting, the decision
  (stubbed sub-agent) produces keystrokes, `send_keys` delivers them, the pane advances,
  and a tick is recorded.
- **Escalation path:** decision returns `escalate` → assert a channel message is posted,
  `awaiting_reply` set, ticking paused; then a simulated channel reply is relayed into the
  pane and the watcher resumes.
- **Remote path:** inject a fake SSH runner; assert remote `tmux` commands are formed as
  `ssh <device> tmux …` and routed correctly.
- **Control:** `watch_control` start/stop/status mutate the store and resolve the project
  from the channel binding; CLI mirrors it.
- **Safety:** loop-protection backs off on unchanged pane; destructive decisions escalate;
  idle/gone session pauses after N ticks.

## Out of scope

- Making *one-off* (non-watcher) Discord requests execute on a remote project. The
  channel-binding fix already makes the bound agent relay-aware; full one-off remote
  execution is a separate piece.
- Deploying a persistent Bemine daemon to non-gateway devices (the per-device topology
  was considered and deferred in favor of SSH-from-gateway).
