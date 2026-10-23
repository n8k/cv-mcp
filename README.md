# CV as an MCP server

Nate Kemppainen's CV, served over the Model Context Protocol so any agent
(Copilot Studio, Claude, etc.) can query it instead of reading a PDF.

Plan and design: [docs/PROJECT.md](docs/PROJECT.md),
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## What it exposes

Read-only endpoint, `/mcp`:

| Type | Name | What it does |
|---|---|---|
| Tool | `get_profile` | Name, headline, contact, summary |
| Tool | `get_experience` | Work history, optional company filter |
| Tool | `get_skills` | Skills by category |
| Tool | `list_case_studies` / `get_case_study` | STAR stories |
| Tool | `search_cv` | Whole-word keyword search across everything (simple plurals included) |
| Tool | `assess_role_fit` | Evidence against criteria **you supply** from your job description; gaps shown, not hidden |
| Resource | `cv://full`, `cv://markdown` | Whole CV as JSON / Markdown |
| Prompt | `interview_candidate` | Paste a job description; the agent derives criteria and runs a structured screening |

Control endpoint, `/control/mcp` — only when `CV_MCP_CONTROL_TOKEN` is set
(testing tier), and only with `Authorization: Bearer <token>`:

| Tool | What it does |
|---|---|
| `validate_cv` | Parse errors, empty required fields, remaining TODOs, criteria with no evidence |
| `reload` | Re-reads the file and reports what was loaded |

`GET /health` returns 200 when the CV file parses, 503 otherwise.

## The CV file

Everything comes from `cv.toml`, re-read on every call, so edits go live
without a restart. Any value starting with `TODO` is hidden from clients until
you replace it. The file is public: keep it to LinkedIn-level detail.

`[target_role]` in the file holds generic default criteria, used only when a
caller supplies none; they deliberately name no specific role.

`examples/cv.example.toml` is a fully populated, fictional CV used by the tests.

## Run locally

Everything comes from the Nix flake (`nix develop` for a shell with Python,
FastMCP and pytest; `nix run` to start the server):

```bash
nix develop
python server.py                                     # http://localhost:8000/mcp
CV_PATH=examples/cv.example.toml python server.py    # fictional example CV
CV_MCP_CONTROL_TOKEN=dev python server.py            # also serves /control/mcp
CV_TRANSPORT=stdio python server.py                  # stdio, for local clients
```

| Variable | Default | Purpose |
|---|---|---|
| `CV_PATH` | `cv.toml` next to `server.py` | CV file to serve |
| `CV_MCP_CONTROL_TOKEN` | unset | Enables `/control/mcp` |
| `HOST`, `PORT` | `0.0.0.0`, `8000` | Bind address |
| `CV_TRANSPORT` | HTTP | `stdio` to run over stdio |

## Test

```bash
nix develop -c pytest      # interactive
nix flake check            # what CI runs: the suite as a Nix build
```

## Deploy

Both tiers run on the Pi from the same image: testing on the home network, and
production publicly at `https://n8k.org` through a Cloudflare Tunnel. The image is
built by the flake (`nix build .#image`, no Dockerfile). Pipeline, tiers,
secrets and runbook: [PIPELINE.md](PIPELINE.md); design:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Connect

Server URL: `https://n8k.org/mcp`. No authentication.

**Claude** (verified 2026-10-23):

1. Settings → Connectors → **Add custom connector**. Name it (e.g. `CV-MCP`),
   paste the URL, leave the OAuth fields empty. On Team or Enterprise plans an
   owner may need to allow custom connectors first.
2. In a chat, open the tools menu under the message box and switch the
   connector on. Claude asks before the first tool call; every tool is
   read-only.
3. Try: "Summarise Nate's experience", "What has he done with agentic AI?",
   or paste a job description and ask "Assess his fit for this role and show
   the gaps." The `interview_candidate` prompt and the `cv://markdown`
   resource are in the same menu, where the client shows MCP prompts.

Sample prompts for reviewers (start a fresh chat with the connector on):

- "Introduce Nate in a short paragraph, then list his case studies with one line each."
- "Tell me, in STAR format, about a time Nate changed how an organisation works."
- "Give me a STAR example of Nate using AI or automation to save leadership time."
- "Walk me through his largest delivery programme as Situation, Task, Action and Result."
- Paste a job description, then: "Assess Nate's fit criterion by criterion, back up
  each strong rating with the most relevant case study in STAR form, and show the gaps."
- "Using his CV tools only, write three behavioural interview questions for this
  role, each tied to one of his case studies."

Asking for examples, stories or "STAR" makes the client call `get_case_study`;
adding "use the CV tools" keeps it from answering from memory.

**Copilot Studio** (not yet verified): agent → Tools → Add a tool → Model
Context Protocol → the URL above, no authentication. If the tenant blocks
external MCP servers, the fallback is a live demo in Claude.
