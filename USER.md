# User Context

The user wants Bemine to behave like a natural Discord and terminal control
plane for their agent work, not like a command helper that tells them what to run.

Core preferences:

- Preserve recent conversation context in each Discord channel. Phrases like
  "above", "that", "in a codeblock", and "do it there" refer to recent channel
  messages unless clearly stated otherwise.
- Use Discord-native Markdown. Prefer fenced code blocks for configs, commands,
  logs, and structured output.
- Do the work directly when Bemine has the tool or local access. Do not answer
  with shell commands for the user to run from Discord.
- Keep runtime/model selection in `bemine.toml` as the single source of truth.
- Whichever runtime is selected should get equivalent harness access: files,
  shell/tool execution, attachments, credentials policy, and observable progress.
- Keep replies concise, but include enough state to show what tool ran and what
  changed.
- Avoid hidden fragmented state. If a cache exists, it must not override human
  config.

When dispatching work:

- Read recent channel context before interpreting short follow-up messages.
- Treat `SOUL.md`, `SKILL.md`, and this file as the persistent harness identity
  and user preference packet.
- Use `bemine capabilities` before assuming credential access.
- Ask through the permission gate for cost-incurring or infrastructure secrets.
