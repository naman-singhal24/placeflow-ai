"""Selective disruption impact analysis for PlaceFlow AI."""

from __future__ import annotations

import copy
from typing import Any


def build_before_after(record: dict[str, Any], after_routing: dict[str, Any]) -> dict[str, Any]:
    """Build a compact, stakeholder-safe route diff for one impacted candidate."""
    before = record.get("routing") or {}
    extracted = record.get("extracted") or {}
    return {
        "recordId": record.get("id"),
        "studentName": extracted.get("student_name") or "Student",
        "studentEmail": str(extracted.get("email") or record.get("ownerEmail") or "").lower(),
        "before": {
            "route": before.get("route"),
            "routeLabel": before.get("routeLabel"),
            "reason": before.get("verdictReason"),
        },
        "after": {
            "route": after_routing.get("route"),
            "routeLabel": after_routing.get("routeLabel"),
            "reason": after_routing.get("verdictReason"),
        },
    }


def route_changed(diff: dict[str, Any]) -> bool:
    """Return true when a disruption changed the candidate's placement outcome."""
    return diff["before"] != diff["after"]


def make_notification(event: dict[str, Any], diff: dict[str, Any], created_at: str) -> dict[str, Any]:
    """Create one targeted alert with an explicit before/after decision."""
    return {
        "id": f'{event["id"]}:{diff["recordId"]}',
        "eventId": event["id"],
        "type": "CAMPUS_DISRUPTION",
        "title": event["title"],
        "message": diff["after"].get("reason") or event["reason"],
        "eventReason": event["reason"],
        "createdAt": created_at,
        "recipient": diff["studentEmail"],
        "stakeholders": ["student", "college", "hr"],
        "recordId": diff["recordId"],
        "studentName": diff["studentName"],
        "before": copy.deepcopy(diff["before"]),
        "after": copy.deepcopy(diff["after"]),
        "read": False,
    }
