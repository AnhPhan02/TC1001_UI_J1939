from __future__ import annotations

from dataclasses import dataclass

from .protocol.j1939 import build_j1939_id


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    channel: int = 0
    bitrate_kbps: int = 250
    pc_address: int = 0xF9
    stm32_address: int = 0x80
    app_name: str = "J1939_Control_App"

    def __post_init__(self) -> None:
        if self.channel < 0:
            raise ValueError("CAN channel must be zero or greater")
        if self.bitrate_kbps <= 0:
            raise ValueError("Bitrate must be greater than zero")
        for name, value in (
            ("PC source address", self.pc_address),
            ("STM32 source address", self.stm32_address),
        ):
            if not 0 <= value <= 0xFF:
                raise ValueError(f"{name} must be in range 0x00..0xFF")

    @property
    def request_can_id(self) -> int:
        return build_j1939_id(
            priority=6,
            pgn=0xEF00,
            source_address=self.pc_address,
            destination_address=self.stm32_address,
        )

    @property
    def response_can_id(self) -> int:
        return build_j1939_id(
            priority=6,
            pgn=0xEF00,
            source_address=self.stm32_address,
            destination_address=self.pc_address,
        )
