# Facebook Creator Agent

Turn a topic into a **scripted, licensed, edited video package for a Facebook Page** — then review it, approve it explicitly, and publish it (dry-run by default) from any MCP-capable coding agent (OpenCode, Codex, VS Code, Claude, Antigravity, Cursor, …).

This repository is the product:

- **MCP server** (`facebook_creator_agent`, Python, stdio + streamable HTTP) exposing **18 typed `creator_*` tools**
- **OpenCode operator agent** (`.opencode/agents/facebook-creator.md`) that drives the workflow
- **62 automated tests** (unit / security / MCP / real-FFmpeg integration / end-to-end)
- **Per-client setup** for Codex, VS Code, Claude, Antigravity, Cursor, generic MCP (below)

> New here? **Install** → **Configure** → **Connect your coding agent** → **Make a video**.

---

## Contents

1. [Functions & features](#functions--features)
2. [How it works](#how-it-works)
3. [How to install](#how-to-install)
4. [How to configure](#how-to-configure)
5. [How to use it](#how-to-use-it)
6. [Supported coding agents & clients](#supported-coding-agents--clients)
7. [Tool reference (18)](#tool-reference-18)
8. [Workspace layout](#project-workspace-layout)
9. [Testing](#testing)
10. [Security model](#security-model)
11. [Known limitations](#known-limitations)

---

## Functions & features

**Production pipeline (what it can produce):**

| Output | Detail |
|---|---|
| Research brief | optional; only when a research endpoint is configured; never fabricates citations |
| Script + storyboard | scene-by-scene, per-scene media queries, on-screen text, target-duration validation, revision flow |
| Licensed media | allowlisted providers only, license policy (default-deny), provenance + SHA-256 per asset, user-supplied intake behind a rights confirmation |
| Narration | command-based TTS engine (any engine you configure) **or** a narration file you supply |
| Captions | SRT generated with alignment when available, documented approximate-timing fallback otherwise |
| Edited video | real FFmpeg: trim / crop / scale / pad to 9:16 or 16:9, concat, narration mix, optional music ducking, subtitle burn-in or sidecar, H.264/AAC MP4 |
| Validation | ffprobe stream/dimension/duration checks + full decode test |
| Cover image | extracted on demand |
| Review package | preview + caption + metadata + asset manifest + warnings + hashes |
| Facebook publishing | official Meta Graph API adapter, guarded upload, capability checks, status reconciliation |

**Safety features (implemented, tested):**

- **Explicit-approval publishing** — an approval is cryptographically bound to the exact Page, media SHA-256, caption/metadata SHA-256, action, expiry and a one-time nonce; single-use; any change invalidates it.
- **Dry-run by default** — real uploads require *both* `FCA_ENABLE_LIVE_PUBLISH=1` *and* a configured token.
- **Unknown-outcome protection** — upload attempts are journaled before the network call; `UNKNOWN` results are reconciled via `creator_get_publish_status` instead of blind retries (no duplicate posts).
- **Default-deny media rights** — unknown or ambiguous licenses are rejected / flagged for manual review; every asset keeps provenance.
- **Secret hygiene** — tokens/keys live only in environment/`.env`; logs are redaction-filtered; nothing sensitive in project files or manifests.
- **Path jail, host allowlists, size caps, content-type + magic-byte checks, hashed filenames** for all writes and downloads; subprocesses run as argv arrays (never a shell).

**Reliability features:** explicit state machine, stage manifests + resume, idempotency, cancellation (terminates FFmpeg), bounded timeouts/retries, structured error taxonomy `{code, safe_explanation, retryable, stage, next_action}`, `creator_doctor` diagnostics.

**Offline-friendly:** with no cloud credentials you can still run everything except script generation (needs an LLM endpoint) and live publishing — using fixture folder media + a narration file you supply.

---

## How it works

```
topic / prompt
   │
   ▼
[1] creator_doctor             environment & config diagnostics
[2] creator_create_project     workspace/projects/<project-id>/
[3] creator_generate_script    brief → script → storyboard      (OpenAI-compatible endpoint, schema-validated)
[4] creator_search_media       allowlisted providers + license policy (default deny)
[5] creator_download_assets    host/size/type checks, SHA-256, provenance
[6] creator_generate_narration TTS command engine — or your own audio file
[7] creator_build_timeline     deterministic edit decision list (EDL)
[8] creator_render_preview     FFmpeg: 9:16 / 16:9, mix, captions → preview.mp4 + final.mp4
[9] creator_validate_render    ffprobe streams/duration + decode test
[10] creator_get_review_package preview + caption + metadata + asset manifest + hashes
──────────────  YOU REVIEW  ──────────────
[11] creator_approve_publish   approval bound to Page + media hash + payload hash + expiry + nonce
[12] creator_publish_video     dry-run by default → journal → upload → SUCCEEDED / FAILED / UNKNOWN
[13] creator_get_publish_status reconcile UNKNOWN outcomes; never blind-retry
```

State machine per project:
`NEW → SCRIPT_READY → ASSETS_READY → NARRATION_READY → RENDERED → REVIEW_REQUIRED → APPROVED → PUBLISHING → PUBLISHED` (+ `FAILED`, `CANCELLED` and documented resume edges — see `ARCHITECTURE.md` §6).

The **application enforces the rules, not the model**: the publisher independently revalidates the approval, hashes, Page and token at upload time. A model (or client) cannot bypass the guard by asserting that the user approved.

Design details: [`ARCHITECTURE.md`](ARCHITECTURE.md) · [`PRD.md`](PRD.md) · status matrix: [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md).

---

## How to install

**Requirements**

| Requirement | Notes |
|---|---|
| Python **3.10+** | tested on 3.10.0, Windows |
| `ffmpeg` + `ffprobe` on `PATH` | tested with 9.0.2; override with `FCA_FFMPEG_PATH` / `FCA_FFPROBE_PATH` |
| Network (optional) | only for LLM endpoint, stock providers, Meta API |

**Windows** (from the repository folder):

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m facebook_creator_agent tools     :: offline sanity check → 18 tools
.venv\Scripts\python.exe -m facebook_creator_agent doctor    :: diagnostics (JSON; exit 0 ready/partial, 2 blocked)
```

**macOS / Linux:**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
.venv/bin/python -m facebook_creator_agent tools
.venv/bin/python -m facebook_creator_agent doctor
```

No global install is needed — MCP clients launch the venv Python directly.

---

## How to configure

Copy `.env.example` → `.env` (git-ignored) and fill in what you have. **Non-secret settings also work via environment variables or tool arguments.** The server loads `.env` from its base directory (default: its working directory; pin it with `FCA_BASE_DIR`).

| Variable | Purpose |
|---|---|
| `FCA_LLM_BASE_URL` / `FCA_LLM_MODEL` / `FCA_LLM_API_KEY` | OpenAI-compatible text endpoint (script + metadata generation) |
| `FCA_RESEARCH_BASE_URL` / `FCA_RESEARCH_API_KEY` | optional topic research; unset = research reported unavailable |
| `FCA_TTS_COMMAND_JSON` | your TTS engine as a JSON argv template, e.g. `["piper","--output_file","{output}","--input_file","{text_file}"]` |
| `FCA_FIXTURE_MEDIA_DIR` | local folder of licensed clips/images for offline runs |
| `FCA_MEDIA_ALLOWED_PROVIDERS` / `FCA_MEDIA_ALLOWED_LICENSES` | media allowlists (default `fixture,user`; default-deny policy) |
| `FCA_PEXELS_API_KEY` | optional stock provider — adapter implemented, **not live-verified** |
| `FCA_FACEBOOK_ACCESS_TOKEN` / `FCA_FACEBOOK_PAGE_ID` | Meta Page publishing (still dry-run until enabled) |
| `FCA_ENABLE_LIVE_PUBLISH` | `1` arms real uploads together with a token |
| `FCA_WORKSPACE_ROOT` | where project workspaces live (default `./workspace`) |
| `FCA_FFMPEG_PATH` / `FCA_FFPROBE_PATH` | override binary locations |
| `FCA_RENDER_FPS` / `FCA_RENDER_MAX_DURATION_SECONDS` / `FCA_RENDER_TIMEOUT_SECONDS` | render limits |
| `FCA_MEDIA_MAX_DOWNLOAD_MB` / `FCA_MEDIA_MAX_REDIRECTS` | download guard limits |

Diagnostics: `.venv\Scripts\python.exe -m facebook_creator_agent doctor --probe-network` (also tests the LLM endpoint). Print the effective redacted config with `... print-config`.

**No TTS engine?** Pass `narration_path` to `creator_generate_narration` with your own audio file.

---

## How to use it

### 1. Connect a coding agent

The server speaks standard **stdio MCP** — any MCP client can launch it with:

```
command: C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe
args:    ["-m", "facebook_creator_agent"]
```

Plus set `FCA_BASE_DIR` (and `cwd` where the client supports it) to this folder. Per-client blocks: see the next section.

### 2. Ask for a video

Talk to your agent in plain language:

- *"make a facebook video about three facts about MCP servers"*
- *"make a 30-second facebook reel about home coffee brewing, in Hindi, bold tone"*
- *"show me the review package for project coffee-reel"*

With the shipped OpenCode agent, that's:

> **`facebook-creator` agent** → *"make a facebook video about three facts about MCP servers"*

The agent runs the pipeline (doctor → project → script → media → narration → timeline → render → validation → review package) and stops at **review**. If the topic is missing it asks exactly one question.

### 3. Review & approve

Nothing is published until **you** say so. Review the preview and caption, then:

> *"approve it and publish"* — or — *"change the caption to … and re-render"*

The agent records your approval via `creator_approve_publish` (bound to the exact payload you reviewed) and calls `creator_publish_video`. Any later change to media/caption/Page invalidates the approval (`PUBLISH_APPROVAL_STALE`).

### 4. Publishing for real

Publishing stays **dry-run** until all three hold:

1. `FCA_FACEBOOK_ACCESS_TOKEN` is set (official Meta OAuth — never a password; use `creator_connect_facebook` for the exact official steps),
2. `FCA_FACEBOOK_PAGE_ID` is set,
3. `FCA_ENABLE_LIVE_PUBLISH=1`.

Then the publisher revalidates token, Page, permissions, and hashes before upload, journals the attempt, and reports `SUCCEEDED` / `FAILED` / `UNKNOWN` — with `creator_get_publish_status` for reconciliation.

### 5. CLI quick reference

```bat
.venv\Scripts\python.exe -m facebook_creator_agent                  :: MCP server, stdio (what clients spawn)
.venv\Scripts\python.exe -m facebook_creator_agent serve --transport streamable-http --port 8765
.venv\Scripts\python.exe -m facebook_creator_agent doctor [--probe-network]
.venv\Scripts\python.exe -m facebook_creator_agent tools
.venv\Scripts\python.exe -m facebook_creator_agent print-config     :: redacted effective config
.venv\Scripts\python.exe scripts\verify_mcp_boot.py                 :: MCP handshake + tools/list proof
```

---

## Supported coding agents & clients

The server is a standard stdio MCP server, so it works with any MCP-capable client. Configs below follow each client's current official MCP documentation. **Verified live in this repository: OpenCode.** The launch command is identical everywhere — only the config file and key names differ.

| Client | Config file | Top-level key |
|---|---|---|
| **OpenCode** | `opencode.json` (ships in this repo root) | `mcp.servers` |
| **OpenAI Codex CLI** | `~/.codex/config.toml` (or `.codex/config.toml` per project) | `mcp_servers` |
| **VS Code (Copilot agent mode)** | `.vscode/mcp.json` (workspace) / user `mcp.json` | `servers` |
| **VS Code portable** | `.mcp.json` (workspace root) | `mcpServers` |
| **Claude Code** | `.mcp.json` or `claude mcp add` | `mcpServers` |
| **Claude Desktop** | `%APPDATA%\Claude\claude_desktop_config.json` | `mcpServers` |
| **Google Antigravity** | `~/.gemini/config/mcp_config.json` or `.agents/mcp_config.json` | `mcpServers` |
| **Cursor** | `~/.cursor/mcp.json` or `.cursor/mcp.json` | `mcpServers` |
| **Any other MCP client** | its stdio config | command `…python.exe`, args `["-m","facebook_creator_agent"]` |

> Replace `C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs` in the examples if you move the folder. JSON path separators: forward slashes work on Windows.

### OpenCode ✅ (verified live in this repo)

Already shipped — [`opencode.json`](opencode.json). Server name `facebook`, so the tools appear as `facebook_creator_*`. Project agent: [`.opencode/agents/facebook-creator.md`](.opencode/agents/facebook-creator.md).

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "facebook": {
        "type": "local",
        "command": ["C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe", "-m", "facebook_creator_agent"],
        "cwd": ".",
        "environment": { "FCA_BASE_DIR": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs" }
      }
    }
  }
}
```

### OpenAI Codex CLI

Add to `~/.codex/config.toml` (global) or `.codex/config.toml` (project-scoped):

```toml
[mcp_servers.facebook-creator]
command = "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe"
args = ["-m", "facebook_creator_agent"]
cwd = "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs"
env = { "FCA_BASE_DIR" = "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs" }
tool_timeout_sec = 300
```

(`tool_timeout_sec` is raised because renders can exceed the 60 s default. Codex CLI, the IDE extension, and the ChatGPT desktop app share `config.toml`.)

### VS Code (GitHub Copilot, agent mode)

Workspace `.vscode/mcp.json`:

```json
{
  "servers": {
    "facebook-creator": {
      "type": "stdio",
      "command": "${workspaceFolder}/.venv/Scripts/python.exe",
      "args": ["-m", "facebook_creator_agent"],
      "cwd": "${workspaceFolder}",
      "envFile": "${workspaceFolder}/.env"
    }
  }
}
```

Open the file → **Start** → approve the trust prompt → the tools appear in Copilot Chat (agent mode). The portable workspace-root `.mcp.json` (top-level `mcpServers`, no `cwd`) also works — set `FCA_BASE_DIR` in `env` there.

### Claude Code

```bat
claude mcp add facebook-creator -e FCA_BASE_DIR=C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs -- C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe -m facebook_creator_agent
```

Or commit a project `.mcp.json`:

```json
{
  "mcpServers": {
    "facebook-creator": {
      "command": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe",
      "args": ["-m", "facebook_creator_agent"],
      "env": { "FCA_BASE_DIR": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs" }
    }
  }
}
```

### Claude Desktop

`%APPDATA%\Claude\claude_desktop_config.json` (Windows) / `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS):

```json
{
  "mcpServers": {
    "facebook-creator": {
      "command": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe",
      "args": ["-m", "facebook_creator_agent"],
      "env": { "FCA_BASE_DIR": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs" }
    }
  }
}
```

Restart Claude Desktop after editing.

### Google Antigravity

`~/.gemini/config/mcp_config.json` (global) or `.agents/mcp_config.json` (workspace), or via **… → MCP Servers → Manage MCP Servers → View raw config**:

```json
{
  "mcpServers": {
    "facebook-creator": {
      "command": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe",
      "args": ["-m", "facebook_creator_agent"],
      "cwd": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs",
      "env": { "FCA_BASE_DIR": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs" }
    }
  }
}
```

### Cursor

`~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (project):

```json
{
  "mcpServers": {
    "facebook-creator": {
      "command": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs/.venv/Scripts/python.exe",
      "args": ["-m", "facebook_creator_agent"],
      "env": { "FCA_BASE_DIR": "C:/Users/KRUNAL/Downloads/facebook_creator_agent_docs" }
    }
  }
}
```

### Any other MCP client

- **Transport:** stdio (`command` + `args`) — no HTTP needed. A streamable-HTTP mode also exists (`serve --transport streamable-http --port 8765`) for clients that prefer URLs.
- **Launch:** `<repo>/.venv/Scripts/python.exe` (Windows) or `<repo>/.venv/bin/python` (macOS/Linux) with args `-m facebook_creator_agent`.
- **Env:** set `FCA_BASE_DIR` to the repo folder unless the client launches with that working directory.
- **Tool names:** canonical `creator_*`; most clients prefix with the server name (e.g. `facebook_creator_*` in OpenCode).

---

## Tool reference (18)

| Tool | What it does | Side effect |
|---|---|---|
| `creator_doctor` | dependencies/config/connectivity diagnostics + remediation | read-only |
| `creator_list_providers` | configured LLM/TTS/media providers (secrets redacted) | read-only |
| `creator_test_llm` | minimal live request to your endpoint | network |
| `creator_connect_facebook` | official Meta authorization guide + token/page identity check | none |
| `creator_list_pages` | authorized Pages + granted capabilities | read-only Graph |
| `creator_create_project` | isolated project workspace (idempotent) | local write |
| `creator_generate_script` | brief → script → storyboard, validated, revisionable | model + local write |
| `creator_search_media` | allowlisted providers, license policy applied | network |
| `creator_download_assets` | guarded download; user-media intake with rights confirmation | local/network |
| `creator_generate_narration` | TTS command engine or your audio file; audio validated | local write |
| `creator_build_timeline` | deterministic EDL from approved scenes | local write |
| `creator_render_preview` | FFmpeg render: 9:16 / 16:9, mix, captions (cancellable) | CPU/disk |
| `creator_validate_render` | ffprobe + decode validation | read-only |
| `creator_get_review_package` | review bundle + hashes + warnings | local write |
| `creator_approve_publish` | records your explicit approval for the exact payload | security-sensitive |
| `creator_publish_video` | guarded upload (dry-run by default) | external side effect |
| `creator_get_publish_status` | reconcile upload/post status | network |
| `creator_cancel_job` | cancel running work (terminates FFmpeg) | side effect |

Every tool returns a structured envelope: `status` (`ok`/`error`), `tool`, `project_id`, `artifacts`, `warnings`, `next_action`, `error` = `{code, safe_explanation, retryable, stage, next_action}`, and `data`.

---

## Project workspace layout

```text
workspace/projects/<project-id>/
  project.json brief.md research.json script.md storyboard.json
  assets/{manifest.json, assets.json, candidates.json, clips/, images/, audio/}
  narration/{narration.*, narration.json}
  captions/subtitles.srt
  timeline/{edit_decision_list.json, segments_v*.txt}
  renders/{final.mp4, preview.mp4, cover.jpg, v<N>/…}
  review/{publish_payload.json, review_package.json, approval.json, approvals_log.jsonl, publish_attempts.json}
  logs/
```

Secret tokens and API keys never live inside `workspace/`.

---

## Testing

```bat
.venv\Scripts\python.exe -m pytest -q              :: 62 tests
.venv\Scripts\python.exe scripts\verify_mcp_boot.py :: MCP handshake + tools/list proof
```

- `tests/unit` — config validation, redaction, path jail, state machine, approval guard (expiry / hash-mismatch / replay), error contract
- `tests/security` — license default-deny; downloader host allowlist, redirects, size caps, content-type + magic bytes (mocked HTTP)
- `tests/mcp` — stdio boot, tool discovery = 18, invalid args → structured errors
- `tests/integration` — **real FFmpeg** 9:16 render with narration mix, ffprobe + decode validation
- `tests/e2e` — full pipeline with a deterministic fake LLM + mocked Facebook network: dry-run → live → idempotent replay; user-media intake behind the rights gate; negatives `PUBLISH_APPROVAL_REQUIRED`, `PUBLISH_APPROVAL_STALE`

---

## Security model

1. **Approval gate** — publication is impossible without an approval matching the exact Page, media SHA-256, payload SHA-256, action and unexpired nonce; single-use; revalidated independently at upload time.
2. **Dry-run default** — live uploads double-gated (`FCA_ENABLE_LIVE_PUBLISH=1` + token).
3. **UNKNOWN outcomes** — journaled, reconciled, never blind-retried.
4. **Default-deny media rights** — allowlisted providers/licenses only; provenance + hashes preserved; ambiguous rights → manual review.
5. **Secrets** — environment/`.env` only; redacted logs; never in project files or prompts.
6. **Filesystem** — canonicalized path jail under the workspace root; hashed filenames for downloads; safe overwrite rules.
7. **Network** — host allowlists per provider, manual redirect validation, size/type limits (SSRF-reducing).
8. **Shell** — subprocesses run as argv arrays; stdout is reserved for the MCP protocol.

---

## Known limitations

- **Live external services are not claimed as verified in this repository** — Meta Graph publishing, live Pexels stock, and live TTS engines have no credentials here. Their code paths are capability-gated, mock-tested, and marked in [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md). Script generation against a real LLM endpoint is likewise unverified here.
- Reels-specific endpoints and scheduling are **feature-gated** and not claimed (they depend on Meta app review and granted permissions).
- The 16:9 render path is implemented but only 9:16 is exercised by the automated suite.
- Per-client auto-configuration is not performed automatically; the sections above are the documented setup (matching `TASK.md` T1001–T1005).

---

## Repository documents

| Document | Role |
|---|---|
| [`PRD.md`](PRD.md) | product requirements — tool catalogue §8, NFRs §9, acceptance §11 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | components, state machine §6, error taxonomy §9, security §7 |
| [`TASK.md`](TASK.md) | task tracker (checkboxes intentionally untouched by the implementation) |
| [`LOOP_ENGINEERING.md`](LOOP_ENGINEERING.md) | evidence-based engineering loop + report format |
| [`IMPLEMENTATION_STATUS.md`](IMPLEMENTATION_STATUS.md) | tested-vs-mocked matrix, verified evidence, gaps |

License: proprietary (placeholder — choose before distribution).
