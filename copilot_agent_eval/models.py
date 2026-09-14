"""Data models for test cases and results."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class TestCase:
    case_id: str
    query: str
    expected_keywords: list[str] = field(default_factory=list)
    session_group: str = "default"
    priority: str = "P2"
    max_latency_ms: Optional[int] = None
    notes: str = ""


def load_cases(path: str | Path) -> list[TestCase]:
    """Load test cases from a CSV.

    Expected columns: case_id, query, expected_keywords, session_group,
    priority, max_latency_ms, notes.
    expected_keywords uses ';' for AND groups and '|' for OR within a group,
    e.g. "create|new;order" means (create OR new) AND (order).
    """
    cases: list[TestCase] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("case_id") or not row.get("query"):
                continue
            kw_raw = (row.get("expected_keywords") or "").strip()
            keywords = [k.strip() for k in kw_raw.split(";") if k.strip()]
            latency_raw = (row.get("max_latency_ms") or "").strip()
            cases.append(
                TestCase(
                    case_id=row["case_id"].strip(),
                    query=row["query"].strip(),
                    expected_keywords=keywords,
                    session_group=(row.get("session_group") or "default").strip() or "default",
                    priority=(row.get("priority") or "P2").strip() or "P2",
                    max_latency_ms=int(latency_raw) if latency_raw.isdigit() else None,
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return cases


@dataclass
class TestResult:
    case_id: str
    query: str
    status: str = "PENDING"  # PASS | FAIL | TIMEOUT | ERROR
    response_text: str = ""
    first_token_latency_ms: Optional[int] = None
    total_latency_ms: Optional[int] = None
    keyword_hits: list[str] = field(default_factory=list)
    keyword_misses: list[str] = field(default_factory=list)
    error_phrases_hit: list[str] = field(default_factory=list)
    error: str = ""
    timestamp: str = ""
    raw_activity_file: Optional[str] = None
    conversation_id: str = ""
    steps: list = field(default_factory=list)  # list[telemetry.Step]

    @staticmethod
    def now_stamp() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
