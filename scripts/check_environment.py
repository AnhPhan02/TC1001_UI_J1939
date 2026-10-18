from __future__ import annotations

import importlib.util
import json
import platform
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from j1939_control.backend.tsmaster_native import (
    _configure_api,
    _find_tsmaster_dll,
    _load_tsmaster_dll,
)



def module_status(module: str) -> dict[str, object]:
    spec = importlib.util.find_spec(module)
    return {"ok": spec is not None, "location": None if spec is None else spec.origin}


def native_api_status() -> dict[str, object]:
    try:
        path = _find_tsmaster_dll()
        dll = _load_tsmaster_dll(path)
        _configure_api(dll)
        return {"ok": True, "location": str(path), "error": None}
    except Exception as exc:
        return {"ok": False, "location": None, "error": str(exc)}


def main() -> int:
    native_api = native_api_status()
    report = {
        "os": platform.platform(),
        "windows": sys.platform == "win32",
        "python": sys.version,
        "python_executable": sys.executable,
        "python_bits": struct.calcsize("P") * 8,
        "packages": {
            "PySide6": module_status("PySide6"),
        },
        "tsmaster_native_api": native_api,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    required_ok = (
        report["windows"]
        and all(item["ok"] for item in report["packages"].values())
        and native_api["ok"]
    )
    print("\nRESULT:", "READY" if required_ok else "NOT READY")
    if not native_api["ok"]:
        print("FIX: Repair/reinstall the 64-bit TSMaster native API. Detail:", native_api["error"])
    return 0 if required_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
