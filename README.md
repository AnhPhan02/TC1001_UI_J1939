# J1939 Control App

A Windows/PySide6 desktop application that configures the fixed **P3-A — 40 SPN / 11 PGN / 480 frame/s** profile on an STM32 over Classic CAN and monitors the resulting traffic through TSMaster.

The active entry point now uses the 40-SPN batch UI. See [P3A_40_SPN_UI.md](P3A_40_SPN_UI.md) for the current workflow, reset limitation, RX-rate validation, and `.asc` recording. The older Engine Speed material later in this file is retained only as protocol-history/reference and is not the active UI flow.

> **Current status:** the offline batch flow and failure/retry paths are implemented and covered by 44 tests. Physical CAN acceptance still requires a reset STM32 and a saved trace from the target adapter.

## 0. Overview

### What the program does

1. Connects to the mock backend or a CAN adapter through TSMaster.
2. Verifies the STM32 with `TC1001`, then reads `GET_STATUS`.
3. Sends `STOP` and waits for its ACK.
4. Sends the 40 fixed `SET_CONST` commands one at a time, waiting for every ACK.
5. Sends one `START` only after ACK 40 succeeds.
6. Counts received frames by CAN ID in one-second windows for 11 expected PGNs.
7. Optionally records TX/RX traffic to a Vector ASCII `.asc` file.

A successful CAN transmit is **not** proof that the STM32 applied a command. The program separately reports transport success, a matching STM32 application ACK, and data confirmation from an independently received EEC1 frame.

### Architecture

```text
PySide6 MainWindow
  - connection settings, setpoint, readback, log
             |
             | Qt signals
             v
CanWorker in QThread
  - polling, ACK matching, timeout/retry, EEC1 decode
             |
             | CanBackend
        +----+------------------+
        |                       |
        v                       v
MockBackend              TSMasterBackend
offline STM32 model      ctypes + TSMaster.dll
                                |
                                v
                          USB-CAN -> STM32
```

| Area | Source |
|---|---|
| Entry point | [`main.py`](main.py) |
| UI | [`j1939_control/ui/main_window.py`](j1939_control/ui/main_window.py) |
| Worker/state flow | [`j1939_control/workers/can_worker.py`](j1939_control/workers/can_worker.py) |
| Command protocol | [`j1939_control/protocol/commands.py`](j1939_control/protocol/commands.py) |
| J1939/EEC1 codec | [`j1939_control/protocol/j1939.py`](j1939_control/protocol/j1939.py) |
| Native backend | [`j1939_control/backend/tsmaster_native.py`](j1939_control/backend/tsmaster_native.py) |
| Offline simulator | [`j1939_control/backend/mock.py`](j1939_control/backend/mock.py) |

### Requirements and setup

- Python 3.11 or newer and PySide6 6.11.0
- Windows, TSMaster, and a compatible CAN adapter for hardware mode
- Matching Python and `TSMaster.dll` architectures; this workspace is validated with 64-bit versions

From this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\check_environment.py
```

The environment check is required for hardware mode, not for mock mode. The backend locates `TSMaster.dll` through the TSMaster registry entry and common installation directories; the runtime path is not hard-coded.

Run the tests and offline UI:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe main.py --backend mock
```

Run with hardware after completing the TSMaster setup in section 2:

```powershell
.\.venv\Scripts\python.exe main.py --backend tsmaster
```

## 1. Data frames and firmware handshake

### CAN addressing

The control protocol uses J1939 Proprietary A, PGN `0xEF00`. Because it is a PDU1 PGN, the PDU Specific byte is the destination address.

| Direction | Priority | PGN | DA | SA | Default 29-bit CAN ID |
|---|---:|---:|---:|---:|---:|
| PC -> STM32 request | 6 | `0xEF00` | `0x80` | `0xF9` | `0x18EF80F9` |
| STM32 -> PC response | 6 | `0xEF00` | `0xF9` | `0x80` | `0x18EFF980` |
| STM32 EEC1 broadcast | 3 | `0xF004` | n/a | `0x80` | `0x0CF00480` |

The UI defaults to PC address `0xF9` and STM32 address `0x80`. Either address changes the control IDs; the STM32 address also changes the accepted EEC1 source address.

### Request payload

Every request is an 8-byte Classic CAN payload encoded little-endian as `<BBHI>`:

| Byte | Field | Size | Meaning |
|---:|---|---:|---|
| 0 | `opcode` | 1 | Command number |
| 1 | `sequence` | 1 | Request/response correlation ID; wraps after `0xFF` |
| 2-3 | `argument16` | 2 | Signal key or short argument |
| 4-7 | `argument32` | 4 | Raw value or long argument |

Commands used by the current UI:

| Command | Opcode | Arguments |
|---|---:|---|
| `TC1001` | `0x01` | zero |
| `STOP` | `0x02` | zero |
| `START` | `0x03` | zero |
| `GET_STATUS` | `0x04` | zero |
| `SET_CONST` | `0x10` | `argument16 = 103`; `argument32 = Engine Speed raw` |

