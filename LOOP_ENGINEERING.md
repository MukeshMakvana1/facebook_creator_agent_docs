# Facebook Creator Agent — Loop Engineering Protocol

## 1. Purpose

Use a repeatable, evidence-based engineering loop for every task. Do not generate large amounts of code before validating assumptions. The implementation advances one bounded feature at a time, with tests and documented results.

## 2. Non-negotiable rules

1. Read `PRD.md`, `TASK.md`, and `ARCHITECTURE.md` before starting a feature.
2. Work on one task or one tightly coupled task group at a time.
3. Inspect existing files and tests before editing.
4. Do not invent API behavior. Verify current official provider/platform documentation before implementing external integrations.
5. Never mark a task complete without test evidence.
6. Do not weaken or delete a test merely to make the suite pass; if a requirement changes, document the reason and update acceptance criteria.
7. Do not log secrets, authorization headers, tokens, or sensitive prompt data unnecessarily.
8. Never bypass the publish approval guard for convenience or tests. Tests should use mocks or an explicitly isolated test fixture.
9. After any code change, run the narrowest relevant test first, then the broader affected suite.
10. If blocked by account approval, external API access, unavailable credentials, or client-specific uncertainty, mark the task blocked and document the external prerequisite.

## 3. Standard loop

### Step A — Select
- Select the next unchecked task from `TASK.md`.
- Confirm dependencies are complete.
- Read its acceptance criteria and relevant architecture sections.
- Identify assumptions and external prerequisites.

### Step B — Inspect
- Inspect repository structure, existing implementation, test coverage, package constraints, and current official documentation where relevant.
- Run baseline tests before changing code when a project already exists.
- Record current failures separately from failures introduced by the change.

### Step C — Plan a minimal change
Write a short implementation plan:
- files expected to change
- public interfaces/contracts affected
- security and failure cases
- tests to add or update
- acceptance criteria to prove

Do not broaden the scope without updating the task plan.

### Step D — Implement
- Make the smallest coherent implementation.
- Keep provider-specific logic behind adapters.
- Validate all external and model-produced data.
- Use timeouts and bounded retries for network/subprocess operations.
- Preserve backward compatibility or document intentional breaking changes.
- Do not claim a feature works on a client/provider that has not been tested.

### Step E — Test
Run in this order where applicable:

1. Formatting/linting.
2. Type checking.
3. Focused unit tests for the changed module.
4. Contract tests with mocked external services.
5. Integration tests for affected workflows.
6. Full test suite and build/package checks when appropriate.
7. Security-focused tests for changes to secrets, paths, downloads, approval, or publishing.

Example command names are placeholders until the repository's tooling is chosen:

```bash
# Replace with the exact commands configured in pyproject.toml
ruff check .
mypy src
pytest tests/unit/test_changed_module.py
pytest
```

Do not report commands as run unless they were actually executed.

### Step F — Diagnose failures
For every failure:
1. Capture the exact command, exit code, and relevant traceback/log excerpt.
2. Determine whether the failure is a regression, pre-existing failure, environment issue, or external service issue.
3. Reproduce with the smallest failing test.
4. Identify the root cause.
5. Make a targeted fix.
6. Rerun the failing test.
7. Rerun all tests affected by the fix.
8. If unresolved, leave the task open/blocked and document the next diagnostic step.

Never repeatedly rerun the same failing command without changing the diagnosis or environment.

### Step G — Review
Check:
- Does the implementation satisfy the PRD?
- Are user inputs and model output validated?
- Are paths, subprocesses, and downloads safe?
- Are secrets redacted?
- Are retry and cancellation semantics correct?
- Could an MCP call trigger a dangerous side effect unexpectedly?
- Is publication approval independently enforced?
- Are license evidence and attribution preserved?
- Are docs and tests updated?

### Step H — Close task
Only mark `[x]` in `TASK.md` after:
- acceptance criteria pass,
- tests are actually run,
- results are recorded,
- documentation is updated,
- known limitations are stated.

If external prerequisites prevent completion, use `[!]` and explain the blocker. Do not mark external integration “complete” merely because a mock passes.

## 4. Required completion report

For every task, report:

```text
Task:
Status: COMPLETE / IN PROGRESS / BLOCKED
Files changed:
Implementation summary:
Commands executed:
Test results:
Security/compliance notes:
Known limitations:
Next task:
```

Use exact test counts and command exit status when available. Distinguish unit/mock test evidence from live provider validation.

## 5. External integration protocol

Before implementing an external integration:
1. Read current official docs and identify the exact API version, scopes/permissions, quota limits, and supported operations.
2. Create a capability table separating confirmed, conditional, and unsupported operations.
3. Identify account/app review requirements and how to test without affecting production users.
4. Keep secrets in environment/credential storage and redact logs.
5. Implement a mocked adapter contract before live calls.
6. Add explicit timeouts, rate-limit handling, error normalization, and retry rules.
7. Test live only with a test account/Page and user authorization.
8. Document any required human setup that cannot be automated.

### Facebook publishing special rule
A network timeout after upload can mean the remote service accepted the upload even if the client did not receive the response. Mark the result `UNKNOWN`, reconcile using supported API identifiers/status checks, and do not blindly retry. The exact upload and reconciliation behavior must be verified against current official Meta documentation.

## 6. Media licensing protocol

For every media provider:
- Review provider API terms and the actual asset license/terms.
- Record provider ID, source URL, license URL/name, retrieval date, attribution, and asset hash.
- Confirm the intended commercial/social-media use is permitted by the applicable terms.
- Reject unknown or conflicting rights by default.
- Never use “free to download” as equivalent to “free for commercial reuse.”
- Do not claim automated legal verification; flag ambiguous cases for human review.

## 7. Security protocol

Changes involving secrets, downloads, filesystem paths, subprocesses, OAuth, or publication require targeted tests for:
- secret leakage
- path traversal and unsafe overwrite
- malicious/oversized media
- untrusted metadata/prompt injection
- expired or missing authorization
- stale/replayed approval
- altered media/payload after approval
- timeout and duplicate side-effect handling

## 8. Task status policy

- `[ ]` Pending: not started or no verified completion evidence.
- `[-]` In progress: implementation underway.
- `[x]` Complete: acceptance criteria pass with evidence.
- `[!]` Blocked: external prerequisite or unresolved issue prevents progress.

A blocked task should name the blocker, its owner (user/developer/provider), and the next action needed.

## 9. Release loop

Before tagging a release:
1. Confirm all P0 tasks are complete or explicitly blocked with product-owner acceptance.
2. Run lint, type checks, unit tests, integration tests, and packaging smoke tests.
3. Verify no secrets or local credentials are present in the repository.
4. Review dependency licenses and supply-chain alerts.
5. Verify README setup on a clean environment.
6. Verify publish approval enforcement and unknown-outcome handling.
7. Review known limitations and supported client matrix.
8. Record version, test evidence, release notes, and rollback guidance.
