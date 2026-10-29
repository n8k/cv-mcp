"""Tests for the CV MCP server.

All tests run against examples/cv.example.toml (fictional data) unless they
write their own CV file. Run with:  pytest
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastmcp import Client
from starlette.testclient import TestClient

import server

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "cv.example.toml"

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def example_cv(monkeypatch):
    monkeypatch.setenv("CV_PATH", str(EXAMPLE))
    monkeypatch.delenv("CV_MCP_CONTROL_TOKEN", raising=False)


def call(srv, tool: str, args: dict | None = None):
    async def run():
        async with Client(srv) as c:
            return (await c.call_tool(tool, args or {})).data
    return asyncio.run(run())


def write_cv(tmp_path: Path, text: str, monkeypatch) -> Path:
    p = tmp_path / "cv.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setenv("CV_PATH", str(p))
    return p


MINIMAL = """
[person]
name = "Test Person"
headline = "TODO: headline"
location = "Nowhere"
summary = "Builds things."
[[experience]]
id = "acme"
company = "Acme"
title = "Engineer"
start = "2020-01"
end = "present"
highlights = ["Shipped the claims chain", "TODO: more"]
skills = ["Python"]
[skills]
core = ["Python"]
[[case_studies]]
id = "one"
title = "One"
situation = "s"
task = "t"
action = "a"
result = "r"
[target_role]
title = "Tester"
[[target_role.criteria]]
name = "AI"
keywords = ["AI"]
"""


# ---------- Surface ----------

def test_lists_expected_tools_resources_and_prompt():
    async def run():
        async with Client(server.mcp) as c:
            return (
                {t.name for t in await c.list_tools()},
                {str(r.uri) for r in await c.list_resources()},
                {p.name for p in await c.list_prompts()},
            )
    tools, resources, prompts = asyncio.run(run())
    assert tools == {
        "get_profile", "get_experience", "get_skills", "list_case_studies",
        "get_case_study", "search_cv", "assess_role_fit",
    }
    assert resources == {"cv://full", "cv://markdown"}
    assert prompts == {"interview_candidate"}


def test_read_only_server_has_no_control_tools():
    async def run():
        async with Client(server.mcp) as c:
            return {t.name for t in await c.list_tools()}
    assert not {"validate_cv", "reload"} & asyncio.run(run())


def test_all_public_tools_are_annotated_read_only():
    async def run():
        async with Client(server.mcp) as c:
            return await c.list_tools()
    for t in asyncio.run(run()):
        assert t.annotations and t.annotations.read_only_hint, t.name


# ---------- Tools ----------

def test_get_profile():
    p = call(server.mcp, "get_profile")
    assert p["name"] == "Alex Morgan"
    assert p["contact"]["email"] == "alex.morgan@example.com"


def test_get_experience_all_and_filtered():
    assert [r["company"] for r in call(server.mcp, "get_experience")] == ["Northbridge Insurance", "Kaito Mobile", "Fjordtel"]
    filtered = call(server.mcp, "get_experience", {"company": "kaito"})
    assert [r["id"] for r in filtered] == ["kaito-mobile"]
    assert call(server.mcp, "get_experience", {"company": "nobody"}) == []


def test_get_skills_all_and_one_category():
    assert set(call(server.mcp, "get_skills")) == {"governance", "ai", "engineering", "languages"}
    assert call(server.mcp, "get_skills", {"category": "ai"})["ai"][0] == "agentic AI"
    assert call(server.mcp, "get_skills", {"category": "missing"}) == {"missing": []}


def test_case_studies_list_and_get():
    ids = [c["id"] for c in call(server.mcp, "list_case_studies")]
    assert ids == ["claims-assistant", "release-cadence", "agent-homelab"]
    story = call(server.mcp, "get_case_study", {"case_id": "claims-assistant"})
    assert {"situation", "task", "action", "result"} <= set(story)


def test_get_case_study_unknown_id_is_an_error():
    with pytest.raises(Exception, match="list_case_studies"):
        call(server.mcp, "get_case_study", {"case_id": "nope"})


def test_search_cv_ranks_and_limits():
    hits = call(server.mcp, "search_cv", {"query": "governance"})
    assert hits and len(hits) <= 10
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    assert call(server.mcp, "search_cv", {"query": "zzzz"}) == []


def test_assess_role_fit_reports_gaps():
    fit = call(server.mcp, "assess_role_fit")
    by_name = {c["criterion"]: c for c in fit["criteria"]}
    assert fit["role"] == "Head of Digital Delivery"
    assert by_name["Data platform ownership"]["coverage"] == server.NOT_FOUND
    assert by_name["Data platform ownership"]["evidence"] == []
    assert by_name["Governance & responsible AI"]["coverage"] == "strong"


def test_assess_role_fit_title_override():
    assert call(server.mcp, "assess_role_fit", {"role_title": "Other"})["role"] == "Other"


# ---------- Whole-word matching ----------

@pytest.mark.parametrize(
    ("term", "text", "expected"),
    [
        ("AI", "claims chain maintenance", 0),
        ("AI", "Applied AI and agentic AI", 2),
        ("ai", "AI-native", 1),
        ("cost", "costly", 0),
        ("data platform", "owns the data   platform", 1),
        ("C++", "Wrote C++ daily", 1),
        (".NET", "Built on .NET", 1),
    ],
)
def test_word_matching(term, text, expected):
    assert server._count_matches(term, text) == expected


def test_role_fit_ignores_substrings(tmp_path, monkeypatch):
    write_cv(tmp_path, MINIMAL, monkeypatch)
    fit = call(server.mcp, "assess_role_fit")
    assert fit["criteria"][0]["coverage"] == server.NOT_FOUND  # "claims chain" must not count as AI
    assert call(server.mcp, "search_cv", {"query": "AI"}) == []


# ---------- Placeholders ----------

def test_placeholders_never_reach_clients(tmp_path, monkeypatch):
    write_cv(tmp_path, MINIMAL, monkeypatch)
    profile = call(server.mcp, "get_profile")
    assert "headline" not in profile
    assert call(server.mcp, "get_experience")[0]["highlights"] == ["Shipped the claims chain"]

    async def read(uri):
        async with Client(server.mcp) as c:
            return (await c.read_resource(uri))[0].text
    for uri in ("cv://full", "cv://markdown"):
        assert "TODO" not in asyncio.run(read(uri))


def test_shipped_cv_template_parses_and_hides_placeholders(monkeypatch):
    monkeypatch.setenv("CV_PATH", str(ROOT / "cv.toml"))

    async def read():
        async with Client(server.mcp) as c:
            return (await c.read_resource("cv://full"))[0].text
    assert "TODO" not in asyncio.run(read())


# ---------- Resources and prompt ----------

def test_resources():
    async def run():
        async with Client(server.mcp) as c:
            full = (await c.read_resource("cv://full"))[0].text
            md = (await c.read_resource("cv://markdown"))[0].text
            return full, md
    full, md = asyncio.run(run())
    assert json.loads(full)["person"]["name"] == "Alex Morgan"
    assert md.startswith("# Alex Morgan")
    assert "### Digital Transformation Manager — Northbridge Insurance (2021-04–present)" in md
    # STAR stories are part of the readable CV, after experience (education, when present,
    # and skills follow).
    assert "## Case studies" in md and "### Generative-AI claims-summary assistant" in md
    assert "- **Result:** Reading time fell to 6 minutes per file" in md
    assert md.index("## Experience") < md.index("## Case studies") < md.index("## Skills")


def test_prompt():
    async def run():
        async with Client(server.mcp) as c:
            return (await c.get_prompt("interview_candidate", {"focus": "delivery"})).messages[0].content.text
    text = asyncio.run(run())
    assert "role focused on delivery" in text and "assess_role_fit" in text
    assert "screening Alex Morgan" in text  # name from the CV file, not the code


def test_name_comes_from_the_cv_file():
    with TestClient(server.build_app()) as http:
        assert http.get("/").text.startswith("Alex Morgan — CV as an MCP server.")
    # Instructions are built at import, from the shipped cv.toml.
    import tomllib
    shipped = tomllib.loads((ROOT / "cv.toml").read_text(encoding="utf-8"))["person"]["name"]
    assert f"the CV of {shipped}." in server.mcp.instructions


def test_landing_page_for_browsers():
    with TestClient(server.build_app()) as http:
        r = http.get("/", headers={"Accept": "text/html,application/xhtml+xml"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    page = r.text
    assert "<h1>Alex Morgan</h1>" in page and "https://testserver/mcp" in page
    assert "Add custom connector" in page and "Model Context Protocol" in page
    assert '<a href="https://github.com/n8k/cv-mcp" rel="noopener">Source Code</a>' in page
    assert "Vibe coded with Claude on the bus ride home." in page
    assert "$" not in page  # every template field filled


def test_landing_page_escapes_cv_fields(tmp_path, monkeypatch):
    write_cv(tmp_path, MINIMAL.replace('name = "Test Person"', 'name = "<script>x</script> Person"'), monkeypatch)
    with TestClient(server.build_app()) as http:
        page = http.get("/", headers={"Accept": "text/html"}).text
    assert "<script>x</script>" not in page and "&lt;script&gt;x&lt;/script&gt; Person" in page
    assert "TODO" not in page  # the placeholder headline is stripped


def test_search_covers_profile_summaries_and_tags(tmp_path, monkeypatch):
    text = (
        MINIMAL.replace('summary = "Builds things."', 'summary = "Builds things and wins buy-in."\ncareer_highlights = ["Grew a guild"]')
        .replace('highlights = ["Shipped the claims chain", "TODO: more"]', 'summary = "Ran the night shift."\nhighlights = ["Shipped the claims chain", "TODO: more"]')
        .replace('result = "r"', 'result = "r"\ntags = ["stakeholder management"]')
    )
    write_cv(tmp_path, text, monkeypatch)

    def sources(q):
        return {h["source"] for h in call(server.mcp, "search_cv", {"query": q})}
    assert sources("buy-in") == {"profile:summary"}
    assert sources("guild") == {"profile:highlight"}
    assert sources("shift") == {"experience:acme"}  # an experience summary, not a highlight
    assert sources("stakeholder") == {"case_study:one"}  # tag only; plural rules still apply
    assert call(server.mcp, "search_cv", {"query": "stakeholder"})[0]["text"].endswith("[tags: stakeholder management]")
    assert sources("TODO") == set()  # placeholders are still stripped before searching


def test_case_study_placeholders_stay_out_of_markdown(tmp_path, monkeypatch):
    write_cv(tmp_path, MINIMAL.replace('result = "r"', 'result = "TODO: result"'), monkeypatch)

    async def read():
        async with Client(server.mcp) as c:
            return (await c.read_resource("cv://markdown"))[0].text
    md = asyncio.run(read())
    assert "### One" in md and "- **Action:** a" in md and "Result" not in md


# ---------- Control tools ----------

def test_validate_cv_on_complete_file():
    result = call(server.control, "validate_cv")
    assert result["ok"] is True
    assert result["todos"] == [] and result["errors"] == []
    assert result["criteria_with_no_evidence"] == ["Data platform ownership"]


def test_validate_cv_reports_todos(tmp_path, monkeypatch):
    write_cv(tmp_path, MINIMAL, monkeypatch)
    result = call(server.control, "validate_cv")
    assert result["ok"] is False
    assert "person.headline" in result["todos"]
    assert "experience[acme].highlights[1]" in result["todos"]


def test_validate_cv_reports_parse_errors(tmp_path, monkeypatch):
    write_cv(tmp_path, "this is = = not toml", monkeypatch)
    result = call(server.control, "validate_cv")
    assert result["ok"] is False and "TOMLDecodeError" in result["errors"][0]


def test_reload():
    result = call(server.control, "reload")
    assert result == {"loaded": True, "file": str(EXAMPLE), "experience": 3, "case_studies": 3, "criteria": 6}


# ---------- HTTP app ----------

def test_health_ok_and_failing(tmp_path, monkeypatch):
    with TestClient(server.build_app()) as http:
        assert http.get("/health").json() == {"status": "ok"}
        monkeypatch.setenv("CV_PATH", str(tmp_path / "missing.toml"))
        r = http.get("/health")
        assert r.status_code == 503 and r.json()["status"] == "error"


def test_mcp_endpoint_serves_initialize():
    with TestClient(server.build_app()) as http:
        r = http.post("/mcp", json=INIT, headers=MCP_HEADERS)
        assert r.status_code == 200


def test_no_control_endpoint_without_token():
    with TestClient(server.build_app()) as http:
        r = http.post("/control/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer x"})
        assert r.status_code == 404


def test_control_endpoint_requires_token(monkeypatch):
    monkeypatch.setenv("CV_MCP_CONTROL_TOKEN", "s3cret")
    with TestClient(server.build_app()) as http:
        assert http.post("/control/mcp", json=INIT, headers=MCP_HEADERS).status_code == 401
        bad = {**MCP_HEADERS, "Authorization": "Bearer wrong"}
        assert http.post("/control/mcp", json=INIT, headers=bad).status_code == 401
        good = {**MCP_HEADERS, "Authorization": "Bearer s3cret"}
        assert http.post("/control/mcp", json=INIT, headers=good).status_code == 200
        # The read-only endpoint and health stay open alongside it.
        assert http.post("/mcp", json=INIT, headers=MCP_HEADERS).status_code == 200
        assert http.get("/health").status_code == 200


def test_profile_includes_education_and_markdown_lists_it(tmp_path, monkeypatch):
    extra = '\n[[education]]\ninstitution = "Uni"\nqualification = "B.A."\nyear = "2004"\n'
    text = MINIMAL.replace('summary = "Builds things."', 'summary = "Builds things."\nlanguages = ["English"]')
    write_cv(tmp_path, text + extra, monkeypatch)
    profile = call(server.mcp, "get_profile")
    assert profile["education"] == [{"institution": "Uni", "qualification": "B.A.", "year": "2004"}]
    assert profile["languages"] == ["English"]

    async def read():
        async with Client(server.mcp) as c:
            return (await c.read_resource("cv://markdown"))[0].text
    md = asyncio.run(read())
    assert "## Education" in md and "B.A., Uni (2004)" in md and "**Languages:** English" in md


# ---------- Reviewer-supplied criteria ----------

REVIEWER_CRITERIA = [
    {"name": "Stakeholder management", "keywords": ["stakeholders", "executive"]},
    {"name": "Cloud platforms", "keywords": ["AWS", "Kubernetes"]},
]


def test_role_fit_with_supplied_criteria():
    fit = call(server.mcp, "assess_role_fit", {"criteria": REVIEWER_CRITERIA, "role_title": "Delivery Lead"})
    by_name = {c["criterion"]: c for c in fit["criteria"]}
    assert fit["role"] == "Delivery Lead"
    assert fit["criteria_source"] == "supplied by caller"
    assert "note" not in fit
    assert by_name["Stakeholder management"]["coverage"] != server.NOT_FOUND
    assert by_name["Cloud platforms"]["coverage"] == server.NOT_FOUND
    assert by_name["Cloud platforms"]["keywords_not_found"] == ["AWS", "Kubernetes"]


def test_role_fit_defaults_carry_a_note():
    fit = call(server.mcp, "assess_role_fit")
    assert fit["criteria_source"] == "generic defaults from the CV file"
    assert "job description" in fit["note"]


def test_supplied_criteria_without_title_are_unnamed():
    fit = call(server.mcp, "assess_role_fit", {"criteria": REVIEWER_CRITERIA})
    assert fit["role"] == "unnamed role"


@pytest.mark.parametrize(
    "criteria",
    [
        [{"name": "x", "keywords": []}],                      # no keywords
        [{"name": "", "keywords": ["a"]}],                    # empty name
        [{"name": "x", "keywords": ["a" * 61]}],              # keyword too long
        [{"name": "x", "keywords": ["a"] * 31}],              # too many keywords
        [{"name": f"c{i}", "keywords": ["a"]} for i in range(21)],  # too many criteria
    ],
)
def test_supplied_criteria_are_validated(criteria):
    with pytest.raises(Exception):
        call(server.mcp, "assess_role_fit", {"criteria": criteria})


def test_role_fit_schema_advertises_criteria():
    async def run():
        async with Client(server.mcp) as c:
            return next(t for t in await c.list_tools() if t.name == "assess_role_fit").input_schema
    schema = json.dumps(asyncio.run(run()))
    assert "criteria" in schema and "keywords" in schema


@pytest.mark.parametrize(
    ("term", "text", "expected"),
    [
        ("stakeholder", "managed stakeholders", 1),
        ("stakeholders", "one stakeholder", 1),
        ("OKR", "KPIs & OKRs", 1),
        ("process", "two processes", 1),
        ("agile release trains", "an agile release train", 1),
        ("AWS", "AW", 0),
    ],
)
def test_plural_matching(term, text, expected):
    assert server._count_matches(term, text) == expected


# ---------- Word forms, phrases, equivalents, JLPT, titles ----------

@pytest.mark.parametrize(
    ("term", "text", "expected"),
    [
        ("training", "trained 150+ staff", 1),
        ("leadership", "led the programme", 1),
        ("management", "managed vendors", 1),
        ("organisation", "a new organization", 1),
        ("program management", "programme management", 1),
        ("data center", "three data centres", 1),
        ("mitigation", "mitigated the risk", 1),
        ("team leadership", "hired and led a 30-person team", 1),
        ("led a team", "led a cross-functional team of 160+", 1),
        ("risk management", "managed risks, issues and dependencies", 1),
        ("people management", "managed 124 projects and 9 project managers", 0),
        ("cost", "costly", 0),
        ("process", "processing", 1),
        ("customer", "built a custom pipeline", 0),
        ("customers", "for the customer", 1),
        ("customer-facing", "customer facing delivery", 1),
        ("numbers", "numbered", 1),
        ("order", "ordered parts", 1),
    ],
)
def test_word_forms_and_near_phrases(term, text, expected):
    assert server._count_matches(term, text) == expected


@pytest.mark.parametrize(
    ("keyword", "alternatives"),
    [
        ("JLPT N2", ["JLPT N2", "JLPT N1"]),
        ("JLPT N1", ["JLPT N1"]),
        ("N3", ["N3", "N1", "N2"]),
        ("Japanese N2 level", ["Japanese N2 level"]),  # no JLPT, not a bare level: left alone
    ],
)
def test_higher_jlpt_levels_satisfy_lower(keyword, alternatives):
    assert server._alternatives(keyword, {}) == alternatives


EQUIVALENTS_CV = """
[person]
name = "Test Person"
headline = "h"
location = "l"
summary = "s"
languages = ["Japanese (JLPT N1)"]

