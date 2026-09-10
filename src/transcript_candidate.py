"""Fresh attempt-scoped candidates. Validation confers no publication authority."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from typing import Optional, Tuple
from uuid import uuid4

from episode_identity import AttemptReference


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
        return result("staging_exists")
    except OSError:
        return result("staging_creation_failed")
    try:
        returned = transcriber.transcribe_file(
            audio_path=str(audio), output_dir=str(directory), language=language,
            initial_prompt=initial_prompt, force_retranscribe=False,
        )
    except Exception:
        return result("transcriber_exception")
    if not isinstance(returned, str) or not returned:
        return result("transcriber_did_not_return_path")
    try:
        # Require the expected lexical path as well as its resolved destination.
        if Path(os.path.abspath(returned)) != txt or Path(returned).resolve(strict=True) != txt:
            return result("returned_path_mismatch")
        txt_bytes = read_staged_artifact(txt, directory)
        json_bytes = read_staged_artifact(json_path, directory)
        schema = recognize_artifacts(txt_bytes.decode("utf-8"), json_bytes.decode("utf-8"))
        if schema != "current":
            return result("invalid_current_output:" + schema)
    except (OSError, ValueError, RuntimeError):
        return result("output_missing_unreadable_or_redirected")
    artifacts = tuple(CandidateArtifact(kind, path, len(content), hashlib.sha256(content).hexdigest())
                      for kind, path, content in (("txt", txt, txt_bytes),
                                                  ("json", json_path, json_bytes)))
    return result(None, ValidatedCandidate(reference, candidate_id, directory, artifacts))
