"""Immutable Drive snapshot candidates via injected Drive v3 service/executor.

The required execute(request, deadline) boundary must enforce the deadline and call
request.execute(num_retries=0). Gate A supplies a deterministic fake executor; no
real HTTP timeout/thread runtime is installed here. There is no Drive HEAD.
"""

import hashlib
from io import BytesIO

from processing_state import (Mutation, eligible, encode, load_snapshot, require,
                              snapshot_ref, text, validate_snapshot)
from transcript_candidate import recognize_artifacts


class SnapshotReadError(RuntimeError):
    pass


class DriveSnapshotStore:
    def __init__(self, *, service, execute, folder_id, feed):
        text(folder_id)
        text(feed)
        require(callable(execute), "deadline-aware executor required")
        self.service, self.execute, self.folder_id, self.feed = service, execute, folder_id, feed

    def _bytes(self, identifier, deadline):
        text(identifier)
        payload = self.execute(self.service.files().get_media(fileId=identifier), deadline)
        require(isinstance(payload, bytes), "Drive media bytes required")
        return payload

    def read(self, reference, deadline):
        snapshot_ref(reference, self.feed)
        try:
            payload = self._bytes(reference["file_id"], deadline)
            require(hashlib.sha256(payload).hexdigest() == reference["sha256"], "snapshot hash mismatch")
            state = load_snapshot(payload, self.feed)
            require(state["id"] == reference["snapshot_id"], "snapshot ID mismatch")
            require(encode(state) == payload, "noncanonical snapshot bytes")
            return state
        except Exception as error:
            raise SnapshotReadError("exact snapshot read/verification unresolved") from error

    def create(self, state, deadline):
        validate_snapshot(state, self.feed)
        payload = encode(state)
        # Existing SDK imported without constructing service, credentials or auth.
        from googleapiclient.http import MediaIoBaseUpload
        from googleapiclient.errors import HttpError
        try:
            request = self.service.files().create(
                body={"name": "snapshot-" + state["id"] + ".json", "parents": [self.folder_id]},
                media_body=MediaIoBaseUpload(BytesIO(payload), mimetype="application/json", resumable=False),
                fields="id")
        except Exception:
            return Mutation("rejected", reason="snapshot_request_preparation_failed")
        try:
            response = self.execute(request, deadline)
        except Exception as error:
            rejected = isinstance(error, HttpError) and error.resp.status in (400, 401, 403, 404, 405, 413, 415, 422)
            return Mutation("rejected" if rejected else "unknown", reason="snapshot_create_unconfirmed")
        identifier = response.get("id") if isinstance(response, dict) else None
        if not isinstance(identifier, str) or not identifier.strip():
            return Mutation("unknown", reason="snapshot_create_missing_id")
        reference = dict(file_id=identifier, sha256=hashlib.sha256(payload).hexdigest(),
                         snapshot_id=state["id"], feed=self.feed)
        try:
            verified = self.read(reference, deadline)
            require(encode(verified) == payload, "snapshot readback differs from candidate")
        except Exception:
            return Mutation("rejected", reference=reference, reason="known_snapshot_unverified")
        return Mutation("success", reference=reference)

    def verify_artifacts(self, evidence, deadline):
        require(eligible(evidence), "complete trusted success evidence required")
        try:
            payloads = []
            for artifact in evidence["artifacts"]:
                payload = self._bytes(artifact["remote_id"], deadline)
                require(len(payload) == artifact["size"]
                        and hashlib.sha256(payload).hexdigest() == artifact["sha256"], "artifact integrity mismatch")
                payloads.append(payload.decode("utf-8"))
            require(recognize_artifacts(*payloads) == "current", "Closure 1 schema verification failed")
        except Exception as error:
            raise SnapshotReadError("artifact recovery verification unresolved") from error