`KEEPALIVE (0x05)`, `SET_PGN_PERIOD (0x11)`, and `TRIGGER_PGN (0x12)` are defined in the protocol enum but are not sent by the current UI.

### Response/ACK payload

Every response is 8 bytes encoded little-endian as `<BBBBI>`:

| Byte | Field | Meaning |
|---:|---|---|
| 0 | `opcode | 0x80` | Response bit and original opcode |
| 1 | `sequence` | Must match a pending request |
| 2 | `status` | `0` = success; non-zero = rejected |
| 3 | `detail` | Command-specific status/error detail |
| 4-7 | `value` | Command-specific result or echoed value |

The PC accepts a response only when it is an extended RX frame (not TX echo, remote, or error), PGN is `0xEF00`, source/destination addresses match, and both sequence and opcode match a pending request. Unmatched ACKs are ignored and logged.

### Automatic connection handshake

```text
PC                                             STM32
 |  TC1001, sequence N                           |
 |---------------------------------------------->|
 |  ACK 0x81, sequence N, status 0               |
 |<----------------------------------------------|
 |  GET_STATUS, sequence N+1                     |
 |---------------------------------------------->|
 |  ACK 0x84, protocol/runtime information       |
 |<----------------------------------------------|
 |  UI becomes "STM32 Ready" only if compatible  |
```

Compatibility requires protocol major `1`, schema `1`, and capability mask `0x1F`. Protocol minor is displayed but is not a compatibility gate.

In a successful `GET_STATUS` ACK, `detail` contains runtime flags and `value` contains protocol information:

| Location | Meaning |
|---|---|
| `detail` bit 0 | running |
| `detail` bit 1 | Engine Speed configured |
| `value` byte 0 | protocol major |
| `value` byte 1 | protocol minor |
| `value` byte 2 | schema version |
| `value` byte 3 | capability mask |

Capability bits 0-4 are `STOP`, `GET_STATUS`, `DEDUP_CACHE`, `SET_CONST`, and `START`. Example for protocol `1.0`, schema `1`, all capabilities, stopped, and no setpoint:

```text
84 <seq> 00 00 01 00 01 1F
```

### Applying Engine Speed

Project signal key `103` maps to J1939 SPN 190. It is a project-defined 1-based ID, not the SPN number and not the zero-based C-array index (`102`).

```text
raw = round-half-up(rpm / 0.125)
rpm = raw * 0.125
valid raw range = 0x0000..0xFAFF
UI range = 0.000..8031.875 rpm
```

Example for `1500 rpm` (`raw = 12000 = 0x00002EE0`):

```text
SET_CONST request
ID      0x18EF80F9
Data    10 03 67 00 E0 2E 00 00
        |  |  |-----| |-----------|
        |  |  key 103  raw 12000 LE
        |  sequence
        opcode

SET_CONST response
ID      0x18EFF980
Data    90 03 00 00 E0 2E 00 00
```

Only after that success ACK does the PC send `START`:

```text
START request   03 04 00 00 00 00 00 00
START response  83 04 00 00 00 00 00 00
```

The UI then shows `Waiting for EEC1`; the ACK itself does not populate readback.

### EEC1/SPN 190 readback

The STM32 must publish EEC1 (PGN `61444`, `0xF004`) as an extended frame. SPN 190 occupies payload bytes 4-5 in J1939's 1-based numbering, or offsets `3:5` in Python, little-endian.

```text
ID      0x0CF00480
Data    FF FF FF E0 2E FF FF FF
                  |---|
                  raw 0x2EE0 = 1500 rpm
```

Raw values above `0xFAFF` are displayed as `NA/error`. After one EEC1 has been received, one second without a new frame changes the readback state to `Stale`.

### Timeout, retry, and firmware deduplication

Each command waits one second for an ACK. On timeout, the PC retries exactly once with the **same sequence and identical payload**. A second timeout fails the command.

Firmware must keep a bounded response cache keyed by `(request source address, opcode, sequence)`:

- An identical duplicate replays the cached ACK without repeating side effects.
- The same key with a different payload is rejected as a sequence conflict.
- Entries remain valid longer than the PC retry window.

The mock uses 64 entries with a five-second TTL. Its rejection contract is:

| Status | Detail | Meaning |
|---:|---:|---|
| 1 | 1 | unknown opcode |
| 2 | 1 | unsupported `SET_CONST` signal key |
| 3 | 1 | `START` before Engine Speed is configured |
| 4 | 1 | same source/opcode/sequence with different payload |

## 2. UI project manual

### Offline workflow

1. Run `python main.py --backend mock`.
2. Leave **Backend** as **Mock (offline)** and click **Connect**.
3. Confirm the log shows `TC1001` followed by `GET_STATUS`.
4. Confirm **State** becomes **STM32 Ready** and protocol `1.0`, schema `1`, and all five capabilities appear.
5. Enter Engine Speed in 0.125 rpm steps, then click **Apply**.
6. Confirm the log shows `SET_CONST`, its ACK, `START`, its ACK, and EEC1.
7. Confirm **Readback** matches the setpoint and its state is `Fresh`.
8. Use **Get Status** to refresh runtime flags.
9. Click **Stop** and confirm readback changes to `Stopped`.

