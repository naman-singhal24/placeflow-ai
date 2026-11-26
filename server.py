"""Local zero-dependency HTTP server with 3-portal RBAC (College, HR, Student)."""

import base64
import copy
import json
import os
import secrets
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from erp_db import COLLEGE_ERP_DB, compute_credibility_score, verify_against_erp
from disruption_engine import build_before_after, make_notification, route_changed
from router_engine import (
    DEFAULT_JDS,
    GEMINI_MODELS,
    build_jd_prep_plan,
    call_gemini_audit,
    evaluate_deterministic_rules,
    generate_skill_test,
    grade_skill_test,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
SAMPLE_DIR = BASE_DIR / "sample_resumes"
ENV_PATH = BASE_DIR / ".env"
AUDIT_PATH = BASE_DIR / "appeal_audit.json"

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))

COLLEGE_EMAIL = os.environ.get("PLACEFLOW_COLLEGE_EMAIL", "admin@rit.edu").lower()
COLLEGE_PASSCODE = os.environ.get("PLACEFLOW_COLLEGE_PASSCODE", "RIT-COLLEGE-2026")
HR_PASSCODE = os.environ.get("PLACEFLOW_HR_PASSCODE", "RIT-HR-2026")
STUDENT_PASSCODE = os.environ.get("PLACEFLOW_STUDENT_PASSCODE", "RIT-STUDENT-2026")

_LOCK = threading.Lock()
SESSIONS: dict[str, dict[str, str]] = {}
RESULTS_STORE: list[dict[str, Any]] = []
JDS_STORE: list[dict[str, Any]] = copy.deepcopy(DEFAULT_JDS)
ACTIVE_JD_SNAPSHOT: dict[str, Any] = JDS_STORE[0]
TESTS_STORE: dict[str, dict[str, Any]] = {}
DISRUPTION_EVENTS: list[dict[str, Any]] = []
NOTIFICATIONS_STORE: list[dict[str, Any]] = []
APPEALS_STORE: list[dict[str, Any]] = []
AUDIT_LOG: list[dict[str, Any]] = []


def _load_audit_state() -> None:
    """Restore persisted appeals and audit events; malformed files fail safely."""
    if not AUDIT_PATH.exists():
        return
    saved = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    if not isinstance(saved, dict):
        raise ValueError("Appeal audit store must contain a JSON object.")
    appeals = saved.get("appeals", [])
    events = saved.get("auditLog", [])
    if not isinstance(appeals, list) or not isinstance(events, list):
        raise ValueError("Appeal audit store has an invalid schema.")
    APPEALS_STORE.clear()
    AUDIT_LOG.clear()
    APPEALS_STORE.extend(item for item in appeals if isinstance(item, dict))
    AUDIT_LOG.extend(item for item in events if isinstance(item, dict))


