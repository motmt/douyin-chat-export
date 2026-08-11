#!/usr/bin/env python3
"""Start the local FastAPI server without uvicorn reload helpers."""
import os
import subprocess
import sys
import traceback

sys.path.insert(0, os.path.dirname(__file__))


PIP_INDEX_URL = "https://mirrors.nju.edu.cn/pypi/web/simple"


def ensure_dependencies() -> None:
    try:
        import uvicorn  # noqa: F401
    except ModuleNotFoundError:
        print("[!] uvicorn missing; installing Python dependencies...")
        subprocess.check_call([
            sys.executable,
            "-m",
            "pip",
            "install",
            "-r",
            os.path.join(os.path.dirname(__file__), "requirements.txt"),
            "-i",
            PIP_INDEX_URL,
        ])


def main() -> int:
    app_dir = os.path.dirname(os.path.abspath(__file__))
    package_dir = os.path.dirname(app_dir)
    os.chdir(app_dir)
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.join(package_dir, "runtime", "ms-playwright"),
    )

    ensure_dependencies()
    from extractor.models import init_db

    init_db()
    print("[+] Database initialized")
    print("[+] Starting backend on http://127.0.0.1:8001")
    print("[+] Panel: http://127.0.0.1:8001/panel")
    print()

    env = os.environ.copy()
    env["PYTHONPATH"] = app_dir + os.pathsep + env.get("PYTHONPATH", "")

    code = subprocess.call([
        sys.executable,
        "-m",
        "uvicorn",
        "backend.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "8001",
    ], cwd=app_dir, env=env)
    if code != 0:
        print()
        print(f"[-] Server exited with code {code}")
        input("Press Enter to close...")
    return code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        input("\nPress Enter to close...")
        raise