The mock emits one EEC1 when `START` succeeds. It verifies the command/decode path but does not simulate continuous bus timing.

### Controls and indicators

| Control | Description |
|---|---|
| Backend | `Mock (offline)` or `TSMaster Native API` |
| CAN channel | Zero-based logical TSMaster channel; default `0` |
| Bitrate | `125`, `250`, `500`, or `1000 kbit/s`; default `250` |
| PC source address | Hex without `0x`; default `F9` |
| STM32 address | Hex without `0x`; default `80` |
| Connect/Disconnect | Opens or closes the backend |
| Retry STM32 Check | Repeats `TC1001` after failed verification |
| Get Status | Refreshes protocol and runtime state |
| Set value | Engine Speed from `0.000` to `8031.875 rpm` |
| Apply | Sends `SET_CONST`, then `START` after its ACK |
| Stop | Sends `STOP`; enabled only after a compatible handshake |
| Readback state | `No data`, `Waiting for EEC1`, `Fresh`, `Stale`, `NA/error`, or `Stopped` |
| Log / Status | Timestamped TX, retry, RX, ACK, decode, and backend messages |

### Hardware/TSMaster workflow

1. Install the CAN adapter driver and matching TSMaster architecture.
2. Open TSMaster and create/select application `J1939_Control_App`.
3. Map the required logical CAN channel to the physical adapter channel and enable it.
4. Configure the actual STM32 bus bitrate; 250 kbit/s is only the UI default.
5. Start a manual measurement and confirm extended STM32 frames are visible.
6. Disable cyclic/RBS transmitters that use the same CAN IDs as the STM32.
7. Stop measurement and close TSMaster so only one process owns the session.
8. Run `scripts/check_environment.py` and confirm `RESULT: READY`.
9. Run `python main.py --backend tsmaster`, select the matching settings, and click **Connect**.

The native backend validates the logical-channel mapping, configures Classic CAN, connects, enables the receive FIFO, and checks every DLL return code. TX uses `tsapp_transmit_can_async`; RX uses `tsfifo_receive_can_msgs` with a packed 24-byte `TLIBCAN` record.

The legacy COM implementation remains for reference and regression tests, but active hardware traffic uses the native backend. COM registration and callbacks are not part of the runtime path.

### Troubleshooting

| Symptom | Check |
|---|---|
| `TSMaster.dll was not found` | Install/repair TSMaster; match Python/DLL architecture |
| No enabled hardware mapping | Map and enable the selected logical channel in TSMaster |
| Connect error/code 82 | Close other sessions; verify driver and adapter |
| `TC1001 timeout` | Firmware opcode/ID/address, bitrate, wiring, termination, bus state |
| `GET_STATUS` incompatible | Major `1`, schema `1`, capability mask `0x1F` |
| `SET_CONST` rejected | Key `103`, raw range, response detail, little-endian encoding |
| `START` never sent | `SET_CONST` ACK is absent, rejected, or mismatched |
| `Waiting for EEC1` persists | STM32 must publish EEC1 after the `START` ACK |
| Readback becomes `Stale` | EEC1 stopped or its period exceeds one second |
| Trace has frames but UI does not | Check extended RX, channel, STM32 SA, and PC DA |

## 3. Current state and old-version review

The old README described a first milestone with later steps “not started.” The source now implements the complete one-signal vertical slice, so those statements were obsolete.

| Area | Current state | Evidence or remaining gate |
|---|---|---|
| Environment audit | Implemented and passes | PySide6 and native `TSMaster.dll` load |
| Protocol/J1939 codecs | Implemented and tested | Golden vectors for IDs, commands, ACKs, SPN 190 |
| Offline STM32 simulation | Implemented and tested | Handshake, control, EEC1, STOP, retry, dedup |
| One-signal UI | Implemented | Compatibility gate, control, readback, freshness, log |
| Native transport | Implemented and unit tested | Mapping, packed ABI, TX, FIFO RX, filtering |
| Physical CAN acceptance | Pending | Save a full command/ACK/EEC1 trace from hardware |
| Multi-signal/waveform support | Not implemented | Current UI is Engine Speed constant mode only |

Validated flow:

```text
Connect -> TC1001 ACK -> GET_STATUS ACK -> STM32 Ready
        -> SET_CONST ACK -> START ACK -> EEC1 readback -> STOP ACK
```

Known limitations:

- No separate first-EEC1 timeout exists after a successful `START` ACK.
- Repeated RX errors are logged but do not force a degraded/disconnected UI state.
- Reconnect, bus-off, cable loss, and long-duration hardware behavior are not stress-tested.
- No physical-bus trace is committed to this repository.
- The control model is hard-coded to Engine Speed/key 103.

Before declaring hardware completion, record the adapter model, driver version, logical/physical channel mapping, bitrate, STM32 address, and a trace containing `TC1001`, `GET_STATUS`, `SET_CONST`, `START`, EEC1 with the expected SPN 190, and `STOP` with EEC1 cessation.
