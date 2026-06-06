from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import BemineSettings
from .project_filters import ignored_project_reason
from ..util import compact_text


DEFAULT_STATUS_DEVICES = ("local", "mbp", "mbp2", "k2", "pi", "windows")
CODE_RE = re.compile(r"\b(?:\d{6,8}|[A-Z0-9]{4}-[A-Z0-9]{4})\b")
TOKEN_RE = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|sk[-_][A-Za-z0-9_-]{12,}"
    # Discord bot tokens (3 dot-separated base64 segments) and JWTs (eyJ...).
    r"|[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,8}\.[A-Za-z0-9_-]{27,}"
    r"|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b"
)
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")


@dataclass(frozen=True)
class SessionStatus:
    device_id: str
    runtime: str
    session_id: str
    cwd: str
    source_path: str
    last_interaction_at: float
    age_seconds: int
    last_role: str
    asks_user: bool
    ask_summary: str
    latest_summary: str
    model: str = ""
    branch: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StatusError:
    device_id: str
    error: str


def collect_fleet_status(
    settings: BemineSettings,
    *,
    devices: list[str] | None = None,
    limit_per_runtime: int = 8,
    timeout: int = 8,
) -> tuple[list[SessionStatus], list[StatusError]]:
    selected = devices or settings.status_devices or list(DEFAULT_STATUS_DEVICES)
    statuses: list[SessionStatus] = []
    errors: list[StatusError] = []
    for device in selected:
        if device == "local":
            statuses.extend(
                collect_local_status(
                    device_id=settings.device_id,
                    codex_home=settings.codex_home,
                    claude_home=settings.claude_home,
                    kimi_home=settings.kimi_home,
                    limit_per_runtime=limit_per_runtime,
                )
            )
            continue
        try:
            statuses.extend(
                collect_ssh_status(
                    host=device,
                    device_id=device,
                    timeout=timeout,
                    limit_per_runtime=limit_per_runtime,
                )
            )
        except Exception as exc:
            errors.append(
                StatusError(device_id=device, error=compact_text(str(exc), limit=160))
            )
    statuses = [status for status in statuses if not _status_is_ignored(status)]
    statuses.sort(key=lambda item: item.last_interaction_at, reverse=True)
    return _dedupe_statuses(statuses), errors


def collect_local_status(
    *,
    device_id: str,
    codex_home: Path,
    claude_home: Path,
    kimi_home: Path,
    limit_per_runtime: int = 8,
) -> list[SessionStatus]:
    rows: list[SessionStatus] = []
    rows.extend(
        _collect_runtime(
            device_id=device_id,
            runtime="codex",
            root=codex_home / "sessions",
            parser=_parse_codex_status,
            limit=limit_per_runtime,
        )
    )
    rows.extend(
        _collect_runtime(
            device_id=device_id,
            runtime="claude",
            root=claude_home / "projects",
            parser=_parse_claude_status,
            limit=limit_per_runtime,
        )
    )
    rows.extend(
        _collect_runtime(
            device_id=device_id,
            runtime="kimi",
            root=kimi_home,
            parser=_parse_kimi_status,
            limit=limit_per_runtime,
        )
    )
    rows.sort(key=lambda item: item.last_interaction_at, reverse=True)
    return [row for row in rows if not _status_is_ignored(row)]


def _dedupe_statuses(statuses: list[SessionStatus]) -> list[SessionStatus]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[SessionStatus] = []
    for status in statuses:
        key = (
            status.device_id,
            status.runtime,
            status.session_id or status.source_path,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(status)
    return deduped


def _status_is_ignored(status: SessionStatus) -> bool:
    return bool(status.cwd and ignored_project_reason(status.cwd))


def collect_ssh_status(
    *,
    host: str,
    device_id: str,
    timeout: int = 8,
    limit_per_runtime: int = 8,
) -> list[SessionStatus]:
    python_cmd = "python" if host.lower() == "windows" else "python3"
    proc = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={min(timeout, 5)}",
            host,
            python_cmd,
            "-",
            device_id,
            str(limit_per_runtime),
        ],
        input=REMOTE_STATUS_SCRIPT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"ssh exited {proc.returncode}")
    rows: list[SessionStatus] = []
    now = time.time()
    for line in proc.stdout.splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        raw["age_seconds"] = max(0, int(now - float(raw["last_interaction_at"])))
        rows.append(SessionStatus(**raw))
    return rows


