# cv-mcp: pipeline

Generated with the pipeline-factory skill from `docs/PROJECT.md` and
`docs/ARCHITECTURE.md` (approved 2026-10-22, revised 2026-10-23 for production on
the Pi; hashes in `intake.json`). The skill's renderer is not shipped yet, so the
files were written by hand to its spec.

| | |
|---|---|
| Project | `cv-mcp` |
| Repos | GitHub `n8k/cv-mcp`, Gitea `nate/cv-mcp` (what Woodpecker builds) |
| App | `cv-mcp`, Python, Tier 2. Reason: needs FastMCP (PROJECT.md `## Apps`) |
| Toolchain | Nix: `devShells.{default,ci}`, `packages.{cv-mcp,image}`, `checks.tests` |
| Branching | trunk (`main`) |
| Tiers | testing and production, both on the Pi (docker compose), from the same aarch64 image. Production is public at `https://n8k.org` through a Cloudflare Tunnel. No staging |
| Host module | NixOS config repo: `hosts/nixos/projects/cv-mcp-pipeline.nix`, `modules/domains/cv-mcp-domains.nix` |

## Flow

```
push to main ─► ci.yaml ─► cd.yaml ──────────────► testing (Pi, https://cv-mcp.local)
                lint        writes testing/release.env
                test        verify /health
                image (aarch64)
                scan
                publish gitea.local/nate/cv-mcp:<sha>, :testing

human: Promote → production ─► release.yaml ────► production (Pi, https://n8k.org)
                               check (main, image exists)
                               tag :production
                               writes production/release.env
                               verify https://n8k.org/health
```

Pull requests run `lint` and `test` only. Production runs exactly the image that
testing ran and CI scanned; promotion never rebuilds.

## Workflows

### `.woodpecker/ci.yaml`: push to `main`, manual, pull requests

| Step | Runs | When |
|---|---|---|
| lint | `nix develop .#ci -c ruff check .`, `nixfmt --check` on the Nix files | always |
| test | `nix flake check` (pytest suite + app package as derivations) | always |
| image | `"$(nix build --print-out-paths .#image)" > image.tar` (aarch64) | main |
| scan | `trivy image --input image.tar --ignorefile .trivyignore.yaml`, fails on fixable HIGH/CRITICAL | main |
| publish | `skopeo copy` to `gitea.local/nate/cv-mcp:${CI_COMMIT_SHA}`, then `:testing` → same digest | main |

The scan runs on the tar before the push, so an image that fails it is never
published.

### `.woodpecker/cd.yaml`: after `ci` on `main`

| Step | Runs |
|---|---|
| deploy-testing | copies `deploy/compose.yaml` and writes `release.env` (`APP_IMAGE=gitea.local/nate/cv-mcp:<sha>`) to `/opt/cicd/cv-mcp/testing/` |
| verify | `https://cv-mcp.local/health` healthy twice in a row (up to ~3.5 min) |

The host's `cv-mcp-testing-deploy.path` sees the new `release.env` and runs
`docker compose up --wait`. The pipeline never touches Docker.

### `.woodpecker/release.yaml`: deployment event, target `production`

Started only by a person: in Woodpecker, open a green `main` pipeline and choose
Promote → `production` (or `woodpecker-cli pipeline deploy nate/cv-mcp <number>
production`). That is the approval gate (PROJECT.md G5); nothing reaches
production on push.

| Step | Runs |
|---|---|
| check | refuses commits not on `main`; `skopeo inspect` that `gitea.local/nate/cv-mcp:<sha>` exists (CI publishes only scanned images) |
| tag | `:production` → same digest (a label only) |
| deploy | copies `deploy/compose.yaml` and writes `release.env` (`APP_IMAGE=gitea.local/nate/cv-mcp:<sha>`) to `/opt/cicd/cv-mcp/production/` |
| verify | `https://n8k.org/health` through Cloudflare and the tunnel, healthy twice in a row (up to ~3.5 min) |

The host's `cv-mcp-production-deploy.path` sees the new `release.env` and runs
`docker compose up --wait`, as for testing.

## Tiers and deployed commits

Fill in after each deploy (the SHA is also the image tag).

| Tier | Where | Image | Deployed SHA | Date |
|---|---|---|---|---|
| testing | Pi, `/opt/cicd/cv-mcp/testing`, port 8250 | `gitea.local/nate/cv-mcp:<sha>` | `0bbe58052719047d638b0023f898205f962f9e22` (main after #3) | 2026-10-23 |
| production | Pi, `/opt/cicd/cv-mcp/production`, port 8251 | `gitea.local/nate/cv-mcp:<sha>` | not deployed yet | |

Which commit is running: `cat /opt/cicd/cv-mcp/<tier>/release.env` on the Pi.

## Milestone sign-offs

### M2: real content (2026-10-23)

Signed off by Nate as public-safe, against the CV served by the testing tier at
`0bbe580` (`cv.toml` as committed on `main`).

- `validate_cv` through `https://cv-mcp.local/control/mcp` reports clean:
  `ok: true`, no `errors`, no `todos`, no `criteria_with_no_evidence`. The same
  result from `server.validate()` run against `cv.toml` at this commit.
- `cv://markdown` read through the read-only `/mcp` endpoint and checked:
  - contact is the public email only; no phone number or street address (R15);
  - LinkedIn-level detail only, nothing employer-sensitive (R7);
  - `[target_role]` holds generic defaults that name no role (R14);
  - no placeholder text reaches the client (R5).

Any later change to `cv.toml` needs the same check before it is promoted to
production: `validate_cv` clean on testing, and the Markdown read through once.

## Rollback

- Testing: open the older commit's pipeline in Woodpecker and restart it (or
  run `cd.yaml` manually on that commit). It rewrites `release.env` with the
  older SHA tag.
