"""Secret-shape families for the README checks (NFR-1), as a test helper.

The README tests load this file by path and call ``scan_content``. It holds the same secret-shaped
regex families, with the same pattern sources and flags, as the shape scanner of the project this
repository was extracted from, so the README checks keep their strength here. CI also runs
gitleaks over the whole tree (``.github/workflows/ci.yml``).

A finding carries the family name and the line only, never the matched value. A line that holds
the exact marker ``pragma: allowlist secret`` is not reported (that line only).
"""

from __future__ import annotations

import re
from typing import NamedTuple

PRAGMA_MARKER = "pragma: allowlist secret"

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # Private key blocks (whole block): the only multiline pattern (re.S).
    (
        "private-key-block",
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    ),
    # AWS access key ids.
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA)[0-9A-Z]{16}\b")),
    # GitHub / GitLab tokens.
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("github-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{60,}\b")),
    ("gitlab-pat", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    # Slack tokens and webhooks.
    ("slack-token", re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b")),
    # Google API keys.
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    # Stripe / OpenAI-style secret keys.
    ("openai-secret-key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("stripe-live-key", re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b")),
    # JWTs.
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    # Authorization / bearer headers.
    (
        "authorization-header",
        re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic|token)\s+)\S+"),
    ),
    ("x-api-key-header", re.compile(r"(?i)(x-api-key\s*[:=]\s*)\S+")),
    # key=value / key: value where the key is secret-shaped.
    (
        "secret-assignment",
        re.compile(
            r"(?i)\b([\w.-]*(?:api[_-]?key|secret|token|passwd|password|pwd|access[_-]?key"
            r"|private[_-]?key)[\w.-]*\s*[:=]\s*)"
            r"[\"']?[A-Za-z0-9_\-./+=]{8,}[\"']?"
        ),
    ),
]

_MULTILINE = [(name, pattern) for name, pattern in PATTERNS if pattern.flags & re.DOTALL]
_SINGLE_LINE = [(name, pattern) for name, pattern in PATTERNS if not pattern.flags & re.DOTALL]


class Finding(NamedTuple):
    """One secret-shaped match: its family and location, never its value."""

    type: str
    path: str
    line: int


def scan_content(text: str, path: str, base_line: int = 1) -> list[Finding]:
    """Every secret-shaped match of ``text``; ``path`` is a label for the report only."""
    findings: list[Finding] = []
    lines = text.split("\n")
    for offset, line in enumerate(lines):
        for name, pattern in _SINGLE_LINE:
            if pattern.search(line):
                findings.append(Finding(name, path, base_line + offset))
    for name, pattern in _MULTILINE:
        for match in pattern.finditer(text):
            findings.append(Finding(name, path, base_line + text.count("\n", 0, match.start())))
    return [f for f in findings if PRAGMA_MARKER not in lines[f.line - base_line]]
