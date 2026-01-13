import io
import json
import os
from typing import Dict, List, Tuple

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
        /* Base layout */
        .main {
            background: radial-gradient(circle at top, #020617 0, #020617 40%, #020617 100%);
            color: #e5e7eb;
        }
        section[data-testid="stSidebar"] {
            background-color: #020617;
            border-right: 1px solid #1f2937;
        }
        section[data-testid="stSidebar"] h1, 
        section[data-testid="stSidebar"] h2, 
        section[data-testid="stSidebar"] h3 {
            color: #f9fafb;
        }

        /* Title + description */
        .app-header h1 {
            font-size: 2.1rem;
            margin-bottom: 0.2rem;
        }
        .app-header p {
            margin-top: 0;
            color: #9ca3af;
            font-size: 0.95rem;
        }

        /* Candidate cards */
        .candidate-card {
            padding: 1.1rem 1.3rem;
            border-radius: 0.9rem;
            background: #020617;
            border: 1px solid #1f2937;
            box-shadow: 0 18px 35px rgba(15,23,42,0.75);
            margin-bottom: 1rem;
        }
        .candidate-card h3 {
            margin-top: 0;
            margin-bottom: 0.25rem;
            font-size: 1.02rem;
        }
        .candidate-card ul {
            margin: 0.35rem 0 0 1.1rem;
            padding: 0;
            font-size: 0.9rem;
        }
        .candidate-card li {
            margin-bottom: 0.12rem;
        }
        .score-badge {
            display: inline-block;
            padding: 0.15rem 0.55rem;
            border-radius: 999px;
            font-size: 0.8rem;
            background: #111827;
            border: 1px solid #4b5563;
            margin-left: 0.4rem;
        }
        .rank-pill {
            display: inline-block;
            padding: 0.15rem 0.6rem;
            border-radius: 999px;
            font-size: 0.8rem;
            background: #1d4ed8;
            color: #e5e7eb;
            margin-right: 0.4rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def build_prompt(job_description: str, resume_filename: str) -> str:
    """Build the per-resume evaluation prompt sent to the LLM."""
    return f"""
You are an expert tech recruiter.

Task:
Given a job description and ONE candidate's resume (attached as a PDF), evaluate how well this candidate fits the role purely from a technical perspective.

Focus rules:
- Focus ONLY on technical skills, work experience, and concrete project experience.
- Ignore non-technical parts of the JD (benefits, company description, perks, HR boilerplate, etc.).
- Use your knowledge to detect implicit/related skills (e.g., strong Postgres/MySQL → SQL; PySpark/Spark → data engineering; Pandas/NumPy → Python analytics).
- Do NOT guess skills that are not clearly implied by the resume.
 - You already have access to the full text of the attached PDF. NEVER say you cannot parse the PDF. NEVER ask the user to resend the resume text.

Scoring rubric (0–100):
- 0–20: Almost no overlap with JD tech stack.
- 21–40: Some overlap but many core requirements missing.
- 41–60: Partial match; several important must-haves missing or shallow.
- 61–80: Solid match; most core requirements present with reasonable depth.
- 81–90: Strong match; nearly all core requirements present with good depth and relevant projects.
- 91–100: Exceptional match; deep experience with almost all core requirements and very relevant projects. Reserve scores above 95 for truly outstanding fits.

Must-have handling:
- If at least ONE clearly required core skill from the JD is missing → cap score at 60.
- If SEVERAL clearly required core skills are missing → cap score at 40.
- If NONE of the core tech stack appears in the resume → cap score at 20.

Output JSON schema:
Return STRICT JSON only (no extra text, no code fences, no commentary). Even if the resume is very short or partially unreadable, you MUST still return valid JSON using this schema and set an appropriate low score with clear gaps.

Keys:
- "candidate_name": string, from the resume if possible; otherwise use a clean version of the filename (no extension).
- "score": integer 0–100 following the rubric above.
- "one_line_reason": one short sentence: why this score vs this JD.
- "seniority": short phrase, e.g. "Senior backend engineer (7y)".
- "recency": short phrase about recency of relevant work, e.g. "Most relevant work 2022–2024".
- "top_tech": short list of key technologies (array of strings).
- "key_projects": 1–3 short project phrases (array of strings).
- "key_gaps": list of important missing things vs the JD (array of strings).
- "match_summary": one short sentence summarizing overall technical fit.

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
    data.setdefault("top_tech", [])
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
- "top_tech": array of strings
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
            model="gpt-4o",
            input=[{"role": "user", "content": [{"type": "input_text", "text": repair_prompt}]}],
            # response_format={"type": "json_object"},
        )
    except Exception:
        return {}

    repaired_text = _extract_text_from_response(repair_response)
    repaired = _parse_json_safe(repaired_text, fallback_name)
    return repaired


def evaluate_resume(
    client: OpenAI, job_description: str, upload, debug_raw: bool = False
) -> Dict:
    """Upload one PDF and call the LLM to score that resume (pass 1)."""
    file_bytes = upload.getvalue()
    buffer = io.BytesIO(file_bytes)
    buffer.name = upload.name

    uploaded_file = client.files.create(file=buffer, purpose="assistants")
    prompt = build_prompt(job_description, upload.name)

    response = client.responses.create(
        model="gpt-4o",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_file", "file_id": uploaded_file.id},
                ],
            }
        ],
        # If your SDK supports response_format, you can add it back for stricter JSON:
        # response_format={"type": "json_object"},
    )

    content = _extract_text_from_response(response)
    parsed = _parse_json_safe(content, upload.name)

    # If parsing clearly failed (default structure), try a one-shot JSON repair
    # using the model itself before giving up.
    is_default = (
        parsed.get("score", 0) == 0
        and parsed.get("one_line_reason") == "No one-line reason returned."
    )
    repaired = {}
    if is_default and content.strip():
        repaired = _repair_json_with_llm(client, content, upload.name)
        # If repair produced something more informative, prefer it
        if repaired.get("score", 0) != 0 or repaired.get(
            "one_line_reason"
        ) != "No one-line reason returned.":
            parsed = repaired

    # Optional debug: show raw model output (and repaired JSON if any)
    if debug_raw or is_default:
        st.markdown(f"**Debug: raw LLM output for `{upload.name}`**")
        st.code(content or "<empty content>")
        if repaired:
            st.markdown("**Debug: repaired JSON candidate**")
            st.code(json.dumps(repaired, indent=2))

    return parsed


def _build_candidate_summary(candidate_id: str, data: Dict) -> str:
    """Create a compact one-line summary for pass 2 re-ranking."""
    name = data.get("candidate_name", "Unknown")
    score = data.get("score", 0)
    one_line = data.get("one_line_reason", "") or data.get("match_summary", "")
    seniority = data.get("seniority", "")
    recency = data.get("recency", "")
    top_tech = ", ".join(data.get("top_tech", []))
    key_projects = "; ".join(data.get("key_projects", []))
    key_gaps = "; ".join(data.get("key_gaps", []))

    return (
        f"id={candidate_id} | name={name} | score={score} | "
        f"reason={one_line} | seniority={seniority} | recency={recency} | "
        f"top_tech={top_tech} | key_projects={key_projects} | key_gaps={key_gaps}"
    )


def _build_rerank_prompt(job_description: str, summaries_text: str) -> str:
    """Build the prompt used for pass 2: relative re-ranking from summaries."""
    return f"""
You are an expert technical recruiter.

Task:
Given a job description and a list of candidate summaries, produce a FINAL ranking of candidates for this single role.

Each candidate summary line has:
- an id,
- candidate name,
- the initial technical score (0–100) from a previous evaluation,
- a short reason for that score,
- seniority and recency hints,
- key technologies,
- key projects,
- key gaps vs. the job description.

Instructions:
- Treat candidates as COMPETING for ONE open role.
- Compare candidates AGAINST EACH OTHER, not in isolation.
- Use the initial score as a signal, but you may adjust it for relative comparison.
- Prefer candidates whose skills and experience best match the job’s core technical requirements and seniority level.
- Consider coverage of must-have skills, depth and recency of experience, and alignment with the problem space.
- If multiple candidates are very similar, you may keep scores close and differentiate ranks by small adjustments.
 - You ONLY see these summaries, not the original PDFs. NEVER ask for PDFs or additional resume text. If a summary contains almost no information, still return an entry with final_score=0 and a clear rerank_reason like "Summary contained insufficient information to evaluate.".

Output JSON schema:
Return STRICT JSON only (no extra text, no code fences, no commentary) with a single key:
- "candidates": array of objects, sorted from BEST to WORST fit, each object with:
  - "id": candidate id from the summaries.
  - "candidate_name": candidate name.
  - "final_score": integer 0–100 (your adjusted score for relative ranking).
  - "rank": integer rank (1 = best).
  - "rerank_reason": one or two very short phrases explaining this candidate’s placement relative to others.

Job Description:
\"\"\"{job_description}\"\"\"

Candidate summaries:
{summaries_text}
""".strip()


def _rerank_candidates(
    client: OpenAI, job_description: str, evaluated: List[Dict], debug_raw: bool = False
) -> List[Dict]:
    """Second pass: re-rank candidates based on compact summaries."""
    if not evaluated:
        return []

    # Assign stable ids if not present
    indexed = []
    summaries = []
    for idx, data in enumerate(evaluated, start=1):
        candidate_id = str(data.get("id", idx))
        data["id"] = candidate_id
        indexed.append(data)
        summaries.append(_build_candidate_summary(candidate_id, data))

    summaries_text = "\n".join(summaries)
    prompt = _build_rerank_prompt(job_description, summaries_text)

    response = client.responses.create(
        model="gpt-4o",
        input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        # response_format={"type": "json_object"},
    )

    content = _extract_text_from_response(response)
    if debug_raw:
        st.markdown("**Debug: raw LLM output for re-ranking call**")
        st.code(content or "<empty content>")

    data = _parse_json_generic(content)
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        # Fallback: sort by initial score only
        fallback_sorted = sorted(
            indexed, key=lambda item: item.get("score", 0), reverse=True
        )
        for rank, row in enumerate(fallback_sorted, start=1):
            row["final_score"] = row.get("score", 0)
            row["final_rank"] = rank
            row["rerank_reason"] = "Ranked by initial technical score only."
        return fallback_sorted

    # Map id -> initial candidate data
    by_id = {c["id"]: c for c in indexed}
    merged: List[Dict] = []
    for entry in candidates:
        cid = str(entry.get("id", ""))
        base = by_id.get(cid)
        if not base:
            continue
        merged_row = dict(base)
        merged_row["final_score"] = entry.get("final_score", base.get("score", 0))
        merged_row["final_rank"] = entry.get("rank", 0)
        merged_row["rerank_reason"] = entry.get(
            "rerank_reason", "No additional re-ranking reason provided."
        )
        merged.append(merged_row)

    # Ensure sorted by final_rank
    merged.sort(key=lambda r: r.get("final_rank", 0) or 1_000_000)
    return merged


def rank_resumes(
    client: OpenAI, job_description: str, uploads, debug_raw: bool = False
):
    """
    Process all uploaded PDFs with two-pass ranking.

    We:
    - Score every resume independently (Stage 1).
    - Globally re-rank only the strongest candidates, capped at 100 (Stage 2).
    """
    evaluated: List[Dict] = []
    for idx, upload in enumerate(uploads, start=1):
        result = evaluate_resume(client, job_description, upload, debug_raw=debug_raw)
        result["id"] = str(idx)
        evaluated.append(result)

    # If there are more than 100 candidates, keep only the top 100 by
    # initial technical score for the comparative re-ranking step.
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
        top_tech = row.get("top_tech", [])
        key_projects = row.get("key_projects", [])
        key_gaps = row.get("key_gaps", [])

        tech_str = ", ".join(top_tech) if top_tech else "—"
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
            <li><b>Top tech:</b> {tech_str}</li>
            <li><b>Key projects:</b> {project_str}</li>
            <li><b>Key gaps vs JD:</b> {gaps_str}</li>
          </ul>
        </div>
        """
        st.markdown(html, unsafe_allow_html=True)


def main():
    st.set_page_config(page_title="Resume Ranker", page_icon="📄", layout="wide")
    _inject_custom_css()

    st.sidebar.title("Resume Ranker")
    st.sidebar.markdown(
        "- Paste a job description\n"
        "- Upload multiple PDF resumes\n"
        "- Get technical scores and a final competitive ranking"
    )

    st.markdown(
        '<div class="app-header"><h1>Resume Ranker</h1>'
        "<p>Upload candidate PDFs and a job description to generate LLM-powered, "
        "technical-fit scores and a competitive ranking across all resumes.</p></div>",
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

        # Save CSV to disk so login_breezy can use it
        tmp_csv_path = os.path.join("resume_pdfs", "uploaded_candidates.csv")
        os.makedirs(os.path.dirname(tmp_csv_path), exist_ok=True)
        with open(tmp_csv_path, "wb") as f:
            f.write(csv_file.getvalue())

        # Clear old PDFs
        pdf_dir = "resume_pdfs"
        for fname in os.listdir(pdf_dir):
            if fname.lower().endswith(".pdf"):
                try:
                    os.remove(os.path.join(pdf_dir, fname))
                except OSError:
                    pass

        client = OpenAI(api_key=api_key)
        with st.spinner("Downloading resumes from Breezy and scoring..."):
            # Download all resumes using Playwright script
            login_breezy.download_resumes_from_csv(tmp_csv_path, headless=True)

            # Collect downloaded PDFs as in-memory uploads
            uploads = []
            for fname in os.listdir(pdf_dir):
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
                uploads.append(buffer)

            ranked = rank_resumes(client, jd, uploads, debug_raw=debug_raw)
        render_results(ranked)


if __name__ == "__main__":
    main()
