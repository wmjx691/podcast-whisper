"""Serialized, generation-fenced authority transitions; no runtime scheduler.

Clock contract: wall time is comparable across owners within the caller's margin;
elapsed time is monotonic locally. Unknown confidence or discontinuities fail closed.
The lease is a client eligibility check, not a GCS-enforced CPU cancellation rule.
Stores must enforce the supplied request deadline; a late response grants no work.
"""

from copy import deepcopy
from dataclasses import dataclass
import threading
from uuid import uuid4

from processing_state import (InvalidState, Mutation, Observation, attempt_record,
                              empty_snapshot, encode, number, require, result_snapshot,
                              text, transition, validate_authority, validate_requirement)


class AuthorityError(RuntimeError):
    pass


@dataclass(frozen=True)
class TimingPolicy:
    lease_duration: float
    renewal_interval: float
    safety_margin: float
    request_deadline: float
    reconciliation_reads: int

    def __post_init__(self):
        for value in (self.lease_duration, self.renewal_interval, self.safety_margin,
                      self.request_deadline):
            require(number(value) > 0, "positive timing parameters required")
        require(self.renewal_interval + 2 * self.request_deadline + 2 * self.safety_margin
                < self.lease_duration, "insufficient lease budget")
        require(type(self.reconciliation_reads) is int and 1 <= self.reconciliation_reads <= 100,
                "bounded reconciliation_reads required (1..100)")


@dataclass(frozen=True)
class ClockReading:
    wall: float
    elapsed: float
    uncertainty: float


