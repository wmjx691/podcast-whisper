# DECISION_LOG.md

## Purpose

This log records only architecture and strategy decisions explicitly approved by Kevin for the Podcast Transcription System.

It does not authorize implementation. Repository modification, refactoring, migration, credential changes, commits, or pushes require separate explicit authorization.

---

## Decision 1 — Runtime Roles and Canonical Code Source

### Decision

- WSL/local is the canonical development environment for development, Git version control, and repository evolution.
- The GitHub repository is the canonical code source of truth and is public.
- GitHub Actions is the supported unattended/scheduled runtime for periodic podcast processing.
- Google Colab is a supported secondary interactive/manual runtime, primarily for GPU-accelerated transcription.
- The runtimes may coexist, but they must not evolve into separate canonical code sources.
- Google Drive repository copies and their `.git` history are not authoritative.

### Rationale

The current operating model already uses:
- WSL for development,
- GitHub Actions for scheduled processing while the local computer may be offline,
- Colab for interactive GPU workloads.

GitHub Actions performance is currently acceptable: approximately 10 minutes for the full pipeline on an approximately 30-minute podcast episode.

### Important constraints

- Canonical implementation history remains in Git.
- Colab may provide an interaction layer but should not independently evolve production/business logic.
- The public repository must not contain private credentials, API secrets, or personal runtime configuration.

### Still unresolved / deferred

- Exact mechanism for Colab to obtain/use canonical repository code.
- Cleanup/consolidation of the historical Google Drive repository copy.
- Exact output-directory architecture.
- Runtime-specific secret/configuration injection.

---

## Decision 2 — Canonical Episode Identity and Processing State

### Decision

- Canonical episode identity uses a source-native stable identifier.
- Title, episode number, and filename are metadata/display information, not canonical identity.
- For RSS sources, the intended primary identity is source/feed namespace plus RSS GUID, subject to validation against actual feeds.
- YouTube uses the YouTube video ID when that source is used.
- Processing state must be explicitly represented rather than inferred only from the existence of local audio, TXT, JSON, or Google Drive files.
- Artifact existence remains useful for validation, reconciliation, and recovery, but does not by itself prove successful processing.

### Rationale

The existing title/EP/filename-based identity is vulnerable to title changes, truncation, collisions, and multi-source ambiguity.

The current implementation also infers processing status from several artifacts whose states can diverge, which has already produced idempotency and recovery problems.

### Important constraints

- Identity semantics and processing-state semantics are separate from output file naming.
- Processing-state storage mechanism is not decided by this Decision.
- RSS GUID availability/stability must be validated against the real feed before implementation relies on it.
- Fallback identity behavior must be evidence-based.

### Still unresolved / deferred

- Exact persistent representation/storage of processing state.
- Exact RSS GUID fallback priority if GUID is missing or unstable.
- Migration/identity assignment for historical transcripts.

---

## Decision 3 — Legacy Transcript Compatibility

### Decision

The project adopts a **read-old / write-new** compatibility policy.

- Existing TXT-only transcripts are accepted legacy artifacts.
- Existing legacy JSON with a top-level segment list must remain recognizable.
- Current metadata+segments JSON remains recognizable.
- New transcription output uses only the current schema.
- Legacy artifacts are not automatically retranscribed or bulk-migrated solely to conform to the current schema.
- Historical metadata that cannot be reliably established remains unknown rather than being reconstructed from current configuration.
- Retranscription of a legacy episode to obtain current timestamped/structured output is an explicit operation.

### Rationale

Historical artifact differences resulted from normal project evolution: JSON and richer metadata were introduced after earlier transcripts already existed.

Treating legacy artifacts as unprocessed would cause unnecessary retranscription and could rewrite valid historical results.

### Important constraints

- Do not fabricate historical model, prompt, runtime, or provenance values.
- Recognition of legacy artifacts does not require rewriting them.
- TXT existence alone must not become the general rule for current-run success.

### Still unresolved / deferred

- Whether lossless legacy structured JSON normalization will ever be persisted.
- Exact compatibility implementation details.
- Any explicit per-episode retranscription requests.

---

## Decision 4 — Configuration Interface

### Decision

`main.py` is no longer considered the intended configuration storage/interface.

Configuration is divided into three semantic layers:

1. **Persistent job configuration**  
   Stable, non-secret job/source defaults.

2. **Per-run options**  
   Execution-specific choices such as episode selection, force retranscription, temporary model override, or manual output selection.

3. **Secrets / environment-specific values**  
   Sensitive or environment-dependent values supplied outside the public repository.

GitHub Actions primarily schedules/selects jobs rather than becoming the sole application-configuration source.

Colab should ultimately call the same application interface as other runtimes rather than maintain a separate copy of pipeline logic.

### Rationale

Using `main.py` as configuration currently forces operational changes to become source-code edits, limits the GitHub repository to one active configuration, and contributed to the need for separate Colab cells/workflows.

### Important constraints

- Ordinary configuration and secrets remain separate.
- The public repository must not contain sensitive credentials.
- Job configuration must not be duplicated unnecessarily between GitHub Actions and Colab.

### Still unresolved / deferred

- YAML, TOML, JSON, or other config-file format.
- CLI framework/library.
- Exact configuration file layout/schema.
- Exact output-policy representation.
- Exact Colab interaction/UI mechanism.

