# J1939 Control App - first vertical slice

This project implements the first milestone from the design document: send a proprietary
`SET_CONST` request for Engine Speed and read SPN 190 back from EEC1. It deliberately starts
with one signal and keeps the transport/backend separate from the UI.

## Current step status

| Step | Status | Evidence / next gate |
|---|---|---|
| 0. Environment audit | Partial | Python, PySide6, pywin32 and TSMaster 2026.6.26 files exist. COM ProgID is not registered. No connected CAN adapter was detected by the Windows device query. |
| 1. Manual CAN trace | User/hardware check required | Map the real adapter, set the STM32 bitrate and confirm extended frames in TSMaster Trace. |
| 2. Official COM example | Blocked by COM registration | Register COM, then run the vendor `OutOfProcess/Python/TestTSMaster.py` example. |
| 3. Console receive/decode | Offline logic complete | EEC1 ID and 1500 rpm vectors are covered by tests; real FIFO input is not yet verified. |
| 4. Console send/ACK/readback | Offline simulation complete | Real bus request, application ACK and EEC1 readback still require firmware/hardware. |
| 5. One-signal UI | Implemented with mock and COM backends | Real acceptance remains blocked by Steps 1-4. |
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
reuse the already installed PySide6 6.11.0 and pywin32 312 without another download. To build a
fully isolated environment, delete/recreate it yourself and run the commands above.

This machine does not expose the `py` launcher, so the commands use `python`. A 64-bit Python
should be paired with the installed 64-bit TSMaster executable.

The environment audit found TSMaster 64-bit at:

```text
C:\Users\AnhPhan\AppData\Local\Programs\TOSUN\TSMaster\bin64\TSMaster64.exe
```

but `TSMaster.TSApplication` is not registered. According to the supplied TSMaster COM guide,
run `TSMaster64.exe` once with **Run as administrator**, close it, and run the environment check
again. If the ProgID remains missing, reinstall TSMaster with administrative privileges. Do not
continue to the real COM test until the audit reports `READY`.

`TSMasterAPI.lib` is not required by this project's out-of-process COM backend. The official
sample and this backend use `pywin32` (`pythoncom` and `win32com.client`). Do not mix native
`TSMasterAPI` calls into the COM backend.

## Step 1 - manual TSMaster/CAN check

1. Connect the USB-CAN adapter and verify its vendor driver.
2. Open the 64-bit TSMaster that matches Python's 64-bit architecture.
3. Create/select an application and map logical CAN channel 1 to the physical adapter channel.
4. Set the bitrate to the STM32 bus bitrate (the UI defaults to 250 kbit/s only as an example).
5. Start measurement and open CAN Trace.
6. Confirm extended frames from STM32, including EEC1 if firmware already transmits it.
7. Disable any TSMaster cyclic/RBS sender that uses the same CAN ID as STM32.

Record the adapter model, driver version, selected hardware channel, bitrate, STM32 source
address and an example trace frame before marking Step 1 complete.

## Step 2 - vendor COM example

After COM registration succeeds, run the copied official example from the repository root:

```powershell
python ".\COM Automation\OutOfProcess\Python\TestTSMaster.py"
```

Review its constants first. The example currently selects a TOSUN virtual device and configures
two CAN FD channels at 500/2000 kbit/s; those settings are not the real project settings. Do not
run it unchanged against the vehicle bus. Adjust the adapter mapping and bitrate first.

## Run and verify offline

```powershell
python -m unittest discover -s tests -v
python main.py --backend mock
```

In the UI, click **Connect**, **Check STM32**, leave Engine Speed at `1500`, then click **Apply**.
The mock backend must show:

- TX ID `0x18EF80F9`, payload `10 .. 67 00 E0 2E 00 00`;
- a matched application ACK from `0x18EFF980`;
- EEC1 ID `0x0CF00480` and readback `1500.000 rpm`;
- `Stale` after one second without another EEC1 frame.

## Run against TSMaster

Only after Steps 1 and 2 pass:

```powershell
python main.py --backend tsmaster
```

The backend uses the documented COM calls `configure_baudrate_can`, `connect`,
`fifo_enable_receive_fifo`, `fifo_receive_can_msg` and `transmit_can_async`. It excludes TX
frames from FIFO reads so a local TX echo is not accepted as firmware feedback. The logical
channel must already be mapped to the intended physical adapter in TSMaster.

The proprietary opcode contract (`TC1001=0x01`, `SET_CONST=0x10`, signal key `103`) must be
confirmed in STM32 firmware. A successful CAN transmit is not treated as an application ACK.
Physical values that fall halfway between two raw steps use decimal round-half-up; the UI itself
steps Engine Speed in exact 0.125 rpm increments.
# TC1001_UI_J1939
