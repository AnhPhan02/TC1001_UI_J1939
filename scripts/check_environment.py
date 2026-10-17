from __future__ import annotations

import importlib.util
import json
import platform
import struct
import sys
import winreg
from pathlib import Path


TSMASTER_PROGID = r"TSMaster.TSApplication"
TSMASTER_PATHS = (
    Path.home() / "AppData/Local/Programs/TOSUN/TSMaster/bin64/TSMaster64.exe",
    Path.home() / "AppData/Local/Programs/TOSUN/TSMaster/bin/TSMaster.exe",
    Path(r"C:/Program Files (x86)/TOSUN/TSMaster/bin/TSMaster.exe"),
    Path(r"C:/Program Files/TOSUN/TSMaster/bin64/TSMaster64.exe"),
)


def module_status(module: str) -> dict[str, object]:
    spec = importlib.util.find_spec(module)
    return {"ok": spec is not None, "location": None if spec is None else spec.origin}


def progid_registered(progid: str) -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid):
            return True
    except FileNotFoundError:
        return False


def main() -> int:
    tsmaster_executables = [str(path) for path in TSMASTER_PATHS if path.exists()]
    tsmaster_com_registered = progid_registered(TSMASTER_PROGID)
    report = {
        "os": platform.platform(),
        "windows": sys.platform == "win32",
        "python": sys.version,
        "python_executable": sys.executable,
        "python_bits": struct.calcsize("P") * 8,
        "packages": {
            "PySide6": module_status("PySide6"),
            "win32com.client": module_status("win32com.client"),
            "pythoncom": module_status("pythoncom"),
        },
        "tsmaster_executables": tsmaster_executables,
        "tsmaster_com_progid": tsmaster_com_registered,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    required_ok = (
        report["windows"]
        and all(item["ok"] for item in report["packages"].values())
        and bool(tsmaster_executables)
        and tsmaster_com_registered
    )
    print("\nRESULT:", "READY" if required_ok else "NOT READY")
    if not tsmaster_com_registered:
        print(
            "FIX: Run TSMaster64.exe once as Administrator to register the COM type library, "
            "then run this check again. If still missing, reinstall TSMaster as Administrator."
        )
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
