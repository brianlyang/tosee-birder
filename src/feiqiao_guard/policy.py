from __future__ import annotations

import re
from dataclasses import dataclass

from .models import RiskLevel


@dataclass(frozen=True)
class PolicyResult:
    allowed_for_remote_approval: bool
    risk_level: RiskLevel
    reason: str


_BLOCKED_PATTERNS = [
    re.compile(r"\brm\s+-rf\b", re.IGNORECASE),
    re.compile(r"\bcurl\b.*\|\s*(sh|bash)\b", re.IGNORECASE),
    re.compile(r"\bwget\b.*\|\s*(sh|bash)\b", re.IGNORECASE),
    re.compile(r"\bsudo\s+su\b", re.IGNORECASE),
]

_L1_PATTERNS = [
    re.compile(r"^\s*(ls|cat|grep|rg|find)\b", re.IGNORECASE),
]

_L2_PATTERNS = [
    re.compile(r"^\s*pkill\s+-f\s+[\w\-\./:]+", re.IGNORECASE),
    re.compile(r"^\s*pmset\s+[a-z0-9_\-]+\b", re.IGNORECASE),
    re.compile(r"^\s*chmod\s+[0-7]{3,4}\s+[/\w\-.]+", re.IGNORECASE),
]


def evaluate_command(command: str) -> PolicyResult:
    text = command.strip()
    if not text:
        return PolicyResult(False, RiskLevel.L3, "empty_command")

    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(text):
            return PolicyResult(False, RiskLevel.L3, "blocked_high_risk_command")

    for pattern in _L1_PATTERNS:
        if pattern.search(text):
            return PolicyResult(True, RiskLevel.L1, "allowlisted_low_risk_command")

    for pattern in _L2_PATTERNS:
        if pattern.search(text):
            return PolicyResult(True, RiskLevel.L2, "allowlisted_medium_risk_command")

    return PolicyResult(False, RiskLevel.L3, "not_in_allowlist")
