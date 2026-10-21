# J1939 Control App - first vertical slice

This project implements the first milestone from the design document: send a proprietary
`SET_CONST` request for Engine Speed and read SPN 190 back from EEC1. It deliberately starts
with one signal and keeps the transport/backend separate from the UI.

## Current step status

| Step | Status | Evidence / next gate |
|---|---|---|
| 0. Environment audit | Implemented | The audit checks Python bitness, PySide6 and loads the installed `TSMaster.dll` native API. |
| 1. Manual CAN trace | User/hardware check required | Map the real adapter, set the STM32 bitrate and confirm extended frames in TSMaster Trace. |
| 2. Official API example | Reviewed | The backend follows `TSMaster/Demo/TSMaster_Message_Send_and_Receive.py` and its packed `TLIBCAN` FIFO API. |
| 3. Console receive/decode | Offline logic complete | EEC1 ID and 1500 rpm vectors are covered by tests; real FIFO input is not yet verified. |
| 4. Console send/ACK/readback | Offline simulation complete | Real bus request, application ACK and EEC1 readback still require firmware/hardware. |
| 5. One-signal UI | Implemented with mock and native TSMaster backends | Real acceptance still requires an STM32 ACK on the physical bus. |
| 6-8 | Not started | Only begin after the one-signal real-bus loop passes. |

## Environment setup

From this directory in PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\check_environment.py
```

For this checked workspace, `.venv` was bootstrapped with `--system-site-packages` so it can
reuse the already installed PySide6 6.11.0 without another download. To build a
fully isolated environment, delete/recreate it yourself and run the commands above.

This machine does not expose the `py` launcher, so the commands use `python`. A 64-bit Python
should be paired with the installed 64-bit TSMaster executable.

The backend loads the 64-bit native API installed at:

```text
C:\Users\AnhPhan\AppData\Local\Programs\TOSUN\TSMaster\bin64\TSMaster.dll
```

`pywin32`, COM registration and makepy are not used. The installed out-of-process COM server
returns corrupt pointer-like FIFO fields and does not expose the `OnRxCAN` connection point on
this machine. The native DLL uses fixed packed records and is the supported runtime path here.

## Step 1 - manual TSMaster/CAN check

1. Connect the USB-CAN adapter and verify its vendor driver.
2. Open the 64-bit TSMaster that matches Python's 64-bit architecture.
3. Create/select an application and map logical CAN channel 1 to the physical adapter channel.
4. Set the bitrate to the STM32 bus bitrate (the UI defaults to 250 kbit/s only as an example).
5. Start measurement and open CAN Trace.
6. Confirm extended frames from STM32, including EEC1 if firmware already transmits it.
7. Disable any TSMaster cyclic/RBS sender that uses the same CAN ID as STM32.

Save the mapping, stop measurement, and close the TSMaster desktop application before starting
the Python UI. Only one process should own the native TSMaster application/hardware session.

Record the adapter model, driver version, selected hardware channel, bitrate, STM32 source
address and an example trace frame before marking Step 1 complete.

## Step 2 - vendor native API reference

The implementation is based on the supplied vendor example:

```powershell
python ".\TSMaster\Demo\TSMaster_Message_Send_and_Receive.py"
```

Do not run that example unchanged against the real bus: it configures a different device and
transmits cyclic test frames. The UI only reuses its API contract and existing TSMaster mapping.

## Run and verify offline

```powershell
python -m unittest discover -s tests -v
python main.py --backend mock
```

In the UI, click **Connect**. The app automatically sends `TC1001`, followed by `GET_STATUS`.
The UI enables control only when protocol major `1`, schema `1`, and all required capabilities
are present. Protocol version, schema, capabilities and runtime state are shown in the UI.
Use **Retry STM32 Check** only if the first request times out. Leave Engine Speed at `1500`,
then click **Apply** after the state changes to **STM32 Ready**.
The mock backend must show this command sequence:

- TX `SET_CONST` on ID `0x18EF80F9`, payload `10 .. 67 00 E0 2E 00 00`;
- RX successful `SET_CONST` ACK `90 .. 00 00 E0 2E 00 00` from `0x18EFF980`;
- automatic TX `START` payload `03 .. 00 00 00 00 00 00`;
- RX successful `START` ACK `83 .. 00 00 00 00 00 00`;
- EEC1 ID `0x0CF00480` and readback `1500.000 rpm`;
- **Get Status** refreshes the running/configured state;
- **Stop** sends opcode `0x02`, stops EEC1 generation and changes readback to `Stopped`.

## Run against TSMaster

Only after Steps 1 and 2 pass:

```powershell
python main.py --backend tsmaster
```

The backend validates that the logical channel has an enabled hardware mapping, configures
Classic CAN, and requires every native API call to return success before reporting CAN connected.
RX uses `tsfifo_receive_can_msgs` with a packed `TLIBCAN`; TX uses
`tsapp_transmit_can_async`. TX echoes, standard CAN frames, remote frames and error frames are
never accepted as STM32 feedback. The logical channel must already be mapped to the intended
physical adapter in TSMaster.

The proprietary opcode contract (`TC1001=0x01`, `STOP=0x02`, `START=0x03`,
`GET_STATUS=0x04`, `SET_CONST=0x10`, signal key `103`) must be implemented in STM32 firmware.
`GET_STATUS` returns runtime flags in `detail` and packs protocol major/minor/schema/capabilities
into the 32-bit response value (bytes `major minor schema capability-mask`). The current contract
is `1.0`, schema `1`, capability mask `0x1F`.

Every request waits one second and is retried once with the exact same sequence and payload.
Firmware must keep a bounded dedup cache keyed by `(source address, opcode, sequence)` and replay
the cached ACK without repeating side effects. The mock uses 64 entries with a five-second TTL.
After a successful `SET_CONST` ACK, the UI
automatically sends `START`; EEC1 is accepted as readback only after independent RX from the
bus. A successful CAN transmit is not treated as an application ACK.
Physical values that fall halfway between two raw steps use decimal round-half-up; the UI itself
steps Engine Speed in exact 0.125 rpm increments.
# TC1001_UI_J1939