def render_status(
    statuses: list[SessionStatus],
    errors: list[StatusError] | None = None,
    *,
    limit: int = 12,
) -> str:
    if not statuses:
        text = "No recent Codex, Claude, or Kimi session interactions found."
    else:
        lines = ["Recent agent sessions, ordered by last interaction:"]
        for item in statuses[:limit]:
            age = _format_age(item.age_seconds)
            project = item.cwd or "(unknown project)"
            session = item.session_id.replace(f"{item.runtime}:", "")
            ask = "yes" if item.asks_user else "no"
            lines.append(f"- {item.device_id} {item.runtime} {age} `{project}`")
            lines.append(f"  session `{session}`")
            lines.append(f"  ask: {ask} - {item.ask_summary}")
        text = "\n".join(lines)

    if errors:
        skipped = ", ".join(f"{error.device_id}: {error.error}" for error in errors[:4])
        text += f"\nSkipped: {skipped}"
    return text + "\n"


def _collect_runtime(
    *,
    device_id: str,
    runtime: str,
    root: Path,
    parser: Any,
    limit: int,
) -> list[SessionStatus]:
    if not root.exists():
        return []
    paths = sorted(
        (path for path in root.rglob("*.jsonl") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )[:limit]
    return [parser(path, device_id=device_id) for path in paths]


def _parse_codex_status(path: Path, *, device_id: str) -> SessionStatus:
    session_id = _session_id_from_path(path)
    cwd = ""
    model = ""
    for obj in _read_jsonl_head(path):
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta":
            session_id = str(payload.get("id") or session_id)
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or payload.get("model_provider") or model)
        elif obj.get("type") == "turn_context":
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or model)
    last_role, latest = "", ""
    for obj in _read_jsonl_tail(path):
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "turn_context":
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or model)
        if obj.get("type") != "response_item":
            continue
        if payload.get("type") != "message":
            continue
        role = str(payload.get("role") or "")
        text = _text_from_content(payload.get("content"))
        if role in {"user", "assistant"} and text:
            last_role = role
            latest = text
    return _status_from_latest(
        path=path,
        device_id=device_id,
        runtime="codex",
        session_id=f"codex:{session_id}",
        cwd=cwd,
        model=model,
        branch="",
        last_role=last_role,
        latest=latest,
    )


def _parse_claude_status(path: Path, *, device_id: str) -> SessionStatus:
    session_id = path.stem
    cwd = ""
    model = ""
    branch = ""
    last_role, latest = "", ""
    for obj in _read_jsonl_head(path):
        session_id = str(obj.get("sessionId") or session_id)
        cwd = str(obj.get("cwd") or cwd)
        branch = str(obj.get("gitBranch") or branch)
    for obj in _read_jsonl_tail(path):
        session_id = str(obj.get("sessionId") or session_id)
        cwd = str(obj.get("cwd") or cwd)
        branch = str(obj.get("gitBranch") or branch)
        role = str(obj.get("type") or "")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        model = str(msg.get("model") or model)
        text = _text_from_content(msg.get("content"))
        if role in {"user", "assistant"} and text:
            last_role = role
            latest = text
    return _status_from_latest(
        path=path,
        device_id=device_id,
        runtime="claude",
        session_id=f"claude:{session_id}",
        cwd=cwd,
        model=model,
        branch=branch,
        last_role=last_role,
        latest=latest,
    )


def _parse_kimi_status(path: Path, *, device_id: str) -> SessionStatus:
    cwd = ""
    latest = ""
    last_role = ""
    for obj in _read_jsonl_tail(path):
        cwd = str(obj.get("cwd") or obj.get("project") or cwd)
        role = str(obj.get("role") or obj.get("type") or "")
        text = _text_from_content(
            obj.get("message") or obj.get("content") or obj.get("text")
        )
        if text:
            last_role = role or "message"
            latest = text
    return _status_from_latest(
        path=path,
        device_id=device_id,
        runtime="kimi",
        session_id=f"kimi:{path.stem}",
        cwd=cwd,
        model="",
        branch="",
        last_role=last_role,
        latest=latest,
    )


def _status_from_latest(
    *,
    path: Path,
    device_id: str,
    runtime: str,
    session_id: str,
    cwd: str,
    model: str,
    branch: str,
    last_role: str,
    latest: str,
) -> SessionStatus:
    st = path.stat()
    asks_user, ask_summary = _infer_ask(last_role=last_role, text=latest)
    return SessionStatus(
        device_id=device_id,
        runtime=runtime,
        session_id=session_id,
        cwd=cwd,
        source_path=str(path),
        last_interaction_at=st.st_mtime,
        age_seconds=max(0, int(time.time() - st.st_mtime)),
        last_role=last_role,
        asks_user=asks_user,
        ask_summary=ask_summary,
        latest_summary=compact_text(_redact(latest), limit=180),
        model=model,
        branch=branch,
    )


