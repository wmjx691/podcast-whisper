"""Closure 2 durable contracts. Paths are historical hints, never shared resources.

Only a GCS conditional pointer transition confers authority. JSON is strict and
versioned; these records assume a trusted store, not adversarial forged evidence.
Closure 1 owns identity and transcript validation; conversions below are bounded.
"""

from copy import deepcopy
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re

from episode_identity import EpisodeObservation, AttemptReference
from transcript_candidate import CandidateResult
from drive_candidate_store import CandidateUploadResult


class InvalidState(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise InvalidState(reason)


def text(value):
    require(isinstance(value, str) and bool(value.strip()), "nonempty text required")
    return value


def fields(value, names):
    require(isinstance(value, dict) and set(value) == set(names.split()),
            "missing or unexpected fields")


def number(value):
    require(type(value) in (int, float) and math.isfinite(value), "finite number required")
    return value


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value), "invalid digest")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def decode(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    try:
        require(isinstance(payload, bytes), "bytes required")
        return json.loads(payload.decode("utf-8"), object_pairs_hook=pairs,
                          parse_constant=lambda _: require(False, "nonfinite JSON"))
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise InvalidState("invalid durable JSON") from error


def observation_record(observation):
    require(isinstance(observation, EpisodeObservation), "Closure 1 observation required")
    return dict(feed=observation.feed_namespace, guid=observation.source_guid,
                observation_id=observation.observation_id, title=observation.title,
                enclosure=observation.enclosure_url,
                local_path=None if observation.local_audio_path is None
                else str(observation.local_audio_path))


def observation_from_record(value, feed):
    fields(value, "feed guid observation_id title enclosure local_path")
    require(value["feed"] == feed, "wrong observation feed")
    text(value["observation_id"])
    require(isinstance(value["title"], str), "invalid title")
    for key in ("guid", "enclosure", "local_path"):
        if value[key] is not None:
            text(value[key])
    # Avoid even transient generation of a replacement observation ID on reload.
    observation = object.__new__(EpisodeObservation)
    for key, item in dict(feed_namespace=feed, source_guid=value["guid"],
                          observation_id=value["observation_id"], title=value["title"],
                          enclosure_url=value["enclosure"], local_audio_path=(
                              None if value["local_path"] is None else Path(value["local_path"]))).items():
        object.__setattr__(observation, key, item)
    observation.__post_init__()
    return observation


def item_key(observation):
    # Canonical key has only namespace/GUID. Unknown observations stay distinct.
    return encode([observation["feed"], "guid" if observation["guid"] is not None
                   else "observation", observation["guid"] if observation["guid"] is not None
                   else observation["observation_id"]]).decode()


def snapshot_ref(value, feed):
    fields(value, "file_id sha256 snapshot_id feed")
    for key in ("file_id", "snapshot_id"):
        text(value[key])
    digest(value["sha256"])
    require(value["feed"] == feed, "wrong snapshot reference feed")


def requirement_record(identifier, observation, intent, source_attempt=None):
    value = dict(id=identifier, observation=observation_record(observation), intent=intent,
                 source_attempt=source_attempt, status="pending", reason="not_completed",
                 satisfied_by=None)
    validate_requirement(value, observation.feed_namespace)
    return value


def validate_requirement(value, feed):
    fields(value, "id observation intent source_attempt status reason satisfied_by")
    text(value["id"])
    observation_from_record(value["observation"], feed)
    require(value["intent"] in ("fresh", "force", "sync"), "invalid intent")
    if value["intent"] == "sync":
        text(value["source_attempt"])
    else:
        require(value["source_attempt"] is None, "fresh intent cannot use old attempt")
    require(value["status"] in ("pending", "satisfied"), "invalid requirement status")
    if value["status"] == "pending":
        text(value["reason"])
        require(value["satisfied_by"] is None, "pending cannot be satisfied")
    else:
        text(value["satisfied_by"])
        require(value["reason"] is None, "satisfied requirement has pending reason")


def attempt_record(reference, requirement, owner, session):
    require(isinstance(reference, AttemptReference), "Closure 1 attempt required")
    value = dict(id=reference.attempt_id, observation=observation_record(reference.observation),
                 requirement=requirement["id"], owner=owner, session=session,
                 operation=requirement["intent"], source_attempt=requirement["source_attempt"],
                 outcome="running", reason="work_intent_saved", evidence=None)
    validate_attempt(value, reference.observation.feed_namespace)
    require(value["observation"] == requirement["observation"], "attempt observation mismatch")
    return value


def validate_evidence(value, attempt, feed):
    fields(value, "candidate_id attempt_id observation validated reason artifacts")
    text(value["candidate_id"])
    evidence_attempt = attempt.get("source_attempt") if attempt.get("operation") == "sync" else attempt["id"]
    require(value["attempt_id"] == evidence_attempt and value["observation"] == attempt["observation"],
            "candidate association mismatch")
    observation_from_record(value["observation"], feed)
    require(type(value["validated"]) is bool, "invalid validation flag")
    if value["validated"]:
        require(value["reason"] is None, "validated candidate has failure reason")
    else:
        text(value["reason"])
    require(isinstance(value["artifacts"], list), "artifact list required")
    require([a.get("kind") for a in value["artifacts"] if isinstance(a, dict)]
            == (["txt", "json"] if value["validated"] else []), "invalid artifact set")
    for artifact in value["artifacts"]:
        fields(artifact, "kind local_path size sha256 folder_id remote_id write verification remote_size remote_sha256 reason")
        text(artifact["local_path"])
        require(type(artifact["size"]) is int and artifact["size"] > 0, "invalid size")
        digest(artifact["sha256"])
        for key in ("folder_id", "remote_id", "reason"):
            if artifact[key] is not None:
                text(artifact[key])
        for key in ("write", "verification"):
            require(artifact[key] in ("success", "failed", "unknown", "not_attempted"), "invalid upload outcome")
        if artifact["write"] == "success":
            text(artifact["remote_id"])
            text(artifact["folder_id"])
        if artifact["verification"] == "success":
            require(artifact["write"] == "success" and artifact["remote_size"] == artifact["size"]
                    and artifact["remote_sha256"] == artifact["sha256"], "invalid verification evidence")
        if artifact["remote_size"] is not None:
            require(type(artifact["remote_size"]) is int and artifact["remote_size"] >= 0, "invalid remote size")
        if artifact["remote_sha256"] is not None:
            digest(artifact["remote_sha256"])


def eligible(evidence):
    return (evidence is not None and evidence["validated"] and len(evidence["artifacts"]) == 2
            and all(a["write"] == a["verification"] == "success" and a["remote_id"]
                    for a in evidence["artifacts"]))


def durable_evidence(result, uploads=None):
    """Retain accepted Closure 1 results without rereading local paths or inferring success."""
    require(isinstance(result, CandidateResult), "Closure 1 CandidateResult required")
    value = dict(candidate_id=result.candidate_id, attempt_id=result.reference.attempt_id,
                 observation=observation_record(result.reference.observation), validated=result.valid,
                 reason=result.reason, artifacts=[])
    if result.valid:
        require(result.candidate.reference == result.reference
                and result.candidate.candidate_id == result.candidate_id, "candidate mismatch")
        if uploads is not None:
            require(isinstance(uploads, CandidateUploadResult) and uploads.reference == result.reference
                    and uploads.candidate_id == result.candidate_id, "upload mismatch")
            require(len(uploads.artifacts) == len(result.candidate.artifacts), "upload set mismatch")
        for i, artifact in enumerate(result.candidate.artifacts):
            upload = None if uploads is None else uploads.artifacts[i]
            if upload is not None:
                require(upload.reference == result.reference and upload.candidate_id == result.candidate_id
                        and upload.artifact == artifact, "artifact association mismatch")
            value["artifacts"].append(dict(
                kind=artifact.artifact_type, local_path=str(artifact.path), size=artifact.size_bytes,
                sha256=artifact.sha256, folder_id=None if upload is None else upload.target_folder_id,
                remote_id=None if upload is None else upload.remote_id,
                write="not_attempted" if upload is None else upload.write_outcome.value,
                verification="not_attempted" if upload is None else upload.verification_outcome.value,
                remote_size=None if upload is None else upload.remote_size_bytes,
                remote_sha256=None if upload is None else upload.remote_sha256,
                reason=None if upload is None else upload.reason))
    else:
        require(uploads is None, "failed candidate cannot attach successful uploads")
    validate_evidence(value, dict(id=result.reference.attempt_id, observation=value["observation"]),
                      result.reference.observation.feed_namespace)
    return value


def validate_attempt(value, feed):
    fields(value, "id observation requirement owner session operation source_attempt outcome reason evidence")
    observation = observation_from_record(value["observation"], feed)
    AttemptReference(observation, value["id"])
    for key in ("requirement", "owner", "session"):
        text(value[key])
    require(value["operation"] in ("fresh", "force", "sync"), "invalid operation")
    if value["operation"] == "sync":
        text(value["source_attempt"])
    else:
        require(value["source_attempt"] is None, "unexpected source attempt")
    require(value["outcome"] in ("running", "success", "failed", "unknown"), "invalid attempt outcome")
    if value["evidence"] is not None:
        validate_evidence(value["evidence"], value, feed)
    if value["outcome"] == "success":
        require(eligible(value["evidence"]) and value["reason"] is None, "success without evidence")
    else:
        text(value["reason"])


def transition(identifier, kind, owner, session, attempt=None, requirement=None, snapshot=None):
    return dict(id=identifier, kind=kind, owner=owner, session=session,
                attempt=attempt, requirement=requirement, snapshot=snapshot)


def validate_transition(value, feed):
    fields(value, "id kind owner session attempt requirement snapshot")
    for key in ("id", "owner", "session"):
        text(value[key])
    require(value["kind"] in ("acquire", "takeover", "begin", "renew", "stage", "publish", "release"),
            "invalid transition kind")
    require((value["attempt"] is None) == (value["requirement"] is None), "partial work association")
    if value["attempt"] is not None:
        text(value["attempt"])
        text(value["requirement"])
    if value["snapshot"] is not None:
        snapshot_ref(value["snapshot"], feed)


def empty_snapshot(feed, identifier, parent, publication):
    return dict(schema=1, feed=feed, id=identifier, parent=parent, publication=publication,
                requirements={}, attempts={}, generations={})


def validate_snapshot(value, feed):
    fields(value, "schema feed id parent publication requirements attempts generations")
    require(type(value["schema"]) is int and value["schema"] == 1 and value["feed"] == feed,
            "wrong snapshot schema/feed")
    text(feed)
    text(value["id"])
    if value["parent"] is not None:
        snapshot_ref(value["parent"], feed)
        require(value["parent"]["snapshot_id"] != value["id"], "self-parent snapshot")
    validate_transition(value["publication"], feed)
    require(value["publication"]["kind"] == "publish" and value["publication"]["snapshot"] is None,
            "invalid snapshot publication association")
    for key in ("requirements", "attempts", "generations"):
        require(isinstance(value[key], dict), "mapping required")
    for key, requirement in value["requirements"].items():
        validate_requirement(requirement, feed)
        require(key == requirement["id"], "requirement key mismatch")
        if requirement["intent"] == "sync":
            source = value["attempts"].get(requirement["source_attempt"])
            require(isinstance(source, dict) and source.get("evidence") is not None
                    and source.get("outcome") != "failed", "dangling sync requirement")
    for key, attempt in value["attempts"].items():
        validate_attempt(attempt, feed)
        require(key == attempt["id"], "attempt key mismatch")
        require(attempt["outcome"] != "running", "active work belongs in authority context")
        requirement = value["requirements"].get(attempt["requirement"])
        require(requirement is not None and requirement["observation"] == attempt["observation"]
                and requirement["intent"] == attempt["operation"]
                and requirement["source_attempt"] == attempt["source_attempt"], "dangling attempt requirement")
        if attempt["outcome"] == "success":
            require(requirement["status"] == "satisfied" and requirement["satisfied_by"] == attempt["id"],
                    "successful attempt contradicts requirement")
        if attempt["operation"] == "sync":
            source = value["attempts"].get(attempt["source_attempt"])
            require(source is not None and source["id"] != attempt["id"]
                    and source["evidence"] is not None and source["evidence"]["validated"]
                    and source["outcome"] != "failed"
                    and item_key(source["observation"]) == item_key(attempt["observation"]), "untrusted sync source")
            if attempt["evidence"] is not None:
                require(attempt["evidence"]["candidate_id"] == source["evidence"]["candidate_id"],
                        "sync candidate provenance mismatch")
                for original, synced in zip(source["evidence"]["artifacts"], attempt["evidence"]["artifacts"]):
                    require(all(original[k] == synced[k] for k in ("kind", "size", "sha256")),
                            "sync changed source artifact")
    for requirement in value["requirements"].values():
        if requirement["status"] == "satisfied":
            attempt = value["attempts"].get(requirement["satisfied_by"])
            require(attempt is not None and attempt["outcome"] == "success"
                    and attempt["requirement"] == requirement["id"], "invalid satisfaction")
    for key, identifier in value["generations"].items():
        attempt = value["attempts"].get(identifier)
        require(attempt is not None and attempt["outcome"] == "success"
                and item_key(attempt["observation"]) == key, "invalid formal artifact generation")
    publication = value["publication"]
    if publication["attempt"] is not None:
        attempt = value["attempts"].get(publication["attempt"])
        require(attempt is not None and attempt["requirement"] == publication["requirement"],
                "invalid publication association")
    return value


def validate_authority(value, feed):
    fields(value, "schema feed snapshot owner session expires_at work transition")
    require(type(value["schema"]) is int and value["schema"] == 1 and value["feed"] == feed,
            "wrong authority schema/feed")
    text(feed)
    if value["snapshot"] is not None:
        snapshot_ref(value["snapshot"], feed)
    if value["owner"] is None:
        require(value["session"] is None and value["expires_at"] is None, "partial ownership")
    else:
        text(value["owner"])
        text(value["session"])
        number(value["expires_at"])
    validate_transition(value["transition"], feed)
    tr = value["transition"]
    if value["transition"]["kind"] == "release":
        require(value["owner"] is None, "release must relinquish owner")
    else:
        require(value["owner"] == value["transition"]["owner"]
                and value["session"] == value["transition"]["session"], "transition owner mismatch")
    work = value["work"]
    if work is not None:
        fields(work, "requirement attempt base candidate")
        validate_requirement(work["requirement"], feed)
        validate_attempt(work["attempt"], feed)
        require(work["requirement"]["status"] == "pending" and work["attempt"]["outcome"] == "running"
                and work["attempt"]["requirement"] == work["requirement"]["id"]
                and work["attempt"]["observation"] == work["requirement"]["observation"]
                and work["attempt"]["operation"] == work["requirement"]["intent"]
                and work["attempt"]["source_attempt"] == work["requirement"]["source_attempt"],
                "invalid durable work")
        require(work["base"] == value["snapshot"], "work base differs from authority")
        if work["candidate"] is not None:
            snapshot_ref(work["candidate"], feed)
        require(tr["attempt"] == work["attempt"]["id"] and tr["requirement"] == work["requirement"]["id"],
                "transition work association mismatch")
    elif tr["kind"] not in ("publish",):
        require(tr["attempt"] is None and tr["requirement"] is None, "transition refers to absent work")
    if tr["kind"] in ("begin", "stage"):
        require(work is not None, "work transition missing context")
    if tr["kind"] == "stage":
        require(tr["snapshot"] is not None and tr["snapshot"] == work["candidate"], "stage pointer mismatch")
    elif tr["kind"] == "publish":
        require(work is None and tr["snapshot"] is not None and tr["snapshot"] == value["snapshot"]
                and tr["attempt"] is not None, "publication pointer mismatch")
    else:
        require(tr["snapshot"] is None, "unexpected transition pointer")
    return value


def load_snapshot(payload, feed):
    return validate_snapshot(decode(payload), feed)


def load_authority(payload, feed):
    return validate_authority(decode(payload), feed)


@dataclass(frozen=True)
class Observation:
    record: dict
    generation: str


@dataclass(frozen=True)
class Mutation:
    outcome: str
    generation: str = None
    reference: dict = None
    reason: str = None


def result_snapshot(base, work, outcome, reason, evidence, identifier, publication):
    """One result changes only its requirement; all other records survive verbatim."""
    state = deepcopy(base)
    state.update(id=identifier, parent=deepcopy(work["base"]), publication=publication)
    requirement, attempt = deepcopy(work["requirement"]), deepcopy(work["attempt"])
    attempt.update(outcome=outcome, reason=reason, evidence=deepcopy(evidence))
    require(attempt["id"] not in state["attempts"], "attempt history is immutable")
    if outcome == "success":
        require(eligible(evidence), "new success requires complete candidate evidence")
        requirement.update(status="satisfied", reason=None, satisfied_by=attempt["id"])
        state["generations"][item_key(attempt["observation"])] = attempt["id"]
    else:
        requirement["reason"] = reason
    state["requirements"][requirement["id"]] = requirement
    state["attempts"][attempt["id"]] = attempt
    return validate_snapshot(state, state["feed"])