- Production: promote the older commit's pipeline to `production`. It rewrites
  production's `release.env` with that SHA tag.

Nothing is rebuilt either way. Never deploy `:testing`, `:production` or
`:latest`; those tags are labels only.

## Hostnames, ports and vhosts

| Name | Tier | Port (127.0.0.1) | How it is reached |
|---|---|---|---|
| `cv-mcp.local` | testing | 8250 → container 8000 | LAN, host nginx with the shared certificate. `/mcp`, `/control/mcp`, `/health`, `/` |
| `n8k.org` | production | 8251 → container 8000 | Public: Cloudflare DNS + TLS → tunnel `988e0353-cb31-49a7-9691-aa7822356bfc` → `cloudflared` on the Pi. Only `/`, `/health`, `/mcp`; anything else 404 |

- Generated by `modules/domains/cv-mcp-domains.nix`: `cv-mcp.local`.
- nginx vhosts in `cv-mcp-pipeline.nix`: `cv-mcp.local`. The two lists match.
- `n8k.org` is a Cloudflare DNS record (`cloudflared tunnel route dns cv-mcp
  n8k.org`, CNAME to `<tunnel id>.cfargotunnel.com`), not a local name; the
  tunnel's ingress rules are in `cv-mcp-pipeline.nix`.
- Port block 8250–8259 (quartet 8220s, 321fire 8230s, star_tensai 8240s). The
  module asserts no collision with `services.cicdCenter.ports`.

## Secrets (names only)

### Woodpecker

| Secret | Scope | Where it comes from | Used by |
|---|---|---|---|
| `ci_registry_user` | global | cicd.nix `globalSecrets` (exists) | ci.yaml publish; release.yaml check, tag |
| `ci_registry_password` | global | cicd.nix `globalSecrets` (exists) | ci.yaml publish; release.yaml check, tag |

No repo secrets: nothing leaves the Pi.

### sops-nix (runtime, on the Pi)

| Key in secrets.yaml | Used by |
|---|---|
| `cv-mcp/testing/CV_MCP_CONTROL_TOKEN` | testing `/control/mcp` bearer token |
| `cloudflared/cv-mcp` | the tunnel's credentials JSON (`AccountTag`, `TunnelID`, `TunnelSecret`), loaded by `cloudflared` as a systemd credential |

The Cloudflare account certificate (`cert.pem`, from `cloudflared tunnel login`)
can manage every tunnel on the account and is not needed to run this one; it is
not kept on the Pi. Production has no runtime secrets.

## MCP endpoints

| Endpoint | Tier | Access |
|---|---|---|
| `https://cv-mcp.local/mcp` | testing | read-only, LAN only |
| `https://cv-mcp.local/control/mcp` | testing | `Authorization: Bearer <CV_MCP_CONTROL_TOKEN>`; tools `validate_cv`, `reload` |
| `https://n8k.org/mcp` | production | read-only, public, no auth (PROJECT.md decision) |

These are paths on the one server, not separate MCP containers. Production
serves no control endpoint: the module asserts `control` is only enabled on
dev/testing, and the tunnel does not forward `/control` paths anyway.
Host-level MCP servers (Gitea, Grafana, Woodpecker) belong to `cicd.nix`.

## Observability

- Testing: blackbox probe `cv-mcp-testing` → `http://127.0.0.1:8250/health`.
  Logs: Loki `{container="cv-mcp-testing-cv-mcp-1"}`.
- Production: two probes, `cv-mcp-production` → `http://127.0.0.1:8251/health`
  and `cv-mcp-production-public` → `https://n8k.org/health` (through Cloudflare
  and the tunnel, so a tunnel or DNS failure alerts too). Off until the first
  promotion (`probe = false`); a probe on nothing alerts after 3 minutes.
  Logs: Loki `{container="cv-mcp-production-cv-mcp-1"}`; the tunnel itself is
  `{unit="cloudflared-tunnel-988e0353-cb31-49a7-9691-aa7822356bfc.service"}`.
- `ProjectProbeFailed` fires after 3 minutes of failed probes.

## Exceptions and deviations