[[experience]]
id = "acme"
company = "Acme"
title = "PMO Department Manager"
start = "2020"
end = "2021"
highlights = ["Quarterly reviews with the CEO and CFO."]
skills = []

[skills]
x = ["y"]

[[case_studies]]
id = "c"
title = "t"

[target_role]
title = "r"

[[target_role.criteria]]
name = "n"
keywords = ["k"]

[matching.equivalents]
"C-level" = ["CEO", "CFO"]
"""


def test_equivalents_from_the_cv_file_count_and_quote_the_text(tmp_path, monkeypatch):
    write_cv(tmp_path, EQUIVALENTS_CV, monkeypatch)
    crit = [{"name": "C-level engagement", "keywords": ["c-level"]}]
    [c] = call(server.mcp, "assess_role_fit", {"criteria": crit})["criteria"]
    assert c["coverage"] == "strong"
    assert c["evidence"][0]["text"] == "Quarterly reviews with the CEO and CFO."


def test_jlpt_and_job_titles_count_as_evidence(tmp_path, monkeypatch):
    write_cv(tmp_path, EQUIVALENTS_CV, monkeypatch)
    crit = [
        {"name": "Japanese", "keywords": ["JLPT N2"]},
        {"name": "Leadership", "keywords": ["department manager"]},
    ]
    japanese, leadership = call(server.mcp, "assess_role_fit", {"criteria": crit})["criteria"]
    assert japanese["coverage"] == "strong"
    assert leadership["coverage"] == "strong"
    assert leadership["evidence"][0]["source"] == "Acme — PMO Department Manager (job title)"


def test_evidence_lists_the_richest_entries_first(tmp_path, monkeypatch):
    many = EQUIVALENTS_CV.replace(
        'highlights = ["Quarterly reviews with the CEO and CFO."]',
        'highlights = ["Risk register.", "Risk log.", "Risk review.", "Risk owner.", "Risk desk.", '
        '"Managed risks, issues and dependencies, with mitigation and escalation."]',
    )
    write_cv(tmp_path, many, monkeypatch)
    crit = [{"name": "Risk", "keywords": ["risk", "issue", "dependency", "mitigation", "escalation"]}]
    [c] = call(server.mcp, "assess_role_fit", {"criteria": crit})["criteria"]
    assert len(c["evidence"]) == 5
    assert c["evidence"][0]["text"].startswith("Managed risks")  # sixth in CV order, first here


def test_not_found_comes_with_instructions_to_check():
    fit = call(server.mcp, "assess_role_fit", {"criteria": REVIEWER_CRITERIA})
    assert "do not mean the experience is missing" in fit["how_to_read"]
    assert "get_experience" in fit["how_to_read"]


def test_search_cv_finds_job_titles():
    sources = [h["source"] for h in call(server.mcp, "search_cv", {"query": "transformation manager"})]
    assert any(s.endswith(":title") for s in sources)


def test_validate_flags_malformed_equivalents(tmp_path, monkeypatch):
    write_cv(tmp_path, EQUIVALENTS_CV.replace('"C-level" = ["CEO", "CFO"]', '"C-level" = "CEO"'), monkeypatch)
    assert any("matching.equivalents" in e for e in call(server.control, "validate_cv")["errors"])


def test_shipped_cv_equivalents_are_well_formed(monkeypatch):
    monkeypatch.setenv("CV_PATH", str(ROOT / "cv.toml"))
    assert not [e for e in call(server.control, "validate_cv")["errors"] if "equivalents" in e]


def test_prompt_with_job_description():
    async def run():
        async with Client(server.mcp) as c:
            r = await c.get_prompt("interview_candidate", {"job_description": "Lead delivery of X."})
            return r.messages[0].content.text
    text = asyncio.run(run())
    assert "Lead delivery of X." in text and "assess_role_fit" in text and "Ask me" not in text


def test_prompt_without_job_description_asks_for_one():
    async def run():
        async with Client(server.mcp) as c:
            return (await c.get_prompt("interview_candidate")).messages[0].content.text
    assert "Ask me for the job description" in asyncio.run(run())


# ---------- Visit log ----------

@pytest.fixture
def visits():
    """The visit log's JSON lines, as dicts."""
    lines: list[str] = []

    class Sink(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage())

    sink = Sink()
    server.visit_log.addHandler(sink)
    yield lambda: [json.loads(line) for line in lines]
    server.visit_log.removeHandler(sink)


