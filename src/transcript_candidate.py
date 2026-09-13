"""Fresh attempt-scoped candidates. Validation confers no publication authority."""

from dataclasses import dataclass
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Tuple
from uuid import uuid4

from episode_identity import AttemptReference


WORK_STAGES = frozenset((
    "episode_selected", "path_prepare", "audio_fetch", "audio_validate",
    "transcriber_init", "transcription", "candidate_build", "candidate_validate",
    "candidate_upload_txt", "candidate_upload_json", "callback_finalize",
    "result_persist", "run_finalize",
))
CHECKPOINT_STATES = frozenset(("START", "COMPLETE", "FAIL"))
_execution = ContextVar("execution_checkpoint", default=None)


class ExecutionContext:
    """Attempt-local, non-durable metadata; never reads or changes exceptions."""

    def __init__(self):
        self.failed_stage = "unspecified"
        self.active = set()
        self.failed = set()
        self.progress_percent = 0

    def event(self, stage, state):
        if type(stage) is not str or stage not in WORK_STAGES:
            return
        if type(state) is not str or state not in CHECKPOINT_STATES:
            return
        if state == "START":
            if stage in self.active or stage in self.failed:
                return
            self.active.add(stage)
        else:
            if stage not in self.active:
                return
            self.active.remove(stage)
            if state == "FAIL":
                self.failed.add(stage)
                if self.failed_stage == "unspecified":
                    self.failed_stage = stage
        # A closed output stream must not change the operation's result.
        try:
            print(f"Execution checkpoint: stage={stage} state={state}", flush=True)
        except Exception:
            pass

    def progress(self, end, duration):
        # Bound output to at most ten increasing ticks, even with bad timings.
        if (type(end) not in (int, float) or type(duration) not in (int, float)
                or not 0 < duration < float('inf') or not 0 <= end < float('inf')):
            return
        percent = int(min(end / duration, 1) * 10) * 10
        if percent > self.progress_percent:
            self.progress_percent = percent
            try:
                print(f"Execution progress: stage=transcription percent={percent}", flush=True)
            except Exception:
                pass


def current_execution():
    return _execution.get()


class execution_context:
    def __enter__(self):
        self.context = ExecutionContext()
        self.token = _execution.set(self.context)
        return self.context

    def __exit__(self, exc_type, error, traceback):
        _execution.reset(self.token)


class checkpoint:
    """No generator context manager: even traceback assignment can fail on a
    frozen exception. __exit__ observes only whether the operation raised.
    """

    def __init__(self, stage):
        self.stage = stage

    def __enter__(self):
        self.context = current_execution()
        self.token = None
        if self.context is None:
            self.context = ExecutionContext()
            self.token = _execution.set(self.context)
        self.owned = self.stage not in self.context.active
        if self.owned:
            self.context.event(self.stage, "START")
        return self.context

    def __exit__(self, exc_type, error, traceback):
        if exc_type is not None:
            self.context.event(self.stage, "FAIL")
        elif self.owned:
            self.context.event(self.stage, "COMPLETE")
        if self.token is not None:
            _execution.reset(self.token)


# Selectively copied pure schema semantics from IdempotencyChecker in
# src/idempotency_checker.py; no classification/authority or recovery dependency.
def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_transcript_segment(segment):
    if not isinstance(segment, dict):
        return False
    segment_id, start, end = (segment.get(k) for k in ("id", "start", "end"))
    return (isinstance(segment_id, int) and not isinstance(segment_id, bool)
            and segment_id >= 1 and _is_number(start) and _is_number(end)
            and 0 <= start <= end and isinstance(segment.get("text"), str))


def is_current_transcript_json(data):
    if not isinstance(data, dict):
        return False
    metadata, segments = data.get("metadata"), data.get("segments")
    if not isinstance(metadata, dict) or not isinstance(segments, list):
        return False
    if any(not isinstance(metadata.get(k), str) or not metadata[k].strip()
           for k in ("model_size", "environment", "language", "timestamp")):
        return False
    if not isinstance(metadata.get("prompt"), (str, type(None))):
        return False
    return all(_is_transcript_segment(segment) for segment in segments)


def is_legacy_transcript_json(data):
    return (isinstance(data, list) and bool(data)
            and all(_is_transcript_segment(segment) for segment in data))


