# J1939 SPN and PGN Payload Analysis

## 1. Executive Correction

The original analysis used an 8-bit boundary. That boundary was incorrect for the intended question.

For **SAE J1939-21 over Classical CAN**, one physical CAN data frame can carry a maximum of **8 bytes (64 bits)**. The correct implementation question is therefore:

```text
Does the complete logical PGN payload fit in one 8-byte CAN frame?
```

The correct classification is:

```text
PGN payload <= 8 bytes  -> single-frame J1939 message
PGN payload > 8 bytes   -> multi-packet J1939 message using Transport Protocol
```

The bit length of an individual SPN remains important for packing and encoding, but it does not determine whether the PGN requires multiple CAN frames.

An SPN may be only 4 or 16 bits wide and still belong to a PGN whose logical payload is longer than 8 bytes. Conversely, several 16-bit and 32-bit SPNs can fit together in one 8-byte frame.

## 2. Standards Confirmation

### 2.1 Classical J1939

SAE J1939/21 defines the J1939 data link layer using the **Classical Extended Frame Format with 29-bit identifiers**. Classical CAN supports no more than 8 data bytes in one physical frame.

The Linux kernel J1939 implementation documents the resulting behavior explicitly:

- Buffers of 8 bytes or less are converted directly into one CAN frame without fragmentation.
- Buffers up to 1785 bytes use J1939 Transport Protocol.
- A Transport Protocol message is split into multiple 8-byte CAN frames.

Therefore, the statement "one Classical J1939 frame has a maximum payload of 8 bytes" is correct.

### 2.2 CAN FD exception

CAN FD increases the physical-frame payload from 8 bytes to as much as 64 bytes. SAE J1939-22 defines a J1939 data link layer using CAN FD.

This report assumes that the Pattern Generator targets **Classical CAN and SAE J1939-21**. If the project uses SAE J1939-22/CAN FD, the frame-capacity and transport logic must be redesigned around that protocol.

### 2.3 References

