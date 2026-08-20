#!/usr/bin/env python3
"""Start the local FastAPI server (in-process uvicorn.run).

设计要点（兼容嵌入式便携版 Python，如 runtime/python/python312.10-embed）：
- 嵌入式 Python 带 python312._pth → 强制 safe_path + 隔离模式：
  - 启动时 cwd 不进 sys.path
  - PYTHONPATH 环境变量被忽略
  因此"python -m uvicorn backend.main:app"子进程在嵌入版下必然
  ModuleNotFoundError: No module named 'backend'。
- 修复：不启动子进程，改为【同进程】uvicorn.run()，并在入口处
  用 sys.path.insert 把 app 目录加入 path（运行时 insert 对嵌入版同样有效，
  safe_path 只影响启动阶段的自动搜索）。
"""
import os
import subprocess
import sys
import traceback

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = os.path.dirname(APP_DIR)

# ── 必须在任何业务 import 之前把 app 目录塞进 sys.path ──
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)
# 项目根也加入（防 backend/common 依赖项目根的相对路径）
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

# 兼容嵌入式 Python：如果存在 _pth 文件，safe_path 会忽略 PYTHONPATH，
# 但运行时 sys.path.insert 依然生效（上面已做）。同时确保子进程（若有）
# 也能找到路径——本版本已无 uvicorn 子进程，故无此顾虑。

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
            os.path.join(APP_DIR, "requirements.txt"),
            "-i",
            PIP_INDEX_URL,
        ])


def main() -> int:
    # 支持 --host / --port 参数（Docker 需要 0.0.0.0 暴露）
    host = "127.0.0.1"
    port = 8001
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif argv[i] == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1

    os.chdir(APP_DIR)
    os.environ.setdefault(
        "PLAYWRIGHT_BROWSERS_PATH",
        os.path.join(PACKAGE_DIR, "runtime", "ms-playwright"),
    )

    ensure_dependencies()

    # 初始化数据库（依赖 extractor.models，app_dir 已在 sys.path）
    from extractor.models import init_db
    init_db()
    print("[+] Database initialized")
    print(f"[+] Starting backend on http://{host}:{port}")
    print("[+] Panel: http://127.0.0.1:8001/panel")
    print()

    # ── 同进程启动 uvicorn（不再 spawn 子进程）──
    # 嵌入版 Python 下子进程 `-m uvicorn backend.main:app` 会因 safe_path
    # 找不到 backend 模块；同进程 uvicorn.run 继承当前 sys.path，直接可用。
    import uvicorn
    try:
        uvicorn.run(
            "backend.main:app",
            host=host,
            port=port,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        print("\n[+] Server stopped.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        input("\nPress Enter to close...")
        raise
