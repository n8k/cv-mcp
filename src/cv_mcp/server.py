"""CV MCP server.

Serves a CV as structured, read-only MCP tools, resources and a prompt, so any
MCP client (Copilot Studio, Claude, etc.) can query it conversationally.

Endpoints
  /mcp            read-only MCP (Streamable HTTP)
  /control/mcp    control MCP, only when CV_MCP_CONTROL_TOKEN is set (testing tier)
  /health         200 when the CV file parses, 503 otherwise

Visit log
  One JSON line per request on stdout ("event": "visit"), for the Grafana dashboard:
  time, path, status, duration, country, truncated IP, browser, referrer, MCP session,
  and for MCP calls the method, tool, arguments and result (capped). /health is not
  logged, nor anything sent to /control. The host keeps logs for 30 days.

Environment
  CV_PATH                 CV file (default: cv.toml next to this file, else at the repo root)
  CV_MCP_CONTROL_TOKEN    enables /control/mcp; clients send "Authorization: Bearer <token>"
  HOST, PORT              bind address (default 0.0.0.0:8000)
  CV_TRANSPORT=stdio      run over stdio instead of HTTP
"""

from __future__ import annotations

import hmac
import html
import ipaddress
import json
import logging
import os
import re
import string
import sys
import time
import tomllib
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import BaseModel, Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send


def _default_cv_path() -> Path:
    """The package ships cv.toml next to this file; in a checkout it sits at the repo root."""
    beside = Path(__file__).with_name("cv.toml")
    return beside if beside.exists() else Path(__file__).resolve().parents[2] / "cv.toml"


def cv_path() -> Path:
    return Path(os.environ.get("CV_PATH", _default_cv_path()))


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

# Words compared by stem, so a keyword finds its other forms: "training" finds
# "trained", "leadership" finds "led", "management" finds "managed", and British and
# American spellings meet ("organisation", "organization").
IRREGULAR = {
    "led": "lead", "taught": "teach", "built": "build", "ran": "run", "brought": "bring",
    "bought": "buy", "wrote": "write", "written": "write", "held": "hold", "made": "make",
    "met": "meet", "sold": "sell", "spoke": "speak", "spoken": "speak", "won": "win",
    "drove": "drive", "driven": "drive", "grew": "grow", "grown": "grow", "began": "begin",
    "begun": "begin", "chose": "choose", "chosen": "choose",
}
# Words whose "-er" is not a suffix: cut, they would meet unrelated words
# ("customer" and "custom", "number" and "numb"). Their plurals and verb forms stop here.
WHOLE_WORDS = {"customer", "number", "member", "order", "letter", "power", "paper", "water"}
BRITISH = {  # spellings the suffix rules don't join
    "programme": "program", "programmes": "programs", "centre": "center", "centres": "centers",
    "labour": "labor", "behaviour": "behavior", "licence": "license", "licences": "licenses",
    "catalogue": "catalog",
}
SUFFIXES = (  # longest first; one is removed per pass
    ("izations", "ize"), ("ization", "ize"), ("ations", "ate"), ("ation", "ate"),
    ("erships", ""), ("ership", ""), ("ments", ""), ("ment", ""), ("ings", ""), ("ing", ""),
    ("ions", ""), ("ion", ""), ("ers", ""), ("er", ""), ("ies", "y"), ("es", ""), ("ed", ""),
    ("s", ""),
)
STOPWORDS = {"a", "an", "and", "at", "by", "for", "in", "of", "on", "or", "the", "to", "with"}
TOKEN = re.compile(r"\w[\w+#]*|\.\w+")  # keeps "C++", ".NET", "C#"; splits on hyphens
NEAR = 4  # a phrase's words may sit this many extra words apart, in any order


def _stem(word: str) -> str:
    w = BRITISH.get(word.lower(), word.lower())
    if w in IRREGULAR:
        return IRREGULAR[w]
    if not w.isalpha() or len(w) < 4:
        return w
    w = re.sub(r"is(e|es|ed|ing|ation|ations)$", r"iz\1", w)
    for ending in ("", "s", "ed", "ing"):
        if w.endswith(ending) and w[: len(w) - len(ending)] in WHOLE_WORDS:
            return w[: len(w) - len(ending)]
    for _ in range(2):  # "engineering" -> "engineer" -> "engine"
        for suffix, repl in SUFFIXES:
            if suffix == "s" and w.endswith(("ss", "us", "is")):
                continue
            if w.endswith(suffix) and len(w) - len(suffix) + len(repl) >= 3:
                w = w[: -len(suffix)] + repl
                break
        else:
            break
    return w[:-1] if w.endswith("e") and len(w) > 3 else w