- [SAE J1939/21_202205 — Data Link Layer](https://saemobilus.sae.org/standards/j193921_202205-data-link-layer)
- [Linux kernel J1939 documentation](https://docs.kernel.org/networking/j1939.html)
- [SAE J1939-22 — CAN FD Data Link Layer](https://saemobilus.sae.org/standards/j1939-22_202107-fd-data-link-layer)
- [Bosch CAN protocol overview](https://www.bosch-semiconductors.com/products/ip-modules/can-ip-modules/can-ip-overview/canipprotocols.html)

## 3. Scope and Source Data

The analysis compares:

- `J1939_179_SPN_Master_Specification_Table.xlsx`
- `瑞麗 J1939 總數據集 20260702.xlsx`

The customer workbook contains 179 signal definitions. These map to:

- **177 unique SPN numbers**
- **60 unique PGNs**

The two duplicated SPN numbers are:

| SPN | Parameter | PGNs | Transmission rate |
|---:|---|---|---|
| 928 | Axle Location | `FEEA`, `F00B` | On request, 20 ms |
| 1807 | Steering Wheel Angle | `F009`, `F01D` | 10 ms for both |

Signal identity must therefore use `(PGN, SPN)`, not SPN alone.

## 4. Workbook Comparison Logic

### 4.1 Record matching

Customer records are matched to master records with:

```text
normalized PGN + normalized English parameter name
```

Normalization:

```text
normalize_pgn(value):
    convert to uppercase
    remove an optional "0x" prefix

normalize_name(value):
    trim leading and trailing whitespace
    collapse repeated whitespace
    compare without case sensitivity
```

Compared fields:

```text
Customer SPN               <-> Master SPN
Customer CAN ID            <-> Master PGN (Hex)
Customer Length            <-> Master Bits
Customer Transmission Rate <-> Master Tx Rate
```

### 4.2 Comparison result

| Check | Result |
|---|---:|
| Master records | 179 |
| Customer records | 179 |
| Matched records | 179 |
| Unmatched records | 0 |
| Ambiguous matches | 0 |
| Length mismatches | 0 |
| Transmission-rate mismatches | 0 |

The workbooks are internally consistent for the fields above.

## 5. Correct Payload Classification

### 5.1 PGN payload-length calculation

For each SPN:

```text
zero_based_start_bit = (Byte - 1) * 8 + (Bit - 1)
exclusive_end_bit = zero_based_start_bit + Bits
```

For each PGN:

```text
required_payload_bits = maximum exclusive_end_bit of all SPNs in that PGN
required_payload_bytes = ceiling(required_payload_bits / 8)
```

Classification for a Classical J1939 implementation:

```text
if required_payload_bytes <= 8:
    payload_class = SINGLE_FRAME
else:
    payload_class = TRANSPORT_PROTOCOL
```

For a partial signal table, this calculation gives the minimum payload length implied by the supplied positions. A complete SAE or customer PGN definition remains the final authority.

### 5.2 Dataset result

| PGN payload class | PGNs | SPN records | Physical-frame behavior |
|---|---:|---:|---|
| `<= 8 bytes` | 57 | 175 | One Classical CAN frame per PGN transmission |
| `> 8 bytes` | 3 | 4 | Multiple Classical CAN frames using J1939 TP |
| **Total** | **60** | **179** |  |

Every physical Classical CAN frame still carries no more than 8 data bytes. The `> 8 bytes` category describes a logical J1939 message that is segmented across multiple physical CAN frames.

### 5.3 PGNs longer than 8 bytes

| PGN | Included SPNs | Minimum or maximum logical payload | Approximate TP.DT packets | Transmission behavior |
|---|---|---:|---:|---|
| `FEE1` | 901, 556 | At least 18 bytes | 3 | On change greater than 10%, fallback every 5 s |
| `FEE3` | 544 | At least 21 bytes | 3 | On change greater than 10%, fallback every 5 s |
| `FEEC` | 237, VIN | Up to 200 bytes | 29 at maximum length | On request |

The TP.DT estimate uses seven transported-data bytes per data-transfer packet. Transport-control frames are additional.

All three logical messages are below the normal J1939 TP limit of 1785 bytes.

### 5.4 Why SPN width is not the frame-payload classification

The source contains these individual SPN widths:

| SPN width | Records |
|---:|---:|
| 2 bits | 49 |
| 3 bits | 3 |
| 4 bits | 10 |
| 5 bits | 1 |
| 8 bits | 62 |
| 16 bits | 43 |
| 32 bits | 10 |
| 1600 bits | 1 |

There are 178 SPNs whose individual value occupies no more than 8 bytes and one SPN, VIN, whose declared maximum length exceeds 8 bytes. This is useful signal metadata, but it must not replace PGN-level payload classification.

SPN 901 demonstrates the difference: it is only 4 bits wide, but it belongs to PGN `FEE1`, which extends through byte 18 and therefore requires Transport Protocol.

## 6. Transmission-Rate Normalization

Transmission Rate belongs to the PGN/message schedule. It should not create a separate physical transmission timer for every SPN.

Equivalent source labels are normalized as follows:

| Source labels | Normalized cycle |
|---|---:|
| `1 s`, `1s`, `1 sec` | 1000 ms |
| `0.5 s`, `500 ms` | 500 ms |
| `250 ms`, `250 msecs` | 250 ms |

For a fixed cycle:

```text
frequency_hz = 1000 / period_ms
```

Event-driven labels are preserved as transmission modes and are not converted into fixed cycles.

### 6.1 Fixed-cycle summary

| Cycle | Frequency | SPN records | PGNs | Payload class |
|---:|---:|---:|---:|---|
| 10 ms | 100 Hz | 9 | 4 | All single-frame |
| 20 ms | 50 Hz | 5 | 2 | All single-frame |
| 50 ms | 20 Hz | 15 | 2 | All single-frame |
| 100 ms | 10 Hz | 56 | 12 | All single-frame |
| 250 ms | 4 Hz | 6 | 2 | All single-frame |
| 330 ms | approximately 3.03 Hz | 1 | 1 | Single-frame |
| 500 ms | 2 Hz | 11 | 4 | All single-frame |
| 1000 ms | 1 Hz | 39 | 18 | All single-frame |
| **Fixed-cycle total** |  | **142** | **45** | **All single-frame** |

The remaining 37 SPN records belong to 15 PGNs whose transmission behavior is on-request, on-change, or engine-speed dependent. The three Transport Protocol PGNs are in this non-fixed group.

## 7. Complete SPN List by Transmission Rate

The following lists cover all 179 source records. `SPN/PGN` is used for duplicated SPN numbers. `[TP]` identifies an SPN whose containing PGN has a logical payload longer than 8 bytes.

### 7.1 Fixed-cycle SPNs

#### 10 ms, 100 Hz

1810, 574, 522, 5113, 5114, 1807/F009, 1808, 1809, 1807/F01D

#### 20 ms, 50 Hz

928/F00B, 1592, 1593, 1594, 1595

#### 50 ms, 20 Hz

1617, 1614, 1618, 1622, 1621, 1620, 1619, 558, 559, 1437, 91, 92, 29, 1623, 1624

#### 100 ms, 10 Hz

3948, 51, 1711, 1710, 524, 523, 900, 520, 1715, 1717, 905, 906, 907, 908, 909, 910, 563, 1121, 521, 1481, 2911, 3412, 3413, 3414, 3415, 3416, 3417, 70, 3807, 595, 596, 597, 598, 599, 600, 601, 602, 86, 976, 527, 1586, 1587, 1588, 1590, 1589, 1798, 1797, 244, 245, 183, 184, 185, 526, 1241, 904, 84

#### 250 ms, 4 Hz

514, 2978, 4952, 4953, 4962, 3236

#### 330 ms, approximately 3.03 Hz

5179

#### 500 ms, 2 Hz

102, 105, 106, 98, 100, 1127, 1128, 1129, 1130, 173, 3467

#### 1000 ms, 1 Hz

114, 115, 985, 977, 4076, 110, 174, 52, 4196, 96, 99, 38, 1856, 116, 117, 118, 1761, 3031, 108, 172, 1087, 1088, 930, 167, 168, 158, 917, 918, 242, 5053, 5054, 1639, 1637, 1135, 175, 169, 3532, 170, 171

### 7.2 Non-fixed-cycle SPNs

#### Engine-speed dependent

899, 512, 513, 1675, 2432, 190

The source does not define the engine-speed thresholds or the cycle selected at each threshold. The applicable SAE/OEM rule or customer requirement is needed.

#### On request

928/FEEA, 959, 960, 961, 963, 962, 964, 623, 624, 582, **237/FEEC [TP]**, 444, 182, 250, 235, 247, 1585, 1760

#### On request with a `100 ms` label

5108, 5109, 5110, 5111, 241

This label is ambiguous. It may mean either an on-request response with a 100 ms timing restriction or a 100 ms periodic transmission that also supports requests. The generator must keep this mode unresolved until the customer confirms the intended behavior.

#### On change greater than 10%, fallback every 5 seconds

**901/FEE1 [TP]**, **556/FEE1 [TP]**, **544/FEE3 [TP]**

#### On switch change, maximum period 1 second and maximum rate 10 Hz

2876, 2875

#### On lamp-state change, maximum period 1 second and maximum rate 10 Hz

2403, 2369, 2367

## 8. Correct Generator Architecture

### 8.1 Schedule PGNs, not individual SPNs

The scheduler should manage 60 PGN definitions rather than 179 independent transmissions.

```text
for each PGN definition:
    collect all SPNs assigned to the PGN
    calculate the logical PGN payload length
    assign one transmission mode to the PGN

    when the PGN becomes due:
        encode every SPN into the logical PGN buffer

        if logical payload length <= 8 bytes:
            transmit one Classical CAN frame
        else:
            transmit the logical message through J1939 TP
```

### 8.2 Suggested data model

```text
PgnDefinition:
    pgn
    priority
    source_address
    destination_address
    logical_payload_length_bytes
    frame_mode                 // SINGLE_FRAME or TRANSPORT_PROTOCOL
    transmission_mode
    period_ms
    maximum_period_ms
    minimum_interval_ms
    event_condition
    spns[]

SpnDefinition:
    pgn
    spn
    start_byte
    start_bit
    bit_length
    resolution
    offset
    physical_min
    physical_max
    unit
```

Recommended transmission modes:

```text
PERIODIC
ON_REQUEST
ON_CHANGE
ON_CHANGE_WITH_FALLBACK
ENGINE_SPEED_DEPENDENT
UNRESOLVED
```

### 8.3 Rate parsing

```text
if rate is a fixed millisecond or second value:
    transmission_mode = PERIODIC
    period_ms = normalized period

else if rate == "On request":
    transmission_mode = ON_REQUEST

else if rate contains "On change" and a fallback period:
    transmission_mode = ON_CHANGE_WITH_FALLBACK
    maximum_period_ms = fallback period

else if rate contains "On change":
    transmission_mode = ON_CHANGE

else if rate == "engine speed dependent":
    transmission_mode = ENGINE_SPEED_DEPENDENT

else:
    transmission_mode = UNRESOLVED
```

### 8.4 Transport selection

```text
if logical_payload_length_bytes <= 8:
    frame_mode = SINGLE_FRAME
else if logical_payload_length_bytes <= 1785:
    frame_mode = J1939_TP
else:
    reject or select an explicitly supported extended protocol
```

For Transport Protocol, the application must also decide whether the transfer uses broadcast or destination-specific connection management according to the PGN and network requirement. This is not determined by payload length alone.

## 9. Source-Data Issues

### 9.1 Position cells converted into dates

In the customer workbook, 50 `Position` cells representing byte ranges are stored as Excel date values. A position intended as `2-3`, for example, may load as a date equivalent to February 3, 2026.

The generator must not interpret the Excel date serial as a signal position.

Preferred handling:

```text
Use the separate integer Byte and Bit columns from the master workbook.
```

### 9.2 Decimal-looking byte and bit positions

Values such as `1.1`, `2.7`, and `7.3` represent `Byte.Bit`, not decimal numbers.

```text
1.1 = byte 1, bit 1
2.7 = byte 2, bit 7
7.3 = byte 7, bit 3
```

Use separate integer fields for byte and bit. Do not parse these values as floating-point positions.

### 9.3 SPN 242 position inconsistency

SPN 242, Tire Temperature, is 16 bits long. The customer workbook records Position as only byte `2`. The master definition starts at byte 2, bit 1, so the signal occupies bytes `2-3`.

The generator should use byte 2, bit 1, length 16. The customer workbook should be corrected or clarified.

### 9.4 Unresolved rate labels

The following labels do not provide enough information for a deterministic scheduler:

- `engine speed dependent`
- `On request (100ms)`

They should remain unresolved configuration states until the applicable rule is provided.

## 10. Validation Rules

Reject or flag a generated definition when:

```text
SPN number is missing
PGN is missing or invalid
bit length <= 0
start byte < 1
start bit is outside 1..8
fixed-cycle period <= 0
two SPNs overlap inside the same PGN without an explicit exception
a PGN longer than 8 bytes is configured as SINGLE_FRAME
a PGN of 8 bytes or less is unnecessarily configured as TP
an event-driven rate is treated as a fixed cycle without an approved rule
a duplicated SPN is keyed without PGN context
the selected CAN controller does not support extended 29-bit identifiers
```

Required consistency checks:

```text
1. Every customer row matches exactly one master row.
2. All SPNs in one PGN share compatible scheduling behavior.
3. PGN length is calculated from the highest occupied bit, not from the largest individual SPN.
4. Every Classical CAN physical frame contains no more than 8 data bytes.
5. Every logical message longer than 8 bytes uses J1939 TP.
6. Raw encoded values fit inside the configured SPN bit width.
7. Physical values follow range, resolution, offset, and unavailable-value rules.
8. The two duplicated SPN numbers are distinguished by PGN.
9. The three long PGNs are covered by transport-protocol tests.
```

## 11. Recommended Test Cases

### 11.1 Single-frame boundary

```text
Logical payload = 8 bytes:
    expect exactly one Classical CAN frame

Logical payload = 9 bytes:
    expect J1939 TP
```

### 11.2 Long PGNs in this dataset

```text
PGN FEE1:
    expect logical payload of at least 18 bytes
    expect TP mode

PGN FEE3:
    expect logical payload of at least 21 bytes
    expect TP mode

PGN FEEC:
    test short and maximum-length VIN values
    expect TP when the encoded VIN payload exceeds 8 bytes
```

### 11.3 Signal-width independence

```text
SPN 901 is only 4 bits wide:
    expect it to be packed into logical PGN FEE1
    expect PGN FEE1 to use TP because the complete PGN exceeds 8 bytes
```

### 11.4 Duplicate SPNs

```text
SPN 928/FEEA and SPN 928/F00B:
    expect different PGN definitions and transmission behavior

SPN 1807/F009 and SPN 1807/F01D:
    expect two independently addressable signal definitions
```

## 12. Recommended Implementation Sequence

1. Confirm that the target bus is Classical CAN/J1939-21 rather than CAN FD/J1939-22.
2. Import the master records and use its separate `Byte` and `Bit` fields.
3. Normalize PGN, SPN, bit length, and transmission-rate labels.
4. Use `(PGN, SPN)` as the signal key.
5. Group all SPNs by PGN.
6. Calculate logical PGN payload length from the highest occupied bit.
7. Classify each PGN as `SINGLE_FRAME` or `J1939_TP` using the 8-byte boundary.
8. Assign one scheduler rule to each PGN.
9. Encode every SPN into the logical PGN buffer before transmission.
10. Send one CAN frame for short PGNs or run TP segmentation for long PGNs.
11. Add automated tests for boundary lengths, duplicate SPNs, source-position parsing, and unresolved rate labels.

## 13. Final Result

For a Classical CAN/J1939-21 Pattern Generator, the corrected result is:

| Item | Result |
|---|---:|
| Source signal definitions | 179 |
| Unique SPN numbers | 177 |
| PGN definitions | 60 |
| PGNs fitting one 8-byte frame | 57 |
| SPN records in those PGNs | 175 |
| PGNs requiring J1939 TP | 3 |
| SPN records in those PGNs | 4 |
| Fixed-cycle PGNs | 45 |
| Request/event/dependent PGNs | 15 |

The original 8-bit classification should not be used to choose the transport method. The generator must classify the **complete logical PGN payload at the 8-byte boundary**, while using each SPN's bit length only for packing, scaling, validation, and extraction.

This analysis validates consistency between the supplied workbooks. It does not independently certify every resolution, offset, unit, range, unavailable-value encoding, or transmission rule against a licensed SAE J1939 Digital Annex.
