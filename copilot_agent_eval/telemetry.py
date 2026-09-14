"""Telemetry / step extraction from Direct Line activities.

Beyond the final answer, a Copilot Studio agent emits intermediate activities
while it works: typing indicators, trace activities (knowledge retrieval,
plugin/MCP calls, Dataverse queries, SQL generation), event activities, and
intermediate adaptive-card messages. This module turns every activity into a
flat, human-readable Step record for timing and debugging.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Step:
    """One intermediate step observed during a turn."""
    offset_ms: int               # ms since the query was sent
    activity_type: str           # typing | trace | event | message | invokeResponse | ...
    activity_name: str = ""      # trace/event/invoke name, e.g. "PluginAction"
    category: str = ""           # our high-level classification
    summary: str = ""            # human-readable one-liner
    detail: str = ""             # longer detail / JSON snippet
    activity_id: str = ""

    def as_row(self) -> list[Any]:
        return [
            self.offset_ms,
            self.activity_type,
            self.activity_name,
            self.category,
            self.summary,
            self.detail,
            self.activity_id,
        ]


STEP_HEADERS = [
    "Offset (ms)", "Type", "Name", "Category", "Summary", "Detail", "Activity ID",
]


# --------------------------------------------------------------------- classify
# Copilot Studio / Bot Framework trace names we know about. The trace `value`
# payload varies; we extract what we can and always keep a JSON snippet.

_TRACE_CATEGORIES: dict[str, str] = {
    "GenerativeAnswer": "answer_generation",
    "GenerativeAnswers": "answer_generation",
    "Knowledge": "knowledge_query",
    "KnowledgeArticle": "knowledge_query",
    "DS": "dataverse_query",
    "DataverseSearch": "dataverse_query",
    "PluginAction": "mcp_action",
    "Action": "mcp_action",
    "ConnectorCall": "mcp_action",
    "Agent": "agent_dispatch",
    "Topic": "topic_match",
    "Intent": "intent_recognition",
    "PVA": "copilot_studio",
    "BotMessageReceived": "pipeline",
    "BotMessageSend": "pipeline",
    "Orchestrator": "orchestration",
    "Core": "pipeline",
}

# Keywords inside a trace value / message text that reveal what the agent is
# doing, mapped to a category + summary.
_CONTENT_HINTS: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"\bknowledge\b|\bgrounding\b|\bwiki\b|\barticle\b", re.I),
     "knowledge_query", "Knowledge base lookup"),
    (re.compile(r"\bmcp\b|\bplugin\b|\bconnector\b|\baction\b|\btool\b", re.I),
     "mcp_action", "MCP / plugin action"),
    (re.compile(r"\bsql\b|\bselect\b|\bfetchxml\b|\bquery\b", re.I),
     "sql_query", "SQL / FetchXML query"),
    (re.compile(r"\bdataverse\b|\bentity\b|\btable\b", re.I),
     "dataverse_query", "Dataverse entity query"),
    (re.compile(r"\bplan\b|\bplanning\b|\bthought\b|\breasoning\b", re.I),
     "planning", "Planning / reasoning"),
    (re.compile(r"\bsearch(ing)?\b|\bretriev", re.I),
     "retrieval", "Retrieving data"),
    (re.compile(r"\bgenerat", re.I),
     "answer_generation", "Generating answer"),
    (re.compile(r"\bhttp\b|\brequest\b|\bapi\b|\bendpoint\b", re.I),
     "http_call", "External HTTP/API call"),
]


def _truncate(text: str, limit: int = 500) -> str:
    text = (text or "").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _value_to_text(value: Any) -> str:
    """Best-effort text extraction from a trace/event value payload."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=None, default=str)
    except Exception:
        return str(value)


def _classify_by_text(text: str) -> tuple[str, str]:
    """Scan text for content hints; return (category, summary) or ('','')."""
    for pattern, category, summary in _CONTENT_HINTS:
        if pattern.search(text):
            return category, summary
    return "", ""


def _extract_card_text(attachments: list[dict]) -> str:
    """Pull visible text out of adaptive-card attachments."""
    chunks: list[str] = []
    for att in attachments or []:
        ct = att.get("contentType", "")
        content = att.get("content", {}) or {}
        if "adaptive" in ct:
            for item in content.get("body", []) or []:
                t = item.get("text") or item.get("title") or ""
                if t:
                    chunks.append(str(t))
                # ColumnSet / nested items
                for col in item.get("columns", []) or []:
                    for ci in col.get("items", []) or []:
                        t = ci.get("text") or ""
                        if t:
                            chunks.append(str(t))
        elif "hero" in ct or "thumbnail" in ct:
            t = content.get("title") or content.get("subtitle") or content.get("text")
            if t:
                chunks.append(str(t))
    return " | ".join(chunks)