---

## Decision 5 — Google Drive Authentication and Scope

### Decision

For the current single-user Pilot:

- Kevin's OAuth user identity is the canonical Google Drive authentication model.
- GitHub Actions may use Kevin's previously authorized OAuth refresh credential for unattended execution.
- WSL and Colab may use runtime-appropriate credential storage/authorization mechanisms while operating under the same current user-authentication model.
- Service account authentication is inactive and is not part of the current architecture.
- Credentials, tokens, client secrets, and service-account credentials do not belong in the public repository or as long-term contents of a Drive code copy.
- Google Drive authorization follows a least-privilege principle, but the exact OAuth scope must be validated against required Drive operations before being changed.
- If the system later becomes multi-user, each user should authorize access using their own OAuth identity rather than sharing Kevin's OAuth credentials.

### Rationale

The current working uploader uses OAuth user credentials and refresh tokens. A service-account credential exists as historical residue but is not used by the active uploader.

OAuth is simpler for the current personal Google Drive workflow and avoids adding a second active identity/permission model.

### Important constraints

- Exact Drive scope must not be reduced without capability validation.
- Multi-user productization would require a separate per-user OAuth design.
- Credential cleanup/revocation is an operational security action, not automatically authorized by this Decision.

### Still unresolved / deferred

- Exact least-privilege Drive scope.
- OAuth credential/bootstrap/rotation implementation details.
- Cleanup/revocation of historical service-account credentials.
- Future hosted/multi-user OAuth implementation.

---

## Decision 6 — Product Source and Operation Scope

### Decision

- Scheduled RSS podcast transcription is the current **Core Operational Scope**.
- GitHub Actions is the primary runtime for that scheduled RSS workflow.
- Manual/private-audio transcription is a supported secondary interactive scope, particularly through Colab/local environments.
- Private/user-provided audio may live outside the public Git repository, including in Google Drive.
- YouTube is a supported secondary source, not currently a production-scheduled source with the same reliability requirement as RSS.
- The application should be multi-podcast-capable, but simultaneous multi-podcast scheduling is not a current Pilot requirement.
- GitHub Actions may currently operate one primary configured podcast job.
- GUI/app, multi-user operation, and large-scale batch orchestration are future context, not current stabilization acceptance requirements.

### Rationale

The demonstrated production-like use case is scheduled RSS processing.

Colab's primary manual value is GPU transcription of private/non-podcast audio stored outside GitHub. YouTube capability exists in the repository but lacks equivalent operational validation.

### Important constraints

- Core architecture should not assume only one podcast can ever exist.
- Multi-podcast capability must not be confused with a requirement to build a multi-job scheduler now.
- YouTube capability should not automatically be treated as equivalent to the validated RSS path.

### Still unresolved / deferred

- Controlled validation of YouTube behavior.
- Addition of future scheduled podcast jobs.
- GUI/app and multi-user architecture.
- Large-scale batch orchestration.

---

## Decision 7 — Pilot Acceptance Criteria

### Decision

### Transcription quality

Pilot quality uses **technical sanity + human usability**, not a formal WER/CER benchmark.

A current transcription is successful only when:
- the transcriber explicitly reports success,
- a valid non-empty TXT output exists,
- a valid current-schema JSON output exists.

### Processing and cloud sync

- `transcribed` and `synced` are distinct states.
- For scheduled RSS processing, an episode is fully synced only when both TXT and JSON are reliably present in the durable cloud destination.
- Partial synchronization must remain visible and recoverable rather than being silently treated as success.

### Retry and observability

- Clearly transient external failures use bounded retry.
- Deterministic authentication/configuration/input failures are not retried indefinitely.
- Scheduled runs must expose enough outcome information to distinguish successful processing, skipped items, failures, and pending repair/recovery work.

### Manual workflow

Manual/private-audio transcription may use a caller-selected output destination and is not required to use the scheduled podcast archive destination.

### Performance

The current demonstrated baseline of approximately 10 minutes for the full GitHub Actions pipeline on an approximately 30-minute podcast episode is acceptable for the Pilot.

Reliability has priority over performance optimization during stabilization.

### Rationale

The current implementation can confuse stale artifacts with current-run success, silently swallow failures, and leave incomplete cloud synchronization unrepaired.

The Pilot needs explicit success semantics without expanding into a formal ML benchmarking project.

### Important constraints

- Existing old TXT must not prove that a failed current retranscription succeeded.
- Partial success must remain observable.
- Retry counts/backoff are implementation details and are not fixed by this Decision.
- Performance optimization is not a current stabilization goal unless significant regression occurs.

### Still unresolved / deferred

- Exact retry count and backoff policy.
- Exact run-summary/logging representation.
- Formal quantitative transcription benchmarks for any future productized system.
- Exact persisted lifecycle-state representation.

---

## Implementation Authorization Boundary

These seven Decisions define the approved architecture/strategy state only.

They do **not** authorize:

- source-code modification,
- refactoring,
- migration,
- legacy transcript rewriting,
- credential deletion/revocation,
- configuration restructuring,
- GitHub Actions changes,
- commits,
- pushes,
- or any other repository/project-state mutation.

Each bounded implementation slice requires separate scope review and explicit Kevin authorization before execution.