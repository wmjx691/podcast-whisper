"""Create-only candidate uploads through an injected Drive v3 service."""

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
from io import BytesIO
from typing import Optional, Tuple

from episode_identity import AttemptReference
from transcript_candidate import (CandidateArtifact, ValidatedCandidate,
                                  read_staged_artifact, recognize_artifacts)


class Outcome(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    NOT_ATTEMPTED = "not_attempted"


@dataclass(frozen=True)
class ArtifactUploadResult:
    reference: AttemptReference
    candidate_id: str
    artifact: CandidateArtifact
    target_folder_id: str
    remote_id: Optional[str] = None
    write_outcome: Outcome = Outcome.NOT_ATTEMPTED
    verification_outcome: Outcome = Outcome.NOT_ATTEMPTED
    remote_size_bytes: Optional[int] = None
    remote_sha256: Optional[str] = None
    reason: Optional[str] = None


@dataclass(frozen=True)
class CandidateUploadResult:
    reference: AttemptReference
    candidate_id: str
    artifacts: Tuple[ArtifactUploadResult, ...]

    @property
    def all_verified(self) -> bool:
        """Candidate integrity only; never an authority or processing decision."""
        return bool(self.artifacts) and all(
            r.write_outcome == Outcome.SUCCESS and r.verification_outcome == Outcome.SUCCESS
            for r in self.artifacts
        )


def upload_candidate(service, *, candidate: ValidatedCandidate,
                     target_folder_id: str) -> CandidateUploadResult:
    """Create each file once, then read its bytes by the returned exact ID.

    Stop on any failure/uncertainty, retaining remaining NOT_ATTEMPTED results.
    There is no retry, search, login, folder creation, cleanup or reconciliation.
    Exceptions after execute starts are conservatively unknown unless the API
    explicitly rejects the request with a definitive client error response.
    """
    if not isinstance(candidate, ValidatedCandidate):
        raise TypeError("ValidatedCandidate required")
    if not isinstance(target_folder_id, str) or not target_folder_id.strip():
        raise ValueError("exact target_folder_id required")
    if tuple(a.artifact_type for a in candidate.artifacts) != ("txt", "json"):
        raise ValueError("a complete ordered TXT/JSON candidate set is required")
    results = [ArtifactUploadResult(candidate.reference, candidate.candidate_id, a,
                                    target_folder_id) for a in candidate.artifacts]

    def finish():
        return CandidateUploadResult(candidate.reference, candidate.candidate_id, tuple(results))

    # Snapshot both validated files before issuing any create. Upload these exact
    # bytes so later local edits cannot silently change the request content.
    payloads = []
    for i, artifact in enumerate(candidate.artifacts):
        try:
            payload = read_staged_artifact(artifact.path, candidate.staging_directory)
            if (len(payload) != artifact.size_bytes
                    or hashlib.sha256(payload).hexdigest() != artifact.sha256):
                raise ValueError("integrity_changed")
            payloads.append(payload)
        except (OSError, ValueError, RuntimeError):
            results[i] = replace(results[i], write_outcome=Outcome.FAILED,
                                 reason="local_artifact_changed_or_unreadable")
            return finish()
    try:
        if recognize_artifacts(*(p.decode("utf-8") for p in payloads)) != "current":
            raise ValueError("invalid_current_schema")
    except (ValueError, RuntimeError):
        results[0] = replace(results[0], write_outcome=Outcome.FAILED,
                             reason="invalid_candidate_content")
        return finish()

    # Existing dependency; importing here does not construct credentials/service.
    from googleapiclient.http import MediaIoBaseUpload
    from googleapiclient.errors import HttpError

    for i, (artifact, payload) in enumerate(zip(candidate.artifacts, payloads)):
        try:
            media = MediaIoBaseUpload(BytesIO(payload), mimetype=(
                "text/plain" if artifact.artifact_type == "txt" else "application/json"
            ), resumable=False)
            request = service.files().create(
                body={"name": artifact.path.name, "parents": [target_folder_id]},
                media_body=media, fields="id",
            )
        except Exception:
            results[i] = replace(results[i], write_outcome=Outcome.FAILED,
                                 reason="create_request_preparation_failed")
            break
        try:
            response = request.execute(num_retries=0)
        except Exception as error:
            rejected = (isinstance(error, HttpError)
                        and error.resp.status in (400, 401, 403, 404, 405, 413, 415, 422))
            results[i] = replace(results[i], write_outcome=(
                Outcome.FAILED if rejected else Outcome.UNKNOWN
            ), reason="create_rejected" if rejected else "create_response_unknown")
            break
        remote_id = response.get("id") if isinstance(response, dict) else None
        if not isinstance(remote_id, str) or not remote_id.strip():
            results[i] = replace(results[i], write_outcome=Outcome.UNKNOWN,
                                 reason="create_response_missing_id")
            break
        results[i] = replace(results[i], remote_id=remote_id, write_outcome=Outcome.SUCCESS)
        try:
            remote = service.files().get_media(fileId=remote_id).execute(num_retries=0)
            if not isinstance(remote, bytes):
                raise TypeError("readback_must_be_bytes")
            matched = remote == payload
            results[i] = replace(
                results[i], verification_outcome=Outcome.SUCCESS if matched else Outcome.FAILED,
                remote_size_bytes=len(remote), remote_sha256=hashlib.sha256(remote).hexdigest(),
                reason=None if matched else "remote_content_mismatch",
            )
        except Exception:
            results[i] = replace(results[i], verification_outcome=Outcome.UNKNOWN,
                                 reason="exact_id_readback_unresolved")
        if results[i].verification_outcome != Outcome.SUCCESS:
            break
    return finish()
