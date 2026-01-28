import io
import json
import os
from typing import Dict, List, Tuple
import csv

from openai import OpenAI
import streamlit as st
from dotenv import load_dotenv

import login_breezy

load_dotenv()


def _inject_custom_css() -> None:
    """Inject custom CSS to make the UI feel more polished."""
    st.markdown(
        """
        <style>
        /* Use Streamlit theme variables so it works in light AND dark mode */
        :root {
            --bg: var(--st-color-background);
            --bg2: var(--st-color-secondary-background);
            --text: var(--st-color-text);
            --muted: color-mix(in srgb, var(--text) 65%, transparent);
            --border: color-mix(in srgb, var(--text) 15%, transparent);
        }

        .main {
            background: var(--bg) !important;
            color: var(--text) !important;
        }

        section[data-testid="stSidebar"] {
            background: var(--bg2) !important;
            border-right: 1px solid var(--border) !important;
        }

        section[data-testid="stSidebar"] h1,
        section[data-testid="stSidebar"] h2,
        section[data-testid="stSidebar"] h3,
        section[data-testid="stSidebar"] p,
        section[data-testid="stSidebar"] span,
        section[data-testid="stSidebar"] div {
            color: var(--text) !important;
        }

        /* Title + description */
        .app-header h1 {
            font-size: 2.1rem;
            margin-bottom: 0.2rem;
            color: var(--text) !important;
        }
        .app-header p {
            margin-top: 0;
            color: var(--muted) !important;
            font-size: 0.95rem;
        }

        /* Candidate cards */
        .candidate-card {
            padding: 1.1rem 1.3rem;
            border-radius: 0.9rem;
            background: var(--bg2) !important;
            border: 1px solid var(--border) !important;
            box-shadow: 0 18px 35px rgba(0,0,0,0.08);
            margin-bottom: 1rem;
            color: var(--text) !important;
        }
        .candidate-card h3 {
            margin-top: 0;
            margin-bottom: 0.25rem;
            font-size: 1.02rem;
            color: var(--text) !important;
        }
        .candidate-card ul {
            margin: 0.35rem 0 0 1.1rem;
            padding: 0;
            font-size: 0.9rem;
            color: var(--text) !important;
        }

        .score-badge {
            display: inline-block;
            padding: 0.15rem 0.55rem;
            border-radius: 999px;
            font-size: 0.8rem;
            background: var(--bg) !important;
            border: 1px solid var(--border) !important;
            color: var(--text) !important;
            margin-left: 0.4rem;
        }

        .rank-pill {
            display: inline-block;
            padding: 0.15rem 0.6rem;
            border-radius: 999px;
            font-size: 0.8rem;
            background: #2563eb; /* fallback */
            background: color-mix(in srgb, #2563eb 80%, var(--bg)) !important;
            color: white !important;
            margin-right: 0.4rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

def _responses_create_json(client: OpenAI, model: str, content: List[Dict], temperature: float = 0):
    """
    Wrapper for responses.create() without response_format
    (SDK compatibility-safe).
    """
    return client.responses.create(
        model=model,
        input=[{"role": "user", "content": content}],
        temperature=temperature,
    )

def build_jd_requirements_prompt(job_description: str) -> str:
    return f"""
You are analyzing a job description.

Task:
Extract what this role ACTUALLY prioritizes.

Return STRICT JSON with:
- "core_competencies": ordered list of the most important competencies (e.g., statistical reasoning, experimentation, BI, ML, backend engineering, stakeholder reporting)
- "must_haves": list of truly required skills/experiences
- "nice_to_haves": list of secondary or optional skills
- "role_type": short phrase describing the role archetype (e.g., "statistical product data scientist", "BI-focused analyst", "ML engineer")

Job Description:
\"\"\"{job_description}\"\"\"
""".strip()


def build_location_gate_prompt() -> str:
    return """
You are extracting a candidate's current (or most recent) US state from a resume PDF.

Task:
Decide if the candidate should be INCLUDED based on whether their current/most recent US state (normalized to a 2-letter abbreviation) is one of:
CT, ME, MA, NH, RI, VT, NY.

Rules:
- Prefer the location in the header/contact area near the top of the resume.
- If not present, use the location from the most recent entry between:
  work experience and education (whichever has the latest date).
- Do NOT guess. If unclear or non-US, set allow=false.

Output:
Return STRICT JSON only:
{
  "allow": boolean,
  "reason": string (brief explanation, e.g. "Boston, MA" or "Most recent role located in NY" or "No location found")
}
""".strip()


def build_prompt(job_description: str, jd_requirements: Dict, resume_filename: str) -> str:
    """Build the per-resume evaluation prompt sent to the LLM."""
    return f"""
You are an expert recruiter and hiring manager.

Task:
Given a job description and ONE candidate's resume (attached as a PDF), evaluate how well this candidate fits the role purely from a job-requirements perspective.

Evaluation rules (CRITICAL):
You MUST evaluate this resume ONLY against the job requirements provided below.
These requirements were extracted directly from the job description and are the
authoritative definition of what this role prioritizes.

Job requirements:
- Core competencies: {jd_requirements.get("core_competencies", [])}
- Must-haves: {jd_requirements.get("must_haves", [])}
- Nice-to-haves: {jd_requirements.get("nice_to_haves", [])}
- Role type: {jd_requirements.get("role_type", "")}

Rules:
- Base the score primarily on evidence for the core competencies and must-haves above.
- Do NOT reward skills, tools, accomplishments, or experience that are not relevant to these requirements.
- Depth and quality of evidence matters more than breadth of unrelated experience.
- If the resume is strong in areas not emphasized above, do NOT increase the score for those areas.

Scoring rubric (0–100):
- 0–20: Almost no overlap with the JD’s core requirements.
- 21–40: Some overlap but many core requirements missing.
- 41–60: Partial match; several important must-haves missing or shallow.
- 61–80: Solid match; most core requirements present with reasonable depth.
- 81–90: Strong match; nearly all core requirements present with good depth and relevant projects.
- 91–100: Exceptional match; deep experience with almost all core requirements and very relevant projects. Reserve scores above 95 for truly outstanding fits.

Must-have handling:
- Identify the JD’s must-haves.
- Treat a must-have as SATISFIED if the resume shows the skill explicitly OR shows clearly equivalent evidence (synonyms/near-equivalents).
  Example: “Figma” counts for “Figma developer” if the resume indicates real usage/ownership (design systems, prototypes, UI/UX design work) — do not require the exact word “developer”.
- Do NOT invent skills. If the resume only lists a keyword with no context, treat it as weak evidence (partial), not missing.
- If at least ONE clearly required must-have is truly missing (no evidence at all, direct or equivalent): cap score at 60.
- If SEVERAL clearly required must-haves are truly missing: cap score at 40.
- If NONE of the JD’s core requirements appear in the resume: cap score at 20.

Output JSON schema:
Return STRICT JSON only (no extra text, no code fences, no commentary). Even if the resume is very short or partially unreadable, you MUST still return valid JSON using this schema and set an appropriate low score with clear gaps.

Keys:
- "candidate_name": string, from the resume if possible; otherwise use a clean version of the filename (no extension).
- "score": integer 0–100 following the rubric above.
- "one_line_reason": one short sentence: why this score vs this JD.
- "seniority": short phrase, e.g. "Senior (7y)" or "Mid-level (3y)".
- "recency": short phrase about recency of relevant work, e.g. "Most relevant work 2022–2024".
- "top_skills": short list of the most relevant skills/qualifications for this role (array of strings).
- "key_projects": 1–3 short project phrases (array of strings).
- "key_gaps": list of important missing things vs the JD (array of strings).
- "match_summary": one short sentence summarizing overall fit for this role.

Job Description:
\"\"\"{job_description}\"\"\"

The candidate's resume is attached as a PDF file. Its filename is: {resume_filename}
""".strip()


def _extract_text_from_response(response) -> str:
    """Try to robustly pull text out of various response shapes."""
    candidates = []
    for path in (
        lambda r: getattr(r, "output_text", None),
        lambda r: getattr(r, "output", None)
        and getattr(r.output[0], "content", None)
        and getattr(r.output[0].content[0], "text", None),
        lambda r: r.get("output_text") if isinstance(r, dict) else None,
        lambda r: r.get("output", [{}])[0]
        .get("content", [{}])[0]
        .get("text")
        if isinstance(r, dict)
        else None,
    ):
        try:
            val = path(response)
            if val:
                candidates.append(val)
        except Exception:
            continue
    return candidates[0] if candidates else ""


def _parse_json_safe(raw_text: str, fallback_name: str) -> Dict:
    """Parse JSON for a single candidate, tolerating extra text/code fences."""
    text = raw_text.strip()
    # Strip code fences if present
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Attempt to extract JSON substring
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                data = json.loads(snippet)
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}

    if not isinstance(data, dict):
        data = {}

    # Fill defaults so downstream UI never breaks
    data.setdefault("candidate_name", fallback_name)
    data.setdefault("score", 0)
    data.setdefault("one_line_reason", "No one-line reason returned.")
    data.setdefault("seniority", "")
    data.setdefault("recency", "")
    data.setdefault("top_skills", [])
    data.setdefault("key_projects", [])
    data.setdefault("key_gaps", [])
    data.setdefault("match_summary", "")
    return data


def _parse_json_generic(raw_text: str) -> Dict:
    """Parse generic JSON (e.g., for batch re-ranking), tolerant of extra text."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                data = json.loads(snippet)
            except json.JSONDecodeError:
                data = {}
        else:
            data = {}
    if not isinstance(data, dict):
        data = {}
    return data


def _repair_json_with_llm(
    client: OpenAI, raw_text: str, fallback_name: str
) -> Dict:
    """
    Attempt to repair a non-JSON response by asking the model to rewrite
    its previous output as strict JSON matching the expected schema.
    """
    if not raw_text.strip():
        return {}

    schema_description = """
Return STRICT JSON only (no extra text, no code fences, no commentary) with keys:
- "candidate_name": string
- "score": integer 0–100
- "one_line_reason": short sentence
- "seniority": short phrase
- "recency": short phrase
- "top_skills": array of strings
- "key_projects": array of strings
- "key_gaps": array of strings
- "match_summary": short sentence
""".strip()

    repair_prompt = f"""
The following was your previous response when asked to evaluate a resume, but it was not valid JSON:

```text
{raw_text}
```

Rewrite this information as STRICT JSON ONLY, matching the following schema. Do not add any extra commentary, code fences, or explanations.

Schema:
{schema_description}
""".strip()

    try:
        repair_response = client.responses.create(
            model="gpt-5.2",
            input=[{"role": "user", "content": [{"type": "input_text", "text": repair_prompt}]}],
            temperature=0,
            # response_format={"type": "json_object"},
        )
    except Exception:
        return {}

    repaired_text = _extract_text_from_response(repair_response)
    repaired = _parse_json_safe(repaired_text, fallback_name)
    return repaired

def location_gate(
    client: OpenAI, file_id: str, debug_raw: bool = False, resume_filename: str = ""
) -> Dict:
    prompt = build_location_gate_prompt()

    try:
        resp = _responses_create_json(
            client,
            model="gpt-5.2",
            content=[
                {"type": "input_text", "text": prompt},
                {"type": "input_file", "file_id": file_id},
            ],
            temperature=0,
        )
        raw = _extract_text_from_response(resp)
        data = _parse_json_generic(raw)
    except Exception as e:
        raw = f"<location_gate_error> {e}"
        data = {}

    allow = data.get("allow", None)
    reason = (data.get("reason") or "").strip()

    # If anything went wrong, DO NOT silently reject. Mark as "unknown" and keep.
    # You can flip this behavior if you want strict filtering.
    if allow is None:
        allow = True
        reason = reason or "Location unclear (gate parse failed) — allowed by fail-open policy."

    if not isinstance(allow, bool):
        allow = True
        reason = reason or "Location unclear (invalid allow type) — allowed by fail-open policy."

    if debug_raw:
        st.markdown(f"**Debug: location gate output for `{resume_filename}`**")
        st.code(raw or "<empty>")

    return {"allow": allow, "reason": reason or "No reason provided."}


def evaluate_resume_with_file_id(
    client: OpenAI,
    job_description: str,
    jd_requirements: Dict,
    file_id: str,
    resume_filename: str,
    debug_raw: bool = False,
) -> Dict:
    prompt = build_prompt(job_description, jd_requirements, resume_filename)

    try:
        response = _responses_create_json(
            client,
            model="gpt-5.2",
            content=[
                {"type": "input_text", "text": prompt},
                {"type": "input_file", "file_id": file_id},
            ],
            temperature=0,
        )
        content = _extract_text_from_response(response)
        parsed = _parse_json_safe(content, resume_filename)
    except Exception as e:
        content = f"<evaluate_error> {e}"
        parsed = _parse_json_safe("{}", resume_filename)
        parsed["one_line_reason"] = f"Evaluation error: {e}"
        parsed["key_gaps"] = ["Evaluation error"]

    if debug_raw:
        st.markdown(f"**Debug: raw LLM output for `{resume_filename}`**")
        st.code(content or "<empty content>")

    return parsed



def _build_candidate_summary(candidate_id: str, data: Dict) -> str:
    """Create a compact one-line summary for pass 2 re-ranking."""
    name = data.get("candidate_name", "Unknown")
    score = data.get("score", 0)
    one_line = data.get("one_line_reason", "") or data.get("match_summary", "")
    seniority = data.get("seniority", "")
    recency = data.get("recency", "")
    top_skills = ", ".join(data.get("top_skills", []))
    key_projects = "; ".join(data.get("key_projects", []))
    key_gaps = "; ".join(data.get("key_gaps", []))

    return (
        f"id={candidate_id} | name={name} | score={score} | "
        f"reason={one_line} | seniority={seniority} | recency={recency} | "
        f"top_skills={top_skills} | key_projects={key_projects} | key_gaps={key_gaps}"
    )


def _build_rerank_prompt(job_description: str, summaries_text: str) -> str:
    return f"""
        You are an expert recruiter and hiring manager.

        Task:
        Given a job description and candidate summaries, produce the FINAL ranked list for ONE role.

        Rules:
        - Rank candidates AGAINST EACH OTHER (comparative).
        - Stay STRICTLY aligned to the job description.
        - Prefer evidence-backed fit: must-haves coverage, depth, recency, relevance.
        - Do NOT reward impressive but irrelevant experience.

        Output STRICT JSON only with exactly:
        {{
        "ranked": [
            {{
            "id": "candidate_id",
            "final_score": 0-100 integer,
            "why": "one short sentence why this rank"
            }}
        ]
        }}

        Constraints:
        - Include EVERY id exactly once.
        - Sort best -> worst.
        - final_score must be monotonic non-increasing down the list (ties allowed).

        Job Description:
        \"\"\"{job_description}\"\"\"

        Candidate summaries:
        {summaries_text}
        """.strip()



def _rerank_candidates(
    client: OpenAI, job_description: str, evaluated: List[Dict], debug_raw: bool = False
) -> List[Dict]:
    if not evaluated:
        return []

    indexed = []
    summaries = []
    for data in evaluated:
        candidate_id = data["filename"]
        data["id"] = str(candidate_id)
        indexed.append(data)
        summaries.append(_build_candidate_summary(candidate_id, data))

    summaries_text = "\n".join(summaries)
    prompt = _build_rerank_prompt(job_description, summaries_text)

    try:
        response = _responses_create_json(
            client,
            model="gpt-5.2",
            content=[{"type": "input_text", "text": prompt}],
            temperature=0,
        )
        content = _extract_text_from_response(response)
        if debug_raw:
            st.markdown("**Debug: raw LLM output for re-ranking call**")
            st.code(content or "<empty content>")

        data = _parse_json_generic(content)
        ranked = data.get("ranked", [])
    except Exception as e:
        ranked = []
        content = f"<rerank_error> {e}"
        if debug_raw:
            st.markdown("**Debug: rerank error**")
            st.code(content)

    # Fallback if rerank fails: sort by Stage-1 score
    if not isinstance(ranked, list) or not ranked:
        fallback_sorted = sorted(indexed, key=lambda item: item.get("score", 0), reverse=True)
        for rank, row in enumerate(fallback_sorted, start=1):
            row["final_rank"] = rank
            row["final_score"] = row.get("score", 0)
            row["rerank_reason"] = "Rerank failed; ranked by initial role-fit score."
        return fallback_sorted

    by_id = {str(c["id"]): c for c in indexed}
    merged = []
    seen = set()

    for rank_pos, item in enumerate(ranked, start=1):
        cid = str(item.get("id", ""))
        if not cid or cid in seen:
            continue
        base = by_id.get(cid)
        if not base:
            continue

        seen.add(cid)
        row = dict(base)
        row["final_rank"] = rank_pos
        row["final_score"] = int(item.get("final_score", row.get("score", 0)) or row.get("score", 0))
        row["rerank_reason"] = (item.get("why") or "Re-ranked by relative fit.").strip()
        merged.append(row)

    # Append any missing deterministically
    missing = [by_id[cid] for cid in by_id.keys() if cid not in seen]
    missing_sorted = sorted(missing, key=lambda r: r.get("score", 0), reverse=True)
    start_rank = len(merged) + 1
    for i, base in enumerate(missing_sorted, start=start_rank):
        row = dict(base)
        row["final_rank"] = i
        row["final_score"] = row.get("score", 0)
        row["rerank_reason"] = "Missing from rerank output; appended by initial score."
        merged.append(row)

    return merged



def rank_resumes(
    client: OpenAI, job_description: str, uploads, debug_raw: bool = False
):
    # ---- PRE-STAGE: extract JD requirements ----
    jd_req_prompt = build_jd_requirements_prompt(job_description)

    jd_resp = _responses_create_json(
        client,
        model="gpt-5.2",
        content=[{"type": "input_text", "text": jd_req_prompt}],
        temperature=0,
    )

    jd_raw = _extract_text_from_response(jd_resp)
    jd_requirements = _parse_json_generic(jd_raw)

    # Hard defaults so prompts are stable even if JD extraction is weird
    jd_requirements.setdefault("core_competencies", [])
    jd_requirements.setdefault("must_haves", [])
    jd_requirements.setdefault("nice_to_haves", [])
    jd_requirements.setdefault("role_type", "")

    evaluated: List[Dict] = []
    skipped: List[Dict] = []

    for idx, upload in enumerate(uploads, start=1):
        file_bytes = upload.getvalue()
        buffer = io.BytesIO(file_bytes)
        buffer.name = upload.name

        # Upload ONCE, reuse file_id for gate + scoring
        uploaded_file = client.files.create(file=buffer, purpose="assistants")
        file_id = uploaded_file.id

        # ---- STAGE 0: location gate ----
        gate = location_gate(
            client,
            file_id=file_id,
            debug_raw=debug_raw,
            resume_filename=upload.name,
        )

        if not gate.get("allow", False):
            skipped.append(
                {
                    "filename": upload.name,
                    "gate_reason": gate.get("reason", "Rejected by location gate."),
                }
            )
            continue

        # ---- STAGE 1: score only allowed resumes ----
        result = evaluate_resume_with_file_id(
            client,
            job_description=job_description,
            jd_requirements=jd_requirements,
            file_id=file_id,
            resume_filename=upload.name,
            debug_raw=debug_raw,
        )
        result["filename"] = upload.name
        result["id"] = upload.name  # stable id
        result["resume_url"] = getattr(upload, "resume_url", None)
        result["resume_bytes"] = upload.getvalue()
        evaluated.append(result)

    # UI note: show how many were filtered out
    if skipped:
        st.info(f"Location filter: kept {len(evaluated)} resumes, rejected {len(skipped)} resumes.")
        with st.expander("See rejected resumes (location filter)"):
            for s in skipped:
                st.write(f"- {s['filename']}: {s['gate_reason']}")

    if not evaluated:
        return []

    # ---- Stage 2: rerank as before (top 100 only) ----
    if len(evaluated) > 100:
        evaluated.sort(key=lambda r: r.get("score", 0), reverse=True)
        top_evaluated = evaluated[:100]
    else:
        top_evaluated = evaluated

    ranked = _rerank_candidates(
        client, job_description, top_evaluated, debug_raw=debug_raw
    )
    return ranked



def render_results(rows: List[Dict]):
    """Display ranking results in Streamlit."""
    if not rows:
        return

    for row in rows:
        name = row.get("candidate_name", "N/A")
        final_rank = row.get("final_rank", 0)
        final_score = row.get("final_score", row.get("score", 0))
        initial_score = row.get("score", 0)
        one_line_reason = row.get("one_line_reason", "")
        match_summary = row.get("match_summary", "")
        rerank_reason = row.get("rerank_reason", "")
        seniority = row.get("seniority", "")
        recency = row.get("recency", "")
        top_skills = row.get("top_skills", [])
        key_projects = row.get("key_projects", [])
        key_gaps = row.get("key_gaps", [])

        skills_str = ", ".join(top_skills) if top_skills else "—"
        project_str = "; ".join(p for p in key_projects if p) if key_projects else "—"
        gaps_str = "; ".join(g for g in key_gaps if g) if key_gaps else "—"

        html = f"""
        <div class="candidate-card">
          <h3>
            <span class="rank-pill">#{final_rank}</span>
            {name}
            <span class="score-badge">Final: {final_score}/100 · Initial: {initial_score}/100</span>
          </h3>
          <ul>
            <li><b>Initial one-line reason:</b> {one_line_reason or "—"}</li>
            <li><b>Initial match summary:</b> {match_summary or "—"}</li>
            <li><b>Re-ranking reason:</b> {rerank_reason or "—"}</li>
            <li><b>Seniority:</b> {seniority or "—"}</li>
            <li><b>Recency of relevant work:</b> {recency or "—"}</li>
            <li><b>Top skills:</b> {skills_str}</li>
            <li><b>Key projects:</b> {project_str}</li>
            <li><b>Key gaps vs JD:</b> {gaps_str}</li>

          </ul>
        </div>
        """
        st.markdown(html, unsafe_allow_html=True)
        # ---- Resume download button (Railway-safe) ----
        if row.get("resume_bytes"):
            st.download_button(
                label="Get Candidate Info (PDF)",
                data=row["resume_bytes"],
                file_name=row["filename"],
                mime="application/pdf",
                key=f"download-{row['filename']}"
            )


# To download rankings in a pdf
def build_rankings_pdf_bytes_like_streamlit(job_description: str, rows: List[Dict]) -> bytes:
    """
    Build a PDF by rendering Streamlit-like HTML cards using Playwright's PDF printer.
    """
    jd_safe = (job_description or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    jd_safe = jd_safe.replace("\n", "<br/>")

    # Reuse the same card styling concept (tweaked for white PDF background)
    css = """
    <style>
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        margin: 32px;
        color: #111827;
        background: #ffffff;
      }
      h1 {
        font-size: 22px;
        margin: 0 0 12px 0;
      }
      h2 {
        font-size: 16px;
        margin: 18px 0 8px 0;
      }
      a {
        color: #1d4ed8;
        text-decoration: underline;
      }
      .muted {
        color: #374151;
        font-size: 12.5px;
        line-height: 1.5;
      }

      .candidate-card {
        padding: 14px 16px;
        border-radius: 12px;
        background: #ffffff;
        border: 1px solid #e5e7eb;
        box-shadow: 0 10px 18px rgba(0,0,0,0.06);
        margin-bottom: 12px;
      }
      .candidate-card h3 {
        margin: 0 0 8px 0;
        font-size: 14px;
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
      }
      .rank-pill {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 12px;
        background: #1d4ed8;
        color: #ffffff;
      }
      .score-badge {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 999px;
        font-size: 12px;
        background: #f3f4f6;
        border: 1px solid #e5e7eb;
        color: #111827;
      }
      ul {
        margin: 0;
        padding-left: 18px;
        font-size: 12.5px;
        line-height: 1.45;
      }
      li { margin: 4px 0; }
      b { color: #111827; }

      /* Ensure long text wraps nicely */
      .wrap {
        overflow-wrap: anywhere;
        word-break: break-word;
      }
    </style>
    """

    # Build cards using the same fields you show in Streamlit
    cards_html = []
    for row in rows:
        name = str(row.get("candidate_name", "N/A"))
        final_rank = row.get("final_rank", 0)
        final_score = row.get("final_score", row.get("score", 0))
        initial_score = row.get("score", 0)
        one_line_reason = row.get("one_line_reason", "") or "—"
        match_summary = row.get("match_summary", "") or "—"
        rerank_reason = row.get("rerank_reason", "") or "—"
        seniority = row.get("seniority", "") or "—"
        recency = row.get("recency", "") or "—"
        top_skills = ", ".join(row.get("top_skills", []) or []) or "—"
        key_projects = "; ".join([p for p in (row.get("key_projects", []) or []) if p]) or "—"
        key_gaps = "; ".join([g for g in (row.get("key_gaps", []) or []) if g]) or "—"
        resume_url = row.get("resume_url")
        

        def esc(s: str) -> str:
            return (
                s.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;")
            )

        cards_html.append(f"""
        <div class="candidate-card">
          <h3 class="wrap">
            <span class="rank-pill">#{final_rank}</span>
            {esc(name)}
            <span class="score-badge">Final: {final_score}/100 · Initial: {initial_score}/100</span>
          </h3>
          <ul class="wrap">
            <li><b>Initial one-line reason:</b> {esc(one_line_reason)}</li>
            <li><b>Initial match summary:</b> {esc(match_summary)}</li>
            <li><b>Re-ranking reason:</b> {esc(rerank_reason)}</li>
            <li><b>Seniority:</b> {esc(seniority)}</li>
            <li><b>Recency:</b> {esc(recency)}</li>
            <li><b>Top skills:</b> {esc(top_skills)}</li>
            <li><b>Key projects:</b> {esc(key_projects)}</li>
            <li><b>Key gaps:</b> {esc(key_gaps)}</li>
            <li>
                <b>Resume:</b>
                {f'<a href="{resume_url}">Get Candidate Info</a>' if resume_url else "—"}
            </li>
          </ul>
        </div>
        """)

    html = f"""
    <html>
      <head>
        <meta charset="utf-8" />
        {css}
      </head>
      <body>
        <h1>Resume Ranker – Final Rankings</h1>

        <h2>Job Description</h2>
        <div class="muted wrap">{jd_safe or "—"}</div>

        <h2>Ranked Candidates</h2>
        {''.join(cards_html) if cards_html else '<div class="muted">No candidates.</div>'}
      </body>
    </html>
    """

    # Use Playwright to print HTML to PDF bytes
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.set_content(html, wait_until="load")
        pdf_bytes = page.pdf(
            format="Letter",
            print_background=True,
            margin={"top": "0.5in", "right": "0.5in", "bottom": "0.5in", "left": "0.5in"},
        )
        browser.close()

    return pdf_bytes


def main():
    st.set_page_config(page_title="Resume Ranker", page_icon="📄", layout="wide")
    _inject_custom_css()
    
    st.sidebar.title("Resume Ranker")
    st.sidebar.markdown(
        "- Paste a job description\n"
        "- Upload multiple PDF resumes\n"
        "- Get resume scores and a final competitive ranking"
    )
    
    st.markdown(
        '<div class="app-header"><h1>Resume Ranker</h1>'
        "<p>Upload candidate PDFs and a job description to generate LLM-powered, "
        "role-fit scores and a competitive ranking across all resumes.</p></div>",
        unsafe_allow_html=True,
    )

    api_key = os.getenv("OPENAI_API_KEY", "")
    if api_key:
        st.success("OPENAI_API_KEY loaded from environment.")
    else:
        st.warning("Set environment variable OPENAI_API_KEY before ranking resumes.")

    col_left, col_right = st.columns([2, 1])
    with col_left:
        jd = st.text_area("Job description", height=260)
    with col_right:
        csv_file = st.file_uploader(
            "Upload candidates CSV (with Breezy resume URLs)",
            type=["csv"],
            accept_multiple_files=False,
        )
        debug_raw = st.checkbox("Show raw LLM output (debug)", value=False)
        breezy_cookie = st.text_area(
            "Breezy Cookie (paste full Cookie header value)",
            height=120,
            help="Paste from Chrome DevTools → Network → Request Headers → Cookie (the value only).",
        )

    if st.button("Rank resumes"):
        if not api_key:
            st.error("Missing OPENAI_API_KEY environment variable.")
            return
        if not jd:
            st.error("Paste a job description to continue.")
            return
        if not csv_file:
            st.error("Upload a candidates CSV file.")
            return
        if not breezy_cookie.strip():
            st.error("Paste your Breezy Cookie (Cookie header value) to download resumes.")
            return

        # Normalize CSV into an internal CSV with deterministic filenames
        tmp_csv_path = os.path.join("resume_pdfs", "uploaded_candidates.normalized.csv")
        os.makedirs(os.path.dirname(tmp_csv_path), exist_ok=True)

        csv_file.seek(0)
        decoded = csv_file.getvalue().decode("utf-8").splitlines()
        reader = csv.DictReader(decoded)

        normalized_rows = []
        resume_url_map = {}

        i = 0
        for row in reader:
            name = (row.get("name") or "").strip() or "candidate"
            url = (row.get("resume") or "").strip()

            # Skip rows that can't be downloaded
            if not url or not url.startswith("http"):
                continue

            i += 1
            filename = f"{name.replace(' ', '_')}__{i}.pdf"

            resume_url_map[filename] = url
            normalized_rows.append({"name": name, "resume": url, "filename": filename})

        # Write the normalized CSV that Breezy downloader will use
        with open(tmp_csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["name", "resume", "filename"])
            w.writeheader()
            w.writerows(normalized_rows)

        # Clear old PDFs
        pdf_dir = "resume_pdfs"
        for fname in sorted(os.listdir(pdf_dir)):
            if fname.lower().endswith(".pdf"):
                try:
                    os.remove(os.path.join(pdf_dir, fname))
                except OSError:
                    pass

        client = OpenAI(api_key=api_key)
        # --- DOWNLOAD (COOKIE AUTH, NO PLAYWRIGHT) ---
        st.markdown("### Downloading resumes")
        with st.spinner("Downloading resumes from Breezy (cookie auth)..."):
            login_breezy.download_resumes_from_csv_with_cookie(
                csv_path=tmp_csv_path,
                output_dir=pdf_dir,
                cookie_header_value=breezy_cookie.strip(),
            )
        st.success("Download successful")

        # --- PREPARE FILES ---
        uploads = []
        for fname in sorted(os.listdir(pdf_dir)):
            if not fname.lower().endswith(".pdf"):
                continue
            path = os.path.join(pdf_dir, fname)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            buffer = io.BytesIO(data)
            buffer.name = fname
            buffer.resume_url = resume_url_map.get(fname)
            uploads.append(buffer)

        # --- RANKING ---
        st.markdown("### Ranking resumes")
        with st.spinner("Ranking resumes..."):
            ranked = rank_resumes(client, jd, uploads, debug_raw=debug_raw)
            st.session_state["ranked"] = ranked
            st.session_state["jd"] = jd

    if "ranked" in st.session_state and st.session_state["ranked"]:
        st.markdown("---")
        pdf_bytes = build_rankings_pdf_bytes_like_streamlit(
            st.session_state.get("jd", ""),
            st.session_state["ranked"],
        )
        st.download_button(
            label="Download rankings as PDF",
            data=pdf_bytes,
            file_name="resume_rankings.pdf",
            mime="application/pdf",
            key="download-rankings-pdf"
        )
        st.markdown("---")
        render_results(st.session_state["ranked"])





if __name__ == "__main__":
    main()
