# BRAINSTORM — CV as an MCP server

Raw notes from the first build session (2026-10-22). Not a plan yet.

## Why

- Goal: show AI-native skills to a hiring panel, not just describe them.
- A PDF CV says nothing about AI-native ways of working. Serving the CV over
  MCP makes the CV itself the demo: the panel can query it with an agent.
- Hook: Copilot Studio can add MCP servers as tools, so reviewers could plug
  it into an enterprise agent platform they may already use.

## Decisions made so far

- **Audience / access:** hosted public HTTPS URL that reviewers add as a
  connector (Copilot Studio or Claude). Not a local package, not demo-only.
- **Language:** Python + FastMCP (v4.1.3), Streamable HTTP at `/mcp`.
- **Features chosen:** structured CV tools, a role-fit tool, case studies.
  Contact/booking tool was not chosen.
- **Data:** one file is the single source of truth, re-read on every call.
  Loader supports YAML, JSON and TOML by extension (`CV_PATH`).
  Leaning TOML (comments, no extra dependency) — not final.
- **Placeholders:** anything starting with `TODO` is stripped before it
  reaches a client, so agents never present it as fact.
- **Repo:** `n8k/cv-mcp`, private, `main` branch.

## What exists now

- `server.py`
  - Tools (all read-only): `get_profile`, `get_experience`, `get_skills`,
    `list_case_studies`, `get_case_study`, `search_cv`, `assess_role_fit`
  - Resources: `cv://full` (JSON), `cv://markdown`
  - Prompt: `interview_candidate` (reviewer starter for a screening interview)
  - HTTP routes: `/health`, `/` landing text
- `cv.yaml`, `cv.json`, `cv.toml` — same placeholder CV in three formats
- `examples/cv.example.toml` — fully filled fictional CV ("Alex Morgan"),
  includes one deliberately unmet criterion to test gap reporting
- `Dockerfile`, `requirements.txt`, `README.md`, `.gitignore`, `.dockerignore`
- Tested end to end over real HTTP with an MCP client.

## Role-fit tool — design notes

- Deterministic keyword matching of CV content against each criterion.
  No LLM judgement, so it is explainable in an interview.
- Reports `strong` / `partial` / `gap` per criterion with evidence sources.
  Gaps are shown, not hidden.
- Criteria in the CV file are currently **guesses** — need the real job
  posting criteria pasted in.

## Known issues / bugs

- **Substring matching bug:** keyword "AI" matches inside "claims", "chain",
  etc. Role-fit coverage is inflated until matching is whole-word.
  Fix before anyone on the panel sees ratings.
- YAML gotcha found: `- TODO: text` parses as a dict, not a string.
  Handled in `_is_placeholder`, but another reason to prefer TOML/JSON.
- Markdown resource prints empty headings for roles whose title/dates are
  still TODO (cosmetic; goes away once filled in).

## Open questions

- Will the reviewers' Copilot Studio tenant allow adding an **external** MCP
  server / custom connector? Unknown — check before building the pitch
  around reviewers connecting it themselves. Fallback: live demo in Claude.
- Exact Copilot Studio / Claude connector menu labels in the README were
  written from memory — verify.
- Where to host: Pi + Cloudflare Tunnel vs a container host
  (Fly.io / Cloud Run / Render)?
- Which CV format to keep? Delete the other two once decided.
- Should the server be unauthenticated? Public + no auth means only
  LinkedIn-level content (no phone, no internal project names).

## To do

- [ ] Attach real CV → fill in the CV file (achievements, dates, titles, summary)
- [ ] Paste the real job criteria into `target_role`
- [ ] Fix whole-word matching in `assess_role_fit` (and `search_cv`)
- [ ] Pick one CV format, remove the others
- [ ] Decide hosting; write a NixOS oci-containers module + Cloudflare Tunnel
      if hosting on the Pi
- [ ] Confirm Copilot Studio can connect from the reviewers' network

## Ideas (not decided)

- Add tests (pytest with FastMCP's in-memory client) to CI.
- Case study about the server itself is already in the CV — keep it current
  as the project evolves.
- Usage logging (which tools reviewers call) — would be a nice talking point,
  but think about privacy of internal reviewers first.
