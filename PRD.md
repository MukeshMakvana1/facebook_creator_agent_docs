# Facebook Creator Agent — Product Requirements Document (PRD)

- **Product:** Facebook Creator Agent
- **Document status:** Draft for implementation
- **Version:** 0.1.0
- **Primary runtime:** Python
- **Distribution:** Repository-based MCP server
- **Primary output:** Edited video package ready for Facebook Page publishing
- **Publishing policy:** Preview and explicit user approval required before publishing

## 1. Executive summary

Facebook Creator Agent is a repository-based Model Context Protocol (MCP) server that lets compatible AI coding clients orchestrate a repeatable video-production workflow. A user provides a topic or prompt; the system researches the topic where configured, drafts a script and scene plan, locates license-compatible stock footage/images, generates narration through a configured local or cloud endpoint, edits real media using FFmpeg, creates captions and publishing metadata, presents a preview, and publishes to an authorized Facebook Page only after explicit approval.

The MCP server exposes typed, bounded tools. It does not itself contain an LLM and does not promise that every client supports identical setup automation. Provider adapters and client-specific setup guides isolate differences.

## 2. Problem statement

Creators currently switch between topic research, script writing, stock-media search, voice generation, editing, captions, metadata writing, and uploading. This is time-consuming and error-prone. Existing automation is often fragmented, requires manual glue code, or obscures media licensing and publishing status.

## 3. Goals

1. Turn a topic/prompt into a reviewable, edited video project with a reproducible local workspace.
2. Support any configured OpenAI-compatible local/cloud endpoint for text generation, with clear capability checks.
3. Use licensed stock media and user-approved sources; preserve asset provenance and licensing evidence.
4. Produce narration, subtitles, a final render, thumbnail/cover image where supported, caption, and relevant metadata.
5. Publish to Facebook Pages using official supported Meta APIs and permissions, only after explicit approval.
6. Provide MCP tools usable from Codex, VS Code MCP-capable extensions, OpenCode, Antigravity, and other compatible clients, with per-client configuration documentation.
7. Recover from interrupted work without silently duplicating uploads or losing project artifacts.
8. Keep secrets out of source control, logs, prompts, and generated project files.

## 4. Non-goals for MVP

- Publishing to personal Facebook profiles.
- Bypassing Meta API restrictions, app review, permissions, rate limits, or platform controls.
- Guaranteeing access to every Facebook publishing feature, Reels endpoint, or scheduling operation for every app/account.
- Generating synthetic video scenes from text. MVP edits real stock/user-supplied clips with FFmpeg.
- A standalone desktop GUI or hosted SaaS dashboard.
- Guaranteed viral performance, engagement, reach, or SEO rankings.
- Downloading arbitrary internet videos without documented usage rights.
- Fully automatic client configuration where a client has no supported configuration interface.
- Multi-platform publishing.
- Voice cloning without explicit consent and rights checks.

## 5. Target users and primary persona

**Primary persona:** A creator or small agency operator who wants to produce repeatable videos from prompts without manually coordinating every production tool.

Secondary personas:
- Developer integrating the MCP server into an AI coding client.
- Creator with a local model endpoint and local voice engine.
- Agency managing multiple authorized Facebook Pages and local project folders.

## 6. User journeys

### 6.1 First-time setup
1. User clones/downloads the repository.
2. User runs the documented setup command.
3. Setup checks Python, FFmpeg, required dependencies, writable output path, and optional endpoint reachability.
4. User configures an OpenAI-compatible endpoint and model; API key is read from environment or a secrets store, never committed to files.
5. User configures a supported TTS provider/command or chooses a manually supplied narration file.
6. User completes Meta developer app setup and Facebook Page authorization using the supported official flow.
7. System verifies Page identity and available publishing capabilities without publishing a test post.
8. User configures their MCP client. Supported clients may be auto-configured only when a reliable, documented mechanism exists; otherwise show exact guided instructions.
9. `doctor`/connection test reports ready, partially configured, or blocked status with actionable remediation.