def _infer_ask(*, last_role: str, text: str) -> tuple[bool, str]:
    if not text:
        return False, "no recent message text found"
    if last_role == "user":
        return False, "latest interaction is user input; agent has not asked back yet"
    ask_phrases = (
        "can you",
        "could you",
        "would you",
        "do you want",
        "want me to",
        "should i",
        "shall i",
        "please approve",
        "please confirm",
        "please choose",
        "please",
        "let me know",
        "tell me",
        "approve this",
        "approve the",
        "approval required",
        "needs approval",
        "requires approval",
        "waiting for approval",
        "pending permission",
        "permission required",
        "provide the",
        "send me",
        "enter the",
        "i need you",
        "need your",
        "requires your",
        "waiting for you",
        "waiting on you",
        "blocked until you",
    )
    for sentence in _sentences(text):
        if sentence.lstrip().startswith("#"):
            continue
        sentence_lower = sentence.lower()
        for phrase in ask_phrases:
            if phrase in sentence_lower:
                return True, compact_text(_redact(sentence), limit=160)
        if "totp" in sentence_lower and any(
            phrase in sentence_lower
            for phrase in ("provide", "send", "enter", "approve")
        ):
            return True, compact_text(_redact(sentence), limit=160)
        if "?" in sentence and any(
            phrase in sentence_lower
            for phrase in (
                "can you",
                "could you",
                "would you",
                "do you want",
                "want me",
                "should i",
            )
        ):
            return True, compact_text(_redact(sentence), limit=160)
    return False, "latest agent turn does not appear to request input"


def _sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?])\s+|\n+", normalized)
        if part.strip()
    ]


def _read_jsonl_head(path: Path, *, max_lines: int = 200) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    break
                obj = _parse_json_line(line)
                if obj:
                    rows.append(obj)
    except OSError:
        return []
    return rows


def _read_jsonl_tail(path: Path, *, max_bytes: int = 512_000) -> list[dict[str, Any]]:
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = []
    for line in raw.splitlines():
        obj = _parse_json_line(line)
        if obj:
            rows.append(obj)
    return rows


