"""Idempotent installer; retry failed installs and refresh after a version update."""
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import sysconfig
import venv


RUNTIME_PROBE = (
    "import json,struct,sys,sysconfig; "
    "print(json.dumps({'version':list(sys.version_info[:3]),"
    "'implementation':sys.implementation.name,'bits':struct.calcsize('P')*8,"
    "'free_threaded':bool(sysconfig.get_config_var('Py_GIL_DISABLED'))}))"
)
IMPORT_PROBE = "import httpx; import lxml.etree; import pandas; import streamlit"


def runtime_details():
    return {"version": list(sys.version_info[:3]), "implementation": sys.implementation.name,
            "bits": struct.calcsize("P") * 8,
            "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED"))}


def runtime_error(details):
    version = tuple(details["version"][:2])
    if details["implementation"] != "cpython" or not (3, 11) <= version <= (3, 14):
        return "Gunakan CPython 3.11-3.14. Versi interpreter ini belum didukung."
    if details["bits"] != 64:
        return "Gunakan Python 64-bit; dependensi aplikasi memerlukan wheel 64-bit."
    if details["free_threaded"]:
        return "Gunakan Python biasa (dengan GIL), bukan build free-threaded."
    return None


def probe_runtime(python):
    result = subprocess.run([str(python), "-c", RUNTIME_PROBE], capture_output=True,
                            text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Python di .venv tidak bisa dijalankan. Instal ulang Python yang membuat lingkungan tersebut.")
    try:
        return json.loads(result.stdout)
    except (ValueError, TypeError):
        raise RuntimeError("Informasi versi Python di .venv tidak dapat dibaca.") from None


def imports_work(python):
    return subprocess.run([str(python), "-c", IMPORT_PROBE], capture_output=True,
                          text=True, timeout=60, check=False)


def prepare_environment(root):
    """Reuse incomplete environments; commit the marker only after verification."""
    environment = root / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        problem = runtime_error(runtime_details())
        if problem:
            print(problem)
            return None
        print("Menyiapkan lingkungan aplikasi...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    details = probe_runtime(python)
    problem = runtime_error(details)
    if problem:
        print(f"{problem} Lingkungan .venv memakai versi yang tercantum di bawah ini.")
        print("Python " + ".".join(map(str, details["version"])))
        return None
    print("Python " + ".".join(map(str, details["version"])) + " (64-bit)", flush=True)
    requirements = root / "requirements.txt"
    expected = hashlib.sha256(
        requirements.read_bytes() + (root / "constraints.txt").read_bytes()
        + json.dumps({"installer": 2, "runtime": details}, sort_keys=True).encode()
    ).hexdigest()
    marker = environment / ".requirements.sha256"
    installed_ok = marker.exists() and marker.read_text(encoding="ascii").strip() == expected
    if installed_ok:
        installed_ok = imports_work(python).returncode == 0
    if not installed_ok:
        # Never silently fall back to compiling pandas/numpy/other native code.
        installed = subprocess.run([str(python), "-m", "pip", "install", "--only-binary=:all:",
                                    "--disable-pip-version-check", "--no-input", "-r", str(requirements)],
                                   cwd=root, check=False)
        if installed.returncode:
            print("Instalasi dependensi belum selesai. Periksa pesan pip di atas (versi/wheel, koneksi, atau izin folder).")
            print("Launcher dapat dijalankan lagi untuk melanjutkan instalasi.")
            return None
        checked = subprocess.run([str(python), "-m", "pip", "check"], cwd=root, check=False)
        if checked.returncode:
            print("Dependensi belum konsisten; aplikasi belum dijalankan.")
            return None
        smoke = imports_work(python)
        if smoke.returncode:
            print("Paket terpasang tetapi belum dapat diimpor. Detail:")
            print((smoke.stderr or smoke.stdout)[-2000:])
            return None
        marker.write_text(expected, encoding="ascii")
    return python


def main():
    root = Path(__file__).resolve().parent
    try:
        python = prepare_environment(root)
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Persiapan aplikasi gagal: {exc}")
        return 1
    if python is None:
        return 1
    return subprocess.call([str(python), "-m", "streamlit", "run", str(root / "app.py"),
                            "--server.address=127.0.0.1", "--browser.gatherUsageStats=false"], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