### 6.2 Create a video
1. User calls the create-project workflow with a topic, language, target duration, aspect ratio, audience, tone, and optional brand instructions.
2. Agent produces a research brief/script/scene plan. Research claims include sources when a web-research provider is configured.
3. User reviews the script and scene plan.
4. System searches configured media providers and selects candidate assets with source URLs, provider IDs, license metadata, and retrieval date.
5. Assets that fail configured licensing policy are excluded or marked for manual review; they must not be silently used.
6. Narration is generated through a configured TTS adapter or supplied by the user.
7. System generates captions and an edit decision list (EDL/timeline manifest).
8. FFmpeg renders the video, mixes audio, burns or packages subtitles, and creates a preview.
9. Automated checks validate file integrity, duration, dimensions, audio presence/levels, caption presence, and missing-media status.
10. User reviews preview and metadata, then explicitly approves or requests revisions.

### 6.3 Publish
1. User selects an authorized Facebook Page and reviews the final video, caption, hashtags, and supported metadata.
2. Agent displays the exact payload and target Page.
3. User explicitly approves this specific publication.
4. Publisher revalidates the approval token, file hash, target Page, permissions, and upload eligibility.
5. System uploads using supported official Meta API operations.
6. System records upload/post identifiers, timestamps, API response, and final status.
7. If the result is uncertain after a network failure, system checks status before retrying to reduce duplicate posts.

## 7. Functional requirements

Priority definitions: **P0** required for MVP; **P1** important; **P2** post-MVP.

### 7.1 Setup, configuration, and diagnostics
- **FR-001 (P0):** Provide `.env.example` containing placeholders only; never real secrets.
- **FR-002 (P0):** Support environment-based configuration and a validated local config file for non-secret settings.
- **FR-003 (P0):** Provide a `doctor` command/tool that checks Python version, FFmpeg availability, writable directories, provider configuration, endpoint connectivity, and Meta configuration status.
- **FR-004 (P0):** Provide per-client MCP setup instructions and configuration examples for Codex, VS Code MCP-capable clients, OpenCode, Antigravity, and generic MCP clients.
- **FR-005 (P0):** Auto-configure a client only if its documented configuration file/location and update semantics are known; back up files before modifying them and never overwrite unrelated settings.
- **FR-006 (P0):** Provide guided fallback when automatic configuration is unsupported, ambiguous, or denied.
- **FR-007 (P1):** Provide a setup wizard/CLI for non-secret configuration and provider tests.
- **FR-008 (P0):** Redact secrets and sensitive authorization values from logs and error messages.

### 7.2 LLM/provider abstraction
- **FR-010 (P0):** Support configurable OpenAI-compatible `base_url`, model name, request timeout, and API key supplied securely.
- **FR-011 (P0):** Check endpoint reachability and model response with a minimal test request.
- **FR-012 (P0):** Keep provider-specific behavior behind an adapter interface.
- **FR-013 (P0):** Treat structured output as best-effort unless the configured endpoint supports it; validate all model output against application schemas.
- **FR-014 (P0):** Implement timeouts, bounded retries, cancellation, and readable provider errors.
- **FR-015 (P1):** Allow separate text and vision endpoints if the configured workflow requires image analysis.
- **FR-016 (P0):** Do not assume an OpenAI-compatible endpoint also provides text-to-speech, web search, image generation, or video generation.

### 7.3 Research, script, and storyboard
- **FR-020 (P0):** Accept topic/prompt plus optional language, duration, audience, niche, tone, CTA, aspect ratio, brand constraints, and target Page.
- **FR-021 (P0):** Generate a structured brief, script, scene list, on-screen text, and media search query for each scene.
- **FR-022 (P0):** Mark factual claims needing verification; preserve research URLs and retrieval metadata when research is available.
- **FR-023 (P0):** Permit script/scene revision before asset selection and rendering.
- **FR-024 (P0):** Do not fabricate citations or represent model-generated claims as verified research.
- **FR-025 (P1):** Support reusable templates by niche and duration.

