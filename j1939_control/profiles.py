from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP


@dataclass(frozen=True, slots=True)
class ProfilePgn:
    name: str
    pgn: int
    period_ms: int
    priority: int
    wire_pgn: int | None = None
    destination_address: int | None = None

    @property
    def expected_fps(self) -> int:
        return 1000 // self.period_ms

    @property
    def receive_pgn(self) -> int:
        return self.pgn if self.wire_pgn is None else self.wire_pgn


@dataclass(frozen=True, slots=True)
class ProfileSignal:
    signal_key: int
    pgn_name: str
    spn: int
    name: str
    unit: str
    bit_length: int
    resolution: Decimal
    offset: Decimal
    minimum: Decimal
    maximum: Decimal
    value: Decimal = Decimal("0")

    @property
    def raw_value(self) -> int:
        return physical_to_raw(self, self.value)


def physical_to_raw(signal: ProfileSignal, value: Decimal | float | int | str) -> int:
    physical = value if isinstance(value, Decimal) else Decimal(str(value))
    if physical < signal.minimum or physical > signal.maximum:
        raise ValueError(
            f"{signal.name} must be within {signal.minimum}..{signal.maximum} {signal.unit}"
        )
    raw = int(
        ((physical - signal.offset) / signal.resolution).to_integral_value(
            rounding=ROUND_HALF_UP
        )
    )
    if not 0 <= raw < (1 << signal.bit_length):
        raise ValueError(
            f"{signal.name} raw value {raw} does not fit in {signal.bit_length} bits"
        )
    return raw


def _signal(
    key: int,
    pgn: str,
    spn: int,
    name: str,
    unit: str = "State",
    bits: int = 2,
    resolution: str = "1",
    offset: str = "0",
    minimum: str = "0",
    maximum: str = "3",
) -> ProfileSignal:
    return ProfileSignal(
        signal_key=key,
        pgn_name=pgn,
        spn=spn,
        name=name,
        unit=unit,
        bit_length=bits,
        resolution=Decimal(resolution),
        offset=Decimal(offset),
        minimum=Decimal(minimum),
        maximum=Decimal(maximum),
    )


PROFILE_40_NAME = "P3-A — 40 SPN / 11 PGN / 480 frame/s"

# P3-A intentionally overrides the older catalog periods for EBS11 and EAC2.
# EBS11 is a PDU1 identifier: 0x02C8 is the project label while received J1939
# frames normalize to PGN 0x0200 and carry the destination in the PS byte.
PROFILE_40_PGNS: tuple[ProfilePgn, ...] = (
    ProfilePgn("EBS11", 0x02C8, 10, 6, wire_pgn=0x0200, destination_address=0xFF),
    ProfilePgn("ETC1", 0xF002, 10, 3),
    ProfilePgn("VDC2", 0xF009, 10, 6),
    ProfilePgn("EAC2", 0xF00B, 20, 6),
    ProfilePgn("HRW", 0xFE6E, 20, 2),
    ProfilePgn("EEC2", 0xF003, 50, 3),
    ProfilePgn("TCO1", 0xFE6C, 50, 3),
    ProfilePgn("ERC1", 0xF000, 100, 6),
    ProfilePgn("EBC1", 0xF001, 100, 6),
    ProfilePgn("FLI2", 0xFE5B, 100, 6),
    ProfilePgn("PTODE", 0xFDA4, 100, 6),
)