def mcp_session(http, headers=None):
    """initialize + initialized; returns the headers that carry the session."""
    r = http.post("/mcp", json=INIT, headers={**MCP_HEADERS, **(headers or {})})
    session = {**MCP_HEADERS, **(headers or {}), "mcp-session-id": r.headers["mcp-session-id"]}
    http.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=session)
    return session


def test_page_visit_is_logged_with_cloudflare_details(visits):
    cf = {
        "Accept": "text/html",
        "CF-Connecting-IP": "203.0.113.77",
        "CF-IPCountry": "JP",
        "User-Agent": "Mozilla/5.0 test",
        "Referer": "https://www.linkedin.com/in/someone?trk=secret#x",
    }
    with TestClient(server.build_app()) as http:
        assert http.get("/", headers=cf).status_code == 200
    [v] = visits()
    assert v["event"] == "visit" and v["kind"] == "page" and v["status"] == 200
    assert isinstance(v["t"], float)
    assert v["country"] == "JP"
    assert v["ip"] == "203.0.113.0"  # network only
    assert v["ua"] == "Mozilla/5.0 test"
    assert v["referrer"] == "https://www.linkedin.com/in/someone"  # no query or fragment


def test_mcp_session_logs_client_tool_arguments_and_result(visits):
    with TestClient(server.build_app()) as http:
        session = mcp_session(http)
        call_ = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_cv", "arguments": {"query": "python"}}}
        r = http.post("/mcp", json=call_, headers=session)
        assert r.status_code == 200
    init, _, tool = visits()
    sid = session["mcp-session-id"]
    assert init["session"] == sid and tool["session"] == sid
    assert init["rpc"] == [{"method": "initialize", "id": 1, "client": {"name": "test", "version": "0"}}]
    [entry] = tool["rpc"]
    assert entry["tool"] == "search_cv" and entry["args"] == {"query": "python"}
    assert entry["result"]["result"] == call(server.mcp, "search_cv", {"query": "python"})