def _stems(text: str) -> list[str]:
    return [_stem(t) for t in TOKEN.findall(text)]


def _count_matches(term: str, text: str) -> int:
    """How often a term occurs in the text, case-insensitively, by word stem.

    A one-word term matches whole words only ("AI" is not in "claims chain"). A phrase
    matches exactly, or when its words (stop words aside) all occur within a few words of
    each other: "team leadership" finds "led a 30-person team".
    """
    want = _stems(term)
    if not want:
        return 0
    words = _stems(text)
    if len(want) == 1:
        return words.count(want[0])
    n = len(want)
    exact = sum(words[i : i + n] == want for i in range(len(words) - n + 1))
    if exact:
        return exact
    needed = {w for w in want if w not in STOPWORDS} or set(want)
    span = len(needed) + NEAR
    count, i = 0, 0
    while i < len(words):
        if words[i] in needed and needed <= set(words[i : i + span]):
            count += 1
            i += span
        else:
            i += 1
    return count


def _jlpt_levels(keyword: str) -> list[str]:
    """A higher JLPT level satisfies a lower one: N1 is the highest, N5 the lowest."""
    m = re.search(r"\bN([1-5])\b", keyword, re.IGNORECASE)
    if not m or not (re.search(r"\bJLPT\b", keyword, re.IGNORECASE) or keyword.strip().upper() == m.group(0).upper()):
        return []
    level = int(m.group(1))
    return [keyword[: m.start()] + f"N{n}" + keyword[m.end() :] for n in range(1, level)]


def _alternatives(keyword: str, equivalents: dict[str, list[str]]) -> list[str]:
    """The keyword, the CV file's equivalents for it ([matching.equivalents]), and higher JLPT levels."""
    listed = next((v for k, v in equivalents.items() if k.lower() == keyword.lower()), [])
    return [keyword, *listed, *_jlpt_levels(keyword)]


# ---------- Read-only server ----------

def _candidate_name() -> str:
    """person.name from the CV file, so the name has one source like everything else."""
    try:
        return _clean(load_cv()).get("person", {}).get("name") or "the candidate"
    except Exception:  # noqa: BLE001 — a broken file shows in /health, not here
        return "the candidate"


