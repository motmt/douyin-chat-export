"""
Direct media export - bypass _download_all_media_parallel bug.

Issue: web_scraper._download_all_media_parallel() references column "server_id"
but the actual schema uses "msg_id". Don't waste time fixing upstream code -
just hit the CDN URLs directly from DB.

Usage:
    python export_media_direct.py [out_dir] [max_workers]

Default: out_dir = D:\\fjj_media_export, workers = 25
"""

import sqlite3
import urllib.request
import ssl
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

DB = r"D:\douyin-chat-export-complete\app\data\chat.db"
DEFAULT_OUT = r"D:\fjj_media_export"

# msg_type → subdir
TYPE_DIR = {
    2: "images",     # 图片
    3: "voice",      # 语音
    4: "videos",     # 视频/文件
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
REFERER = "https://www.douyin.com/"

# 会话 + 显示名
CONVS = [
    ("0:1:98880361157:7588998437715723322", "fjj"),
    ("0:1:98880361157:281069785987623",     "凤晶晶"),
]


def get_ext(url: str, msg_type: int) -> str:
    """根据 URL 和 msg_type 推断扩展名"""
    p = urlparse(url)
    path = p.path.lower()
    # 抖音 CDN 路径里通常带 webp/jpeg/png/mp4 等
    for ext in (".webp", ".jpeg", ".jpg", ".png", ".gif", ".mp4", ".m4a", ".mp3", ".aac"):
        if path.endswith(ext):
            return ext
    # 按 msg_type 兜底
    if msg_type == 2:
        return ".jpg"
    if msg_type == 3:
        return ".m4a"  # 语音
    if msg_type == 4:
        return ".mp4"
    # 从 query 找 format
    qs = p.query.lower()
    for k in ("format=", "mime="):
        if k in qs:
            for ext in (".webp", ".png", ".jpg", ".mp4", ".m4a"):
                if ext.lstrip(".") in qs:
                    return ext
    return ".bin"


def make_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


_CTX = make_ctx()
_LOCK = threading.Lock()
_STATS = {"ok": 0, "skip": 0, "fail": 0, "bytes": 0, "errors": []}


def fetch_one(url: str, dst: str):
    if os.path.exists(dst) and os.path.getsize(dst) > 0:
        with _LOCK:
            _STATS["skip"] += 1
        return "skip"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
    try:
        with urllib.request.urlopen(req, timeout=20, context=_CTX) as r:
            data = r.read()
        if len(data) < 50:
            raise RuntimeError(f"too small ({len(data)}B)")
        with open(dst, "wb") as f:
            f.write(data)
        with _LOCK:
            _STATS["ok"] += 1
            _STATS["bytes"] += len(data)
        return "ok"
    except Exception as e:
        with _LOCK:
            _STATS["fail"] += 1
            if len(_STATS["errors"]) < 30:
                _STATS["errors"].append(f"{url[:80]}: {e}")
        return "fail"


def main():
    out_dir = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    workers = int(sys.argv[2]) if len(sys.argv) > 2 else 25

    print(f"DB: {DB}")
    print(f"OUT: {out_dir}")
    print(f"WORKERS: {workers}")
    print()

    os.makedirs(out_dir, exist_ok=True)

    conn = sqlite3.connect(DB)
    tasks = []  # (url, dst)

    for conv_id, name in CONVS:
        # count per type
        per_type = {}
        for r in conn.execute(
            "SELECT msg_type, COUNT(*) FROM messages "
            "WHERE conv_id=? AND media_url IS NOT NULL GROUP BY msg_type",
            (conv_id,),
        ):
            per_type[r[0]] = r[1]
        print(f"[{name}] msg_type counts: {per_type}")

        # 按 (conv_short, type) 攒路径
        conv_short = name  # 用显示名当顶层子目录
        for msg_type, type_dir in TYPE_DIR.items():
            sub = os.path.join(out_dir, conv_short, type_dir)
            os.makedirs(sub, exist_ok=True)
            n = 0
            for row in conn.execute(
                "SELECT msg_id, media_url FROM messages "
                "WHERE conv_id=? AND msg_type=? AND media_url IS NOT NULL",
                (conv_id, msg_type),
            ):
                msg_id, url = row
                ext = get_ext(url, msg_type)
                dst = os.path.join(sub, f"{msg_id}{ext}")
                tasks.append((url, dst))
                n += 1
            print(f"  → {type_dir}: {n} files")

    total = len(tasks)
    print(f"\nTotal: {total} files")
    if total == 0:
        print("nothing to do")
        return

    start = time.time()
    completed = 0
    last_report = start
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch_one, u, d): (u, d) for u, d in tasks}
        for fut in as_completed(futs):
            completed += 1
            now = time.time()
            if now - last_report >= 2 or completed == total:
                elapsed = now - start
                rate = completed / max(elapsed, 0.01)
                eta = (total - completed) / max(rate, 0.01)
                mb = _STATS["bytes"] / 1024 / 1024
                print(
                    f"\r[{completed}/{total}] ok={_STATS['ok']} skip={_STATS['skip']} fail={_STATS['fail']} "
                    f"{mb:.1f}MB {rate:.1f}f/s ETA {eta:.0f}s    ",
                    end="", flush=True,
                )
                last_report = now

    elapsed = time.time() - start
    print()
    print()
    print("=" * 60)
    print(f"Done in {elapsed:.1f}s")
    print(f"  OK:    {_STATS['ok']}")
    print(f"  Skip:  {_STATS['skip']} (already exist)")
    print(f"  Fail:  {_STATS['fail']}")
    print(f"  Total: {_STATS['bytes']/1024/1024:.1f} MB downloaded")
    if _STATS["errors"]:
        print(f"\nFirst {min(10,len(_STATS['errors']))} errors:")
        for e in _STATS["errors"][:10]:
            print(f"  - {e}")


if __name__ == "__main__":
    main()