def test_long_arguments_and_results_are_capped(visits):
    jd = "x" * (server.LOG_CAP * 2)
    with TestClient(server.build_app()) as http:
        session = mcp_session(http)
        call_ = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_cv", "arguments": {"query": jd}}}
        http.post("/mcp", json=call_, headers=session)
    entry = visits()[-1]["rpc"][0]
    assert isinstance(entry["args"], str) and len(entry["args"]) <= server.LOG_CAP + 1


def test_tool_errors_are_logged(visits):
    with TestClient(server.build_app()) as http:
        session = mcp_session(http)
        call_ = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_case_study", "arguments": {"case_id": "nope"}}}
        http.post("/mcp", json=call_, headers=session)
    entry = visits()[-1]["rpc"][0]
    assert entry.get("is_error") or "error" in entry


def test_health_and_control_are_never_logged(visits, monkeypatch):
    monkeypatch.setenv("CV_MCP_CONTROL_TOKEN", "s3cret")
    with TestClient(server.build_app()) as http:
        http.get("/health")
        good = {**MCP_HEADERS, "Authorization": "Bearer s3cret"}
        assert http.post("/control/mcp", json=INIT, headers=good).status_code == 200
    assert visits() == []


def test_log_never_contains_request_headers_it_does_not_name(visits):
    with TestClient(server.build_app()) as http:
        http.post("/mcp", json=INIT, headers={**MCP_HEADERS, "Authorization": "Bearer hunter2", "Cookie": "a=b"})
    text = json.dumps(visits())
    assert "hunter2" not in text and "a=b" not in text


@pytest.mark.parametrize(
    ("ip", "expected"),
    [
        ("198.51.100.23", "198.51.100.0"),
        ("2001:db8:1234:5678::1", "2001:db8:1234::"),
        ("not-an-ip", None),
        (None, None),
    ],
)
def test_truncated_ip(ip, expected):
    assert server._truncated_ip(ip) == expected


def test_landing_page_has_the_logging_notice():
    with TestClient(server.build_app()) as http:
        page = http.get("/", headers={"Accept": "text/html"}).text
    assert "logged for 30 days" in page
