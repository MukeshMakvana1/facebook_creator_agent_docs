# Implementation status — Facebook Creator Agent

Date: 2026-10-18 · Platform: Windows · Python 3.10 (repo `.venv`) · `mcp` SDK 2.3.0 · ffmpeg/ffprobe on PATH

Status record for `TASK.md` (checkboxes intentionally left untouched — this file is the record, per `LOOP_ENGINEERING.md` §8).

## Verification evidence (executed, not assumed)

| Check | Command / call | Result |
|---|---|---|
| Full test suite | `.venv\Scripts\python.exe -m pytest -q` | **62 passed in 22.82s** |
| MCP boot (stdio) | `.venv\Scripts\python.exe scripts\verify_mcp_boot.py` | PASS — `initialize` OK (`facebook-creator-agent` 0.1.0, protocol `2025-11-25`), `tools/list` = **18/18**, invalid args rejected, empty topic → structured `INVALID_INPUT` |
| Diagnostics | `creator_doctor` called live through the configured server | `status: ok`, `overall: partial` — llm/research unconfigured, TTS declarative, Meta token absent and live-publish off (Graph `v23.0`); all expected without credentials |
| OpenCode registration | repo-root `opencode.json` | JSON valid; absolute venv python, `-m facebook_creator_agent`, `cwd` + `FCA_BASE_DIR` set; live session exposes `facebook_creator_*` |
| Project agent | `.opencode/agents/facebook-creator.md` | frontmatter parses (PyYAML): `mode: all`, `steps: 60`, 7 permission rules; `forge_agent.py` validator: clean |
| Cleanup | probe scripts + smoke project removed | repo root clean (no stray test artifacts) |

## Status matrix

Legend: ✅ real, tested · 🟡 implemented, mock/gated-tested only · ⚠️ partial / known gap · ⚪ not started / blocked