### 7.4 Media search, licensing, and provenance
- **FR-030 (P0):** Integrate only documented provider APIs or sources that permit the intended use.
- **FR-031 (P0):** Store for every asset: provider, provider asset ID, source page URL, direct download URL when allowed, creator/attribution data if supplied, license name/URL, retrieval date, intended use, local path, and review status.
- **FR-032 (P0):** Enforce a configurable allowlist of providers/licenses; reject unknown or incompatible rights by default.
- **FR-033 (P0):** Do not claim a license has been legally verified solely because an API returns a license label. Preserve evidence and mark uncertain cases for manual review.
- **FR-034 (P0):** Avoid downloading from arbitrary URLs unless the source is approved and its terms permit downloading and reuse.
- **FR-035 (P0):** Verify downloads using file signatures, size limits, content type, and safe filename handling.
- **FR-036 (P0):** Support user-provided media, while requiring the user to confirm they have the necessary rights.
- **FR-037 (P1):** Provide an asset manifest and attribution block where required.
- **FR-038 (P1):** Cache approved assets with provenance and avoid repeated downloads.

### 7.5 Narration and captions
- **FR-040 (P0):** Define a TTS adapter contract separate from the LLM adapter.
- **FR-041 (P0):** Allow a configured TTS provider/command or user-provided audio when no compatible TTS endpoint exists.
- **FR-042 (P0):** Record voice provider, model/voice identifier, language, generation settings, and output path.
- **FR-043 (P0):** Generate SRT or WebVTT captions using available timing/alignment capability; if alignment is unavailable, use an explicitly documented approximate-timing fallback.
- **FR-044 (P0):** Validate generated audio exists, is decodable, and has plausible duration before rendering.
- **FR-045 (P1):** Support voice selection, speed, pronunciation dictionary, and multiple speakers where provider capability permits.

### 7.6 Editing and rendering
- **FR-050 (P0):** Build a deterministic timeline/EDL from approved scenes, clips, narration, captions, music, and transitions.
- **FR-051 (P0):** Use FFmpeg/ffprobe through argument arrays (not unsafe shell string concatenation).
- **FR-052 (P0):** Support vertical 9:16 and landscape 16:9 presets, with configurable output resolution and frame rate.
- **FR-053 (P0):** Match clip lengths to scene durations; crop/scale/pad according to preset and avoid accidental stretching.
- **FR-054 (P0):** Mix narration and optional licensed music with configurable gain/ducking and peak checks.
- **FR-055 (P0):** Generate a preview and final MP4 using a documented H.264/AAC-compatible profile where available.
- **FR-056 (P0):** Validate final output with ffprobe and a playback/decode test.
- **FR-057 (P0):** Preserve intermediate artifacts and logs when a render fails; allow resume from the last valid stage.
- **FR-058 (P1):** Add branded templates, animated captions, transition presets, and thumbnail generation.

### 7.7 Facebook Page integration
- **FR-060 (P0):** Use official supported Meta APIs and OAuth/authorization flows; do not collect a user's Facebook password.
- **FR-061 (P0):** Display authorized Pages and the granted capabilities returned by the API.
- **FR-062 (P0):** Keep access tokens out of project folders, source control, prompts, and logs; use OS credential storage where available or a restricted local secret store.
- **FR-063 (P0):** Check configured app status, token validity, Page permissions, and target Page before upload.
- **FR-064 (P0):** Support only API operations available to the app/account and approved permissions. Feature-gate unsupported upload modes.
- **FR-065 (P0):** Require explicit per-publication approval bound to the exact target Page, media hash, caption/metadata, and action.
- **FR-066 (P0):** Invalidate approval if the media file, Page, caption, or publish parameters change.
- **FR-067 (P0):** Record upload status and available post/video identifiers; distinguish success, failure, and unknown outcome.
- **FR-068 (P0):** Use bounded retry and status reconciliation to reduce duplicate publication.
- **FR-069 (P1):** Support scheduling only when official API capability and account permissions allow it.
- **FR-070 (P1):** Support Page connection revocation and token refresh/re-authentication guidance.

### 7.8 MCP tools and workflow
- **FR-080 (P0):** Expose small, typed, purpose-specific MCP tools rather than one unrestricted tool.
- **FR-081 (P0):** Validate tool arguments and paths; return structured status, artifact paths, warnings, and next actions.
- **FR-082 (P0):** Keep long-running operations observable through job IDs and progress/status tools where the MCP transport/client permits.
- **FR-083 (P0):** Use explicit workflow states: `NEW`, `SCRIPT_READY`, `ASSETS_READY`, `NARRATION_READY`, `RENDERED`, `REVIEW_REQUIRED`, `APPROVED`, `PUBLISHING`, `PUBLISHED`, `FAILED`, `CANCELLED`.
- **FR-084 (P0):** Do not let the LLM bypass application-level approval and authorization checks.
- **FR-085 (P1):** Provide batch jobs and reusable production profiles after the single-video workflow is reliable.

