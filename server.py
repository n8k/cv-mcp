"""CV MCP server — Nathan Kemppainen.

Serves a CV as structured, read-only MCP tools, resources and a prompt, so any
MCP client (Copilot Studio, Claude, etc.) can query it conversationally.

Endpoints
  /mcp            read-only MCP (Streamable HTTP)
  /control/mcp    control MCP, only when CV_MCP_CONTROL_TOKEN is set (testing tier)
  /health         200 when the CV file parses, 503 otherwise

Environment
  CV_PATH                 CV file (default: cv.toml next to this file)
  CV_MCP_CONTROL_TOKEN    enables /control/mcp; clients send "Authorization: Bearer <token>"
  HOST, PORT              bind address (default 0.0.0.0:8000)
  CV_TRANSPORT=stdio      run over stdio instead of HTTP
"""

from __future__ import annotations

import hmac
import json
import os
import re
import tomllib
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount
from starlette.types import ASGIApp, Receive, Scope, Send


def cv_path() -> Path:
    return Path(os.environ.get("CV_PATH", Path(__file__).with_name("cv.toml")))


def load_cv() -> dict[str, Any]:
    """Re-read on every call so edits go live without a restart."""
    return tomllib.loads(cv_path().read_text(encoding="utf-8"))


# ---------- Placeholders ----------

