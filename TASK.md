# Facebook Creator Agent — Task Tracker

- **Status convention:** `[ ]` pending · `[-]` in progress · `[x]` complete · `[!]` blocked
- **Rule:** Never mark a task complete without evidence (tests, command output, or reviewed artifact).
- **Current overall status:** `[ ]` Planning baseline; no implementation task is assumed complete.

## Phase 0 — Project governance and repository baseline

- [ ] T0001 Create repository skeleton and `.gitignore`.
- [ ] T0002 Add `README.md` with project purpose, supported clients, limitations, setup, and security notes.
- [ ] T0003 Add `LICENSE` after choosing an appropriate project license.
- [ ] T0004 Add `CONTRIBUTING.md` and issue/PR templates.
- [ ] T0005 Add `.env.example` with placeholders only.
- [ ] T0006 Define supported Python version after dependency compatibility checks.
- [ ] T0007 Configure formatter, linter, type checker, and test runner.
- [ ] T0008 Configure CI for unit tests and static checks on supported operating systems.

**Acceptance:** Clean clone has documented installation steps; CI runs lint/type/tests; no secrets committed.

## Phase 1 — Core application and configuration

- [ ] T0101 Create package structure and application entry points.
- [ ] T0102 Implement typed settings model with validation.
- [ ] T0103 Implement environment variable loading and safe non-secret local config.
- [ ] T0104 Implement structured logging and secret redaction.
- [ ] T0105 Implement project workspace manager and path validation.
- [ ] T0106 Implement job IDs, state machine, cancellation, and stage manifests.
- [ ] T0107 Implement `creator_doctor` diagnostics.
- [ ] T0108 Add unit tests for config validation, redaction, paths, and state transitions.

**Acceptance:** Invalid configuration fails with actionable errors; secrets never appear in captured logs; path traversal tests pass.

## Phase 2 — MCP server and tool contracts

- [ ] T0201 Select and pin the compatible Python MCP SDK version.
- [ ] T0202 Implement MCP server entry point and documented transport.
- [ ] T0203 Define JSON schemas/models for every initial tool input and output.
- [ ] T0204 Implement read-only provider listing and endpoint test tools.
- [ ] T0205 Implement project creation and project status tools.
- [ ] T0206 Add tool-level authorization/approval guard interfaces.
- [ ] T0207 Add MCP integration tests for tool discovery, valid inputs, and invalid inputs.
- [ ] T0208 Document stdio launch configuration and generic MCP client setup.

**Acceptance:** A test MCP client discovers tools and receives schema-valid results; errors are structured and do not expose secrets.

## Phase 3 — LLM provider adapter

- [ ] T0301 Define provider interface for text generation.
- [ ] T0302 Implement OpenAI-compatible chat/completions adapter using configured base URL/model.
- [ ] T0303 Add connection/model test with timeout and bounded retry.
- [ ] T0304 Validate model outputs against Pydantic/application schemas.
- [ ] T0305 Add retry/cancellation/error normalization.
- [ ] T0306 Add mocked tests for success, malformed JSON, timeout, auth failure, rate limit, and unreachable endpoint.
- [ ] T0307 Document compatibility limits: endpoint support does not imply TTS, vision, web search, or video generation.

**Acceptance:** Supported endpoint returns a validated script object; malformed or unsafe output is rejected or routed to revision.

## Phase 4 — Research, script, storyboard

- [ ] T0401 Define brief, research reference, script, scene, and storyboard models.
- [ ] T0402 Implement prompt-to-brief and script generation.
- [ ] T0403 Implement scene duration allocation and target runtime validation.
- [ ] T0404 Implement scene-level media search queries and on-screen text.
- [ ] T0405 Add revision flow and versioned artifacts.
- [ ] T0406 Add tests for multilingual text, empty output, unsupported durations, and model schema drift.

**Acceptance:** A prompt produces a versioned script/storyboard; duration and required fields validate; research sources are never fabricated.

## Phase 5 — Media provider and licensing provenance

- [ ] T0501 Evaluate providers and record current API, access, license, attribution, and commercial-use requirements.
- [ ] T0502 Choose initial approved stock provider(s) for MVP.
- [ ] T0503 Define `MediaProvider` adapter and normalized asset schema.
- [ ] T0504 Implement search and candidate ranking based on scene query and media type.
- [ ] T0505 Implement license policy allowlist and unknown-license rejection/manual-review state.
- [ ] T0506 Implement safe download with host/provider validation, content-type checks, size limits, and hashes.
- [ ] T0507 Implement user-supplied asset intake and rights-confirmation metadata.
- [ ] T0508 Write asset manifest with source, license evidence, attribution, retrieval date, and local path.
- [ ] T0509 Add tests for unknown license, disallowed host, oversized file, invalid media, and missing attribution.

**Acceptance:** Every automatically selected asset has recorded provenance and passes configured policy; uncertain rights are not silently accepted.

## Phase 6 — Narration and captions

- [ ] T0601 Define `TTSProvider` interface and capability model.
- [ ] T0602 Implement first selected TTS adapter only after provider/engine compatibility review.
- [ ] T0603 Add manual narration file fallback.
- [ ] T0604 Implement audio probing and duration validation.
- [ ] T0605 Implement SRT/VTT generation using alignment when available and documented fallback otherwise.
- [ ] T0606 Add multilingual filename/text handling tests and caption validation.
- [ ] T0607 Document voice model licensing and voice-consent constraints.