## 8. Proposed MCP tool catalogue

Exact names may change during implementation, but contracts must be documented and tested.

| Tool | Purpose | Side-effect level |
|---|---|---|
| `creator_doctor` | Check dependencies/configuration | Read-only |
| `creator_list_providers` | Show configured model/TTS/media providers | Read-only, secrets redacted |
| `creator_test_llm` | Test configured endpoint | Network request |
| `creator_connect_facebook` | Begin/document supported authorization flow | Authorization side effect |
| `creator_list_pages` | List authorized Pages and available capabilities | Read-only |
| `creator_create_project` | Create isolated project workspace | Local write |
| `creator_generate_script` | Create brief/script/storyboard | Local write/model request |
| `creator_search_media` | Search configured providers | Network request |
| `creator_download_assets` | Download selected eligible assets | Local/network write |
| `creator_generate_narration` | Generate narration via TTS adapter | Local/network write |
| `creator_build_timeline` | Create validated timeline manifest | Local write |
| `creator_render_preview` | Render preview | CPU/disk intensive |
| `creator_validate_render` | Validate final video and assets | Read-only/CPU |
| `creator_get_review_package` | Return preview, caption, license manifest and hash | Read-only |
| `creator_approve_publish` | Record explicit approval for exact payload | Security-sensitive |
| `creator_publish_video` | Upload approved video to Page | External side effect |
| `creator_get_publish_status` | Reconcile upload/post status | Network request |
| `creator_cancel_job` | Cancel cancellable work | Side effect |

Tools that cause external side effects must clearly describe them. The publisher must independently enforce authorization and approval regardless of the calling client or model.

## 9. Non-functional requirements

- **NFR-001 Security:** Least privilege, secret redaction, secure token handling, path validation, no password collection.
- **NFR-002 Reliability:** Each pipeline stage writes a status manifest and can resume from verified artifacts.
- **NFR-003 Idempotency:** Repeated tool calls should not create duplicate projects, downloads, renders, or posts without explicit intent.
- **NFR-004 Observability:** Structured logs, job IDs, stage durations, redacted provider errors.
- **NFR-005 Portability:** Support Windows, macOS, and Linux where dependencies and providers permit; Windows is a first-class test target.
- **NFR-006 Performance:** Long tasks must not block the MCP server event loop; subprocesses need timeouts and cancellation.
- **NFR-007 Resource limits:** Configurable download size, clip count, duration, render resolution, concurrency, and temporary-disk budgets.
- **NFR-008 Maintainability:** Provider adapters, media pipeline, publishing, configuration, and MCP interface are separated.
- **NFR-009 Accessibility:** Captions and readable review metadata are first-class artifacts.
- **NFR-010 Legal/compliance:** Preserve license evidence and use official platform APIs; do not claim legal certainty from metadata alone.
- **NFR-011 Testability:** Unit tests, mocked provider/API tests, FFmpeg integration tests, and manual Meta sandbox/developer-app validation.
- **NFR-012 User control:** Publishing is never automatic in MVP; exact payload requires explicit approval.

## 10. Data model and workspace layout

Suggested workspace:

```text
workspace/
  projects/
    <project-id>/
      project.json
      brief.md
      research.json
      script.md
      storyboard.json
      assets/
        manifest.json
        clips/
        images/
        audio/
      narration/
      captions/
        subtitles.srt
      timeline/
        edit_decision_list.json
      renders/
        preview.mp4
        final.mp4
      review/
        publish_payload.json
        approval.json
      logs/
        project.log
```

Secret tokens and API keys must live outside project workspaces.

