"""Small shared helpers: ids, time, JSON, secret redaction."""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_key(ts: str | None = None) -> str:
    """UTC month bucket 'YYYY-MM' for a timestamp (default: now)."""
    return (ts or now_iso())[:7]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def round_money(x: float) -> float:
    return round(float(x), 6)


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)([^\s\",}]{6,})"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
]


def redact(text: str) -> str:
    """Remove likely secrets from text before it is logged or stored in audit trails."""
    out = text
    out = _SECRET_PATTERNS[0].sub("[REDACTED]", out)
    out = _SECRET_PATTERNS[1].sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", out)
    out = _SECRET_PATTERNS[2].sub("Bearer [REDACTED]", out)
    return out


def tokenize(text: str) -> set[str]:
    """Lower-case word tokens (len>=3) used for duplicate detection."""
    return {t for t in re.findall(r"[a-z0-9áéíóúñü]+", text.lower()) if len(t) >= 3}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)