def _persist_audit_state() -> None:
    """Atomically persist appeal state and append-only audit entries to local JSON."""
    payload = {"appeals": APPEALS_STORE, "auditLog": AUDIT_LOG}
    temp_path = AUDIT_PATH.with_suffix(".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(AUDIT_PATH)


try:
    _load_audit_state()
except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
    print(f"WARNING: Could not restore appeal audit store: {exc}")


def load_api_key() -> str:
    """Read GEMINI_API_KEY from environment or local .env file."""
    env_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if env_key:
        return env_key
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def save_api_key(key: str) -> None:
    """Persist GEMINI_API_KEY into local .env file and process env."""
    clean = key.strip()
    os.environ["GEMINI_API_KEY"] = clean
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = [
            ln
            for ln in ENV_PATH.read_text(encoding="utf-8").splitlines()
            if not ln.strip().startswith("GEMINI_API_KEY=")
        ]
    lines.append(f"GEMINI_API_KEY={clean}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def mask_key(key: str) -> str:
    if len(key) <= 10:
        return "***" if key else ""
    return f"{key[:6]}...{key[-4:]}"


def _build_college_analytics() -> dict[str, Any]:
    """Compute branchwise shortlist counts, acceptance rates, and lacking skills."""
    with _LOCK:
        ok_items = [r for r in RESULTS_STORE if r.get("status") == "ok"]
        active_jd = dict(ACTIVE_JD_SNAPSHOT)

    branches: dict[str, dict[str, Any]] = {}
    skill_gaps: Counter[str] = Counter()

    for r in ok_items:
        rt = r.get("routing", {})
        ex = r.get("extracted", {})
        br = str(rt.get("normalizedBranch") or "UNKNOWN")
        bucket = branches.setdefault(
            br,
            {
                "branch": br,
                "total": 0,
                "shortlisted": 0,
                "invited": 0,
                "onHold": 0,
                "declined": 0,
                "exception": 0,
                "upskill": 0,
                "rerouted": 0,
                "acceptanceRatePct": 0,
            },
        )
        bucket["total"] += 1
        route = rt.get("route")
        hr_decision = str((r.get("hrAction") or {}).get("decision") or "").upper()
        if hr_decision == "INTERVIEW_INVITED":
            bucket["invited"] += 1
        elif hr_decision == "ON_HOLD":
            bucket["onHold"] += 1
        elif hr_decision == "DECLINED":
            bucket["declined"] += 1

        if route == "SHORTLISTED" or hr_decision == "INTERVIEW_INVITED":
            bucket["shortlisted"] += 1
        elif route == "PORTFOLIO_EXCEPTION":
            bucket["exception"] += 1
        elif route == "UPSKILL_TRACK":
            bucket["upskill"] += 1
        else:
            bucket["rerouted"] += 1

        for sk in rt.get("missingMustHave", []) or []:
            skill_gaps[str(sk)] += 1
        for cl in ex.get("unverified_claims", []) or []:
            if cl.get("skill"):
                skill_gaps[f"{cl['skill']} (Unverified)"] += 1

    branch_list = []
    for br_info in branches.values():
        tot = max(br_info["total"], 1)
        br_info["acceptanceRatePct"] = round((br_info["shortlisted"] / tot) * 100)
        branch_list.append(br_info)
    branch_list.sort(key=lambda b: (-b["acceptanceRatePct"], -b["invited"], b["branch"]))

    top_lacking = [{"skill": k, "count": v} for k, v in skill_gaps.most_common(8)]
    erp_dir = [{"email": email, **info} for email, info in sorted(COLLEGE_ERP_DB.items())]

    total_ok = len(ok_items)
    total_invited = sum(
        1 for r in ok_items if (r.get("hrAction") or {}).get("decision") == "INTERVIEW_INVITED"
    )
    total_shortlisted = sum(
        1
        for r in ok_items
        if r.get("routing", {}).get("route") == "SHORTLISTED"
        or (r.get("hrAction") or {}).get("decision") == "INTERVIEW_INVITED"
    )
    overall_rate = round((total_shortlisted / total_ok) * 100) if total_ok else 0

    return {
        "totalEvaluated": total_ok,
        "totalShortlisted": total_shortlisted,
        "totalInvited": total_invited,
        "overallAcceptanceRatePct": overall_rate,
        "branchStats": branch_list,
        "topLackingSkills": top_lacking,
        "erpDirectory": erp_dir,
        "activeJd": active_jd,
    }


def _record_belongs_to_student(record: dict[str, Any], student_email: str) -> bool:
    """Return True when an evaluated record belongs to the signed-in student."""
    key = student_email.strip().lower()
    extracted_email = str(record.get("extracted", {}).get("email", "")).strip().lower()
    owner_email = str(record.get("ownerEmail", "")).strip().lower()
    return extracted_email == key or owner_email == key


def _find_student_profile(email: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return latest evaluated record for student plus fallback academic profile from ERP."""
    key = email.strip().lower()
    with _LOCK:
        own_records = [
            r
            for r in RESULTS_STORE
            if r.get("status") == "ok" and _record_belongs_to_student(r, key)
        ]
        latest = own_records[-1] if own_records else None
        active_jd = dict(ACTIVE_JD_SNAPSHOT)

    if latest:
        return latest, active_jd

    erp = COLLEGE_ERP_DB.get(key)
    if erp:
        fallback_extracted = {
            "student_name": erp["name"],
            "email": key,
            "branch": erp["branch"],
            "cgpa": erp["cgpa"],
            "active_backlogs": erp["active_backlogs"],
            "graduation_year": erp["graduation_year"],
            "verified_skills": [],
            "unverified_claims": [],
        }
        return {"extracted": fallback_extracted, "id": None}, active_jd

    return (
        {
            "extracted": {
                "student_name": key.split("@", 1)[0].title(),
                "email": key,
                "branch": "CSE",
                "cgpa": 7.5,
                "active_backlogs": 0,
                "verified_skills": [],
                "unverified_claims": [],
            },
            "id": None,
        },
        active_jd,
    )


def _audit_transition(entity_type: str, entity_id: str, action: str, actor: str, from_status: str = "", to_status: str = "", details: str = "") -> dict[str, Any]:
    entry = {
        "id": f"audit_{secrets.token_hex(5)}",
        "entityType": entity_type, "entityId": entity_id, "action": action,
        "actor": actor, "fromStatus": from_status, "toStatus": to_status,
        "details": details[:500], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    AUDIT_LOG.append(entry)
    _persist_audit_state()
    return entry


def _decision_explanation(record: dict[str, Any]) -> dict[str, Any]:
    extracted = record.get("extracted", {})
    routing = record.get("routing", {})
    jd = next((item for item in JDS_STORE if item.get("id") == record.get("jdId")), ACTIVE_JD_SNAPSHOT)
    cgpa = float(extracted.get("cgpa") or 0)
    branch = str(extracted.get("branch") or "").upper()
    backlogs = int(extracted.get("active_backlogs") or 0)
    matched = list(routing.get("matchedMustHave") or [])
    missing = list(routing.get("missingMustHave") or [])
    return {
        "outcome": routing.get("routeLabel") or routing.get("route") or "Evaluated",
        "reason": routing.get("verdictReason") or routing.get("reason") or "Decision generated by the placement rules engine.",
        "rules": [
            {"name": "Minimum CGPA", "actual": cgpa, "constraint": f'>= {jd.get("minCgpa", 0)}', "passed": cgpa >= float(jd.get("minCgpa") or 0)},
            {"name": "Allowed branch", "actual": branch or "Unknown", "constraint": ", ".join(jd.get("allowedBranches") or []), "passed": branch in [str(x).upper() for x in jd.get("allowedBranches", [])]},
            {"name": "Active backlogs", "actual": backlogs, "constraint": f'<= {jd.get("maxBacklogs", 0)}', "passed": backlogs <= int(jd.get("maxBacklogs") or 0)},
        ],
        "matchedSkills": matched, "missingSkills": missing,
        "skillFitPct": routing.get("matchPct", 0),
        "credibilityScore": (routing.get("credibility") or {}).get("score"),
        "jdId": record.get("jdId"), "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


class PlaceFlowHandler(BaseHTTPRequestHandler):
    """Serves PlaceFlow AI with strict College, HR, and Student role isolation."""

    server_version = "PlaceFlowAI/3.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, data: Any) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 25 * 1024 * 1024:
            raise ValueError("Payload exceeds 25MB limit.")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        return json.loads(raw.decode("utf-8"))

    def _get_session(self) -> dict[str, str] | None:
        token = self.headers.get("X-Session-Token", "").strip()
        if not token:
            auth = self.headers.get("Authorization", "").strip()
            if auth.lower().startswith("bearer "):
                token = auth[7:].strip()
        if not token:
            return None
        with _LOCK:
            return SESSIONS.get(token)

    def _require_role(self, *allowed_roles: str) -> dict[str, str] | None:
        session = self._get_session()
        if not session:
            self._send_json(401, {"error": "Authentication required. Please sign in."})
            return None
        if session["role"] not in allowed_roles:
            self._send_json(
                403,
                {
                    "error": (
                        f"Access denied: restricted to {', '.join(allowed_roles)} portal "
                        f"(signed in as {session['role']})."
                    )
                },
            )
            return None
        return session

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        qs = parse_qs(parsed_url.query)

        if path == "/" or path == "/index.html":
            html_file = STATIC_DIR / "index.html"
            content = html_file.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        if path == "/api/health":
            key = load_api_key()
            session = self._get_session()
            self._send_json(
                200,
                {
                    "ok": True,
                    "hasApiKey": bool(key),
                    "maskedKey": mask_key(key),
                    "models": GEMINI_MODELS,
                    "user": session,
                },
            )
            return

        if path == "/api/jds":
            with _LOCK:
                jds_copy = copy.deepcopy(JDS_STORE)
            self._send_json(200, {"jds": jds_copy})
            return

        if path == "/api/demo-resumes":
            if not self._require_role("college"):
                return
            files = []
            for pdf in sorted(SAMPLE_DIR.glob("*.pdf")):
                b64 = base64.b64encode(pdf.read_bytes()).decode("ascii")
                files.append(
                    {
                        "fileName": pdf.name,
                        "sizeBytes": pdf.stat().st_size,
                        "pdfBase64": b64,
                    }
                )
            self._send_json(200, {"files": files})
            return

        if path == "/api/appeals":
            session = self._require_role("college", "student", "hr")
            if not session:
                return
            with _LOCK:
                if session["role"] == "college":
                    appeals = copy.deepcopy(APPEALS_STORE)
                elif session["role"] == "student":
                    appeals = copy.deepcopy([a for a in APPEALS_STORE if a.get("studentEmail") == session["email"].lower()])
                else:
                    appeals = copy.deepcopy([a for a in APPEALS_STORE if a.get("stakeholders", {}).get("hr")])
            self._send_json(200, {"ok": True, "appeals": appeals})
            return

        if path == "/api/audit":
            session = self._require_role("college", "student")
            if not session:
                return
            with _LOCK:
                if session["role"] == "college":
                    entries = AUDIT_LOG
                else:
                    student_email = session["email"].lower()
                    owned_record_ids = {
                        str(record.get("id"))
                        for record in RESULTS_STORE
                        if _record_belongs_to_student(record, student_email)
                    }
                    owned_appeal_ids = {
                        str(appeal.get("id"))
                        for appeal in APPEALS_STORE
                        if appeal.get("studentEmail") == student_email
                    }
                    entries = [
                        entry for entry in AUDIT_LOG
                        if (
                            entry.get("entityType") == "record"
                            and str(entry.get("entityId")) in owned_record_ids
                        ) or (
                            entry.get("entityType") == "appeal"
                            and str(entry.get("entityId")) in owned_appeal_ids
                        )
                    ]
                self._send_json(200, {"ok": True, "entries": copy.deepcopy(entries[-200:])})
            return

        if path == "/api/college/disruptions":
            if not self._require_role("college"):
                return
            with _LOCK:
                self._send_json(200, {"ok": True, "events": copy.deepcopy(DISRUPTION_EVENTS)})
            return

        if path == "/api/notifications":
            session = self._require_role("college", "hr", "student")
            if not session:
                return
            with _LOCK:
                if session["role"] == "college":
                    items = copy.deepcopy(NOTIFICATIONS_STORE)
                elif session["role"] == "student":
                    items = copy.deepcopy([n for n in NOTIFICATIONS_STORE if n.get("recipient") == session["email"].lower()])
                else:
                    items = copy.deepcopy([n for n in NOTIFICATIONS_STORE if "hr" in n.get("stakeholders", [])])
            self._send_json(200, {"ok": True, "notifications": items})
            return

        if path == "/api/college/results":
            if not self._require_role("college"):
                return
            with _LOCK:
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "results": copy.deepcopy(RESULTS_STORE),
                        "activeJd": dict(ACTIVE_JD_SNAPSHOT),
                    },
                )
            return

        if path == "/api/college/analytics":
            if not self._require_role("college"):
                return
            self._send_json(200, {"ok": True, **_build_college_analytics()})
            return

        if path == "/api/hr/candidates":
            if not self._require_role("hr"):
                return
            branch_filter = (qs.get("branch", [""])[0] or "").strip().upper()
            include_all = qs.get("includeAll", ["0"])[0] == "1"
            erp_only = qs.get("erpOnly", ["0"])[0] == "1"
            min_cred = int(qs.get("minCred", ["0"])[0] or "0")
            with _LOCK:
                items = copy.deepcopy(RESULTS_STORE)
                active_jd = dict(ACTIVE_JD_SNAPSHOT)

            candidates = []
            for r in items:
                if r.get("status") != "ok":
                    continue
                route = r.get("routing", {}).get("route")
                if not include_all and route not in ("SHORTLISTED", "PORTFOLIO_EXCEPTION"):
                    continue
                br = str(r.get("routing", {}).get("normalizedBranch") or "").upper()
                if branch_filter and branch_filter != "ALL" and br != branch_filter:
                    continue
                erp_ok = bool(r.get("routing", {}).get("erpVerification", {}).get("verified"))
                if erp_only and not erp_ok:
                    continue
                cred_score = int(r.get("routing", {}).get("credibility", {}).get("score", 0))
                if cred_score < min_cred:
                    continue
                candidates.append(r)

            candidates.sort(
                key=lambda x: x.get("routing", {}).get("credibility", {}).get("score", 0),
                reverse=True,
            )
            invited_count = sum(
                1 for c in candidates if (c.get("hrAction") or {}).get("decision") == "INTERVIEW_INVITED"
            )
            erp_verified_count = sum(
                1 for c in candidates if c.get("routing", {}).get("erpVerification", {}).get("verified")
            )
            avg_cred = (
                round(
                    sum(c.get("routing", {}).get("credibility", {}).get("score", 0) for c in candidates)
                    / len(candidates)
                )
                if candidates
                else 0
            )
            self._send_json(
                200,
                {
                    "ok": True,
                    "activeJd": active_jd,
                    "candidates": candidates,
                    "hrSummary": {
                        "totalCandidates": len(candidates),
                        "interviewInvitedCount": invited_count,
                        "erpVerifiedCount": erp_verified_count,
                        "avgCredibility": avg_cred,
                    },
                },
            )
            return

        if path == "/api/student/sample-pdf":
            session = self._require_role("student")
            if not session:
                return
            email_prefix = session["email"].split("@", 1)[0].split(".")[0].lower()
            matched = next(
                (p for p in sorted(SAMPLE_DIR.glob("*.pdf")) if email_prefix in p.name.lower()),
                None,
            )
            if not matched:
                self._send_json(404, {"error": "No sample PDF bundled for this student email."})
                return
            b64 = base64.b64encode(matched.read_bytes()).decode("ascii")
            self._send_json(
                200,
                {"ok": True, "fileName": matched.name, "sizeBytes": matched.stat().st_size, "pdfBase64": b64},
            )
            return

        if path == "/api/student/me":
            session = self._require_role("student")
            if not session:
                return
            student_email = session["email"].lower()
            with _LOCK:
                own_records = [
                    copy.deepcopy(r)
                    for r in RESULTS_STORE
                    if r.get("status") == "ok"
                    and (
                        str(r.get("extracted", {}).get("email", "")).strip().lower() == student_email
                        or str(r.get("ownerEmail", "")).strip().lower() == student_email
                    )
                ]
                jds_copy = copy.deepcopy(JDS_STORE)
                active_jd = dict(ACTIVE_JD_SNAPSHOT)

            erp_rec = COLLEGE_ERP_DB.get(student_email)
            base_profile, _ = _find_student_profile(student_email)
            opportunities = [
                {
                    "jd": jd,
                    **build_jd_prep_plan(base_profile["extracted"], jd),
                    "eligible": (
                        float(base_profile["extracted"].get("cgpa") or 0) >= float(jd["minCgpa"])
                        and base_profile["extracted"].get("branch", "").upper()
                        in [b.upper() for b in jd["allowedBranches"]]
                        and int(base_profile["extracted"].get("active_backlogs") or 0)
                        <= int(jd["maxBacklogs"])
                    ),
                }
                for jd in jds_copy
            ]

            self._send_json(
                200,
                {
                    "ok": True,
                    "student": session,
                    "erpRecord": erp_rec,
                    "records": own_records,
                    "activeJd": active_jd,
                    "opportunities": opportunities,
                },
            )
            return

        self._send_json(404, {"error": f"Unknown route: {path}"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        if path == "/api/login":
            try:
                body = self._read_json()
                role = str(body.get("role", "")).strip().lower()
                email = str(body.get("email", "")).strip().lower()
                passcode = str(body.get("passcode", "")).strip()

                if role == "college":
                    if email != COLLEGE_EMAIL or passcode != COLLEGE_PASSCODE:
                        self._send_json(401, {"error": "Invalid College Admin email or passcode."})
                        return
                    user = {"role": "college", "email": email, "name": "RIT Placement Cell"}
                elif role == "hr":
                    if not email or "@" not in email or passcode != HR_PASSCODE:
                        self._send_json(401, {"error": "Invalid HR Recruiter email or passcode."})
                        return
                    company = email.split("@", 1)[1].split(".", 1)[0].title()
                    user = {"role": "hr", "email": email, "name": f"{company} Recruiter"}
                elif role == "student":
                    if not email or "@" not in email or passcode != STUDENT_PASSCODE:
                        self._send_json(401, {"error": "Invalid Student email or passcode."})
                        return
                    erp = COLLEGE_ERP_DB.get(email)
                    name = erp["name"] if erp else email.split("@", 1)[0].replace(".", " ").title()
                    user = {"role": "student", "email": email, "name": name}
                else:
                    self._send_json(400, {"error": "Role must be 'college', 'hr', or 'student'."})
                    return

                token = secrets.token_urlsafe(24)
                with _LOCK:
                    SESSIONS[token] = user
                self._send_json(200, {"ok": True, "token": token, "user": user})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/logout":
            token = self.headers.get("X-Session-Token", "").strip()
            with _LOCK:
                SESSIONS.pop(token, None)
            self._send_json(200, {"ok": True})
            return

        if path == "/api/config":
            if not self._require_role("college"):
                return
            try:
                body = self._read_json()
                key = str(body.get("apiKey", "")).strip()
                if not key:
                    self._send_json(400, {"error": "API key cannot be empty."})
                    return
                save_api_key(key)
                self._send_json(200, {"ok": True, "maskedKey": mask_key(key)})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/college/jds":
            if not self._require_role("college"):
                return
            try:
                body = self._read_json()
                jd = {
                    "id": str(body.get("id") or f"custom_{int(time.time() * 1000)}"),
                    "company": str(body.get("company") or "Custom Company"),
                    "role": str(body.get("role") or "Software Engineer"),
                    "ctc": str(body.get("ctc") or "Competitive"),
                    "minCgpa": float(body.get("minCgpa") or 7.0),
                    "allowedBranches": [
                        str(b).strip().upper()
                        for b in (body.get("allowedBranches") or ["CSE", "ISE"])
                        if str(b).strip()
                    ],
                    "maxBacklogs": int(body.get("maxBacklogs") or 0),
                    "mustHaveSkills": [
                        str(s).strip() for s in (body.get("mustHaveSkills") or []) if str(s).strip()
                    ],
                    "goodToHaveSkills": [
                        str(s).strip() for s in (body.get("goodToHaveSkills") or []) if str(s).strip()
                    ],
                }
                with _LOCK:
                    JDS_STORE.insert(0, jd)
                self._send_json(200, {"ok": True, "jd": jd})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/college/erp-verify":
            if not self._require_role("college"):
                return
            try:
                body = self._read_json()
                email = str(body.get("email") or "").strip().lower()
                res = verify_against_erp(
                    email=email,
                    resume_cgpa=float(body.get("resumeCgpa") or 0.0),
                    resume_branch=str(body.get("resumeBranch") or ""),
                    resume_backlogs=int(body.get("resumeBacklogs") or 0),
                    claimed_semester=(
                        int(body["claimedSemester"])
                        if body.get("claimedSemester") not in (None, "")
                        else None
                    ),
                )
                updated_record = None
                if body.get("syncRecord"):
                    with _LOCK:
                        rec_id = str(body.get("recordId") or "")
                        target = next(
                            (
                                r
                                for r in reversed(RESULTS_STORE)
                                if (rec_id and r.get("id") == rec_id)
                                or (
                                    not rec_id
                                    and str(r.get("extracted", {}).get("email", "")).strip().lower()
                                    == email
                                )
                            ),
                            None,
                        )
                        if target:
                            target["routing"]["erpVerification"] = res
                            target["routing"]["credibility"] = compute_credibility_score(
                                target["extracted"],
                                target["routing"],
                                res,
                                target.get("skillTest"),
                            )
                            updated_record = copy.deepcopy(target)
                self._send_json(200, {"ok": True, "erpVerification": res, "updatedRecord": updated_record})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/student/appeal":
            session = self._require_role("student")
            if not session:
                return
            try:
                body = self._read_json()
                rec_id = str(body.get("recordId") or "").strip()
                reason = str(body.get("reason") or "").strip()
                if not rec_id or not reason:
                    self._send_json(400, {"error": "A record and brief appeal reason are required."})
                    return
                if len(reason) > 1000:
                    self._send_json(400, {"error": "Appeal reason must be 1,000 characters or fewer."})
                    return
                with _LOCK:
                    target = next((r for r in RESULTS_STORE if r.get("id") == rec_id and _record_belongs_to_student(r, session["email"])), None)
                    if not target:
                        self._send_json(404, {"error": "Your placement decision was not found."})
                        return
                    appeal_id = f"app_{secrets.token_hex(5)}"
                    appeal = {"id": appeal_id, "recordId": rec_id, "studentEmail": session["email"].lower(), "studentName": target.get("extracted", {}).get("student_name", session.get("name", "")), "reason": reason, "status": "SUBMITTED", "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "originalDecision": copy.deepcopy(target.get("routing", {})), "explanation": copy.deepcopy(target.get("decisionExplanation") or _decision_explanation(target)), "history": []}
                    APPEALS_STORE.append(appeal)
                    _audit_transition("appeal", appeal_id, "APPEAL_SUBMITTED", session["email"], "", "SUBMITTED", reason)
                    appeal["history"].append(copy.deepcopy(AUDIT_LOG[-1]))
                    _persist_audit_state()
                    result = copy.deepcopy(appeal)
                self._send_json(201, {"ok": True, "appeal": result})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/college/appeal-review":
            session = self._require_role("college")
            if not session:
                return
            try:
                body = self._read_json()
                appeal_id = str(body.get("appealId") or "")
                status = str(body.get("status") or "").upper()
                note = str(body.get("note") or "").strip()
                allowed = {"UNDER_REVIEW", "NEED_INFO", "ACCEPTED", "REJECTED", "OVERRIDDEN"}
                if status not in allowed:
                    self._send_json(400, {"error": "Invalid appeal status."})
                    return
                if not note:
                    self._send_json(400, {"error": "A review note is required for every appeal status change."})
                    return
                if len(note) > 1000:
                    self._send_json(400, {"error": "Review note must be 1,000 characters or fewer."})
                    return
                with _LOCK:
                    appeal = next((a for a in APPEALS_STORE if a.get("id") == appeal_id), None)
                    if not appeal:
                        self._send_json(404, {"error": "Appeal not found."})
                        return
                    previous = appeal["status"]
                    if previous in {"ACCEPTED", "REJECTED", "OVERRIDDEN"}:
                        self._send_json(409, {"error": "This appeal is already closed and cannot be changed."})
                        return
                    if status == previous:
                        self._send_json(409, {"error": "Appeal is already in that status."})
                        return
                    target = None
                    override_route = ""
                    if status == "OVERRIDDEN":
                        target = next((r for r in RESULTS_STORE if r.get("id") == appeal["recordId"]), None)
                        override_route = str(body.get("overrideRoute") or "").upper()
                        if not target or override_route not in {"SHORTLISTED", "PORTFOLIO_EXCEPTION", "UPSKILL_TRACK", "REROUTED"}:
                            self._send_json(400, {"error": "A valid overrideRoute and existing candidate record are required."})
                            return
                    appeal["status"] = status
                    appeal["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                    appeal["reviewNote"] = note[:1000]
                    if status == "OVERRIDDEN":
                        appeal["originalDecision"] = copy.deepcopy(appeal.get("originalDecision") or target.get("routing", {}))
                        target["humanOverride"] = {"route": override_route, "reason": note[:1000], "by": session["email"], "at": appeal["updatedAt"], "automatedDecisionPreserved": True}
                        target["routing"]["humanOverride"] = copy.deepcopy(target["humanOverride"])
                        target["routing"]["route"] = override_route
                        target["routing"]["routeLabel"] = f"{override_route.replace('_', ' ').title()} (Human Override)"
                        target["routing"]["verdictReason"] = f"Human override by {session['email']}: {note[:500]}"
                        target["decisionExplanation"] = _decision_explanation(target)
                    entry = _audit_transition("appeal", appeal_id, "STATUS_CHANGED", session["email"], previous, status, note)
                    appeal.setdefault("history", []).append(copy.deepcopy(entry))
                    _audit_transition("record", appeal["recordId"], "HUMAN_OVERRIDE" if status == "OVERRIDDEN" else "APPEAL_REVIEWED", session["email"], previous, status, note)
                    _persist_audit_state()
                    result = copy.deepcopy(appeal)
                self._send_json(200, {"ok": True, "appeal": result})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/college/disruption":
            session = self._require_role("college")
            if not session:
                return
            try:
                body = self._read_json()
                jd_id = str(body.get("jdId") or "").strip()
                trigger_type = str(body.get("triggerType") or "eligibility_cutoff").strip().lower()
                if trigger_type not in ("eligibility_cutoff", "cancellation", "resource_unavailable"):
                    self._send_json(400, {"error": "Trigger must be eligibility_cutoff, cancellation, or resource_unavailable."})
                    return
                with _LOCK:
                    jd = next((item for item in JDS_STORE if item.get("id") == jd_id), None)
                    if not jd:
                        self._send_json(404, {"error": "Target placement drive not found."})
                        return
                    old_cutoff = float(jd.get("minCgpa") or 0)
                    new_cutoff = old_cutoff
                    if trigger_type == "eligibility_cutoff":
                        if body.get("newMinCgpa") in (None, ""):
                            self._send_json(400, {"error": "newMinCgpa is required for a cutoff change."})
                            return
                        new_cutoff = float(body["newMinCgpa"])
                        if not 0 <= new_cutoff <= 10:
                            self._send_json(400, {"error": "CGPA cutoff must be between 0 and 10."})
                            return
                        if new_cutoff == old_cutoff:
                            self._send_json(400, {"error": "New cutoff must differ from the current cutoff."})
                            return
                        jd["minCgpa"] = new_cutoff
                    else:
                        jd["isActive"] = False

                    event_id = f"dis_{secrets.token_hex(5)}"
                    default_title = (
                        "Eligibility cutoff changed" if trigger_type == "eligibility_cutoff"
                        else ("Campus resource unavailable" if trigger_type == "resource_unavailable" else "Placement drive cancelled")
                    )
                    default_reason = (
                        f"{jd.get('company')} raised the CGPA cutoff from {old_cutoff:.2f} to {new_cutoff:.2f}."
                        if trigger_type == "eligibility_cutoff"
                        else (
                            f"A required campus resource for {jd.get('company')} — {jd.get('role')} is unavailable."
                            if trigger_type == "resource_unavailable"
                            else f"{jd.get('company')} — {jd.get('role')} has been cancelled."
                        )
                    )
                    title = str(body.get("title") or default_title)[:120]
                    reason = str(body.get("details") or default_reason)[:500]
                    event = {
                        "id": event_id, "triggerType": trigger_type, "jdId": jd_id,
                        "company": jd.get("company"), "role": jd.get("role"),
                        "title": title, "reason": reason,
                        "oldMinCgpa": old_cutoff, "newMinCgpa": new_cutoff,
                        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "triggeredBy": session["email"],
                    }
                    target_records = [
                        r for r in RESULTS_STORE
                        if r.get("status") == "ok"
                        and (r.get("jdId") or ACTIVE_JD_SNAPSHOT.get("id")) == jd_id
                    ]
                    if trigger_type == "eligibility_cutoff":
                        low, high = sorted((old_cutoff, new_cutoff))
                        impacted = []
                        for record in target_records:
                            ex = record.get("extracted", {})
                            official = record.get("routing", {}).get("erpVerification", {}).get("officialCgpa")
                            cgpa = float(official if official is not None else ex.get("cgpa") or 0)
                            if low <= cgpa < high:
                                impacted.append(record)
                    else:
                        impacted = target_records

                    diffs = []
                    alternative_jds = [
                        item for item in JDS_STORE
                        if item.get("isActive", True) and item.get("id") != jd_id
                    ]
                    impact_rows = []
                    for record in impacted:
                        before = copy.deepcopy(record.get("routing") or {})
                        after = copy.deepcopy(before)
                        if trigger_type in ("cancellation", "resource_unavailable"):
                            ranked = []
                            evaluated_alternatives = []
                            for alternative in alternative_jds:
                                candidate_route = evaluate_deterministic_rules(
                                    record.get("extracted", {}), alternative, alternative_jds,
                                    skill_test=record.get("skillTest")
                                )
                                gates = {
                                    "cgpa": bool(candidate_route.get("passesCgpa")),
                                    "branch": bool(candidate_route.get("passesBranch")),
                                    "backlogs": bool(candidate_route.get("passesBacklogs")),
                                }
                                eligible = all(gates.values())
                                evaluated_alternatives.append({
                                    "jdId": alternative.get("id"),
                                    "company": alternative.get("company"),
                                    "role": alternative.get("role"),
                                    "fitPct": candidate_route.get("matchPct", 0),
                                    "eligible": eligible,
                                    "gates": gates,
                                    "missingSkills": copy.deepcopy(candidate_route.get("missingMustHave", [])),
                                    "matchedSkills": copy.deepcopy(candidate_route.get("matchedMustHave", [])),
                                })
                                if eligible:
                                    ranked.append((candidate_route.get("matchPct", 0), alternative, candidate_route))
                            ranked.sort(key=lambda item: (item[0], item[1].get("company", "")), reverse=True)
                            if ranked:
                                _, chosen_alternative, after = ranked[0]
                                after["alternativeEvaluations"] = evaluated_alternatives
                                after["route"] = "REROUTED"
                                after["routeLabel"] = f"Auto-Rerouted · {chosen_alternative.get('company')}"
                                after["verdictReason"] = (
                                    f"Original drive {jd.get('company')} — {jd.get('role')} became unavailable. "
                                    f"Matched to {chosen_alternative.get('company')} — {chosen_alternative.get('role')} "
                                    f"with {after.get('matchPct', 0)}% verified skill fit and passed academic eligibility."
                                )
                                after["alternativeJd"] = {
                                    "jdId": chosen_alternative.get("id"),
                                    "company": chosen_alternative.get("company"),
                                    "role": chosen_alternative.get("role"),
                                    "ctc": chosen_alternative.get("ctc", ""),
                                    "fitPct": after.get("matchPct", 0),
                                }
                            else:
                                after["route"] = "REROUTED"
                                after["routeLabel"] = "No eligible alternative"
                                after["verdictReason"] = (
                                    f"{jd.get('company')} — {jd.get('role')} became unavailable. "
                                    "No other active drive passed this student's academic eligibility rules."
                                )
                                after["alternativeJd"] = None
                                after["alternativeEvaluations"] = evaluated_alternatives
                            impact_rows.append({
                                "recordId": record.get("id"),
                                "studentName": record.get("extracted", {}).get("student_name") or "Student",
                                "alternative": copy.deepcopy(after.get("alternativeJd")),
                                "evaluations": copy.deepcopy(after.get("alternativeEvaluations", [])),
                            })
                        else:
                            active_jds = [item for item in JDS_STORE if item.get("isActive", True)]
                            after = evaluate_deterministic_rules(
                                record.get("extracted", {}), jd, active_jds,
                                skill_test=record.get("skillTest")
                            )
                        diff = build_before_after(record, after)
                        diff["alternative"] = copy.deepcopy(after.get("alternativeJd"))
                        record["routing"] = after
                        explanation_record = {"extracted": record.get("extracted", {}), "routing": after, "jdId": (after.get("alternativeJd") or {}).get("jdId") or record.get("jdId")}
                        record["decisionExplanation"] = _decision_explanation(explanation_record)
                        _audit_transition("record", record.get("id", ""), "AUTOMATED_DECISION_UPDATED", session["email"], str(before.get("route") or ""), str(after.get("route") or ""), event.get("reason", ""))
                        record["disruptionHistory"] = record.get("disruptionHistory", [])
                        record["disruptionHistory"].append({
                            "eventId": event_id, "before": diff["before"], "after": diff["after"],
                            "alternative": diff["alternative"],
                        })
                        if route_changed(diff):
                            diffs.append(diff)
                            notification = make_notification(event, diff, event["createdAt"])
                            notification["alternative"] = copy.deepcopy(diff["alternative"])
                            NOTIFICATIONS_STORE.append(notification)

                    alternative_distribution = {}
                    for row in impact_rows:
                        chosen = row.get("alternative")
                        if chosen:
                            company = chosen.get("company") or "Unknown"
                            alternative_distribution[company] = alternative_distribution.get(company, 0) + 1
                    found_count = sum(1 for row in impact_rows if row.get("alternative"))
                    event["impactSummary"] = {
                        "recomputed": len(impacted),
                        "changed": len(diffs),
                        "alternativeFound": found_count,
                        "noAlternative": len(impact_rows) - found_count,
                        "alternativeDistribution": [
                            {"company": company, "count": count}
                            for company, count in sorted(alternative_distribution.items(), key=lambda item: (-item[1], item[0]))
                        ],
                        "candidateExplanations": impact_rows,
                    }
                    event["candidatePoolCount"] = len(target_records)
                    event["alternativeDriveCount"] = len(alternative_jds)
                    event["recomputedCount"] = len(impacted)
                    event["affectedCount"] = len(diffs)
                    event["unaffectedCount"] = max(0, len(RESULTS_STORE) - len(impacted))
                    event["diffs"] = diffs
                    DISRUPTION_EVENTS.append(event)
                    response_event = copy.deepcopy(event)

                self._send_json(200, {"ok": True, "event": response_event,
                                      "recomputedCount": len(impacted),
                                      "affectedCount": len(diffs),
                                      "unaffectedCount": event["unaffectedCount"],
                                      "impactSummary": response_event.get("impactSummary", {}),
                                      "diffs": diffs})
            except (TypeError, ValueError) as exc:
                self._send_json(400, {"error": f"Invalid disruption input: {exc}"})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/hr/decision":
            session = self._require_role("hr")
            if not session:
                return
            try:
                body = self._read_json()
                rec_id = str(body.get("id") or "")
                decision = str(body.get("decision") or "INTERVIEW_INVITED").strip().upper()
                slot = str(body.get("interviewSlot") or "").strip()
                note = str(body.get("hrNote") or "").strip()
                if decision not in ("INTERVIEW_INVITED", "ON_HOLD", "DECLINED"):
                    self._send_json(400, {"error": "Invalid HR decision."})
                    return
                with _LOCK:
                    target = next((r for r in RESULTS_STORE if r.get("id") == rec_id), None)
                    if not target or target.get("status") != "ok":
                        self._send_json(404, {"error": "Candidate record not found."})
                        return
                    previous_hr = (target.get("hrAction") or {}).get("decision", "")
                    target["hrAction"] = {
                        "decision": decision,
                        "interviewSlot": slot,
                        "hrNote": note,
                        "updatedBy": session["email"],
                        "updatedAt": time.strftime("%Y-%m-%d %H:%M"),
                    }
                    _audit_transition("record", rec_id, "HR_DECISION_CHANGED", session["email"], previous_hr, decision, note)
                    updated = copy.deepcopy(target)
                self._send_json(200, {"ok": True, "record": updated})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/college/clear":
            if not self._require_role("college"):
                return
            with _LOCK:
                RESULTS_STORE.clear()
            self._send_json(200, {"ok": True})
            return

        if path == "/api/college/approve-waiver":
            if not self._require_role("college"):
                return
            try:
                body = self._read_json()
                rec_id = str(body.get("id", ""))
                with _LOCK:
                    target = next((r for r in RESULTS_STORE if r.get("id") == rec_id), None)
                    if not target or target.get("status") != "ok":
                        self._send_json(404, {"error": "Candidate record not found."})
                        return
                    rt = target["routing"]
                    rt["route"] = "SHORTLISTED"
                    rt["routeLabel"] = "Shortlisted (Waiver)"
                    rt["waiverApproved"] = True
                    rt["verdictReason"] = (
                        f"College Coordinator approved Portfolio Exception waiver ({rt['matchPct']}% verified fit) "
                        "— promoted to HR Shortlist."
                    )
                    updated = copy.deepcopy(target)
                self._send_json(200, {"ok": True, "record": updated})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path in ("/api/verify-resume", "/api/student/upload-resume"):
            is_student_upload = path == "/api/student/upload-resume"
            session = self._require_role("student" if is_student_upload else "college")
            if not session:
                return
            started = time.time()
            try:
                body = self._read_json()
                api_key = load_api_key()
                if not api_key:
                    self._send_json(
                        400,
                        {"error": "No Gemini API key configured. Ask College Admin to set Gemini Key first."},
                    )
                    return

                with _LOCK:
                    all_jds = body.get("allJds") or copy.deepcopy(JDS_STORE)
                jd_id = str(body.get("jdId") or "")
                jd = body.get("jd") or next((j for j in all_jds if j["id"] == jd_id), all_jds[0])
                pdf_b64 = body.get("pdfBase64")
                resume_text = body.get("resumeText")
                rec_id = str(body.get("id") or f"rec_{secrets.token_hex(6)}")
                file_name = str(body.get("fileName", "resume.pdf"))

                audit = call_gemini_audit(
                    api_key=api_key,
                    jd=jd,
                    pdf_base64=pdf_b64,
                    resume_text=resume_text,
                )
                if is_student_upload and not audit["extracted"].get("email"):
                    audit["extracted"]["email"] = session["email"]
                routing = evaluate_deterministic_rules(audit["extracted"], jd, all_jds)
                record_explanation = _decision_explanation({"extracted": audit["extracted"], "routing": routing, "jdId": str(jd.get("id") or "")})
                elapsed_ms = round((time.time() - started) * 1000)

                record = {
                    "id": rec_id,
                    "jdId": str(jd.get("id") or ""),
                    "fileName": file_name,
                    "status": "ok",
                    "uploadedBy": "student" if is_student_upload else "college",
                    "ownerEmail": session["email"].lower() if is_student_upload else str(audit["extracted"].get("email", "")).strip().lower(),
                    "extracted": audit["extracted"],
                    "routing": routing,
                    "decisionExplanation": record_explanation,
                    "meta": {
                        "modelUsed": audit["modelUsed"],
                        "attemptsLog": audit["attemptsLog"],
                        "durationMs": elapsed_ms,
                    },
                    "checkedDays": {},
                    "appliedAlt": False,
                    "skillTest": None,
                    "hrAction": None,
                }
                with _LOCK:
                    global ACTIVE_JD_SNAPSHOT
                    ACTIVE_JD_SNAPSHOT = jd
                    RESULTS_STORE[:] = [
                        r
                        for r in RESULTS_STORE
                        if r.get("id") != rec_id
                        and not (
                            is_student_upload
                            and str(r.get("ownerEmail", "")).lower() == session["email"].lower()
                        )
                    ]
                    RESULTS_STORE.append(record)

                self._send_json(200, {"ok": True, **record})
            except Exception as exc:
                self._send_json(500, {"error": str(exc)})
            return

        if path == "/api/student/prepare-jd":
            session = self._require_role("student")
            if not session:
                return
            try:
                body = self._read_json()
                jd_id = str(body.get("jdId") or "")
                with _LOCK:
                    target_jd = next((j for j in JDS_STORE if j["id"] == jd_id), JDS_STORE[0])
                profile, _ = _find_student_profile(session["email"])
                plan = build_jd_prep_plan(profile["extracted"], target_jd)
                self._send_json(200, {"ok": True, **plan})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/student/generate-test":
            session = self._require_role("student")
            if not session:
                return
            try:
                body = self._read_json()
                jd_id = str(body.get("jdId") or "")
                with _LOCK:
                    target_jd = next((j for j in JDS_STORE if j["id"] == jd_id), ACTIVE_JD_SNAPSHOT)
                profile, _ = _find_student_profile(session["email"])
                plan = build_jd_prep_plan(profile["extracted"], target_jd)
                skills = body.get("skills") or plan["missingSkills"] or target_jd.get("mustHaveSkills", [])[:2]

                generated = generate_skill_test(
                    api_key=load_api_key(),
                    student_name=session["name"],
                    target_jd=target_jd,
                    skills=skills,
                    courses=plan["recommendedCourses"],
                )
                test_id = f"test_{secrets.token_hex(6)}"
                with _LOCK:
                    TESTS_STORE[test_id] = {
                        "testId": test_id,
                        "email": session["email"].lower(),
                        "jd": target_jd,
                        "questions": generated["questions"],
                        "source": generated["source"],
                    }

                public_qs = [
                    {
                        "id": q.get("id", f"q{i+1}"),
                        "skill": q.get("skill"),
                        "question": q.get("question"),
                        "options": q.get("options"),
                    }
                    for i, q in enumerate(generated["questions"])
                ]
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "testId": test_id,
                        "jd": target_jd,
                        "source": generated["source"],
                        "questions": public_qs,
                    },
                )
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/student/submit-test":
            session = self._require_role("student")
            if not session:
                return
            try:
                body = self._read_json()
                test_id = str(body.get("testId") or "")
                answers = [int(a) for a in (body.get("answers") or [])]
                proctoring = body.get("proctoring") or {}
                student_email = session["email"].lower()

                with _LOCK:
                    test_obj = TESTS_STORE.get(test_id)
                if not test_obj or test_obj["email"] != student_email:
                    self._send_json(404, {"error": "Active test session not found."})
                    return

                result = grade_skill_test(test_obj["questions"], answers, proctoring)

                # If passed, upgrade the student's verified skills & recompute routing + HR credibility
                updated_record = None
                with _LOCK:
                    target = next(
                        (
                            r
                            for r in reversed(RESULTS_STORE)
                            if r.get("status") == "ok"
                            and _record_belongs_to_student(r, student_email)
                        ),
                        None,
                    )
                    if target:
                        target["skillTest"] = result
                        if result["passed"]:
                            verified = target["extracted"].setdefault("verified_skills", [])
                            existing_lower = {str(v.get("skill", "")).lower() for v in verified}
                            for sk in result["verifiedSkills"]:
                                if sk.lower() not in existing_lower:
                                    verified.append(
                                        {
                                            "skill": sk,
                                            "evidence_source": (
                                                f"Passed AI-Proctored Skill Test ({result['scorePct']}%, "
                                                f"Webcam/Mic Logged)"
                                            ),
                                        }
                                    )
                                    existing_lower.add(sk.lower())
                            # Remove newly proven skills from unverified_claims
                            passed_lower = {s.lower() for s in result["verifiedSkills"]}
                            target["extracted"]["unverified_claims"] = [
                                c
                                for c in target["extracted"].get("unverified_claims", [])
                                if str(c.get("skill", "")).lower() not in passed_lower
                            ]
                        target["routing"] = evaluate_deterministic_rules(
                            target["extracted"],
                            ACTIVE_JD_SNAPSHOT,
                            JDS_STORE,
                            skill_test=result,
                        )
                        updated_record = copy.deepcopy(target)

                self._send_json(
                    200,
                    {"ok": True, "result": result, "updatedRecord": updated_record},
                )
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/student/toggle-day":
            session = self._require_role("student")
            if not session:
                return
            try:
                body = self._read_json()
                rec_id = str(body.get("id", ""))
                day_idx = str(body.get("dayIdx", ""))
                student_email = session["email"].lower()
                with _LOCK:
                    target = next((r for r in RESULTS_STORE if r.get("id") == rec_id), None)
                    if not target:
                        self._send_json(404, {"error": "Record not found."})
                        return
                    if not _record_belongs_to_student(target, student_email):
                        self._send_json(403, {"error": "Cannot modify another student's checklist."})
                        return
                    checked = target.setdefault("checkedDays", {})
                    checked[day_idx] = not bool(checked.get(day_idx, False))
                self._send_json(200, {"ok": True, "checkedDays": checked})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        if path == "/api/student/confirm-reroute":
            session = self._require_role("student")
            if not session:
                return
            try:
                body = self._read_json()
                rec_id = str(body.get("id", ""))
                student_email = session["email"].lower()
                with _LOCK:
                    target = next((r for r in RESULTS_STORE if r.get("id") == rec_id), None)
                    if not target:
                        self._send_json(404, {"error": "Record not found."})
                        return
                    if not _record_belongs_to_student(target, student_email):
                        self._send_json(403, {"error": "Cannot modify another student's drive."})
                        return
                    target["appliedAlt"] = True
                self._send_json(200, {"ok": True})
            except Exception as exc:
                self._send_json(400, {"error": str(exc)})
            return

        self._send_json(404, {"error": f"Unknown route: {path}"})


def main() -> None:
    httpd = ThreadingHTTPServer((HOST, PORT), PlaceFlowHandler)
    print(f"PlaceFlow AI running at http://{HOST}:{PORT}")
    print(f"College login: {COLLEGE_EMAIL} / {COLLEGE_PASSCODE}")
    print(f"HR login:      hr@cloudbackend.com / {HR_PASSCODE}")
    print(f"Student login: <student>@rit.edu / {STUDENT_PASSCODE}")
    print("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


if __name__ == "__main__":
    main()