# Instructions are sent once per session, so they take the name at start-up; a new
# name in cv.toml arrives with the redeploy that ships it.
mcp = FastMCP(
    name="nate-kemppainen-cv",
    instructions=(
        f"This server is the CV of {_candidate_name()}. Use get_profile first, "
        "then the other tools to answer questions about their experience, skills, "
        "case studies and fit for roles. Only state what the tools return; if "
        "something is not in the CV, say so rather than guessing. Tool calls and "
        "their results are logged for 30 days."
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
    """Whole-word keyword search across the profile, experience, skills and case studies (tags included). Returns up to 10 snippets with their source."""
    cv = _clean(load_cv())
    terms = [t for t in re.split(r"\s+", query.strip()) if t]
    hits: list[dict] = []

    def consider(source: str, text: str) -> None:
        score = sum(_count_matches(t, text) for t in terms)
        if score:
            hits.append({"source": source, "text": text, "score": score})

    # The same text assess_role_fit reads, so the two tools agree on what the CV says.
    p = cv.get("person", {})
    for field in ("headline", "summary"):
        if p.get(field):
            consider(f"profile:{field}", p[field].strip())
    for h in p.get("career_highlights", []):
        consider("profile:highlight", h)
    for r in cv["experience"]:
        if r.get("title"):
            consider(f"experience:{r['id']}:title", f"{r['title']}, {r['company']}")
        if r.get("summary"):
            consider(f"experience:{r['id']}", r["summary"])
        for h in r.get("highlights", []):
            consider(f"experience:{r['id']}", h)
        consider(f"experience:{r['id']}:skills", ", ".join(r.get("skills", [])))
    for cat, items in cv["skills"].items():
        consider(f"skills:{cat}", ", ".join(items))
    for c in cv["case_studies"]:
        body = " ".join(str(c.get(k, "")) for k in ("situation", "task", "action", "result"))
        tags = f" [tags: {', '.join(c['tags'])}]" if c.get("tags") else ""
        consider(f"case_study:{c['id']}", f"{c.get('title', '')}: {body}{tags}")

    return sorted(hits, key=lambda h: h["score"], reverse=True)[:10]


NOT_FOUND = "not found in CV text"
EVIDENCE_CHARS = 300
HOW_TO_READ = (
    "'not found in CV text' and keywords_not_found mean those words (or their forms and "
    "listed equivalents) do not appear in the CV. They do not mean the experience is "
    "missing: a CV states experience in its own words. Before calling anything a gap, read "
    "the evidence text, check get_experience and search_cv for related wording, and say "
    "what you checked."
)


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
        role = f"{r['company']} — {r.get('title', '')}".rstrip(" —")
        if r.get("title"):
            sources.append((f"{role} (job title)", r["title"]))
        if r.get("summary"):
            sources.append((role, r["summary"]))
        sources += [(role, h) for h in r.get("highlights", [])]
        sources.append((f"{r['company']} skills", ", ".join(r.get("skills", []))))
    for c in cv["case_studies"]:
        body = " ".join(str(c.get(k, "")) for k in ("title", "situation", "task", "action", "result"))
        sources.append((f"Case study: {c.get('title', c['id'])}", f"{body} {' '.join(c.get('tags', []))}"))
    for cat, items in cv["skills"].items():
        sources.append((f"Skills ({cat})", ", ".join(items)))

    equivalents = cv.get("matching", {}).get("equivalents", {})
    results = []
    for crit in criteria:
        kws = crit["keywords"]
        alts = {k: _alternatives(k, equivalents) for k in kws}
        evidence = []
        for label, text in sources:
            matched = sorted({k for k in kws if any(_count_matches(a, text) for a in alts[k])})
            if matched:
                snippet = text.strip()
                evidence.append({
                    "source": label,
                    "matched": matched,
                    "text": snippet if len(snippet) <= EVIDENCE_CHARS else snippet[:EVIDENCE_CHARS] + "…",
                })
        covered = {k for e in evidence for k in e["matched"]}
        coverage = len(covered) / len(kws) if kws else 0
        results.append({
            "criterion": crit["name"],
            "coverage": "strong" if coverage >= 0.6 else "partial" if coverage > 0 else NOT_FOUND,
            "keywords_matched": sorted(covered),
            "keywords_not_found": sorted(set(kws) - covered),
            # Richest first: the entries matching the most keywords, CV order among equals.
            "evidence": sorted(evidence, key=lambda e: -len(e["matched"]))[:5],
        })

    out = {
        "role": role_title or (None if supplied else defaults.get("title")) or "unnamed role",
        "criteria_source": "supplied by caller" if supplied else "generic defaults from the CV file",
        "method": (
            "Keyword match of CV text (job titles included) against each criterion: whole words, "
            "any word form (led/leading/leadership, trained/training), phrases whose words sit "
            "close together, equivalents listed in the CV file, and higher JLPT levels for lower "
            "ones. Deterministic; no LLM judgement."
        ),
        "how_to_read": HOW_TO_READ,
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
    the CV evidence that matches (with its text), the keywords found and not
    found, and a coverage rating: strong / partial / not found in CV text. A
    keyword not found is a prompt to check get_experience and search_cv, not
    proof the experience is missing. Without criteria it falls back to generic
    defaults, which say little about fit.
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
    if cv.get("case_studies"):
        out += ["", "## Case studies"]
        for c in cv["case_studies"]:
            out += ["", f"### {c.get('title') or c['id']}"]
            out += [
                f"- **{label}:** {c[key]}"
                for label, key in (("Situation", "situation"), ("Task", "task"), ("Action", "action"), ("Result", "result"))
                if c.get(key)
            ]
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
        f"You are screening {_candidate_name()}{role}. Start with get_profile.{jd} "
        "For each criterion rated 'partial' or 'not found in CV text', look for supporting detail with search_cv "
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


INDEX_TEMPLATE = Path(__file__).with_name("index.html")

# The page is static apart from the escaped CV fields: inline style and script only,
# nothing loaded from elsewhere, no framing.
INDEX_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _mcp_url(request: Request) -> str:
    """Public /mcp URL as the visitor reached it. Both tiers are HTTPS (Cloudflare,
    nginx); only a local run is plain HTTP."""
    host = request.headers.get("host", "localhost")
    local = host.split(":")[0] in ("localhost", "127.0.0.1")
    return f"{'http' if local else 'https'}://{host}/mcp"


def render_index(mcp_url: str) -> str:
    try:
        person = _clean(load_cv()).get("person", {})
    except Exception:  # noqa: BLE001 — a broken file shows in /health, not here
        person = {}
    name = person.get("name") or "the candidate"
    linkedin = person.get("contact", {}).get("linkedin", "")
    if not linkedin.startswith("https://"):
        linkedin = ""
    esc = html.escape
    return string.Template(INDEX_TEMPLATE.read_text(encoding="utf-8")).substitute(
        name=esc(name),
        first_name=esc(name.split()[0]),
        headline=esc(person.get("headline", "")),
        mcp_url=esc(mcp_url),
        linkedin=f' · <a href="{esc(linkedin)}" rel="noopener">LinkedIn</a>' if linkedin else "",
    )


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request) -> Response:
    """Landing page for browsers; a one-line pointer for anything else (curl, probes)."""
    if "text/html" in request.headers.get("accept", ""):
        return HTMLResponse(render_index(_mcp_url(request)), headers={"Content-Security-Policy": INDEX_CSP})
    return PlainTextResponse(
        f"{_candidate_name()} — CV as an MCP server.\n"
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
    for key, alts in cv.get("matching", {}).get("equivalents", {}).items():
        if not isinstance(alts, list) or not all(isinstance(a, str) and a.strip() for a in alts):
            errors.append(f"matching.equivalents.{key} must be a list of non-empty strings")

    todos = _find_placeholders(cv)
    gaps: list[str] = []
    if "target_role" in cv:
        fit = _role_fit(_clean(cv))
        gaps = [c["criterion"] for c in fit["criteria"] if c["coverage"] == NOT_FOUND]
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


# ---------- Visit log ----------

visit_log = logging.getLogger("cv_mcp.visits")
if not visit_log.handlers:
    _handler = logging.StreamHandler(sys.stdout)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    visit_log.addHandler(_handler)
    visit_log.setLevel(logging.INFO)
    visit_log.propagate = False

LOG_CAP = 4000  # characters kept of each logged argument set or result
BODY_CAP = 1 << 20  # request and response bytes read for logging; larger ones are not parsed
UNLOGGED = ("/health", "/control")


def _capped(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= LOG_CAP else text[:LOG_CAP] + "…"


def _truncated_ip(ip: str | None) -> str | None:
    """Network part only: IPv4 /24, IPv6 /48. Enough for a country or network, not a household."""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return None
    prefix = 24 if addr.version == 4 else 48
    return str(ipaddress.ip_network(f"{addr}/{prefix}", strict=False).network_address)


def _referrer(value: str) -> str | None:
    """Origin and path only; a query string can carry anything."""
    return value.split("?")[0].split("#")[0][:300] or None


def _rpc_requests(body: bytes) -> list[dict]:
    """The JSON-RPC messages in an MCP POST, summarised for the log."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return []
    out = []
    for msg in parsed if isinstance(parsed, list) else [parsed]:
        if not isinstance(msg, dict) or "method" not in msg:
            continue
        params = msg.get("params") or {}
        entry: dict[str, Any] = {"method": msg["method"]}
        if "id" in msg:
            entry["id"] = msg["id"]
        if msg["method"] == "initialize":
            info = params.get("clientInfo") or {}
            entry["client"] = {k: str(info.get(k, ""))[:100] for k in ("name", "version")}
        elif msg["method"] == "tools/call":
            entry["tool"] = params.get("name")
            entry["args"] = _capped(params.get("arguments") or {})
        elif msg["method"] == "resources/read":
            entry["uri"] = params.get("uri")
        elif msg["method"] == "prompts/get":
            entry["prompt"] = params.get("name")
            entry["args"] = _capped(params.get("arguments") or {})
        out.append(entry)
    return out


def _rpc_responses(body: bytes, content_type: str) -> list[dict]:
    """JSON-RPC responses from a JSON or server-sent-events MCP reply."""
    text = body.decode("utf-8", "replace")
    chunks = [line[5:] for line in text.splitlines() if line.startswith("data:")] if "event-stream" in content_type else [text]
    out = []
    for chunk in chunks:
        try:
            parsed = json.loads(chunk)
        except ValueError:
            continue
        out += [m for m in (parsed if isinstance(parsed, list) else [parsed]) if isinstance(m, dict) and "id" in m]
    return out


def _outcome(response: dict) -> dict:
    if "error" in response:
        return {"error": _capped((response["error"] or {}).get("message"))}
    result = response.get("result") or {}
    if "structuredContent" in result:
        shown = result["structuredContent"]
    elif "content" in result:
        shown = [c.get("text", c.get("type")) for c in result["content"] if isinstance(c, dict)]
    elif "contents" in result:  # resources/read
        shown = [c.get("uri") for c in result["contents"] if isinstance(c, dict)]
    elif "messages" in result:  # prompts/get
        shown = f"{len(result['messages'])} message(s)"
    else:
        return {}
    out: dict[str, Any] = {"result": _capped(shown)}
    if result.get("isError"):
        out["is_error"] = True
    return out


def _replay(buffered: list[Message], receive: Receive) -> Receive:
    """A receive that returns the already-read messages first, then the real stream."""

    async def replay() -> Message:
        return buffered.pop(0) if buffered else await receive()

    return replay


class VisitLog:
    """Writes one "visit" JSON line per HTTP request. Never changes a request or response,
    and a logging failure never fails a request."""

    LOGGED_RESULTS = {"tools/call", "resources/read", "prompts/get"}

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or path.startswith(UNLOGGED):
            await self.app(scope, receive, send)
            return

        started = time.monotonic()
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        is_mcp_post = scope.get("method") == "POST" and path.rstrip("/").endswith("/mcp")

        # MCP requests are small JSON-RPC messages: read the body once, hand the same
        # messages to the app, then pass any later ones (disconnect) straight through.
        rpc: list[dict] = []
        if is_mcp_post:
            buffered: list[Message] = []
            body = b""
            while True:
                message = await receive()
                buffered.append(message)
                if message["type"] != "http.request":
                    break
                if len(body) <= BODY_CAP:
                    body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            if len(body) <= BODY_CAP:
                rpc = _rpc_requests(body)
            receive = _replay(buffered, receive)

        capture = any(r["method"] in self.LOGGED_RESULTS for r in rpc)
        reply: dict[str, Any] = {"status": None, "type": "", "session": None, "body": b""}

        async def watch(message: Message) -> None:
            if message["type"] == "http.response.start":
                reply["status"] = message["status"]
                for k, v in message.get("headers", []):
                    name = k.decode("latin-1").lower()
                    if name == "content-type":
                        reply["type"] = v.decode("latin-1")
                    elif name == "mcp-session-id":
                        reply["session"] = v.decode("latin-1")
            elif message["type"] == "http.response.body" and capture and len(reply["body"]) <= BODY_CAP:
                reply["body"] += message.get("body", b"")
            await send(message)

        try:
            await self.app(scope, receive, watch)
        finally:
            try:
                self._write(scope, headers, rpc, reply, started)
            except Exception:  # noqa: BLE001 — the log is best effort
                pass

    def _write(self, scope: Scope, headers: dict, rpc: list[dict], reply: dict, started: float) -> None:
        path = scope.get("path", "")
        client = scope.get("client") or ("", 0)
        ip = headers.get("cf-connecting-ip") or headers.get("x-real-ip") or client[0]
        if path == "/":
            kind = "page" if "text/html" in headers.get("accept", "") else "root"
        elif path.rstrip("/").endswith("/mcp"):
            kind = "mcp"
        else:
            kind = "other"
        record: dict[str, Any] = {
            "event": "visit",
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "t": round(time.time(), 3),  # numeric, so Loki can unwrap it (session duration)
            "kind": kind,
            "method": scope.get("method"),
            "path": path,
            "status": reply["status"],
            "ms": round((time.monotonic() - started) * 1000, 1),
            "country": headers.get("cf-ipcountry"),
            "ip": _truncated_ip(ip),
            "ua": headers.get("user-agent", "")[:300] or None,
            "referrer": _referrer(headers.get("referer", "")),
            "session": headers.get("mcp-session-id") or reply["session"],
        }
        if rpc:
            if reply["body"] and len(reply["body"]) <= BODY_CAP:
                by_id = {r["id"]: r for r in _rpc_responses(reply["body"], reply["type"])}
                for entry in rpc:
                    if entry["method"] in self.LOGGED_RESULTS and entry.get("id") in by_id:
                        entry.update(_outcome(by_id[entry["id"]]))
            record["rpc"] = rpc
        visit_log.info(json.dumps({k: v for k, v in record.items() if v is not None}, ensure_ascii=False, default=str))


# ---------- App assembly ----------

def build_app() -> ASGIApp:
    """Read-only app, plus the control app under /control when a token is set, behind the visit log."""
    return VisitLog(_routes())


def _routes() -> Starlette:
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
