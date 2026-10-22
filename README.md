# CV as an MCP server

Nathan Kemppainen's CV, served over the Model Context Protocol so any agent
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

Testing runs on the Pi; production runs on Azure Container Apps. The image is
built by the flake (`nix build .#image`, no Dockerfile). Pipeline, tiers,
secrets and runbook: [PIPELINE.md](PIPELINE.md); design:
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Connect

Unverified — confirm these menu names during milestone M6:

- **Copilot Studio:** agent → Tools → Add a tool → Model Context Protocol → server URL `https://<host>/mcp`, no auth.
- **Claude:** Settings → Connectors → Add custom connector → same URL.