**Acceptance:** Pipeline can use generated or supplied narration; audio decodes; subtitle file validates and is tied to the correct script version.

## Phase 7 — FFmpeg rendering pipeline

- [ ] T0701 Add FFmpeg/ffprobe detection and version diagnostics.
- [ ] T0702 Define versioned timeline/EDL schema.
- [ ] T0703 Implement clip trim, scale/crop/pad, concat, and aspect-ratio presets.
- [ ] T0704 Implement narration mix and optional approved background music with ducking/gain controls.
- [ ] T0705 Implement subtitle burn-in or sidecar export.
- [ ] T0706 Implement preview and final render profiles.
- [ ] T0707 Implement subprocess timeout, cancellation, progress parsing, and sanitized logs.
- [ ] T0708 Implement ffprobe validation and full decode test.
- [ ] T0709 Add tiny fixture-based render integration tests.
- [ ] T0710 Test Windows path handling and Unicode filenames.

**Acceptance:** A fixture project renders a valid MP4 in 9:16 and 16:9; audio/video stream checks and decode validation pass.

## Phase 8 — Review package and approval security

- [ ] T0801 Assemble preview, script, captions, publishing caption, metadata, asset manifest, warnings, and render hash.
- [ ] T0802 Implement explicit approval bound to target Page, media hash, metadata hash, and one-time nonce.
- [ ] T0803 Add expiry and invalidation when payload or media changes.
- [ ] T0804 Add tests for stale approval, changed caption, changed Page, changed media, replayed approval, and unauthorized caller.
- [ ] T0805 Ensure model instructions cannot bypass application-level approval enforcement.

**Acceptance:** Any change to approved payload invalidates approval; publication guard rejects missing, stale, or replayed approval.

## Phase 9 — Meta/Facebook Page integration

- [ ] T0901 Review current official Meta developer docs and choose supported Graph API version.
- [ ] T0902 Document app setup, required permissions, review/access prerequisites, and user authorization.
- [ ] T0903 Implement secure token storage/credential abstraction.
- [ ] T0904 Implement token/Page capability checks and `creator_list_pages`.
- [ ] T0905 Implement supported upload adapter behind a capability gate.
- [ ] T0906 Implement upload attempt journal and idempotency/reconciliation strategy.
- [ ] T0907 Implement publish status tool with success/failure/unknown states.
- [ ] T0908 Test permission denial, expired token, upload failure, timeout, and unknown outcome with mocks.
- [ ] T0909 Perform controlled integration test using an authorized test Page/app.
- [ ] T0910 Document unsupported features and avoid claims of universal Reels/scheduling support.

**Acceptance:** Only supported official API flows are used; publish requires matching approval; uncertain upload outcomes are reconciled before retry.

## Phase 10 — Client onboarding and packaging

- [ ] T1001 Document Codex MCP configuration.
- [ ] T1002 Document VS Code MCP-capable extension configuration.
- [ ] T1003 Document OpenCode configuration.
- [ ] T1004 Document Antigravity configuration based on verified client support.
- [ ] T1005 Document generic MCP client setup.
- [ ] T1006 Implement safe client-config detection and backups only for verified formats/locations.
- [ ] T1007 Implement guided fallback and post-setup connection check.
- [ ] T1008 Add clean-machine installation test on Windows.
- [ ] T1009 Add macOS/Linux smoke tests if included in declared support matrix.

**Acceptance:** Each listed client has a verified support status and reproducible connection instructions; unsupported auto-configuration falls back safely.

## Phase 11 — End-to-end QA and MVP release

- [ ] T1101 Create end-to-end test using fixture media and mocked providers.
- [ ] T1102 Create end-to-end test with configured LLM endpoint and local render.
- [ ] T1103 Verify project resume after interruption at each pipeline stage.
- [ ] T1104 Verify no secret leakage in logs, manifests, test artifacts, or repository.
- [ ] T1105 Verify publish is impossible without approval.
- [ ] T1106 Verify duplicate-upload protection for uncertain outcomes.
- [ ] T1107 Complete threat-model review and dependency/license review.
- [ ] T1108 Complete user-facing docs, troubleshooting guide, and known limitations.
- [ ] T1109 Tag MVP only after all release gates pass.

**Release gate:** No critical security defects; all P0 acceptance criteria pass; Meta publishing tested only where credentials and permissions permit; blocked external prerequisites are clearly documented.

## Post-MVP backlog

- [ ] T2001 Additional stock-media providers.
- [ ] T2002 Additional TTS adapters and pronunciation dictionaries.
- [ ] T2003 Brand templates, animated captions, and thumbnails.
- [ ] T2004 Scheduling if supported by current official API and permissions.
- [ ] T2005 Batch project queue and profiles.
- [ ] T2006 Analytics/insights where official API access permits.
- [ ] T2007 Additional social platforms after separate platform-specific PRD review.

## Engineering loop for every task

1. Read the task and acceptance criteria.
2. Inspect current code, tests, and relevant docs before changing anything.
3. Make the smallest complete change.
4. Run focused tests, then the relevant broader test suite.
5. If a test fails, capture the exact error and reproduce it.
6. Fix the root cause, not just the symptom.
7. Rerun the failing test and all impacted tests.
8. Update docs and mark `[x]` only when evidence supports completion.
9. Report changed files, commands run, results, known limitations, and next task.