def recognize_artifacts(txt_content: Optional[str], json_content: Optional[str]) -> str:
    """Recognize supplied content only. None means absent; no filesystem effects."""
    txt_valid = isinstance(txt_content, str) and bool(txt_content.strip())
    if json_content is None:
        return "legacy_txt" if txt_valid else "partial"
    if not isinstance(json_content, str):
        return "unreadable"
    try:
        data = json.loads(json_content)
    except (ValueError, RecursionError):
        return "malformed_json"
    if is_current_transcript_json(data):
        return "current" if txt_valid else "current_incomplete"
    if is_legacy_transcript_json(data):
        return "legacy_json" if txt_content is None or txt_valid else "partial"
    if isinstance(data, dict) and ("metadata" in data or "segments" in data):
        return "current_incomplete"
    return "non_transcript_json"


@dataclass(frozen=True)
class CandidateArtifact:
    artifact_type: str
    path: Path
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class ValidatedCandidate:
    reference: AttemptReference
    candidate_id: str
    staging_directory: Path
    artifacts: Tuple[CandidateArtifact, ...]


@dataclass(frozen=True)
class CandidateResult:
    reference: AttemptReference
    candidate_id: str
    staging_directory: Path
    expected_txt_path: Path
    expected_json_path: Path
    candidate: Optional[ValidatedCandidate]
    reason: Optional[str]

    @property
    def valid(self) -> bool:
        return self.candidate is not None

    @property
    def work_stage(self):
        """Fixed metadata for existing invalid results; not durable evidence."""
        if self.reason in ("staging_exists", "staging_creation_failed"):
            return "candidate_build"
        if self.reason == "transcriber_exception":
            return "transcription"
        return "candidate_validate" if not self.valid else "unspecified"


def read_staged_artifact(path: Path, directory: Path) -> bytes:
    """Reject redirected paths, symlinks and hard-linked historical artifacts.

    The staging root is caller controlled; this is not a hostile concurrent
    filesystem/ownership boundary. No archive enumeration is performed.
    """
    if directory.is_symlink() or directory.resolve(strict=True) != directory:
        raise ValueError("staging_directory_redirected")
    if path.parent != directory or path.is_symlink() or path.resolve(strict=True) != path:
        raise ValueError("artifact_path_redirected")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("artifact_not_independent_regular_file")
    return path.read_bytes()


def build_candidate(transcriber, *, reference: AttemptReference, staging_root,
                    language: str, initial_prompt: str) -> CandidateResult:
    """Use an injected transcribe_file implementation with the existing contract.

    Caller supplies an existing staging root and a new attempt reference.
    Failures leave this attempt's evidence in place and never clean old outputs.
    """
    with checkpoint("candidate_build") as context:
        if not isinstance(reference, AttemptReference):
            raise TypeError("explicit AttemptReference required")
        audio = reference.observation.local_audio_path
        if audio is None:
            raise ValueError("caller-supplied local audio path required")
        root = Path(staging_root).resolve(strict=True)
        directory = root / reference.attempt_id
        base = os.path.splitext(audio.name)[0]
        txt, json_path = directory / (base + ".txt"), directory / (base + ".json")
        candidate_id = uuid4().hex

        def result(reason, candidate=None):
            return CandidateResult(reference, candidate_id, directory, txt, json_path,
                                   candidate, reason)

        try:
            directory.mkdir(exist_ok=False)
        except FileExistsError:
            context.event("candidate_build", "FAIL")
            return result("staging_exists")
        except OSError:
            context.event("candidate_build", "FAIL")
            return result("staging_creation_failed")
    with checkpoint("transcription") as context:
        try:
            returned = transcriber.transcribe_file(
                audio_path=str(audio), output_dir=str(directory), language=language,
                initial_prompt=initial_prompt, force_retranscribe=False,
            )
        except Exception:
            context.event("transcription", "FAIL")
            return result("transcriber_exception")
    with checkpoint("candidate_validate") as context:
        def invalid(reason):
            context.event("candidate_validate", "FAIL")
            return result(reason)
        if not isinstance(returned, str) or not returned:
            return invalid("transcriber_did_not_return_path")
        try:
            # Require the expected lexical path as well as its resolved destination.
            if Path(os.path.abspath(returned)) != txt or Path(returned).resolve(strict=True) != txt:
                return invalid("returned_path_mismatch")
            txt_bytes = read_staged_artifact(txt, directory)
            json_bytes = read_staged_artifact(json_path, directory)
            schema = recognize_artifacts(txt_bytes.decode("utf-8"), json_bytes.decode("utf-8"))
            if schema != "current":
                return invalid("invalid_current_output:" + schema)
        except (OSError, ValueError, RuntimeError):
            return invalid("output_missing_unreadable_or_redirected")
        artifacts = tuple(CandidateArtifact(kind, path, len(content), hashlib.sha256(content).hexdigest())
                          for kind, path, content in (("txt", txt, txt_bytes),
                                                      ("json", json_path, json_bytes)))
        return result(None, ValidatedCandidate(reference, candidate_id, directory, artifacts))
