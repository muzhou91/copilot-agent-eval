"""Assertion evaluation for a bot turn."""
from __future__ import annotations

from dataclasses import dataclass

from .config import AssertionConfig
from .models import TestCase, TestResult


@dataclass
class AssertionOutcome:
    passed: bool
    hits: list[str]
    misses: list[str]
    error_phrases: list[str]
    latency_ok: bool
    reasons: list[str]


def _keyword_matches(keyword_group: str, text: str) -> bool:
    """A keyword group supports '|' for OR. Substring match, case-insensitive."""
    options = [o.strip() for o in keyword_group.split("|") if o.strip()]
    if not options:
        return True
    haystack = text.lower()
    return any(o.lower() in haystack for o in options)


def evaluate(
    case: TestCase,
    result: TestResult,
    config: AssertionConfig,
) -> AssertionOutcome:
    reasons: list[str] = []
    text = result.response_text or ""

    hits: list[str] = []
    misses: list[str] = []
    for kw in case.expected_keywords:
        if _keyword_matches(kw, text):
            hits.append(kw)
        else:
            misses.append(kw)
    if misses:
        reasons.append(f"missing keywords: {', '.join(misses)}")

    error_phrases: list[str] = []
    for phrase in config.fail_on_error_phrases:
        if phrase and phrase.lower() in text.lower():
            error_phrases.append(phrase)
    if error_phrases:
        reasons.append(f"error phrase(s) found: {', '.join(error_phrases)}")

    latency_limit = case.max_latency_ms or config.default_max_latency_ms
    latency_ok = True
    if result.total_latency_ms is not None and result.total_latency_ms > latency_limit:
        latency_ok = False
        reasons.append(
            f"latency {result.total_latency_ms}ms exceeds limit {latency_limit}ms"
        )

    if not text.strip():
        reasons.append("empty response")

    passed = not misses and not error_phrases and latency_ok and bool(text.strip())
    return AssertionOutcome(passed, hits, misses, error_phrases, latency_ok, reasons)