def _is_placeholder(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper().startswith("TODO")


def _clean(obj: Any) -> Any:
    """Strip TODO placeholders so agents never present them as facts."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if not _is_placeholder(v)}
    if isinstance(obj, list):
        return [_clean(v) for v in obj if not _is_placeholder(v)]
    return obj


def _find_placeholders(obj: Any, path: str = "") -> list[str]:
    if _is_placeholder(obj):
        return [path or "<root>"]
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += _find_placeholders(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            label = v.get("id", i) if isinstance(v, dict) else i
            found += _find_placeholders(v, f"{path}[{label}]")
    return found


# ---------- Matching ----------

def _word_pattern(term: str) -> re.Pattern[str]:
    """Whole-word, case-insensitive match. Multi-word terms match as a phrase.

    Simple plurals match both ways: "stakeholder" finds "stakeholders" and
    "OKRs" finds "OKR". Lookarounds instead of \\b so terms like "C++" or
    ".NET" still work.
    """
    parts = term.split()
    last = parts[-1]
    if re.fullmatch(r"\w*[^s\W]s", last, re.IGNORECASE) and len(last) > 3:
        last = last[:-1]  # "stakeholders" -> "stakeholder", but not "process"
    stem = r"\s+".join(re.escape(p) for p in [*parts[:-1], last])
    suffix = "(?:e?s)?" if re.search(r"\w$", last) else ""
    return re.compile(rf"(?<!\w){stem}{suffix}(?!\w)", re.IGNORECASE)


def _count_matches(term: str, text: str) -> int:
    return len(_word_pattern(term).findall(text))


# ---------- Read-only server ----------

mcp = FastMCP(
    name="nate-kemppainen-cv",
    instructions=(
        "This server is Nathan (Nate) Kemppainen's CV. Use get_profile first, "
        "then the other tools to answer questions about his experience, skills "
        "and fit for roles. Only state what the tools return; if something is "
        "not in the CV, say so rather than guessing."
    ),
)

READ_ONLY = {"readOnlyHint": True}


@mcp.tool(annotations=READ_ONLY)
def get_profile() -> dict:
    """Name, headline, location, summary, languages, certifications, career highlights, education and contact details."""
    cv = _clean(load_cv())
    profile = dict(cv["person"])
    if cv.get("education"):
        profile["education"] = cv["education"]
    return profile


@mcp.tool(annotations=READ_ONLY)
def get_experience(company: str | None = None) -> list[dict]:
    """Work history, most recent first. Optionally filter by company name (case-insensitive substring)."""
    roles = load_cv()["experience"]
    if company:
        roles = [r for r in roles if company.lower() in r["company"].lower()]
    return _clean(roles)


@mcp.tool(annotations=READ_ONLY)
def get_skills(category: str | None = None) -> dict:
    """Skills grouped by category. Call without a category to see the category names, then optionally request one."""
    skills = _clean(load_cv()["skills"])
    if category:
        return {category: skills.get(category, [])}
    return skills


@mcp.tool(annotations=READ_ONLY)
def list_case_studies() -> list[dict]:
    """Short index of case studies (id, title, tags). Use get_case_study for the full STAR story."""
    return [
        {"id": c["id"], "title": c["title"], "tags": c.get("tags", [])}
        for c in load_cv()["case_studies"]
        if not _is_placeholder(c.get("title"))
    ]


@mcp.tool(annotations=READ_ONLY)
def get_case_study(case_id: str) -> dict:
    """Full Situation / Task / Action / Result story for one case study."""
    for c in load_cv()["case_studies"]:
        if c["id"] == case_id:
            return _clean(c)
    raise ValueError(f"No case study with id '{case_id}'. Call list_case_studies for valid ids.")


@mcp.tool(annotations=READ_ONLY)
def search_cv(query: str) -> list[dict]:
    """Whole-word keyword search across experience, skills and case studies. Returns up to 10 snippets with their source."""
    cv = _clean(load_cv())
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    hits: list[dict] = []

    def consider(source: str, text: str) -> None:
        score = sum(_count_matches(t, text) for t in terms)
        if score:
            hits.append({"source": source, "text": text, "score": score})

    for r in cv["experience"]:
        for h in r.get("highlights", []):
            consider(f"experience:{r['id']}", h)
        consider(f"experience:{r['id']}:skills", ", ".join(r.get("skills", [])))
    for cat, items in cv["skills"].items():
        consider(f"skills:{cat}", ", ".join(items))
    for c in cv["case_studies"]:
        body = " ".join(str(c.get(k, "")) for k in ("situation", "task", "action", "result"))
        consider(f"case_study:{c['id']}", f"{c.get('title', '')}: {body}")

    return sorted(hits, key=lambda h: h["score"], reverse=True)[:10]


def _role_fit(cv: dict[str, Any], criteria: list[dict] | None = None, role_title: str | None = None) -> dict:
    defaults = cv.get("target_role", {})
    supplied = criteria is not None
    criteria = criteria if supplied else defaults.get("criteria", [])
    sources: list[tuple[str, str]] = []
    person = cv.get("person", {})
    if person.get("summary"):
        sources.append(("Profile summary", person["summary"]))
    if person.get("languages"):
        sources.append(("Languages", ", ".join(person["languages"])))
    for c in person.get("certifications", []):
        sources.append(("Certification", c))
    for h in person.get("career_highlights", []):
        sources.append(("Career highlight", h))
    for r in cv["experience"]:
        sources += [(f"{r['company']} — {r.get('title', '')}".rstrip(" —"), h) for h in r.get("highlights", [])]
        sources.append((f"{r['company']} skills", ", ".join(r.get("skills", []))))
    for c in cv["case_studies"]:
        body = " ".join(str(c.get(k, "")) for k in ("title", "situation", "task", "action", "result"))
        sources.append((f"Case study: {c.get('title', c['id'])}", f"{body} {' '.join(c.get('tags', []))}"))
    for cat, items in cv["skills"].items():
        sources.append((f"Skills ({cat})", ", ".join(items)))

    results = []
    for crit in criteria:
        kws = crit["keywords"]
        evidence = []
        for label, text in sources:
            matched = sorted({k for k in kws if _count_matches(k, text)})
            if matched:
                evidence.append({"source": label, "matched": matched})
        covered = {k for e in evidence for k in e["matched"]}
        coverage = len(covered) / len(kws) if kws else 0
        results.append({
            "criterion": crit["name"],
            "coverage": "strong" if coverage >= 0.6 else "partial" if coverage > 0 else "gap",
            "keywords_matched": sorted(covered),
            "keywords_missing": sorted(set(kws) - covered),
            "evidence": evidence[:5],
        })

    out = {
        "role": role_title or (None if supplied else defaults.get("title")) or "unnamed role",
        "criteria_source": "supplied by caller" if supplied else "generic defaults from the CV file",
        "method": "Whole-word keyword match (simple plurals included) of CV content against each criterion. Deterministic; no LLM judgement.",
        "criteria": results,
    }
    if not supplied:
        out["note"] = (
            "These are generic default criteria written from the CV itself, so they will mostly match. "
            "For a meaningful assessment, call again with criteria taken from the job description."
        )
    return out


class Criterion(BaseModel):
    """One requirement of the role, with the words that would evidence it."""

    name: Annotated[str, Field(min_length=1, max_length=120, description="Requirement, e.g. 'Stakeholder management'")]
    keywords: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=60)]],
        Field(min_length=1, max_length=30, description="3–8 words or short phrases that would show this in a CV"),
    ]


@mcp.tool(annotations=READ_ONLY)
def assess_role_fit(
    criteria: Annotated[
        list[Criterion] | None,
        Field(max_length=20, description="The role's requirements. Derive 4–8 from the job description. Omit to use generic defaults."),
    ] = None,
    role_title: str | None = None,
) -> dict:
    """Evidence-based fit of this CV against a role's criteria.

    Pass the role's own criteria: turn the job description into 4–8 requirements,
    each with 3–8 keywords or short phrases. For each criterion the tool returns
    the CV evidence that matches, the keywords found and missing, and a coverage
    rating (strong / partial / gap). Gaps are reported, not hidden. Without
    criteria it falls back to generic defaults, which say little about fit.
    """
    supplied = [c.model_dump() for c in criteria] if criteria is not None else None
    return _role_fit(_clean(load_cv()), supplied, role_title)


@mcp.resource("cv://full", mime_type="application/json", description="The complete CV as JSON.")
def full_cv() -> str:
    return json.dumps(_clean(load_cv()), ensure_ascii=False, indent=2)


@mcp.resource("cv://markdown", mime_type="text/markdown", description="The CV rendered as Markdown.")
def cv_markdown() -> str:
    cv = _clean(load_cv())
    p = cv["person"]
    out = [f"# {p['name']}"]
    sub = " · ".join(x for x in (p.get("headline"), p.get("location")) if x)
    if sub:
        out.append(f"**{sub}**")
    if p.get("summary"):
        out += ["", p["summary"].strip()]
    facts = [("Languages", p.get("languages")), ("Certifications", p.get("certifications"))]
    for label, items in facts:
        if items:
            out += ["", f"**{label}:** {'; '.join(items)}"]
    if p.get("career_highlights"):
        out += ["", "## Career highlights"] + [f"- {h}" for h in p["career_highlights"]]
    out += ["", "## Experience"]
    for r in cv["experience"]:
        dates = "–".join(x for x in (r.get("start"), r.get("end")) if x)
        heading = " — ".join(x for x in (r.get("title"), r["company"]) if x)
        out.append(f"### {heading}" + (f" ({dates})" if dates else ""))
        if r.get("summary") and not r.get("highlights"):
            out.append(r["summary"])
        out += [f"- {h}" for h in r.get("highlights", [])]
    if cv.get("education"):
        out += ["", "## Education"]
        out += [f"- {e.get('qualification', '')}, {e.get('institution', '')} ({e.get('year', '')})" for e in cv["education"]]
    out += ["", "## Skills"] + [f"- **{k}:** {', '.join(v)}" for k, v in cv["skills"].items()]
    return "\n".join(out)


@mcp.prompt()
def interview_candidate(job_description: str = "", focus: str = "") -> str:
    """Starter prompt for a reviewer: screen this CV against your job description."""
    role = f" for a role focused on {focus}" if focus else ""
    jd = (
        f"\n\nJob description:\n{job_description.strip()}\n\n"
        "Turn this job description into 4–8 criteria, each with 3–8 keywords or short phrases, "
        "and pass them to assess_role_fit."
        if job_description.strip()
        else " Ask me for the job description first, turn it into 4–8 criteria with 3–8 keywords each, "
        "and pass them to assess_role_fit."
    )
    return (
        f"You are screening Nathan Kemppainen{role}. Start with get_profile.{jd} "
        "For each criterion rated 'partial' or 'gap', look for supporting detail with search_cv "
        "or get_case_study before concluding, then propose two probing interview questions. "
        "Cite which tool each fact came from, and do not state anything the tools did not return."
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    try:
        load_cv()
    except Exception as exc:  # noqa: BLE001 — any parse/read failure means unhealthy
        return JSONResponse({"status": "error", "detail": type(exc).__name__}, status_code=503)
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/", methods=["GET"])
async def index(_: Request) -> PlainTextResponse:
    return PlainTextResponse(
        "Nathan Kemppainen — CV as an MCP server.\n"
        "Add this server's /mcp URL as a connector in Copilot Studio or Claude, then ask about the CV.\n"
    )


# ---------- Control server (testing tier only) ----------

control = FastMCP(
    name="nate-kemppainen-cv-control",
    instructions="Maintenance tools for the CV file. Testing tier only.",
)

REQUIRED = {
    "person": ["name", "headline", "location", "summary"],
    "experience": ["id", "company", "title", "start", "end"],
}


def validate(cv: dict[str, Any]) -> dict:
    errors: list[str] = []
    for section in ("person", "experience", "skills", "case_studies", "target_role"):
        if section not in cv:
            errors.append(f"missing section: {section}")
    for field in REQUIRED["person"]:
        if not cv.get("person", {}).get(field):
            errors.append(f"person.{field} is empty")
    for i, r in enumerate(cv.get("experience", [])):
        for field in REQUIRED["experience"]:
            if not r.get(field):
                errors.append(f"experience[{r.get('id', i)}].{field} is empty")
    ids = [c.get("id") for c in cv.get("case_studies", [])]
    if len(ids) != len(set(ids)):
        errors.append("case_studies ids are not unique")

    todos = _find_placeholders(cv)
    gaps: list[str] = []
    if "target_role" in cv:
        fit = _role_fit(_clean(cv))
        gaps = [c["criterion"] for c in fit["criteria"] if c["coverage"] == "gap"]
    return {
        "file": str(cv_path()),
        "ok": not errors and not todos,
        "errors": errors,
        "todos": todos,
        "criteria_with_no_evidence": gaps,
    }


@control.tool(annotations=READ_ONLY)
def validate_cv() -> dict:
    """Check the CV file: parse errors, empty required fields, remaining TODOs, and role criteria with no evidence."""
    try:
        cv = load_cv()
    except Exception as exc:  # noqa: BLE001
        return {"file": str(cv_path()), "ok": False, "errors": [f"{type(exc).__name__}: {exc}"], "todos": [], "criteria_with_no_evidence": []}
    return validate(cv)


@control.tool()
def reload() -> dict:
    """Re-read the CV file now and report what was loaded. (The server reads the file on every call, so this confirms the current state rather than clearing a cache.)"""
    try:
        cv = load_cv()
    except Exception as exc:  # noqa: BLE001
        return {"loaded": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "loaded": True,
        "file": str(cv_path()),
        "experience": len(cv.get("experience", [])),
        "case_studies": len(cv.get("case_studies", [])),
        "criteria": len(cv.get("target_role", {}).get("criteria", [])),
    }


class BearerTokenGuard:
    """Rejects requests without the exact bearer token (constant-time compare)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            supplied = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(supplied, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---------- App assembly ----------

def build_app() -> Starlette:
    """Read-only app, plus the control app under /control when a token is set."""
    main_app = mcp.http_app(path="/mcp")
    token = os.environ.get("CV_MCP_CONTROL_TOKEN", "")
    if not token:
        return main_app

    control_app = control.http_app(path="/mcp")

    @asynccontextmanager
    async def lifespan(_: Starlette):
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(main_app.router.lifespan_context(main_app))
            await stack.enter_async_context(control_app.router.lifespan_context(control_app))
            yield

    return Starlette(
        routes=[
            Mount("/control", app=BearerTokenGuard(control_app, token)),
            Mount("/", app=main_app),
        ],
        lifespan=lifespan,
    )


if __name__ == "__main__":
    if os.environ.get("CV_TRANSPORT") == "stdio":
        mcp.run()
    else:
        import uvicorn

        uvicorn.run(build_app(), host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8000)))