def activity_to_step(act: dict, start_time: float) -> Optional[Step]:
    """Convert one raw activity into a Step, or None if it should be ignored."""
    atype = act.get("type", "")
    if atype == "typing":
        # Typing may carry a progress card; record it as a heartbeat.
        atts = act.get("attachments") or []
        card_text = _extract_card_text(atts)
        return Step(
            offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
            activity_type="typing",
            category="heartbeat",
            summary=card_text or "typing…",
            detail="",
            activity_id=act.get("id", ""),
        )

    if atype == "trace":
        name = act.get("name", "") or ""
        value = act.get("value")
        label = act.get("label", "") or ""
        value_text = _value_to_text(value)

        category = _TRACE_CATEGORIES.get(name, "")
        summary = label or name.replace("_", " ").title()
        if not category:
            category, hint_summary = _classify_by_text(
                f"{name} {label} {value_text[:300]}"
            )
            if hint_summary and not label:
                summary = hint_summary

        # Try to pull a more specific summary from common trace shapes.
        if isinstance(value, dict):
            for key in ("query", "sql", "statement", "searchText", "text",
                        "knowledgeBaseName", "pluginName", "actionName",
                        "connectionName", "entityName", "tableName",
                        "intent", "topicName"):
                v = value.get(key)
                if v and isinstance(v, (str, int, float)):
                    summary = f"{summary}: {_truncate(str(v), 120)}"
                    break

        return Step(
            offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
            activity_type="trace",
            activity_name=name,
            category=category or "trace",
            summary=_truncate(summary, 200),
            detail=_truncate(value_text, 1000),
            activity_id=act.get("id", ""),
        )

    if atype == "event":
        name = act.get("name", "") or ""
        value_text = _value_to_text(act.get("value"))
        category, hint = _classify_by_text(f"{name} {value_text[:300]}")
        return Step(
            offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
            activity_type="event",
            activity_name=name,
            category=category or "event",
            summary=_truncate(hint or name, 200),
            detail=_truncate(value_text, 500),
            activity_id=act.get("id", ""),
        )

    if atype == "invokeResponse":
        value = act.get("value") or {}
        status = value.get("status", "")
        return Step(
            offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
            activity_type="invokeResponse",
            category="invoke_response",
            summary=f"invoke response status={status}",
            detail=_truncate(_value_to_text(value.get("body")), 300),
            activity_id=act.get("id", ""),
        )

    if atype == "message":
        atts = act.get("attachments") or []
        has_oauth = any(
            "oauth" in (a.get("contentType", "") or "").lower() for a in atts
        )
        if has_oauth:
            return Step(
                offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
                activity_type="message",
                category="oauth_card",
                summary="OAuth sign-in card",
                activity_id=act.get("id", ""),
            )
        # Copilot Studio connection-manager card (MCP/connector auth).
        card_text = _extract_card_text(atts)
        if "user-connections" in card_text or "connection manager" in card_text.lower():
            return Step(
                offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
                activity_type="message",
                category="connection_card",
                summary="Connection manager auth card (MCP/connector)",
                detail=_truncate(card_text, 500),
                activity_id=act.get("id", ""),
            )
        # Intermediate message (could be a progress card or the final answer).
        text = (act.get("text") or "").strip()
        if not text and not card_text:
            return None
        # A long message with "Results"/"Next steps"/tables is the final answer.
        is_answer = len(text) > 200 or any(
            marker in text for marker in ("**Results**", "**Next steps**", "Results\n")
        )
        if is_answer:
            category = "answer"
            summary = "Final answer (" + str(len(text)) + " chars)"
        else:
            category, hint = _classify_by_text(f"{text} {card_text[:300]}")
            category = category or "message"
            summary = _truncate(card_text or text.split("\n")[0], 200)
        return Step(
            offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
            activity_type="message",
            category=category,
            summary=summary,
            detail=_truncate(text, 2000) if is_answer else "",
            activity_id=act.get("id", ""),
        )

    # Any other activity type — record it raw.
    return Step(
        offset_ms=int((act.get("_received_at", start_time) - start_time) * 1000),
        activity_type=atype or "unknown",
        category="other",
        summary=_truncate(str(act.get("text") or act.get("name") or ""), 200),
        detail="",
        activity_id=act.get("id", ""),
    )
