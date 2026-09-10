"""Offline-composable Segment 3A boundary; no client creation or bootstrap.

The coordinator factory must return a fresh, exclusively owned session each
time. Work/uploader callbacks perform non-authoritative operations only. They
must not access that session. Recovery uses durable observations, never searches
for historical files. A caller-provided worker resolves any required audio.
"""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import uuid4

from authority_coordinator import AuthorityError
from authority_heartbeat import AuthorityHeartbeat
from episode_identity import AttemptReference
from processing_state import (durable_evidence, eligible, item_key,
                              observation_from_record, observation_record,
                              requirement_record, validate_evidence)


PRODUCTION_FEED_NAMESPACE = "podcast-whisper-primary"


@dataclass
class RunReport:
    processed: list = field(default_factory=list)
    recovered: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    pending: list = field(default_factory=list)
    run_errors: list = field(default_factory=list)


def cutover_reason(observation, published_at, cutover_at):
    if observation.identity is None:
        return "identity_unknown"
    if not isinstance(published_at, datetime) or published_at.utcoffset() is None:
        return "cutover_eligibility_unknown"
    if published_at < cutover_at:
        return "legacy_pre_cutover"
    if published_at == cutover_at:
        return "cutover_boundary_ineligible"
    return None


class PipelineOrchestrator:
    def __init__(self, *, coordinator_factory, discover, work, upload,
                 cutover_at, feed_namespace=PRODUCTION_FEED_NAMESPACE,
                 heartbeat_factory=AuthorityHeartbeat, new_id=lambda: uuid4().hex,
                 sync_work=None):
        if not isinstance(cutover_at, datetime) or cutover_at.utcoffset() is None:
            raise ValueError("explicit timezone-aware cutover_at required")
        if not isinstance(feed_namespace, str) or not feed_namespace.strip():
            raise ValueError("explicit feed_namespace required")
        self.coordinator_factory, self.discover = coordinator_factory, discover
        self.work, self.upload, self.sync_work = work, upload, sync_work
        self.cutover_at, self.feed = cutover_at, feed_namespace
        self.heartbeat_factory, self.new_id = heartbeat_factory, new_id

    def _coordinator(self):
        c = self.coordinator_factory()
        if c.feed != self.feed or c.observed is not None or c.unresolved is not None:
            raise AuthorityError("fresh exclusive coordinator for configured feed required")
        return c

    @staticmethod
    def _confirmed(result, operation):
        if result.outcome != "success":
            raise AuthorityError(operation + ": " + result.outcome)

    @staticmethod
    def _state(c):
        return c.snapshot_store.read(c.observed.record["snapshot"], c.timing.request_deadline)

    @staticmethod
    def _facts(report, state):
        # Only durable failed attempts belong in failed. Local errors stay separate.
        report.failed = [a for a in state["attempts"].values() if a["outcome"] == "failed"]
        report.pending = [p for p in report.pending if p.get("fact") != "durable_requirement"]
        report.pending.extend(dict(fact="durable_requirement", **r)
                              for r in state["requirements"].values() if r["status"] == "pending")

    def _attempt(self, requirement, report, *, recovery):
        c = self._coordinator()
        reference = AttemptReference(observation_from_record(requirement["observation"], self.feed), self.new_id())
        fact = dict(observation=requirement["observation"], requirement=requirement["id"],
                    attempt=reference.attempt_id)
        try:
            self._confirmed(c.acquire(), "acquire")
            self._confirmed(c.begin(requirement, reference), "begin")
            heartbeat = self.heartbeat_factory(c)
            evidence, outcome, reason = None, "failed", None
            heartbeat.start()
            try:
                if requirement["intent"] == "sync":
                    source = self._state(c)["attempts"][requirement["source_attempt"]]["evidence"]
                    evidence = self.sync_work(requirement, reference, source)
                    validate_evidence(evidence, c.observed.record["work"]["attempt"], self.feed)
                else:
                    candidate = self.work(requirement, reference)
                    if candidate.reference != reference:
                        raise ValueError("worker candidate association mismatch")
                    if not candidate.valid:
                        report.run_errors.append(dict(phase="work", reason=candidate.reason,
                                                      durable=False, **fact))
                    uploads = self.upload(candidate.candidate) if candidate.valid else None
                    evidence = durable_evidence(candidate, uploads)
                if eligible(evidence):
                    outcome = "success"
                else:
                    uncertain = any(a["write"] == "unknown" or a["verification"] == "unknown"
                                    for a in evidence["artifacts"])
                    outcome = "unknown" if uncertain else "failed"
                    reason = evidence["reason"] or "candidate_upload_" + outcome
            except Exception as error:
                evidence = None
                reason = "local_work_error: " + str(error)
                report.run_errors.append(dict(phase="work", reason=reason, durable=False, **fact))
            finally:
                heartbeat.stop()
                heartbeat.join()
            heartbeat.assert_healthy()
            self._confirmed(c.renew(), "final_renew")
            self._confirmed(c.publish_result(outcome=outcome, reason=reason, evidence=evidence), "publish_result")
            state = self._state(c)
            self._facts(report, state)
            if outcome == "success":
                (report.recovered if recovery else report.processed).append(fact)
            self._confirmed(c.release(), "release")
            return state
        except Exception as error:
            # Never reconcile/refresh this execution after losing confidence.
            report.run_errors.append(dict(phase="authority", reason=str(error), **fact))
            report.pending.append(dict(reason="reconciliation_required", **fact))
            return None

    def run(self, *, force=False):
        report = RunReport()
        try:
            c = self._coordinator()
            initial = c.authority_store.read(c.timing.request_deadline)
            if initial is None or initial.record["snapshot"] is None:
                report.run_errors.append(dict(reason="initialization_required"))
                return report
            # Exact durable reads happen before acquisition and before discovery.
            state = c.snapshot_store.read(initial.record["snapshot"], c.timing.request_deadline)
            self._facts(report, state)
            self._confirmed(c.acquire(), "recovery_acquire")
            work = c.observed.record["work"]
            if work is not None:
                self._confirmed(c.renew(), "recovery_final_renew")
                known = observation_from_record(work["requirement"]["observation"], self.feed).identity is not None
                self._confirmed(c.recover_work(adopt_candidate=known and work["candidate"] is not None), "recover_work")
                state = self._state(c)
                attempt = state["attempts"][work["attempt"]["id"]]
                if attempt["outcome"] == "success":
                    report.recovered.append(dict(observation=attempt["observation"],
                                                 requirement=attempt["requirement"], attempt=attempt["id"]))
            state = self._state(c)
            self._facts(report, state)
            self._confirmed(c.release(), "recovery_release")
        except Exception as error:
            report.run_errors.append(dict(phase="recovery", reason=str(error)))
            report.pending.append(dict(reason="reconciliation_required"))
            return report

        handled = set()
        # A durable requirement is existing authorized intent, not a new RSS item.
        for requirement in list(state["requirements"].values()):
            if requirement["status"] != "pending":
                continue
            key = item_key(requirement["observation"])
            handled.add(key)
            observation = observation_from_record(requirement["observation"], self.feed)
            reason = ("identity_unknown" if observation.identity is None else
                      "sync_worker_required" if requirement["intent"] == "sync" and self.sync_work is None else None)
            if reason:
                report.pending.append(dict(reason=reason, requirement=requirement["id"],
                                           observation=requirement["observation"]))
                continue
            state = self._attempt(requirement, report, recovery=True)
            if state is None:
                return report

        try:
            for published in self.discover():
                observation = published.observation
                record = observation_record(observation)
                if observation.feed_namespace != self.feed:
                    report.pending.append(dict(reason="feed_namespace_mismatch", observation=record))
                    continue
                reason = cutover_reason(observation, published.published_at, self.cutover_at)
                if reason:
                    target = report.skipped if reason in ("legacy_pre_cutover", "cutover_boundary_ineligible") else report.pending
                    target.append(dict(reason=reason, observation=record))
                    continue
                key = item_key(record)
                if key in handled:
                    report.skipped.append(dict(reason="already_planned_this_run", observation=record))
                    continue
                if not force and key in state["generations"]:
                    report.skipped.append(dict(reason="already_satisfied", observation=record))
                    continue
                handled.add(key)
                requirement = requirement_record(self.new_id(), observation, "force" if force else "fresh")
                state = self._attempt(requirement, report, recovery=False)
                if state is None:
                    return report
        except Exception as error:
            report.run_errors.append(dict(phase="discovery", reason=str(error)))
        return report