| What | Why |
|---|---|
| FastMCP 4.1.3 and 8 dependencies are wheel derivations in `nix/python-packages.nix` | nixos-26.05 has fastmcp 3.2.3 and mcp 1.26.0. Added beside the Python set (no overrides), so the rest stay cache hits; `certifi`, `idna`, `aiofile` pins relaxed |
| `.trivyignore.yaml` accepts 13 findings | All in nixpkgs-provided packages (aiohttp, anyio, certifi, cryptography, python-multipart, starlette, urllib3) that cannot be bumped without overriding the set. Each has a reason and expires 2026-11-22, so the scan fails again and forces a re-check after `nix flake update` |
| `skopeo --insecure-policy` | The step container has no `/etc/containers/policy.json`; this is the same accept-anything policy skopeo ships as its default |
| Testing deploys with compose, not oci-containers | ARCHITECTURE.md says "oci-container"; the oci-containers method needs a Nix commit and rebuild per deploy, which contradicts M4 (deploy on push). Compose is the skill default and what hippo/321fire use |
| Testing hostname `cv-mcp.local`, not `cv-mcp.testing` | Named that way in ARCHITECTURE.md and M4 |
| `extra_hosts` on steps | `woodpecker-cli lint` (3.19) warns that the schema does not list it; the agent accepts it for Trusted repos (same as clone) |
| Production on the Pi, not a cloud host | Revised 2026-10-23 (PROJECT.md Decisions): Azure's setup was too heavy; the tunnel keeps hosting free and runs the tested image unchanged. Security mitigations are in ARCHITECTURE.md |

## Pre-flight

Checked while generating (2026-10-22, in a scratch environment, not on the Pi):

- [x] Flake evaluates; `nix flake check` passes (49 tests) on x86_64 against
      nixos-26.05.
- [x] `nix build --dry-run` for aarch64 builds only the 9 wheels, the Python
      env, the app, the image script and the shells; everything else is
      fetched from cache.nixos.org.
- [x] Image streams, runs, `/health` answers; `cv-mcp-health` works.
- [x] `ruff`, `nixfmt --check`, `woodpecker-cli lint` (all three workflows valid).
- [x] Trivy scan passes with the ignore file.
- [x] NixOS module: parses, statix/deadnix/nixfmt clean, evaluates in a stub
      system (vhost, hostnames, units, sops template, assertions).

To do on the Pi before the first run:

- [x] `flake.lock` committed: nixpkgs `nixos-26.05` at `da6dc0e7` (the
      revision CI resolved on 2026-10-22, and the one tested against). CI
      cannot create or update the lock (the alpine step image has no `git`),
      so update it locally with `nix flake update` and commit it. The Pi's
      own NixOS config pins an older nixpkgs; both are cache.nixos.org hits.
      Check on the Pi: `nix build --dry-run .#cv-mcp .#image`.
- [x] Gitea repo `nate/cv-mcp` exists (pull mirror of GitHub, like the NixOS
      repo). PROJECT.md assumed Gitea primary with a push mirror to GitHub;
      the work so far is on GitHub, so this follows the NixOS repo's pattern.
      Overrule if Gitea should be primary.
- [x] NixOS config switched with `cv-mcp-pipeline.nix`, `cv-mcp-domains.nix`
      and `"nate/cv-mcp"` in `woodpecker.repos` (activates the repo, sets
      Trusted network + volumes, recreates the webhook).
      `getent hosts cv-mcp.local` resolves; SAN list has it.
- [x] First `main` push green (after `flake.lock`, #3); `probe = true` and
      rebuilt.
- [ ] Probe `cv-mcp-testing` up in Prometheus and container logs in Loki
      (Grafana Explore: `probe_success{project="cv-mcp-testing"}`,
      `{container="cv-mcp-testing-cv-mcp-1"}`).
- [x] Control endpoint: sops key added, `control = true`; 200 with the token,
      401 without. Token rotated once (2026-10-23) after it was exposed.

Before the first promotion (M5):

- [x] Cloudflare: `n8k.org` on the account, tunnel `cv-mcp` created, DNS route
      `n8k.org` → tunnel (done by hand, 2026-10-23).
- [x] Tunnel credentials JSON in sops as `cloudflared/cv-mcp`.
- [ ] Plain-text credentials removed from the Pi (`shred -u
      ~/.cloudflared/<id>.json`) and `cert.pem` deleted or moved off the Pi.
- [ ] NixOS config switched with the production tier and tunnel. Check:
      `systemctl status cloudflared-tunnel-988e0353-cb31-49a7-9691-aa7822356bfc`
      is active, and `curl -s -o /dev/null -w '%{http_code}\n' https://n8k.org/health`
      gives 502 or 404 until the first promotion (the tunnel is up, nothing
      listens on 8251 yet).
- [ ] First promotion green; then `probe = true` for production and rebuild;
      both production probes up in Prometheus.
- [ ] Optional: a Cloudflare rate-limiting rule for `n8k.org` (dashboard →
      Security → WAF), if abuse ever shows up.
- [ ] Alertmanager receiver confirmed with a test alert, or alerts accepted as
      record-only.