Core records:
- `Project`: ID, prompt, locale, status, profile, timestamps.
- `Scene`: order, narration segment, desired duration, search query, selected asset IDs.
- `Asset`: provider, source URL, license evidence, local path, hash, status.
- `Render`: path, hash, codec/container metadata, validation result.
- `PublishApproval`: exact media hash, target Page ID, caption/metadata hash, expiry, approver confirmation, one-time nonce.
- `PublishAttempt`: idempotency key, status, response identifiers, timestamps, sanitized errors.

## 11. Acceptance criteria for MVP

MVP is acceptable when all P0 items below pass:

1. A clean environment can install dependencies and run diagnostics.
2. A compatible MCP client can connect and discover the documented tools.
3. An OpenAI-compatible endpoint can generate a validated script/storyboard; invalid responses are handled safely.
4. Media search returns candidates with provenance; unknown/unapproved licensing is excluded from automatic selection.
5. A user can supply licensed media if stock providers are unavailable.
6. A configured TTS adapter or user narration file can be used to create captions and a rendered video.
7. FFmpeg produces a valid MP4 in selected aspect ratio; ffprobe and decode validation pass.
8. A review package includes video path/preview, script, caption, metadata, asset manifest, warnings, and file hash.
9. Publishing is impossible without an approval matching the exact Page, media hash, and payload.
10. Publisher uses official supported Meta API operations and reports blocked permissions clearly.
11. Uncertain upload outcomes are reconciled before retry; duplicate posts are not blindly created.
12. Secrets do not appear in committed files, project manifests, test snapshots, or logs.
13. Windows setup and a generic MCP configuration path are tested; other clients are documented with support status, not falsely claimed as fully automated.
14. Tests cover normal, invalid, timeout, cancellation, permission-denied, missing-FFmpeg, and unknown-upload-outcome cases.

## 12. MVP scope and release strategy

### MVP-1: Local production pipeline
- Setup/doctor
- OpenAI-compatible text adapter
- Script/storyboard
- One or more approved stock media adapters plus user-supplied assets
- TTS adapter contract and manual-audio fallback
- FFmpeg render and validation
- Review package
- No live publishing until local workflow tests pass

### MVP-2: Facebook Page publishing
- Meta app configuration guide
- Official authorization/token handling
- Page discovery and capability checks
- Explicit approval token
- Upload/status reconciliation in a testable environment

### MVP-3: Client onboarding and robustness
- Per-client config templates and supported automation
- Setup wizard
- Retry/resume and richer diagnostics
- Windows/macOS/Linux validation as feasible

### Post-MVP
- Scheduling where supported
- More media providers and voice adapters
- Templates/branding and thumbnails
- Multi-project queue
- Analytics/feedback where officially available
- Additional publishing platforms only after a separate requirements review

## 13. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Meta permission/app review blocks publishing | Validate developer app and permissions early; keep publisher capability-gated |
| API compatibility is mistaken for full multimodal support | Separate LLM/TTS/research/media adapters and capability checks |
| Media license is ambiguous | Default-deny, store evidence, manual review path |
| AI produces false facts | Research provenance, claims flagged for verification, user review |
| Client config formats change | Client-specific adapters/docs, backups, version checks, manual fallback |
| Upload timeout creates duplicate post | Store attempt state and reconcile status before retry |
| CPU-heavy render stalls workflow | Separate subprocesses, progress reporting, cancellation, resource limits |
| Malicious prompt/path/asset URL | Validate paths, allowlist hosts, limit file sizes, avoid shell interpolation |
| Token leakage | Secret store/environment, redaction, restrictive file permissions |

## 14. Open implementation decisions

These can be finalized during architecture implementation without changing the product goals:
- Exact supported Python version after dependency compatibility testing.
- MCP Python SDK version and transport mode(s).
- Initial stock provider selection and current provider API/license requirements.
- Initial TTS adapter(s).
- Meta Graph API version and exact currently supported video upload flow.
- Exact client config locations and auto-configuration capabilities by client/version.
- Default output resolution/frame rate and render resource limits.

## 15. Definition of Done

A feature is done only when:
- Acceptance criteria are documented.
- Tests cover success and failure paths.
- Logs are useful and secrets are redacted.
- User-facing documentation is updated.
- Windows workflow is tested where applicable.
- No external publishing side effect occurs without the explicit approval guard.
- The implementation is committed with a clear summary and test results.
