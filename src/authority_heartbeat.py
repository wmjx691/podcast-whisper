"""Renew-only blocking-work companion. The caller exclusively owns the session.

No foreground coordinator operation is allowed between start and join. stop is
only a signal; join is deliberately unbounded so no renewal thread can outlive
its caller. The injected coordinator/store must enforce request deadlines.
"""

import threading

from authority_coordinator import AuthorityError


class AuthorityHeartbeat:
    def __init__(self, coordinator, *, wait=None):
        self._coordinator = coordinator
        self._interval = coordinator.timing.renewal_interval
        self._wait = wait or (lambda event, interval: event.wait(interval))
        self._stop = threading.Event()
        self._thread = None
        self._joined = False
        self._unsafe = None
        self.renewal_count = 0

    @property
    def first_unsafe(self):
        return self._unsafe

    @property
    def authority_unsafe(self):
        return self._unsafe is not None

    @property
    def joined(self):
        return self._joined

    def start(self):
        if self._thread is not None:
            raise AuthorityError("heartbeat cannot restart")
        self._thread = threading.Thread(target=self._run, name="authority-heartbeat", daemon=False)
        self._thread.start()

    def _run(self):
        try:
            while not self._wait(self._stop, self._interval):
                if self._stop.is_set():
                    break
                result = self._coordinator.renew()
                self.renewal_count += 1
                if result.outcome != "success":
                    self._unsafe = result
                    break
        except BaseException as error:
            self._unsafe = error

    def stop(self):
        self._stop.set()

    def join(self):
        if self._thread is None:
            raise AuthorityError("heartbeat not started")
        self._thread.join()
        self._joined = True

    def assert_healthy(self):
        if not self._joined:
            raise AuthorityError("heartbeat must be stopped and joined first")
        if self.authority_unsafe:
            raise AuthorityError("heartbeat authority permanently unsafe: " + str(self._unsafe))