def _parse_json_line(line: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(line.strip())
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _text_from_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    return ""


def session_transcript_tail(
    path: Path, runtime: str, *, max_lines: int = 30, max_chars: int = 1600
) -> str:
    """Last ~max_lines of human-readable output from a session log (redacted). Used to
    put the model's recent output into notifications so you can read it without opening
    the terminal."""
    runtime = (runtime or "").lower()
    out_lines: list[str] = []
    for obj in _read_jsonl_tail(path):
        role, text = "", ""
        if runtime == "codex":
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "response_item" and payload.get("type") == "message":
                role = str(payload.get("role") or "")
                text = _text_from_content(payload.get("content"))
        elif runtime == "claude":
            role = str(obj.get("type") or "")
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            text = _text_from_content(msg.get("content"))
        else:  # kimi / generic
            role = str(obj.get("role") or obj.get("type") or "")
            text = _text_from_content(
                obj.get("message") or obj.get("content") or obj.get("text")
            )
        if role in {"user", "assistant"} and text.strip():
            label = "you" if role == "user" else "model"
            for line in text.strip().splitlines():
                out_lines.append(f"[{label}] {line}")
    tail = _redact("\n".join(out_lines[-max_lines:]))
    if len(tail) > max_chars:
        tail = "…\n" + tail[-max_chars:]
    return tail


def _session_id_from_path(path: Path) -> str:
    match = UUID_RE.search(path.stem)
    return match.group(1) if match else path.stem


def _redact(text: str) -> str:
    return TOKEN_RE.sub("<token>", CODE_RE.sub("<code>", text))


def _format_age(seconds: int) -> str:
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


REMOTE_STATUS_SCRIPT = r"""
from pathlib import Path
import json
import re
import sys
import time

device_id = sys.argv[1]
limit_per_runtime = int(sys.argv[2])
code_re = re.compile(r"\b(?:\d{6,8}|[A-Z0-9]{4}-[A-Z0-9]{4})\b")
token_re = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9_]{20,}|hf_[A-Za-z0-9]{20,}|sk[-_][A-Za-z0-9_-]{12,})\b")
uuid_re = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$")

def compact(value, limit=160):
    cleaned = " ".join(str(value).split())
    return cleaned if len(cleaned) <= limit else cleaned[:limit - 3].rstrip() + "..."

def redact(text):
    return token_re.sub("<token>", code_re.sub("<code>", text))

def parse_json(line):
    try:
        obj = json.loads(line.strip())
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None

def head(path, max_lines=200):
    rows = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= max_lines:
                    break
                obj = parse_json(line)
                if obj:
                    rows.append(obj)
    except OSError:
        return []
    return rows

def tail(path, max_bytes=512000):
    try:
        size = path.stat().st_size
        start = max(0, size - max_bytes)
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return [obj for obj in (parse_json(line) for line in raw.splitlines()) if obj]

def text_from(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    if isinstance(value, dict):
        text = value.get("text") or value.get("content")
        return text if isinstance(text, str) else ""
    return ""

def sentences(text):
    normalized = re.sub(r"\s+", " ", str(text)).strip()
    if not normalized:
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", normalized) if part.strip()]

def infer(last_role, text):
    if not text:
        return False, "no recent message text found"
    if last_role == "user":
        return False, "latest interaction is user input; agent has not asked back yet"
    phrases = ("can you", "could you", "would you", "do you want", "want me to", "should i", "shall i", "please approve", "please confirm", "please choose", "please", "let me know", "tell me", "approve this", "approve the", "approval required", "needs approval", "requires approval", "waiting for approval", "pending permission", "permission required", "provide the", "send me", "enter the", "i need you", "need your", "requires your", "waiting for you", "waiting on you", "blocked until you")
    for sentence in sentences(text):
        if sentence.lstrip().startswith("#"):
            continue
        sentence_lower = sentence.lower()
        for phrase in phrases:
            if phrase in sentence_lower:
                return True, compact(redact(sentence))
        if "totp" in sentence_lower and any(phrase in sentence_lower for phrase in ("provide", "send", "enter", "approve")):
            return True, compact(redact(sentence))
        if "?" in sentence and any(term in sentence_lower for term in ("can you", "could you", "would you", "do you want", "want me", "should i")):
            return True, compact(redact(sentence))
    return False, "latest agent turn does not appear to request input"

def session_id(path):
    match = uuid_re.search(path.stem)
    return match.group(1) if match else path.stem

def status(path, runtime, sid, cwd, model, branch, last_role, latest):
    st = path.stat()
    asks, summary = infer(last_role, latest)
    return {
        "device_id": device_id,
        "runtime": runtime,
        "session_id": f"{runtime}:{sid}",
        "cwd": cwd,
        "source_path": str(path),
        "last_interaction_at": st.st_mtime,
        "age_seconds": max(0, int(time.time() - st.st_mtime)),
        "last_role": last_role,
        "asks_user": asks,
        "ask_summary": summary,
        "latest_summary": compact(redact(latest), 180),
        "model": model,
        "branch": branch,
    }

def codex(path):
    sid = session_id(path)
    cwd = ""
    model = ""
    for obj in head(path):
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "session_meta":
            sid = str(payload.get("id") or sid)
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or payload.get("model_provider") or model)
        elif obj.get("type") == "turn_context":
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or model)
    last_role = ""
    latest = ""
    for obj in tail(path):
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        if obj.get("type") == "turn_context":
            cwd = str(payload.get("cwd") or cwd)
            model = str(payload.get("model") or model)
        if obj.get("type") == "response_item" and payload.get("type") == "message":
            role = str(payload.get("role") or "")
            text = text_from(payload.get("content"))
            if role in {"user", "assistant"} and text:
                last_role, latest = role, text
    return status(path, "codex", sid, cwd, model, "", last_role, latest)

def claude(path):
    sid = path.stem
    cwd = ""
    model = ""
    branch = ""
    for obj in head(path):
        sid = str(obj.get("sessionId") or sid)
        cwd = str(obj.get("cwd") or cwd)
        branch = str(obj.get("gitBranch") or branch)
    last_role = ""
    latest = ""
    for obj in tail(path):
        sid = str(obj.get("sessionId") or sid)
        cwd = str(obj.get("cwd") or cwd)
        branch = str(obj.get("gitBranch") or branch)
        role = str(obj.get("type") or "")
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        model = str(msg.get("model") or model)
        text = text_from(msg.get("content"))
        if role in {"user", "assistant"} and text:
            last_role, latest = role, text
    return status(path, "claude", sid, cwd, model, branch, last_role, latest)

def kimi(path):
    cwd = ""
    last_role = ""
    latest = ""
    for obj in tail(path):
        cwd = str(obj.get("cwd") or obj.get("project") or cwd)
        role = str(obj.get("role") or obj.get("type") or "")
        text = text_from(obj.get("message") or obj.get("content") or obj.get("text"))
        if text:
            last_role, latest = role or "message", text
    return status(path, "kimi", path.stem, cwd, "", "", last_role, latest)

roots = [
    ("codex", Path.home() / ".codex" / "sessions", codex),
    ("claude", Path.home() / ".claude" / "projects", claude),
    ("kimi", Path.home() / "devlog" / "raw" / "kimi", kimi),
]
rows = []
for runtime, root, parser in roots:
    if not root.exists():
        continue
    paths = sorted((path for path in root.rglob("*.jsonl") if path.is_file()), key=lambda path: path.stat().st_mtime, reverse=True)[:limit_per_runtime]
    for path in paths:
        try:
            rows.append(parser(path))
        except Exception:
            pass
for row in sorted(rows, key=lambda item: item["last_interaction_at"], reverse=True):
    print(json.dumps(row, sort_keys=True))
"""
