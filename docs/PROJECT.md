# cv-mcp

Status: Approved 2026-10-22. Revised 2026-10-23: production moved from Azure
Container Apps to the Pi behind a Cloudflare Tunnel (approved by merging the PR
that carries this revision).

## Summary

A public, read-only MCP server that serves Nate's CV as structured tools,
resources and an interview prompt. Hiring panels add its URL as a connector in
Copilot Studio or Claude and query the CV conversationally, so the CV itself
demonstrates AI-native work instead of describing it. The repository is
`n8k/cv-mcp`, and the pipeline uses the same name, `cv-mcp`.

## Goals

- G1. A reviewer can add one public HTTPS URL to Copilot Studio or Claude and
  get correct answers about the CV through the server's tools.
- G2. Every fact the server returns comes from one TOML file; placeholders
  never reach a client.
- G3. `assess_role_fit` reports strong / partial / gap per criterion with
  evidence, using whole-word matching, against criteria the reviewer supplies,
  and shows gaps rather than hiding them.
- G4. Production costs nothing to host: it runs on the Pi, and the only running
  cost is the `n8k.org` domain.
- G5. Changes reach production only through testing on the Pi and an explicit
  human approval.

## Non-goals

- Usage logging or analytics of reviewer calls.
- Authentication on the public endpoint.
- A contact or booking tool.
- YAML or JSON CV formats.
- A staging tier.
- A Dockerfile (images are Nix-built).

## Users and use cases

- **Reviewer (hiring panel member):** adds the connector; asks for profile,
  experience, skills and case studies; runs `interview_candidate`; reads
  `assess_role_fit` with criteria from their own job description.
- **Nate (maintainer):** edits the TOML file; checks it in testing with
  `validate_cv`; approves promotion to production.

## Requirements

| ID | Requirement | Priority | Source |
|---|---|---|---|
| R1 | Serve the CV as read-only MCP tools: `get_profile`, `get_experience`, `get_skills`, `list_case_studies`, `get_case_study`, `search_cv`, `assess_role_fit` | must | brainstorm "What exists now"; first build session |
| R2 | Resources `cv://full` (JSON) and `cv://markdown`; prompt `interview_candidate` | must | brainstorm "What exists now" |
| R3 | Streamable HTTP transport at `/mcp`, plus `GET /health` | must | brainstorm "Decisions made so far" |
| R4 | CV content in a single `cv.toml`, re-read on every call; YAML/JSON loaders and files removed | must | Clarify round 1 Q5 |
| R5 | Values starting with `TODO` are stripped before reaching a client | must | brainstorm "Decisions made so far" |
| R6 | `search_cv` and `assess_role_fit` match keywords as whole words, case-insensitively | must | brainstorm "Known issues" |
| R7 | Real CV content replaces all placeholders; only LinkedIn-level information | must | brainstorm "To do"; history-rewrite discussion |
| R8 | Automated tests cover every tool, resource and the prompt, and run in CI | must | Clarify round 1 Q5 |
| R9 | Testing tier on the Pi exposes a control endpoint (`validate_cv`, `reload`) protected by an approval token; production does not | must | Clarify round 1 Q4 |
| R10 | Production on the Pi, public HTTPS at `https://n8k.org` through a Cloudflare Tunnel; no inbound port opened | must | Nate, 2026-10-23 (replaces Azure Container Apps) |
| R11 | Image built by Nix; both tiers run the same aarch64 image | must | Clarify round 1 Q3; revised 2026-10-23 |
| R12 | README connect steps verified against current Copilot Studio and Claude UIs | should | brainstorm "Open questions" |
| R13 | Fictional example CV kept in `examples/` for tests | should | first build session |
| R14 | `assess_role_fit` accepts role criteria supplied by the caller; the CV file holds only generic defaults that name no role | must | Nate, after M2 |
| R15 | Public contact email published; no phone number or street address | must | Nate, after M2 |

## Apps

cv-mcp | python | yes | needs FastMCP

## Milestones

| # | Milestone | Done when |
|---|---|---|
| M1 | Code fixes | TOML only; whole-word matching; control endpoint behind a flag and token; tests pass locally against `examples/cv.example.toml` |
| M2 | Real content | `cv.toml` has no `TODO` left; `validate_cv` reports clean; Nate signs off the content as public-safe |
| M3 | Nix build | Flake provides `devShells.default`, `devShells.ci`, the app package and the image; image builds on the Pi |
| M4 | Testing tier | Woodpecker deploys to the Pi on push to `main`; `https://cv-mcp.local/health` is green; Prometheus probe and Loki logs in place |
| M5 | Production | After approval, Woodpecker promotes the tested image to production on the Pi; `https://n8k.org/health` is green through the tunnel; local and public probes in Prometheus |
| M6 | Reviewer check | Server added as a connector in Claude and answers correctly; Copilot Studio connection tested from the reviewers' side, or the live-demo fallback chosen |

## Delivery

- Tiers: testing and production, both on the Pi. Production is public through a
  Cloudflare Tunnel. No staging.
