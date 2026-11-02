from __future__ import annotations

import re
import os
from collections.abc import Iterable


def private_reason(value: str, secrets: Iterable[str] = (), *, max_length: int = 512) -> str:
    """Bounded admin reason, never request content. Redact known credentials first."""
    value = value.strip()
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        value = value.replace(secret, "[redacted]")
    patterns = (
        r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)",
        r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~+/-]+=*",
        r"(?i)\b(?:api[_-]?key|password|token|secret|authorization)\s*[:=]\s*\S+",
        r"(?i)\b[A-Z_][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|ACCESS_CODE|ACTIVATION_CODE)[A-Z0-9_]*\s*[:=]\s*\S+",
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16})\b",
        r"\b\d{6,}:[A-Za-z0-9_-]{25,}\b",
    )
    for pattern in patterns:
        value = re.sub(pattern, "[redacted]", value)
    return value[:max_length]


def known_secrets(*values: str) -> tuple[str, ...]:
    """Redaction only. Never log, serialize or expose this collection."""
    environment_values = (value for name, value in os.environ.items()
                          if re.search(r"TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY|ACCESS_KEY|ACCESS_CODE|ACTIVATION_CODE", name, re.I))
    return tuple(set(value for value in (*values, *environment_values) if value))
