# PlaceFlow AI — Live Demo Runbook

This is the recommended 5–7 minute judge demonstration for the two Donut Challenges. Use the same running server throughout so candidate records, disruption events, appeals, and audit entries remain in the same process.

## Before the judges arrive

1. Use a fresh checkout of the `main` branch.
2. Start the app with `start_windows.bat` on Windows or `python3 server.py` on macOS/Linux.
3. Open `http://127.0.0.1:8765` in Chrome.
4. Sign in to College using `admin@rit.edu` / `RIT-COLLEGE-2026`.
5. Click **Set Gemini Key** and confirm the key is configured. Do not show or share the key.
6. Load the five sample PDFs and run **Verify & Route Batch**. Wait for processing to finish.
7. In the candidate table, choose a candidate with a successful evaluated record for **CloudBackend Systems** whose CGPA is at least 7.5 but below 8.5, and note the candidate's actual email and original route. This is the controlled example for the cutoff-change demo. If no such candidate exists, choose a candidate who will visibly change route under a cutoff increase and set the new cutoff accordingly. Do not claim a record was impacted unless the UI reports it.
8. Keep the College tab open. Open a second browser tab for the Student portal.

## Demo sequence

### 1. Establish the original decision (45–60 seconds)

- In College, point out the original route, credibility score, ERP checks, and skill evidence for the chosen candidate.
- Explain that eligibility constraints are deterministic and Gemini is used for resume evidence extraction/audit, not as an unbounded decision-maker.
- Sign in to the Student portal using the candidate's actual email and the student passcode `RIT-STUDENT-2026`.
- Show **Why did PlaceFlow decide this?** and identify the actual CGPA, branch, backlog constraint, matched skills, and missing skills.

### 2. Trigger a campus disruption (60–90 seconds)

- Return to College and open the **Disruptions** tab.
- Select **CloudBackend Systems**.
- Select **Eligibility cutoff changed**.
- Set the new minimum CGPA to **8.5** only if the selected candidate satisfies the precondition above; otherwise choose a cutoff that changes that candidate's eligibility.
- Add a clear incident note, e.g. `Recruiter raised the eligibility cutoff shortly before shortlist lock.`
- Click **Trigger disruption & re-route**.
- Pause on the impact summary: candidate pool, records recomputed, changed outcomes, and unaffected records.
- Show the before/after diff and targeted notification. Emphasise that the engine reports its actual recompute and impact counts.

### 3. Show the student-facing consequence (30–45 seconds)

- Refresh the Student portal if needed by signing out and back in.
- Show the changed outcome and updated explanation. Point out the new constraint and what changed; do not imply that unaffected candidates were reprocessed.

### 4. Submit an appeal (30–45 seconds)

- In the Student portal, enter a concise, evidence-based appeal, for example:
  `My project repository contains evidence of the required skills. Please review the linked project work and confirm whether the eligibility outcome should change.`
- Click **Submit appeal for review**.
- Show the appeal status as **SUBMITTED** and the student's own decision/appeal timeline.

### 5. Review and override as College (60–90 seconds)

- Return to College → **Disruptions** → **Decision appeals & override review**.
- Refresh the appeal list.
- Start review and add a note describing what was checked.
- For the controlled demo, choose **Override route** only if the evidence supports it. Select the appropriate route and enter a specific reason.
- Show the original automated decision remains preserved in the appeal record while the human override is separately recorded.
- Refresh the audit trail and point out the timestamped submission, review, and override transitions.

## Judge-facing explanation

- **Challenge 1 — Last-Minute Campus Chaos Re-Router:** A College officer triggers a real event; the backend identifies impacted records, selectively recomputes them, and emits stakeholder-specific before/after diffs.
- **Challenge 2 — Decision Explainability & Appeal Trail:** Students can inspect the rules and evidence behind their result, dispute it, and follow its status. College can review and apply a reasoned human override. Transitions are timestamped and persisted locally in `appeal_audit.json`.

## Reliability and honesty checklist

- Use a real Gemini API key and verify the app health before the demo.
- Never claim a GitHub repository was verified unless the current implementation actually fetched and evaluated it. Describe project-proof evidence only to the extent shown by the app.
- Do not invent a changed candidate, alert, recompute count, or appeal outcome.
- Keep one person on the College portal and another on the Student portal if possible.
- Avoid restarting the server during the flow. Candidate and disruption stores are in memory; restarting loses those live demo records. Appeal and audit data are persisted in `appeal_audit.json`.
- If Gemini is unavailable, use already evaluated sample records and clearly distinguish previously generated results from a live Gemini run.
- Do not use a real student's private resume or personal data without permission.

## Reset between rehearsals

Stop the server and restart it to clear the in-memory candidate/disruption state. If a completely clean appeal/audit history is required, back up and then remove the local `appeal_audit.json` file before restarting. Do not do this during the judging session.
