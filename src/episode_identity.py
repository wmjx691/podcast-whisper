"""Explicit, in-memory work references; no identity inference or persistence."""

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional
from uuid import uuid4
import re


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be explicit nonempty text")


@dataclass(frozen=True)
class EpisodeIdentity:
    feed_namespace: str
    source_guid: str

    def __post_init__(self):
        _require_text(self.feed_namespace, "feed_namespace")
        _require_text(self.source_guid, "source_guid")


@dataclass(frozen=True)
class EpisodeObservation:
    feed_namespace: str
    source_guid: Optional[str] = None
    title: str = ""
    enclosure_url: Optional[str] = None
    local_audio_path: Optional[Path] = None
    observation_id: str = field(default_factory=lambda: uuid4().hex, init=False)

    def __post_init__(self):
        _require_text(self.feed_namespace, "feed_namespace")
        if self.source_guid is not None:
            _require_text(self.source_guid, "source_guid")
        if self.local_audio_path is not None:
            object.__setattr__(self, "local_audio_path", Path(self.local_audio_path))

    @property
    def identity(self) -> Optional[EpisodeIdentity]:
        if self.source_guid is None:
            return None
        return EpisodeIdentity(self.feed_namespace, self.source_guid)

    @property
    def identity_status(self) -> str:
        return "identity_unknown" if self.identity is None else "identified"

    def with_audio_path(self, path) -> "EpisodeObservation":
        associated = replace(self, local_audio_path=Path(path))
        object.__setattr__(associated, "observation_id", self.observation_id)
        return associated


@dataclass(frozen=True)
class AttemptReference:
    observation: EpisodeObservation
    attempt_id: str

    def __post_init__(self):
        if not isinstance(self.observation, EpisodeObservation):
            raise TypeError("observation must be an EpisodeObservation")
        # An opaque caller reference that is also safe as one directory component.
        if not isinstance(self.attempt_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", self.attempt_id
        ):
            raise ValueError("attempt_id must be a safe, explicit directory component")
