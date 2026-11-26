"""Automated verification tests for PlaceFlow AI 3-portal RBAC, ERP, HR credibility & tests."""

import copy
import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server
from erp_db import verify_against_erp
from router_engine import DEFAULT_JDS, _build_ssl_context, evaluate_deterministic_rules


def test_erp_and_deterministic_router() -> None:
    ctx = _build_ssl_context()
    assert ctx.cert_store_stats()["x509_ca"] > 0

    # ERP exact verification vs tampering detection & re-validation recovery
    ok_erp = verify_against_erp("kiran.gowda@rit.edu", 7.25, "ECE", 0, claimed_semester=7)
    assert ok_erp["status"] == "VERIFIED" and ok_erp["usn"] == "1MS23EC055"

    bad_erp = verify_against_erp("kiran.gowda@rit.edu", 9.10, "CSE", 0, claimed_semester=8)
    assert bad_erp["status"] == "INVALID" and len(bad_erp["discrepancies"]) == 3

    recheck_erp = verify_against_erp("kiran.gowda@rit.edu", 7.25, "ECE", 0, claimed_semester=7)
    assert recheck_erp["status"] == "VERIFIED" and recheck_erp["verified"] is True

    cloud_jd = DEFAULT_JDS[0]

    # 1. Strong match -> SHORTLISTED + high credibility
    res1 = evaluate_deterministic_rules(
        {
            "email": "aarav.sharma@rit.edu",
            "branch": "Computer Science & Engineering (CSE)",
            "cgpa": 8.72,
            "active_backlogs": 0,
            "verified_skills": [
                {"skill": "Python", "evidence_source": "CampusRide"},
                {"skill": "FastAPI", "evidence_source": "CampusRide"},
                {"skill": "PostgreSQL", "evidence_source": "CampusRide"},
                {"skill": "Docker", "evidence_source": "CampusRide"},
                {"skill": "Git", "evidence_source": "CampusRide"},
            ],
            "unverified_claims": [],
        },
        cloud_jd,
        DEFAULT_JDS,
    )
    assert res1["route"] == "SHORTLISTED" and res1["matchPct"] == 100
    assert res1["credibility"]["score"] >= 90

    # 2. Buzzword / Skill-Gap -> UPSKILL_TRACK + YouTube courses
    res2 = evaluate_deterministic_rules(
        {
            "email": "rohan.verma@rit.edu",
            "branch": "CSE",
            "cgpa": 7.9,
            "active_backlogs": 0,
            "verified_skills": [
                {"skill": "Python", "evidence_source": "CLI"},
                {"skill": "Git", "evidence_source": "Repo"},
            ],
            "unverified_claims": [{"skill": "Kubernetes", "reason": "No proof"}],
            "upskilling_plan_7_days": [
                {"day": "Day 1-2", "focus_topic": "FastAPI", "mini_task": "API", "youtube_query": "FastAPI"}
            ],
        },
        cloud_jd,
        DEFAULT_JDS,
    )
    assert res2["route"] == "UPSKILL_TRACK" and res2["routeLabel"] == "Flagged + Upskill"
    assert any(c["skill"] == "Docker" for c in res2["recommendedCourses"])

    # 3. ECE/Low CGPA -> REROUTED to DataPulse
    res3 = evaluate_deterministic_rules(
        {
            "email": "meera.nair@rit.edu",
            "branch": "ECE",
            "cgpa": 7.12,
            "active_backlogs": 0,
            "verified_skills": [
                {"skill": "Python", "evidence_source": "Pandas"},
                {"skill": "SQL", "evidence_source": "Retail"},
                {"skill": "Excel", "evidence_source": "Retail"},
                {"skill": "Power BI", "evidence_source": "Retail"},
            ],
            "unverified_claims": [],
        },
        cloud_jd,
        DEFAULT_JDS,
    )
    assert res3["route"] == "REROUTED" and res3["altMatches"][0]["jdId"] == "data"

    # 4. Cutoff miss WITH strong verified projects -> PORTFOLIO_EXCEPTION
    # Even if resume lied and wrote cgpa 9.0, ERP forces 7.25 -> still caught!
    res4 = evaluate_deterministic_rules(
        {
            "email": "kiran.gowda@rit.edu",
            "branch": "ECE",
            "cgpa": 9.0,
            "active_backlogs": 0,
            "verified_skills": [
                {"skill": "Python", "evidence_source": "CampusPay"},
                {"skill": "FastAPI", "evidence_source": "CampusPay"},
                {"skill": "PostgreSQL", "evidence_source": "CampusPay"},
                {"skill": "Docker", "evidence_source": "CampusPay"},
                {"skill": "Git", "evidence_source": "CampusPay"},
            ],
            "unverified_claims": [],
        },
        cloud_jd,
        DEFAULT_JDS,
    )
    assert res4["route"] == "PORTFOLIO_EXCEPTION"
    assert res4["erpVerification"]["status"] == "INVALID"