- Triggers: push to `main` → test, build, deploy to testing. Production is a
  manual promotion after Nate's approval.
- Health endpoint per tier: `GET /health` (ports and hostnames in
  ARCHITECTURE.md).
- Secrets (names only): `CV_MCP_CONTROL_TOKEN` (runtime, testing), the
  Cloudflare Tunnel credentials (runtime, host), both in sops. CI uses only the
  existing registry secrets.

## Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Reviewers' Copilot Studio tenant blocks external MCP servers | medium | high | Test early (M6); fallback is a live demo in Claude |
| FastMCP 4.x not in nixpkgs or too old there | medium | medium | Package it with `buildPythonPackage` in the flake; pin the version |
| Home internet or power outage takes production down | medium | medium | Public probe alerts within minutes; acceptable for a CV. The same image can move to a cloud host later if uptime matters more |
| The public path exposes the home network | low | high | Outbound-only tunnel, no inbound port; the tunnel forwards only `/`, `/health` and `/mcp`; read-only, non-root container; no control endpoint in production; Cloudflare in front (TLS, DDoS protection, optional rate limits) |
| Production competes with CI and other projects for the Pi's CPU and memory | low | low | The server is small (about 60–100 MB); CI is capped at two workflows |
| Personal or employer-sensitive details reach the public file | low | high | Content sign-off in M2; repo history already scrubbed once |

## Assumptions

- Cloudflare's free plan covers the tunnel, DNS and TLS for `n8k.org`.
- Reviewer traffic is small enough for the Pi.
- Gitea on the Pi is the pipeline's source of truth and push-mirrors to the
  public GitHub repo `n8k/cv-mcp` (agent's assumption; overrule if GitHub
  should stay primary).
- Production keeps no data, so it needs no backups beyond the Git repo.

## Open questions

- Exact Copilot Studio and Claude connector menu labels for the README (Nate,
  during M6).
- Whether the CV stays at the domain root (`n8k.org`) or moves to a subdomain
  such as `cv.n8k.org` if the domain gets other uses (one DNS route and one line
  in the NixOS module).
- When to make the GitHub repo public (Nate, after M2 sign-off).

## Decisions

| Date | Decision | Reason | Alternatives considered |
|---|---|---|---|
| 2026-10-22 | Python with FastMCP | Use FastMCP; server already built and tested | Go (official go-sdk), Rust (rmcp) |
| 2026-10-22 | Hosted public HTTPS URL | Reviewers connect it themselves | Local package; live demo only |
| 2026-10-22 | TOML as the only CV format | Comments, no extra dependency, avoids YAML pitfalls | YAML, JSON |
| 2026-10-22 | ~~Production on Azure Container Apps~~ (superseded 2026-10-23) | Free grant; scale-to-zero; same ecosystem as Copilot Studio | Google Cloud Run (equivalent free tier), Render (one-minute cold start), Fly.io (no free plan for new customers) |
| 2026-10-22 | ~~Testing on the Pi, production in the cloud~~ (superseded 2026-10-23) | Keeps the Gitea → Woodpecker flow; only promotion leaves the Pi | Both tiers in the cloud |
| 2026-10-22 | ~~Nix-built x86_64 image; Pi builds, NixyBookPro fallback~~ (superseded 2026-10-23) | Platform rule; cloud hosts run x86_64 | Dockerfile |
| 2026-10-22 | Project and repo both named `cv-mcp` | Project names allow only `[a-z0-9._-]`; one name everywhere | Keep repo as `cv_mcp` |
| 2026-10-22 | No authentication on production | Public, read-only, LinkedIn-level content | Token or OAuth |
| 2026-10-22 | Reviewers supply role-fit criteria | Criteria in a public file would reveal the role; criteria written from the CV always match | Real criteria in `cv.toml` |
| 2026-10-23 | Production on the Pi, public at `n8k.org` through a Cloudflare Tunnel | Azure's setup was too heavy for a small public service; free; production runs byte-for-byte the tested image; no emulation or second registry; always on, so no cold start | Azure Container Apps (chosen 2026-10-22, dropped), Koyeb (0.1 vCPU free instance), Google Cloud Run (billing account, Artifact Registry), Render (one-minute cold start), Oracle Cloud Always Free (a VM to run, hard sign-up) |
| 2026-10-23 | One aarch64 image for both tiers | Both tiers run on the Pi | x86_64 image built under emulation for a cloud host |

## Sources

- docs/BRAINSTORM.md
- Azure Container Apps pricing: https://azure.microsoft.com/pricing/details/container-apps/
- Cloud Run pricing: https://cloud.google.com/run/pricing
- Render free tier: https://render.com/docs/free
- Fly.io discontinued plans: https://docs.fly.io/about/discontinued-plans
- Cloudflare Tunnel: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/
- Cloudflare Tunnel ingress rules: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/configure-tunnels/local-management/ingress/
- MCP Go SDK releases (checked, not chosen): https://github.com/modelcontextprotocol/go-sdk/releases
- rmcp (checked, not chosen): https://docs.rs/crate/rmcp