PROFILE_40_SIGNALS: tuple[ProfileSignal, ...] = (
    _signal(113, "EBS11", 5113, "Vehicle ABS Active"),
    _signal(114, "EBS11", 5114, "Brake Light Switch"),
    _signal(74, "ETC1", 574, "Transmission Shift In Process"),
    _signal(75, "ETC1", 522, "Percent Clutch Slip", "%", 8, "0.4", maximum="100"),
    _signal(12, "VDC2", 1807, "Steering Wheel Angle", "deg", 16, "0.000976562", "-31.374", "-31.374", "31.374"),
    _signal(13, "VDC2", 1808, "Yaw Rate", "rad/s", 16, "0.00012207", "-3.92", "-3.92", "3.92"),
    _signal(14, "VDC2", 1809, "Lateral Acceleration", "m/s2", 16, "0.000488281", "-15.687", "-15.687", "15.687"),
    _signal(15, "VDC2", 1810, "Longitudinal Acceleration", "m/s2", 8, "0.1", "-12.5", "-12.5", "12.5"),
    _signal(83, "EAC2", 928, "Axle Location", bits=8, maximum="255"),
    _signal(59, "HRW", 1592, "Front Axle, Left Wheel Speed", "km/h", 16, "0.00390625", maximum="250.996"),
    _signal(60, "HRW", 1593, "Front Axle, Right Wheel Speed", "km/h", 16, "0.00390625", maximum="250.996"),
    _signal(61, "HRW", 1594, "Rear Axle, Left Wheel Speed", "km/h", 16, "0.00390625", maximum="250.996"),
    _signal(62, "HRW", 1595, "Rear Axle, Right Wheel Speed", "km/h", 16, "0.00390625", maximum="250.996"),
    _signal(94, "EEC2", 558, "Accelerator Pedal 1 Low Idle Switch"),
    _signal(95, "EEC2", 559, "Accelerator Pedal Kickdown Switch"),
    _signal(96, "EEC2", 1437, "Road Speed Limit Status"),
    _signal(97, "EEC2", 91, "Accelerator Pedal Position 1", "%", 8, "0.4", maximum="100"),
    _signal(98, "EEC2", 92, "Engine Percent Load At Current Speed", "%", 8, maximum="250"),
    _signal(99, "EEC2", 29, "Accelerator Pedal Position 2", "%", 8, "0.4", maximum="100"),
    _signal(24, "TCO1", 1617, "Driver 1 Time Related States", bits=4, maximum="15"),
    _signal(25, "TCO1", 1614, "Vehicle Overspeed"),
    _signal(26, "TCO1", 1618, "Driver 2 Time Related States", bits=4, maximum="15"),
    _signal(27, "TCO1", 1622, "System Event"),
    _signal(28, "TCO1", 1621, "Handling Information"),
    _signal(29, "TCO1", 1620, "Tachograph Performance"),
    _signal(30, "TCO1", 1619, "Direction Indicator"),
    _signal(31, "TCO1", 1623, "Tachograph Output Shaft Speed", "rpm", 16, "0.125", maximum="8031.875"),
    _signal(32, "TCO1", 1624, "Tachograph Vehicle Speed", "km/h", 16, "0.00390625", maximum="250.996"),
    _signal(84, "ERC1", 900, "Retarder Torque Mode", bits=4, maximum="15"),
    _signal(85, "ERC1", 520, "Actual Retarder Percent Torque", "%", 8, offset="-125", minimum="-125", maximum="125"),
    _signal(86, "ERC1", 1715, "Drivers Demand Retarder Percent Torque", "%", 8, offset="-125", minimum="-125", maximum="125"),
    _signal(87, "ERC1", 1717, "Actual Maximum Available Retarder Percent Torque", "%", 8, offset="-125", minimum="-125", maximum="125"),
    _signal(122, "EBC1", 563, "Anti-Lock Braking (ABS) Active"),
    _signal(123, "EBC1", 1121, "EBS Brake Switch"),
    _signal(124, "EBC1", 521, "Brake Pedal Position", "%", 8, "0.4", maximum="100"),
    _signal(125, "EBC1", 1481, "Source Address of Controlling Device", "SA", 8, maximum="255"),
    _signal(126, "EBC1", 2911, "Halt Brake Switch"),
    _signal(67, "FLI2", 1711, "Lane Tracking Status Right Side"),
    _signal(68, "FLI2", 1710, "Lane Tracking Status Left Side"),
    _signal(38, "PTODE", 3948, "At Least One PTO Engaged"),
)


if len(PROFILE_40_SIGNALS) != 40:  # pragma: no cover - import-time invariant
    raise RuntimeError("P3-A must contain exactly 40 signals")

if sum(pgn.expected_fps for pgn in PROFILE_40_PGNS) != 480:  # pragma: no cover
    raise RuntimeError("P3-A expected rate must be exactly 480 frame/s")
