#!/usr/bin/env python3
"""Main entry point: extract Douyin chat data via web version."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))


def _parse_args():
    """Parse CLI arguments."""
    args = {
        "mode": "extract",
        "name_filter": None,
        "incremental": "--incremental" in sys.argv,
        "download_images": "--download-images" in sys.argv,
        "output_format": "jsonl",
        "output_path": None,
    }

    if "--discover" in sys.argv:
        args["mode"] = "discover"
    elif "--list-conversations" in sys.argv:
        args["mode"] = "list_conversations"
    elif "--export" in sys.argv:
        args["mode"] = "export"

    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--filter" and i < len(sys.argv) - 1:
            args["name_filter"] = sys.argv[i + 1]
        elif arg == "--format" and i < len(sys.argv) - 1:
            args["output_format"] = sys.argv[i + 1]
        elif arg == "--output" and i < len(sys.argv) - 1:
            args["output_path"] = sys.argv[i + 1]

    return args


def run_export(args):
    """Export chat data to ChatLab format (no browser needed)."""
    from extractor.exporter import ChatLabExporter

    fmt = args["output_format"]
    ext = ".json" if fmt == "json" else ".jsonl"
    output_path = args["output_path"] or os.path.join("data", f"export{ext}")

    exporter = ChatLabExporter(
        conv_name=args["name_filter"],
        output_format=fmt,
    )
    exporter.export(output_path)


async def run():
    args = _parse_args()

    # Export mode: no browser needed
    if args["mode"] == "export":
        run_export(args)
        return 0

    from extractor.web_scraper import WebChatScraper

    scraper = WebChatScraper(
        discovery_mode=(args["mode"] == "discover"),
        name_filter=args["name_filter"],
        incremental=args["incremental"],
        download_images=args["download_images"],
    )

    try:
        await scraper.launch()
        logged_in = await scraper.wait_for_login()
        if not logged_in:
            print("[-] 未能登录，退出")
            return 2  # non-zero exit so the panel surfaces this as a failure

        if args["mode"] == "discover":
            duration = 60
            for arg in sys.argv[1:]:
                if arg.isdigit():
                    duration = int(arg)
            await scraper.run_discovery(duration=duration)
        elif args["mode"] == "list_conversations":
            convs = await scraper.list_conversations()
            out_path = os.path.join(
                os.path.dirname(__file__), "data", "conversations_list.json"
            )
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            import json as _json
            import time as _time
            payload = {
                "discovered_at": int(_time.time()),
                "items": [
                    {
                        "nickname": c.get("nickname", ""),
                        "name": c.get("name", ""),
                        "time": c.get("time", ""),
                        "preview": c.get("preview", ""),
                    }
                    for c in convs
                ],
            }
            with open(out_path, "w", encoding="utf-8") as f:
                _json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"[+] 会话列表已写入 {out_path}")
        else:
            await scraper.extract_all()

        return 0

    except KeyboardInterrupt:
        print("\n[*] 用户中断")
        return 130
    except Exception as e:
        print(f"\n[-] 错误: {e}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        await scraper.close()
        # 兜底清理：即使 Playwright 正常 close，也确保没有残留的 Chromium
        # 占用 browser_profile（否则下次采集/刷新会 profile 锁冲突卡死，
        # 用户每次都要手动 cleanpid）。
        _cleanup_orphan_chromium()


def _cleanup_orphan_chromium():
    """杀掉残留的 Chromium 子进程（本项目的 browser_profile 专用）。

    Playwright 启动的 Chromium 命令行带 `--user-data-dir=...browser_profile`，
    用 PowerShell CIM 按该特征精确匹配，只杀本项目残留的进程，
    绝不误伤用户自己开的 Chrome/Edge。
    """
    try:
        import subprocess
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -like '*browser_profile*' -and "
            "$_.Name -match 'chrom' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(asyncio.run(run()) or 0)