| Area | TASK.md | Status | Evidence / notes |
|---|---|---|---|
| Core config, logging + redaction, path jail, ids, error contract | T0101–T0105, T0108 | ✅ | `tests/unit/test_core.py` (27) |
| State machine, cancellation, stage manifests | T0106 | ✅ | unit transition tests + `_stage` records; cancellation via `creator_cancel_job` |
| `creator_doctor` | T0107 | ✅ | boot script + live call |
| MCP server, schemas, stdio + streamable-http entry | T0201–T0204, T0207, T0208 | ✅ / ⚠️ | stdio real-tested (`tests/mcp`, 2 + boot script); streamable-http implemented, not test-exercised |
| Project creation; status in every envelope (no dedicated status tool in the PRD §8 surface of 18) | T0205 | ✅ | e2e |
| Approval guard interfaces | T0206, T0802–T0805 | ✅ | `tests/unit/test_approval.py` (7) + e2e refusals |
| LLM adapter (OpenAI-compatible) | T0301–T0305, T0307 | 🟡 | complete + capability-gated; e2e uses deterministic fake; live endpoint unverified |
| LLM failure matrix tests (malformed/timeout/auth/rate limit) | T0306 | ⚠️ | not written (fake exercises the happy path only) |
| Script / storyboard / revision flow | T0401–T0405 | ✅ (fake LLM) | e2e |
| Multilingual + schema-drift tests | T0406 | ⚠️ | partial |
| Media adapter, ranking, license policy, user intake, manifest | T0503–T0505, T0507, T0508 | ✅ | e2e fixture flow + user-media test (rights gate, copy-in, sha256, `USER-SUPPLIED-CONFIRMED`) |
| Safe download (host, redirects, size, type, signature, hash) | T0506, T0509 | ✅ | `tests/security` (mocked HTTP) |
| Pexels provider (live) | T0501/T0502 | 🟡 | capability-gated (`FCA_PEXELS_API_KEY`); live unverified |
| Manual narration + audio probe | T0603, T0604 | ✅ | e2e real WAV intake |
| Command-TTS engine | T0601, T0602 | 🟡 | declarative metadata; engine not live-verified |
| Captions (SRT + burn-in, alignment fallback) | T0605 | ✅ | SRT written and burned in the e2e render |
| Caption / multilingual filename tests | T0606, T0710 | ⚠️ | partial (Windows runtime implicit; Unicode filenames not explicit) |
| FFmpeg pipeline: detection, EDL, trim/scale/concat, narration mix, burn-in/sidecar, preview + final profiles, subprocess control | T0701–T0707 | ✅ / ⚠️ | `tests/integration` + e2e real renders (9:16, narration mix, burn-in, decode); optional music/ducking and final-profile variants not separately exercised |
| ffprobe validation + full decode | T0708 | ✅ | integration + e2e |
| Fixture-based render integration tests | T0709 | ✅ | `tests/integration/test_render_pipeline.py` (2) |
| 9:16 + 16:9 acceptance | T0703/T0709 | ⚠️ | executed tests exercise **9:16 only**; no 16:9 smoke render run |
| Review package | T0801 | ✅ | e2e |
| Meta: credential abstraction, capability checks, `creator_list_pages`, upload adapter + journal, publish status | T0903–T0907 | 🟡 | fake-adapter e2e (dry-run → live → idempotent replay → `SUCCEEDED`); live Graph unverified |
| Meta failure matrix (denial, expired token, timeout, unknown outcome) | T0908 | ⚠️ | not written; refusal/reconciliation gates tested at the publisher level |
| Meta live E2E on a test Page | T0909 | ⚪ blocked | no credentials in this environment |
| Unsupported-feature documentation | T0910 | ✅ | README + tool descriptions avoid universal Reels/scheduling claims |
| OpenCode client setup + project agent | T1003 | ✅ | README §OpenCode + `.opencode/agents/facebook-creator.md` |
| Other clients (Codex / VS Code / Antigravity / generic) | T1001, T1002, T1004, T1005 | ⚠️/⚪ | generic stdio block documented; per-client pages not written |
| E2E with fixture media + mocked providers | T1101 | ✅ | `tests/e2e` (3) |
| E2E with live LLM endpoint | T1102 | ⚪ blocked | needs an endpoint |
| Resume after interruption | T1103 | ⚠️ | resume edges unit-tested; no full interruption E2E |
| No secret leakage | T1104 | ✅ (scope) | redaction unit tests; no secrets committed (`.env` git-ignored; `.env.example` placeholders only) |
| Publish impossible without approval | T1105 | ✅ | e2e: 3 refusals (`PUBLISH_APPROVAL_REQUIRED` ×2, `PUBLISH_APPROVAL_STALE`), zero adapter uploads |
| Duplicate-upload protection | T1106 | ⚠️ | idempotent replay ✅ e2e; direct UNKNOWN-outcome refusal negative test pending |
| Governance (LICENSE, CONTRIBUTING, CI) | T0003–T0008 | ⚪ | out of scope for this build |

## Test inventory (62)

| File | Tests |
|---|---|
| `tests/unit/test_core.py` | 27 |
| `tests/security/test_policy_and_downloader.py` | 21 |
| `tests/unit/test_approval.py` | 7 |
| `tests/e2e/test_creator_flow.py` | 3 |
| `tests/integration/test_render_pipeline.py` | 2 |
| `tests/mcp/test_mcp_server.py` | 2 |

## Documented deviations

- Per-agent `temperature` is omitted from `.opencode/agents/facebook-creator.md` (OpenCode V2 ignores it); behavioural constraints live in the prompt body instead.
- The OpenCode server is registered as `facebook`, so normalized tool IDs are `facebook_creator_*`; MCP-side names remain `creator_*` (both documented in the agent body).
- `.env` is only overlaid on the real server path; tests build hermetic settings from explicit env mappings (explicit env wins over `.env`).

## Known limitations

- **Live Meta publishing is UNVERIFIED** (no app/Page credentials here). The path exists behind the double gate (`FCA_ENABLE_LIVE_PUBLISH=1` + token) and defaults to dry-run; uncertain outcomes are journaled (`STARTED/UNKNOWN`) and must be reconciled via `creator_get_publish_status` before any retry.
- **Live Pexels** and **live command-TTS** engines are unverified; manual narration and the fixture provider are real-tested.
- **LLM live endpoint** unverified (deterministic fake used in tests).
- Remaining QA gaps: T0306, T0908, T1103 E2E, T1106 direct negative, 16:9 render smoke, streamable-http test.

## Reproduce

```bat
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\verify_mcp_boot.py
.venv\Scripts\python.exe -m facebook_creator_agent doctor
```