def _invoke(method: str, path: str, body: dict | None = None, token: str = "") -> tuple[int, bytes]:
    raw_body = json.dumps(body).encode("utf-8") if body is not None else b""
    headers = {"Content-Length": str(len(raw_body))}
    if token:
        headers["X-Session-Token"] = token

    class DummyHandler(server.PlaceFlowHandler):
        def __init__(self) -> None:
            self.path = path
            self.headers = headers
            self.rfile = io.BytesIO(raw_body)
            self.wfile = io.BytesIO()
            self.status_code = 0

        def send_response(self, code: int, message: str | None = None) -> None:
            self.status_code = code

        def send_header(self, keyword: str, value: str) -> None:
            return

        def end_headers(self) -> None:
            return

    h = DummyHandler()
    if method == "GET":
        h.do_GET()
    else:
        h.do_POST()
    return h.status_code, h.wfile.getvalue()


def test_3_portal_rbac_and_proctored_test() -> None:
    server.SESSIONS.clear()
    server.RESULTS_STORE.clear()
    server.TESTS_STORE.clear()
    server.JDS_STORE[:] = copy.deepcopy(DEFAULT_JDS)
    server.ACTIVE_JD_SNAPSHOT = server.JDS_STORE[0]

    assert _invoke("GET", "/")[0] == 200
    assert _invoke("GET", "/api/demo-resumes")[0] == 401
    assert _invoke("GET", "/api/hr/candidates")[0] == 401
    assert _invoke("GET", "/api/student/me")[0] == 401

    college_tok = json.loads(
        _invoke("POST", "/api/login", {"role": "college", "email": server.COLLEGE_EMAIL, "passcode": server.COLLEGE_PASSCODE})[1].decode("utf-8")
    )["token"]
    hr_tok = json.loads(
        _invoke("POST", "/api/login", {"role": "hr", "email": "hr@cloudbackend.com", "passcode": server.HR_PASSCODE})[1].decode("utf-8")
    )["token"]
    ananya_tok = json.loads(
        _invoke("POST", "/api/login", {"role": "student", "email": "ananya.rao@rit.edu", "passcode": server.STUDENT_PASSCODE})[1].decode("utf-8")
    )["token"]

    # Strict cross-portal 403 isolation
    assert _invoke("GET", "/api/hr/candidates", token=college_tok)[0] == 403
    assert _invoke("GET", "/api/student/me", token=college_tok)[0] == 403
    assert _invoke("GET", "/api/college/analytics", token=hr_tok)[0] == 403
    assert _invoke("GET", "/api/student/me", token=hr_tok)[0] == 403
    assert _invoke("GET", "/api/college/analytics", token=ananya_tok)[0] == 403
    assert _invoke("GET", "/api/hr/candidates", token=ananya_tok)[0] == 403

    # College can verify ERP & fetch 5 sample PDFs
    assert len(json.loads(_invoke("GET", "/api/demo-resumes", token=college_tok)[1].decode("utf-8"))["files"]) == 5
    erp_res = json.loads(
        _invoke("POST", "/api/college/erp-verify", {"email": "ananya.rao@rit.edu", "resumeCgpa": 8.2, "resumeBranch": "ISE", "resumeBacklogs": 0, "claimedSemester": 7}, token=college_tok)[1].decode("utf-8")
    )
    assert erp_res["erpVerification"]["verified"] is True

    # Seed Ananya as UPSKILL_TRACK (missing REST APIs, Docker) and Aarav as SHORTLISTED
    ananya_ext = {
        "student_name": "Ananya Rao",
        "email": "ananya.rao@rit.edu",
        "branch": "ISE",
        "cgpa": 8.2,
        "active_backlogs": 0,
        "verified_skills": [
            {"skill": "Python", "evidence_source": "MessMenu"},
            {"skill": "SQL", "evidence_source": "MessMenu"},
            {"skill": "Git", "evidence_source": "MessMenu"},
        ],
        "unverified_claims": [],
        "project_summary": "Python & SQL CLI tools.",
        "upskilling_plan_7_days": [],
        "drafted_notification": "Upskill required.",
    }
    aarav_ext = {
        "student_name": "Aarav Sharma",
        "email": "aarav.sharma@rit.edu",
        "branch": "CSE",
        "cgpa": 8.72,
        "active_backlogs": 0,
        "verified_skills": [
            {"skill": "Python", "evidence_source": "CampusRide"},
            {"skill": "SQL", "evidence_source": "CampusRide"},
            {"skill": "REST APIs", "evidence_source": "CampusRide"},
            {"skill": "Docker", "evidence_source": "CampusRide"},
            {"skill": "Git", "evidence_source": "CampusRide"},
        ],
        "unverified_claims": [],
        "project_summary": "Full FastAPI + Docker suite.",
        "upskilling_plan_7_days": [],
        "drafted_notification": "Shortlisted.",
    }
    server.RESULTS_STORE.extend(
        [
            {"id": "rec_ananya", "fileName": "ananya.pdf", "status": "ok", "extracted": ananya_ext, "routing": evaluate_deterministic_rules(ananya_ext, DEFAULT_JDS[0], DEFAULT_JDS), "checkedDays": {}, "appliedAlt": False, "skillTest": None},
            {"id": "rec_aarav", "fileName": "aarav.pdf", "status": "ok", "extracted": aarav_ext, "routing": evaluate_deterministic_rules(aarav_ext, DEFAULT_JDS[0], DEFAULT_JDS), "checkedDays": {}, "appliedAlt": False, "skillTest": None},
        ]
    )

    # College Analytics checks branch acceptance rates & lacking skills
    analytics = json.loads(_invoke("GET", "/api/college/analytics", token=college_tok)[1].decode("utf-8"))
    cse_stat = next(b for b in analytics["branchStats"] if b["branch"] == "CSE")
    assert cse_stat["acceptanceRatePct"] == 100
    assert any(s["skill"] == "Docker" for s in analytics["topLackingSkills"])

    # HR sees only Aarav initially (with credibility score)
    hr_before = json.loads(_invoke("GET", "/api/hr/candidates", token=hr_tok)[1].decode("utf-8"))
    assert len(hr_before["candidates"]) == 1 and hr_before["candidates"][0]["id"] == "rec_aarav"

    # Ananya generates AI skill test (ensure no answer key leaked!) and passes with webcam/mic
    orig_load = server.load_api_key
    server.load_api_key = lambda: ""  # test deterministic local question bank cleanly
    try:
        st_gen, gen_b = _invoke("POST", "/api/student/generate-test", {"jdId": "cloud", "skills": ["Docker", "REST APIs"]}, token=ananya_tok)
        assert st_gen == 200
        gen = json.loads(gen_b.decode("utf-8"))
        assert len(gen["questions"]) == 5
        assert all("correctIndex" not in q for q in gen["questions"])
    finally:
        server.load_api_key = orig_load

    correct_answers = [q["correctIndex"] for q in server.TESTS_STORE[gen["testId"]]["questions"]]
    st_sub, sub_b = _invoke(
        "POST",
        "/api/student/submit-test",
        {"testId": gen["testId"], "answers": correct_answers, "proctoring": {"webcamActive": True, "micActive": True, "tabSwitches": 0, "durationSec": 45}},
        token=ananya_tok,
    )
    assert st_sub == 200
    sub = json.loads(sub_b.decode("utf-8"))
    assert sub["result"]["passed"] is True
    assert sub["updatedRecord"]["routing"]["route"] == "SHORTLISTED"

    # Now HR sees BOTH Aarav and Ananya!
    hr_after = json.loads(_invoke("GET", "/api/hr/candidates", token=hr_tok)[1].decode("utf-8"))
    assert len(hr_after["candidates"]) == 2

    # College ERP re-validation flips stored record INVALID -> VERIFIED when corrected
    st_inv, inv_b = _invoke(
        "POST",
        "/api/college/erp-verify",
        {
            "recordId": "rec_ananya",
            "syncRecord": True,
            "email": "ananya.rao@rit.edu",
            "resumeCgpa": 9.85,
            "resumeBranch": "ISE",
            "resumeBacklogs": 0,
            "claimedSemester": 7,
        },
        token=college_tok,
    )
    assert st_inv == 200
    inv_res = json.loads(inv_b.decode("utf-8"))
    assert inv_res["erpVerification"]["status"] == "INVALID"
    assert inv_res["updatedRecord"]["routing"]["erpVerification"]["status"] == "INVALID"

    st_fix, fix_b = _invoke(
        "POST",
        "/api/college/erp-verify",
        {
            "recordId": "rec_ananya",
            "syncRecord": True,
            "email": "ananya.rao@rit.edu",
            "resumeCgpa": 8.20,
            "resumeBranch": "ISE",
            "resumeBacklogs": 0,
            "claimedSemester": 7,
        },
        token=college_tok,
    )
    assert st_fix == 200
    fix_res = json.loads(fix_b.decode("utf-8"))
    assert fix_res["erpVerification"]["status"] == "VERIFIED"
    assert fix_res["updatedRecord"]["routing"]["erpVerification"]["status"] == "VERIFIED"

    # Student self-upload flows directly to College & HR portals
    rohan_tok = json.loads(
        _invoke("POST", "/api/login", {"role": "student", "email": "rohan.verma@rit.edu", "passcode": server.STUDENT_PASSCODE})[1].decode("utf-8")
    )["token"]
    st_samp, samp_b = _invoke("GET", "/api/student/sample-pdf", token=rohan_tok)
    assert st_samp == 200
    samp = json.loads(samp_b.decode("utf-8"))
    assert samp["fileName"].endswith(".pdf") and samp["pdfBase64"]

    orig_load_key = server.load_api_key
    orig_audit = server.call_gemini_audit
    server.load_api_key = lambda: "demo-test-key"
    server.call_gemini_audit = lambda api_key, jd, pdf_base64=None, resume_text=None: {
        "extracted": {
            "student_name": "Rohan Verma",
            "email": "rohan.verma@rit.edu",
            "branch": "CSE",
            "cgpa": 7.90,
            "active_backlogs": 0,
            "claimed_semester": 7,
            "verified_skills": [
                {"skill": "Python", "evidence_source": "CampusPulse"},
                {"skill": "SQL", "evidence_source": "CampusPulse"},
                {"skill": "REST APIs", "evidence_source": "CampusPulse"},
                {"skill": "Docker", "evidence_source": "CampusPulse"},
                {"skill": "Git", "evidence_source": "CampusPulse"},
            ],
            "unverified_claims": [],
            "project_summary": "FastAPI & Docker microservice.",
            "upskilling_plan_7_days": [],
            "drafted_notification": "Shortlisted for CloudScale Systems.",
        },
        "modelUsed": "mock-gemini",
        "attemptsLog": [],
    }
    try:
        st_up, up_b = _invoke(
            "POST",
            "/api/student/upload-resume",
            {"id": "rec_rohan_self", "jdId": "cloud", "fileName": samp["fileName"], "pdfBase64": samp["pdfBase64"]},
            token=rohan_tok,
        )
        assert st_up == 200
        up = json.loads(up_b.decode("utf-8"))
        assert up["uploadedBy"] == "student"
        assert up["routing"]["route"] == "SHORTLISTED"
        assert up["routing"]["erpVerification"]["status"] == "VERIFIED"
    finally:
        server.load_api_key = orig_load_key
        server.call_gemini_audit = orig_audit

    college_res = json.loads(_invoke("GET", "/api/college/results", token=college_tok)[1].decode("utf-8"))
    assert any(r["id"] == "rec_rohan_self" and r.get("uploadedBy") == "student" for r in college_res["results"])

    hr_filtered = json.loads(_invoke("GET", "/api/hr/candidates?branch=ALL&includeAll=0&erpOnly=1&minCred=70", token=hr_tok)[1].decode("utf-8"))
    assert any(c["id"] == "rec_rohan_self" for c in hr_filtered["candidates"])

    # HR Invite/Hold/Decline action syncs back to College & Student portals
    st_dec, dec_b = _invoke(
        "POST",
        "/api/hr/decision",
        {
            "id": "rec_rohan_self",
            "decision": "INTERVIEW_INVITED",
            "interviewSlot": "Thu 11:00 AM - Technical Round 1",
            "hrNote": "Strong Docker & FastAPI portfolio",
        },
        token=hr_tok,
    )
    assert st_dec == 200
    dec = json.loads(dec_b.decode("utf-8"))
    assert dec["record"]["hrAction"]["decision"] == "INTERVIEW_INVITED"

    college_after_hr = json.loads(_invoke("GET", "/api/college/results", token=college_tok)[1].decode("utf-8"))
    rohan_col = next(r for r in college_after_hr["results"] if r["id"] == "rec_rohan_self")
    assert rohan_col["hrAction"]["decision"] == "INTERVIEW_INVITED"

    analytics_after_hr = json.loads(_invoke("GET", "/api/college/analytics", token=college_tok)[1].decode("utf-8"))
    assert analytics_after_hr["totalInvited"] >= 1
    cse_after_hr = next(b for b in analytics_after_hr["branchStats"] if b["branch"] == "CSE")
    assert cse_after_hr["invited"] >= 1

    # Even a Portfolio Exception invited by HR reflects in College acceptanceRatePct & invited stats
    kiran_ext = {
        "student_name": "Kiran Gowda",
        "email": "kiran.gowda@rit.edu",
        "branch": "ECE",
        "cgpa": 7.25,
        "active_backlogs": 0,
        "verified_skills": [
            {"skill": "Python", "evidence_source": "CampusPay"},
            {"skill": "SQL", "evidence_source": "CampusPay"},
            {"skill": "REST APIs", "evidence_source": "CampusPay"},
            {"skill": "Docker", "evidence_source": "CampusPay"},
            {"skill": "Git", "evidence_source": "CampusPay"},
        ],
        "unverified_claims": [],
        "project_summary": "Full FastAPI + Docker payment backend.",
        "upskilling_plan_7_days": [],
        "drafted_notification": "Portfolio exception.",
    }
    server.RESULTS_STORE.append(
        {"id": "rec_kiran_stat", "fileName": "kiran.pdf", "status": "ok", "extracted": kiran_ext, "routing": evaluate_deterministic_rules(kiran_ext, DEFAULT_JDS[0], DEFAULT_JDS), "checkedDays": {}, "appliedAlt": False, "skillTest": None, "hrAction": None}
    )
    assert _invoke("POST", "/api/hr/decision", {"id": "rec_kiran_stat", "decision": "INTERVIEW_INVITED", "interviewSlot": "Fri 10:00 AM", "hrNote": "Strong projects"}, token=hr_tok)[0] == 200
    analytics_kiran = json.loads(_invoke("GET", "/api/college/analytics", token=college_tok)[1].decode("utf-8"))
    ece_after_hr = next(b for b in analytics_kiran["branchStats"] if b["branch"] == "ECE")
    assert ece_after_hr["invited"] == 1 and ece_after_hr["shortlisted"] == 1 and ece_after_hr["acceptanceRatePct"] == 100

    rohan_me = json.loads(_invoke("GET", "/api/student/me", token=rohan_tok)[1].decode("utf-8"))
    assert rohan_me["records"][-1]["hrAction"]["interviewSlot"] == "Thu 11:00 AM - Technical Round 1"

    hr_summary_res = json.loads(_invoke("GET", "/api/hr/candidates?erpOnly=1&minCred=70", token=hr_tok)[1].decode("utf-8"))
    assert hr_summary_res["hrSummary"]["interviewInvitedCount"] >= 1

    # Verify UI structure: email queue lives in HR portal, switcher exists, old college email queue removed
    html = Path(server.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="hr-email-queue"' in html
    assert 'id="demo-switcher"' in html
    actions_chunk = html.split('id="tab-actions"', 1)[1].split("</section>", 1)[0]
    assert 'id="email-queue"' not in actions_chunk




def test_selective_campus_disruption_reroute() -> None:
    """A cutoff event must recompute only candidates inside the changed boundary."""
    server.RESULTS_STORE.clear()
    server.DISRUPTION_EVENTS.clear()
    server.NOTIFICATIONS_STORE.clear()
    server.JDS_STORE[:] = copy.deepcopy(DEFAULT_JDS)
    server.ACTIVE_JD_SNAPSHOT = server.JDS_STORE[0]
    server.SESSIONS["disruption-college-token"] = {
        "role": "college", "email": server.COLLEGE_EMAIL, "name": "Placement Cell"
    }
    server.SESSIONS["disruption-student-token"] = {
        "role": "student", "email": "boundary.student@example.edu", "name": "Boundary Student"
    }
    skills = [
        {"skill": "Python", "evidence_source": "CampusRide"},
        {"skill": "SQL", "evidence_source": "CampusRide"},
        {"skill": "REST APIs", "evidence_source": "CampusRide"},
        {"skill": "Docker", "evidence_source": "CampusRide"},
        {"skill": "Git", "evidence_source": "CampusRide"},
    ]
    def seed(record_id: str, email: str, cgpa: float) -> dict:
        extracted = {
            "student_name": record_id, "email": email, "branch": "CSE",
            "cgpa": cgpa, "active_backlogs": 0, "verified_skills": copy.deepcopy(skills),
            "unverified_claims": [], "upskilling_plan_7_days": [],
        }
        return {
            "id": record_id, "jdId": "cloud", "status": "ok",
            "extracted": extracted,
            "routing": evaluate_deterministic_rules(extracted, server.JDS_STORE[0], server.JDS_STORE),
            "skillTest": None, "ownerEmail": email,
        }

    impacted = seed("boundary_candidate", "boundary.student@example.edu", 7.8)
    unaffected = seed("safe_candidate", "safe.student@example.edu", 9.2)
    unaffected_before = copy.deepcopy(unaffected["routing"])
    server.RESULTS_STORE.extend([impacted, unaffected])

    status, payload = _invoke(
        "POST", "/api/college/disruption",
        {"jdId": "cloud", "triggerType": "eligibility_cutoff", "newMinCgpa": 8.0},
        token="disruption-college-token",
    )
    response = json.loads(payload.decode("utf-8"))
    assert status == 200 and response["ok"] is True
    assert response["recomputedCount"] == 1
    assert response["affectedCount"] == 1
    assert response["unaffectedCount"] == 1
    assert unaffected["routing"] == unaffected_before
    assert impacted["routing"]["route"] != "SHORTLISTED"
    assert len(server.NOTIFICATIONS_STORE) == 1
    notification = server.NOTIFICATIONS_STORE[0]
    assert notification["before"]["route"] == "SHORTLISTED"
    assert notification["after"]["route"] == impacted["routing"]["route"]

    student_status, student_payload = _invoke(
        "GET", "/api/notifications", token="disruption-student-token"
    )
    student_alerts = json.loads(student_payload.decode("utf-8"))["notifications"]
    assert student_status == 200 and len(student_alerts) == 1
    assert student_alerts[0]["recipient"] == "boundary.student@example.edu"



def test_resource_outage_auto_matches_alternative_drive():
    server.RESULTS_STORE.clear()
    server.DISRUPTION_EVENTS.clear()
    server.NOTIFICATIONS_STORE.clear()
    server.JDS_STORE[:] = copy.deepcopy(DEFAULT_JDS)
    server.SESSIONS["resource-token"] = {"role": "college", "email": server.COLLEGE_EMAIL, "name": "Placement Cell"}
    ex = {"student_name": "Test Student", "email": "test.resource@example.edu", "branch": "CSE", "cgpa": 8.2, "active_backlogs": 0, "verified_skills": [{"skill": x, "evidence_source": "Capstone"} for x in ["Python", "SQL", "REST APIs", "Docker", "Git"]], "unverified_claims": []}
    route = evaluate_deterministic_rules(ex, server.JDS_STORE[0], server.JDS_STORE)
    assert route["route"] == "SHORTLISTED"
    server.RESULTS_STORE.append({"id": "resource_candidate", "jdId": "cloud", "status": "ok", "extracted": ex, "routing": route, "skillTest": None, "ownerEmail": ex["email"]})
    status, payload = _invoke("POST", "/api/college/disruption", {"jdId": "cloud", "triggerType": "resource_unavailable", "details": "Venue unavailable"}, token="resource-token")
    result = json.loads(payload.decode("utf-8"))
    assert status == 200 and result["recomputedCount"] == 1
    assert result["diffs"][0]["alternative"]["jdId"] == "data"
    assert result["diffs"][0]["alternative"]["company"] == "DataPulse Analytics"
    assert len(server.NOTIFICATIONS_STORE) == 1


def test_decision_appeal_audit_trail() -> None:
    temp_dir = tempfile.TemporaryDirectory()
    original_audit_path = server.AUDIT_PATH
    server.AUDIT_PATH = Path(temp_dir.name) / "appeal_audit.json"
    server.RESULTS_STORE.clear()
    server.APPEALS_STORE.clear()
    server.AUDIT_LOG.clear()
    server.JDS_STORE[:] = copy.deepcopy(DEFAULT_JDS)
    server.ACTIVE_JD_SNAPSHOT = server.JDS_STORE[0]
    server.SESSIONS["appeal-student-token"] = {"role": "student", "email": "appeal.student@example.edu", "name": "Appeal Student"}
    server.SESSIONS["appeal-college-token"] = {"role": "college", "email": server.COLLEGE_EMAIL, "name": "Placement Cell"}
    extracted = {"student_name": "Appeal Student", "email": "appeal.student@example.edu", "branch": "CSE", "cgpa": 8.1, "active_backlogs": 0, "verified_skills": [{"skill": x, "evidence_source": "Project"} for x in ["Python", "SQL", "REST APIs", "Docker", "Git"]], "unverified_claims": []}
    routing = evaluate_deterministic_rules(extracted, server.JDS_STORE[0], server.JDS_STORE)
    record = {"id": "appeal_record", "jdId": "cloud", "status": "ok", "extracted": extracted, "routing": routing, "ownerEmail": extracted["email"]}
    record["decisionExplanation"] = server._decision_explanation(record)
    server.RESULTS_STORE.append(record)

    status, payload = _invoke("POST", "/api/student/appeal", {"recordId": "appeal_record", "reason": "My resume evidence for the required skill was overlooked."}, token="appeal-student-token")
    submitted = json.loads(payload.decode("utf-8"))
    assert status == 201 and submitted["appeal"]["status"] == "SUBMITTED"
    appeal_id = submitted["appeal"]["id"]
    assert submitted["appeal"]["explanation"]["rules"]
    # The submission event and appeal history must both survive a reload.
    persisted = json.loads(server.AUDIT_PATH.read_text(encoding="utf-8"))
    assert persisted["appeals"][0]["history"][0]["toStatus"] == "SUBMITTED"
    server.APPEALS_STORE.clear()
    server.AUDIT_LOG.clear()
    server._load_audit_state()
    assert server.APPEALS_STORE[0]["id"] == appeal_id
    assert server.AUDIT_LOG[0]["action"] == "APPEAL_SUBMITTED"

    assert _invoke("GET", "/api/appeals", token="appeal-college-token")[0] == 200
    status, payload = _invoke("POST", "/api/college/appeal-review", {"appealId": appeal_id, "status": "UNDER_REVIEW", "note": "Evidence review started."}, token="appeal-college-token")
    assert status == 200 and json.loads(payload.decode("utf-8"))["appeal"]["status"] == "UNDER_REVIEW"
    status, payload = _invoke("POST", "/api/college/appeal-review", {"appealId": appeal_id, "status": "OVERRIDDEN", "overrideRoute": "SHORTLISTED", "note": "Verified project evidence; manual correction approved."}, token="appeal-college-token")
    assert status == 200
    assert record["routing"]["route"] == "SHORTLISTED"
    assert record["humanOverride"]["automatedDecisionPreserved"] is True
    assert record["decisionExplanation"]["outcome"].endswith("(Human Override)")
    assert server.APPEALS_STORE[0]["originalDecision"]["route"] != "SHORTLISTED" or server.APPEALS_STORE[0]["originalDecision"].get("route") == routing["route"]
    assert [entry["toStatus"] for entry in server.APPEALS_STORE[0]["history"]] == ["SUBMITTED", "UNDER_REVIEW", "OVERRIDDEN"]
    # Closed appeals cannot be silently re-opened or overwritten.
    status, _ = _invoke("POST", "/api/college/appeal-review", {"appealId": appeal_id, "status": "REJECTED", "note": "Attempted post-close change."}, token="appeal-college-token")
    assert status == 409
    # Every transition requires a meaningful reviewer note.
    status, _ = _invoke("POST", "/api/college/appeal-review", {"appealId": "another", "status": "UNDER_REVIEW", "note": ""}, token="appeal-college-token")
    assert status == 400
    # Student audit view must not reveal another student's record trail.
    server.RESULTS_STORE.append({"id": "private_record", "status": "ok", "ownerEmail": "other@example.edu", "extracted": {"email": "other@example.edu"}})
    server._audit_transition("record", "private_record", "PRIVATE_EVENT", "other@example.edu", "", "PRIVATE", "private details")
    status, payload = _invoke("GET", "/api/audit", token="appeal-student-token")
    own_events = json.loads(payload.decode("utf-8"))["entries"]
    assert status == 200 and all(event.get("entityId") != "private_record" for event in own_events)
    # A different student cannot retrieve or appeal the first student's appeal.
    server.SESSIONS["other-student-token"] = {"role": "student", "email": "other@example.edu", "name": "Other"}
    status, payload = _invoke("GET", "/api/appeals", token="other-student-token")
    assert status == 200 and json.loads(payload.decode("utf-8"))["appeals"] == []
    server.AUDIT_PATH = original_audit_path
    temp_dir.cleanup()
    assert len(server.AUDIT_LOG) >= 4
    assert all(entry.get("timestamp") for entry in server.AUDIT_LOG)
    assert _invoke("POST", "/api/student/appeal", {"recordId": "not-owned", "reason": "Try to appeal another student's decision."}, token="appeal-student-token")[0] == 404

if __name__ == "__main__":
    test_erp_and_deterministic_router()
    test_3_portal_rbac_and_proctored_test()
    test_selective_campus_disruption_reroute()
    test_resource_outage_auto_matches_alternative_drive()
    test_decision_appeal_audit_trail()
    print("ALL TESTS PASSED")
