---
name: ampixa-blog
description: Publish technical work to ampixa.com/blog through the Bemine orchestrator. Use when you complete a benchmark result, dataset, model release, training milestone, or hard-won technical lesson worth publishing — or when the user says "blog this", "publish this", "write this up for ampixa". Submits a draft for review; NEVER publishes directly.
---

# Publishing to the Ampixa blog

You are an agent in the user's fleet. When your work produces something worth
publishing on https://ampixa.com/blog, you submit a DRAFT to Bemine (the
Discord orchestrator). Review, change requests, and the actual publish happen
there — never publish or deploy the site yourself.

## When this skill fires

- You finished a benchmark, evaluation, dataset build, model release, or
  training run with a concrete result.
- You learned a technical lesson the Ampixa audience (Nepali speech/audio
  infrastructure) would care about.
- The user says "blog this", "write this up", "publish this on ampixa".

If the result is preliminary, unverified, or has no artifact to link — do not
submit. Capture it as an SMM backlog idea instead
(`bemine backlog add "..." --owner SMM`).

## Ampixa philosophy (the draft must follow this)

- Text-first, plain, technical. Monospace sensibility; no marketing voice.
- Lead with the concrete artifact (repo, dataset, model card, benchmark,
  demo). Every claim ties to one — a link or a number you actually measured.
- Prefer "we built / we measured / we released / we are testing" over hype.
  Words like "revolutionary" or "world-class" FAIL validation.
- One technical detail that proves the work is real (the messy part: bad
  microphones, code-switching, dialect, noise — that is the brand).
- End with a low-pressure prompt only if useful.
- 400+ characters minimum; title under 90.

## Draft shape

```markdown
We measured/built/released <artifact>.

<Why it matters for Nepali/regional speech infrastructure — 1-2 paragraphs.>

The detail that proves it is real: <one concrete technical specific,
with the number/link>. Artifact: <https://github.com/Ampixa/...>

<Optional: what is next / how to use it.>
```

## How to submit

Write the draft to a markdown file, then:

On cdjk (the orchestrator host):

    uv run --directory /home/cdjk/bm bemine blog submit <draft.md> \
        --title "<title>" --project <project> --author <your-session-name>

On any other machine (mbp, mbp2, ...), pipe over ssh:

    cat draft.md | ssh cdjk 'cd ~/bm && ~/.local/bin/uv run bemine blog submit - \
        --title "<title>" --project <project> --author <your-session-name>'

Attach images with `--image <file.png>` (repeatable; local-file path only on
cdjk — from remote machines, scp the image to cdjk first or note it in the
draft for the orchestrator to generate).

## What happens next (not your job)

The draft is queued and announced in #ampixa-blog. Bemine or the user reviews,
may send it back (`needs_input` — you may be asked for more context through
your project channel), validates style, then publishes. Your submission output
includes the style-check verdict — if it lists problems, fix the draft and
resubmit under a new title, or wait for the change request.

## Hard rules

- NEVER run `bemine blog publish` or touch the ampixa site/server yourself.
- NEVER invent numbers or results to satisfy the artifact rule.
- One draft per distinct result; do not spam the queue with variants.