class AuthorityCoordinator:
    def __init__(self, *, feed, owner, authority_store, snapshot_store, clock, timing):
        text(feed)
        text(owner)
        require(isinstance(timing, TimingPolicy), "explicit TimingPolicy required")
        self.feed, self.owner, self.session = feed, owner, uuid4().hex
        self.authority_store, self.snapshot_store = authority_store, snapshot_store
        self.clock, self.timing = clock, timing
        self._lock = threading.RLock()
        self._observed = None
        self._pending = None
        self._last_clock = None
        self._last_renewal = None
        self._driving = False

    @property
    def observed(self):
        with self._lock:
            return deepcopy(self._observed)

    @property
    def unresolved(self):
        with self._lock:
            return deepcopy(self._pending)

    def _now(self):
        sample = self.clock.sample()
        require(isinstance(sample, ClockReading), "clock confidence required")
        for value in (sample.wall, sample.elapsed, sample.uncertainty):
            number(value)
        require(0 <= sample.uncertainty <= self.timing.safety_margin, "clock uncertainty exceeds policy")
        if self._last_clock is not None:
            elapsed = sample.elapsed - self._last_clock.elapsed
            wall = sample.wall - self._last_clock.wall
            require(elapsed >= 0 and abs(wall - elapsed) <= 2 * self.timing.safety_margin,
                    "clock discontinuity")
        self._last_clock = sample
        return sample

    def _usable(self):
        if self._pending is not None:
            raise AuthorityError("unresolved transition; read-only reconciliation required")
        observed = self._observed
        if observed is None:
            raise AuthorityError("no confirmed acquisition")
        record = observed.record
        if record["owner"] != self.owner or record["session"] != self.session:
            raise AuthorityError("wrong owner/session")
        sample = self._now()
        if sample.wall + self.timing.safety_margin + self.timing.request_deadline >= record["expires_at"]:
            raise AuthorityError("lease has insufficient request budget")
        return sample

    def _association(self, kind, snapshot=None, identifier=None):
        work = None if self._observed is None else self._observed.record["work"]
        return transition(identifier or uuid4().hex, kind, self.owner, self.session,
                          None if work is None else work["attempt"]["id"],
                          None if work is None else work["requirement"]["id"], snapshot)

    def _send(self, intended, expected):
        validate_authority(intended, self.feed)
        # Retain the exact intended bytes before the first transport invocation.
        self._pending = deepcopy(intended)
        result = self.authority_store.write(intended, expected, self.timing.request_deadline)
        if result.outcome == "success":
            self._observed = Observation(deepcopy(intended), result.generation)
            self._pending = None
        elif result.outcome == "rejected":
            self._pending = None
        # Conflict cannot identify the winner. Never adopt a fresh token here.
        return result

    def acquire(self, *, initialize=False):
        with self._lock:
            if self._pending is not None or self._observed is not None:
                raise AuthorityError("existing handle cannot be refreshed by acquisition")
            sample = self._now()
            observed = self.authority_store.read(self.timing.request_deadline)
            sample = self._now()
            if observed is None:
                if not initialize:
                    raise AuthorityError("explicit initialization required")
                record = dict(schema=1, feed=self.feed, snapshot=None, owner=None, session=None,
                              expires_at=None, work=None, transition=None)
                expected, kind = None, "acquire"
            else:
                record = deepcopy(observed.record)
                if record["owner"] is not None:
                    if sample.wall - self.timing.safety_margin <= record["expires_at"]:
                        raise AuthorityError("valid or uncertain owner blocks acquisition")
                    kind = "takeover"
                else:
                    kind = "acquire"
                expected = observed.generation
            record.update(owner=self.owner, session=self.session,
                          expires_at=sample.wall + self.timing.lease_duration)
            work = record["work"]
            record["transition"] = transition(uuid4().hex, kind, self.owner, self.session,
                                               None if work is None else work["attempt"]["id"],
                                               None if work is None else work["requirement"]["id"])
            result = self._send(record, expected)
            if result.outcome == "success":
                self._last_renewal = sample.elapsed
                self._usable()
            return result

    def _base(self):
        pointer = self._observed.record["snapshot"]
        if pointer is None:
            return empty_snapshot(self.feed, uuid4().hex, None, self._association("publish"))
        return self.snapshot_store.read(pointer, self.timing.request_deadline)

    def begin(self, requirement, reference):
        with self._lock:
            self._usable()
            record = deepcopy(self._observed.record)
            if record["work"] is not None:
                raise AuthorityError("prior durable work must be recovered first")
            validate_requirement(requirement, self.feed)
            require(requirement["status"] == "pending", "cannot begin satisfied requirement")
            base = self._base()
            old = base["requirements"].get(requirement["id"])
            require(old is None or old == requirement, "requirement identity/intent is immutable")
            require(reference.attempt_id not in base["attempts"], "attempt ID already used")
            attempt = attempt_record(reference, requirement, self.owner, self.session)
            if requirement["intent"] == "sync":
                source = base["attempts"].get(requirement["source_attempt"])
                require(source is not None and source["outcome"] != "failed"
                        and source["evidence"] is not None and source["evidence"]["validated"]
                        and source["observation"] == attempt["observation"],
                        "sync requires durable validated provenance")
            record["work"] = dict(requirement=deepcopy(requirement), attempt=attempt,
                                  base=record["snapshot"], candidate=None)
            record["transition"] = transition(uuid4().hex, "begin", self.owner, self.session,
                                               attempt["id"], requirement["id"])
            self._usable()
            return self._send(record, self._observed.generation)

    def renew(self):
        with self._lock:
            sample = self._usable()
            record = deepcopy(self._observed.record)
            record.update(expires_at=sample.wall + self.timing.lease_duration,
                          transition=self._association("renew"))
            result = self._send(record, self._observed.generation)
            if result.outcome == "success":
                self._last_renewal = sample.elapsed
            return result

    def run_work(self, requirement, reference, steps):
        """Persist begin, then drive a cooperative iterator with automatic renewal.

        `steps()` and each next() must return within renewal_interval elapsed time;
        yield boundaries are the injected renewal drive contract. Blocking inference
        needs later runtime integration. Late steps stop this driver; publication
        independently checks the lease. It cannot interrupt a CPU step already running.
        """
        with self._lock:
            if self._driving:
                raise AuthorityError("one active work driver per session")
            result = self.begin(requirement, reference)
            if result.outcome != "success":
                return result
            self._usable()
            self._driving = True
        try:
            with self._lock:
                started = self._now().elapsed
            iterator = iter(steps())
            while True:
                with self._lock:
                    now = self._usable()
                    if now.elapsed - started > self.timing.renewal_interval:
                        raise AuthorityError("work step exceeded cooperative deadline")
                    if now.elapsed - self._last_renewal >= self.timing.renewal_interval:
                        renewal = self.renew()
                        if renewal.outcome != "success":
                            return renewal
                    started = self._now().elapsed
                try:
                    next(iterator)
                except StopIteration as finished:
                    with self._lock:
                        now = self._usable()
                        if now.elapsed - started > self.timing.renewal_interval:
                            raise AuthorityError("final work step exceeded cooperative deadline")
                    return finished.value
        finally:
            with self._lock:
                self._driving = False

    def _stage_and_publish(self, state):
        self._usable()
        created = self.snapshot_store.create(state, self.timing.request_deadline)
        if created.outcome != "success":
            # No automatic repeat of an uncertain snapshot create. Persisted begin
            # remains recoverable even if its exact remote candidate ID is unknown.
            if created.outcome == "unknown":
                self._pending = {"snapshot_create_unresolved": state["id"]}
            return created
        self._usable()
        record = deepcopy(self._observed.record)
        record["work"]["candidate"] = created.reference
        record["transition"] = self._association("stage", created.reference)
        staged = self._send(record, self._observed.generation)
        if staged.outcome != "success":
            return staged
        self._usable()
        record = deepcopy(self._observed.record)
        record.update(snapshot=created.reference, work=None,
                      transition=self._association("publish", created.reference,
                                                   state["publication"]["id"]))
        return self._send(record, self._observed.generation)

    def publish_result(self, *, outcome, reason=None, evidence=None):
        with self._lock:
            self._usable()
            work = self._observed.record["work"]
            if work is None:
                raise AuthorityError("no durable attempt")
            if work["attempt"]["session"] != self.session or work["attempt"]["owner"] != self.owner:
                raise AuthorityError("previous owner work requires explicit recovery")
            if work["candidate"] is not None:
                raise AuthorityError("staged result requires recovery, not replacement")
            require(outcome in ("success", "failed", "unknown"), "final outcome required")
            base = self._base()
            state = result_snapshot(base, work, outcome, reason, evidence, uuid4().hex,
                                    self._association("publish"))
            if outcome == "success":
                self.snapshot_store.verify_artifacts(evidence, self.timing.request_deadline)
            return self._stage_and_publish(state)

    def release(self):
        with self._lock:
            self._usable()
            record = deepcopy(self._observed.record)
            # Preserve active work verbatim; do not claim a clean/completed episode.
            record.update(owner=None, session=None, expires_at=None,
                          transition=self._association("release"))
            return self._send(record, self._observed.generation)

    def reconcile(self):
        """Bounded read-only observation. An old read never disproves delayed commit."""
        with self._lock:
            if self._pending is None:
                raise AuthorityError("no uncertain transition")
            for _ in range(self.timing.reconciliation_reads):
                try:
                    observed = self.authority_store.read(self.timing.request_deadline)
                except Exception:
                    continue
                if observed is not None and encode(observed.record) == encode(self._pending):
                    self._observed = observed
                    self._pending = None
                    try:
                        sample = self._usable()
                    except (AuthorityError, InvalidState):
                        return "confirmed_without_ownership"
                    self._last_renewal = sample.elapsed
                    return "confirmed_with_ownership"
            return "unresolved"

    def recover_work(self, *, adopt_candidate=False):
        """Legal owner's exact-reference recovery; never discover candidates by name.

        Failed/partial evidence is preserved. Adoption additionally rereads exact
        artifact IDs, applies Closure 1 schema recognition, and retains requirement
        intent. A result without a durable candidate reference stays unknown.
        """
        with self._lock:
            self._usable()
            work = self._observed.record["work"]
            if work is None:
                raise AuthorityError("no pending durable work")
            base = self._base()
            outcome, reason, evidence = "unknown", "executor_interrupted_result_unresolved", None
            if work["candidate"] is not None:
                candidate = self.snapshot_store.read(work["candidate"], self.timing.request_deadline)
                require(candidate["parent"] == work["base"], "recovery parent mismatch")
                attempt = candidate["attempts"].get(work["attempt"]["id"])
                require(attempt is not None, "recovery attempt missing")
                for key in work["attempt"]:
                    if key not in ("outcome", "reason", "evidence"):
                        require(attempt[key] == work["attempt"][key], "recovery work mismatch")
                outcome, reason, evidence = attempt["outcome"], attempt["reason"], attempt["evidence"]
                # Rebuild from current authoritative base; never adopt unrelated
                # changes from a candidate snapshot or overwrite historical records.
                if outcome == "success":
                    if adopt_candidate:
                        self.snapshot_store.verify_artifacts(evidence, self.timing.request_deadline)
                    else:
                        outcome, reason = "unknown", "candidate_awaits_explicit_adoption"
            elif adopt_candidate:
                raise AuthorityError("no exact durable candidate reference")
            state = result_snapshot(base, work, outcome, reason, evidence, uuid4().hex,
                                    self._association("publish"))
            return self._stage_and_publish(state)
