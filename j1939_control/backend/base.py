from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..config import ConnectionConfig


@dataclass(frozen=True, slots=True)
class CanFrame:
    channel: int
    can_id: int
    data: bytes
    timestamp_us: int = 0
    is_extended: bool = True
    is_tx: bool = False


class CanBackend(Protocol):
    def connect(self, config: ConnectionConfig) -> None: ...

    def disconnect(self) -> None: ...

    def send(self, frame: CanFrame) -> None: ...

    def receive(self) -> CanFrame | None: ...
