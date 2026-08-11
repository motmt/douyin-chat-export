"""Backfill self-recorded IM videos.

Flow:
  1. Playwright opens /chat once (loads session cookies + warms SDK).
  2. POST /aweme/v1/web/maya/story/batch_play_info/v1/ in page context
     to batch-resolve N tos_keys → signed CDN URLs (~10 vids per call).
  3. urllib downloads the encrypted bytes from each URL.
  4. extractor.cenc decrypts the MPEG-CENC AES-128-CTR samples in-place
     using cj.video.skey (16 bytes), giving a playable mp4.
  5. Save to data/media/videos/<msg_id>.mp4 and update DB.

No UI scrolling, no clicking, no Web Worker, no wasm. Just HTTPS + Python.
"""
import asyncio
import json
import os
import sqlite3
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from extractor.web_scraper import WebChatScraper, _conv_subdir
from extractor.models import get_db
from extractor.cenc import decrypt_cenc_mp4


VIDEOS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "media", "videos",
)

BATCH_SIZE = 10
BATCH_API_PATH = (
    "/aweme/v1/web/maya/story/batch_play_info/v1/"
    "?device_platform=webapp&aid=6383&channel=channel_pc_web"
    "&app_name=douyin_web&pc_client_type=1"
)


def _msg_video(msg) -> dict | None:
    """Returns cj.video dict if present (has tkey + skey)."""
    try:
        ro = json.loads(msg["raw_data"])
        cj = json.loads(ro.get("content_json", "{}"))
        v = cj.get("video") or {}
        if v.get("tkey") and v.get("skey"):
            return v
    except Exception:
        pass
    return None


def pending_videos(conn, conv_id=None, limit=None):
    """Video messages missing a working local mp4 (with cj.video.tkey present)."""
    sql = """SELECT msg_id, conv_id, timestamp, raw_data, media_local_path
             FROM messages
             WHERE (msg_type = 5 OR (raw_data LIKE '%tkey%' AND raw_data LIKE '%poster%'))
               AND (media_local_path IS NULL OR media_local_path = ''
                    OR media_local_path NOT LIKE '%.mp4')"""
    args = []
    if conv_id:
        sql += " AND conv_id = ?"
        args.append(conv_id)
    sql += " ORDER BY timestamp DESC"
    rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        if _msg_video(r):
            out.append(r)
            if limit and len(out) >= limit:
                break
    return out


def reset_local_paths(conn):
    """Clear media_local_path for all video messages so they re-enter pending."""
    cur = conn.execute(
        """UPDATE messages SET media_local_path = NULL
           WHERE media_local_path LIKE '%.mp4'"""
    )
    conn.commit()
    return cur.rowcount


def _download(url: str, timeout: int = 60) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


async def _resolve_batch(page, tkeys: list[str]) -> dict[str, str]:
    """Resolve a batch of tkeys → main_url. Returns {tkey: url}."""
    try:
        result = await asyncio.wait_for(page.evaluate(
            r"""async ({path, tkeys}) => {
                try {
                    const body = JSON.stringify({
                        req_infos: tkeys.map(k => ({ item_id: 0, tos_key: k, type: 2 })),
                        with_caption: true,
                    });
                    const r = await fetch(path, {
                        method: 'POST', credentials: 'include',
                        headers: { 'Content-Type': 'application/json' },
                        body,
                    });
                    return { status: r.status, body: await r.text() };
                } catch (e) { return { err: String(e) }; }
            }""", {"path": BATCH_API_PATH, "tkeys": tkeys},
        ), timeout=30)
    except asyncio.TimeoutError:
        return {}
    if result.get("err") or result.get("status") != 200:
        return {}
    try:
        payload = json.loads(result["body"])
    except Exception:
        return {}
    if payload.get("err_no") != 0:
        return {}
    out = {}
    infos = (payload.get("data") or {}).get("play_infos") or []
    for tkey, info in zip(tkeys, infos):
        eu = (info or {}).get("encrypted_url") or {}
        url = eu.get("main_url") or eu.get("backup_url")
        if url:
            out[tkey] = url
    return out


def _faststart_mp4(path: str) -> None:
    """Move moov to the front so HTML5 <video> can start playback without
    reading the whole file. ffmpeg remux only — no re-encode."""
    import shutil
    import subprocess
    if not shutil.which("ffmpeg"):
        return
    tmp = path + ".faststart.mp4"
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", path,
             "-c", "copy", "-movflags", "+faststart", "-f", "mp4", tmp],
            capture_output=True, timeout=60,
        )
        if proc.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            os.replace(tmp, path)
        else:
            try: os.remove(tmp)
            except OSError: pass
    except Exception:
        try: os.remove(tmp)
        except OSError: pass


def _process_one(enc_bytes: bytes, skey_hex: str, out_path: str) -> int:
    """Decrypt + save (with moov-at-front faststart). Returns size on disk."""
    if len(enc_bytes) < 1024 or enc_bytes[4:8] != b'ftyp':
        raise ValueError(f"bad mp4 ({len(enc_bytes)}B magic={enc_bytes[:8].hex()})")
    plain = decrypt_cenc_mp4(enc_bytes, skey_hex)
    with open(out_path, "wb") as f:
        f.write(plain)
    # In-place CENC decrypt preserves the unfaststart layout; remux to put moov first.
    _faststart_mp4(out_path)
    return os.path.getsize(out_path)


async def backfill(conv_id: str | None = None, limit: int | None = None,
                   batch_size: int = BATCH_SIZE, progress_cb=None) -> dict:
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    conn = get_db()

    pending = pending_videos(conn, conv_id=conv_id, limit=limit)
    log = [f"[*] {len(pending)} pending videos"]
    if not pending:
        return {"total": 0, "ok": 0, "fail": 0, "skipped": 0, "log": log}

    # Group by tkey (one tkey may map to multiple msg_ids if forwarded)
    by_tkey: dict[str, list[tuple[str, str, str]]] = {}  # tkey → [(msg_id, skey, conv_id)...]
    for m in pending:
        v = _msg_video(m)
        if not v:
            continue
        by_tkey.setdefault(v["tkey"], []).append((m["msg_id"], v["skey"], m["conv_id"]))

    log.append(f"[*] {len(by_tkey)} unique tkeys")

    ok = 0
    fail = 0
    skipped = 0
    total = len(pending)

    s = WebChatScraper()
    await s.launch()
    if not await s.wait_for_login():
        log.append("[-] login required")
        return {"total": total, "ok": 0, "fail": 0, "skipped": 0, "log": log}
    page = s.page

    log.append("[*] warming SDK at /chat")
    # 与主采集（web_scraper.CHAT_URL）保持一致：带 isPopup=1，避免弹窗/跳转差异
    await page.goto("https://www.douyin.com/chat?isPopup=1", wait_until="commit", timeout=30000)
    await asyncio.sleep(6)

    log.append(f"[*] resolving + decrypting in batches of {batch_size}")
    tkeys = list(by_tkey.keys())

    for batch_idx in range(0, len(tkeys), batch_size):
        batch = tkeys[batch_idx:batch_idx + batch_size]
        urls = await _resolve_batch(page, batch)
        log.append(f"  batch {batch_idx//batch_size + 1}: {len(batch)} req → {len(urls)} URL")

        for tkey in batch:
            url = urls.get(tkey)
            for msg_id, skey, msg_conv_id in by_tkey[tkey]:
                # 按会话分子目录: data/media/conv_<safe_id>/videos/<msg_id>.mp4
                conv_dir = _conv_subdir(msg_conv_id) if msg_conv_id else ""
                out_rel = os.path.join(conv_dir, "videos", f"{msg_id}.mp4").replace("\\", "/") if conv_dir \
                    else f"videos/{msg_id}.mp4"
                out_abs = os.path.join(VIDEOS_DIR, conv_dir, f"{msg_id}.mp4") if conv_dir \
                    else os.path.join(VIDEOS_DIR, f"{msg_id}.mp4")
                os.makedirs(os.path.dirname(out_abs), exist_ok=True)
                if os.path.exists(out_abs) and os.path.getsize(out_abs) > 0:
                    conn.execute("UPDATE messages SET media_local_path=? WHERE msg_id=?",
                                 (out_rel, msg_id))
                    conn.commit()
                    skipped += 1
                    continue
                if not url:
                    fail += 1
                    log.append(f"    [-] {msg_id}: no URL from resolver")
                    continue
                try:
                    enc = await asyncio.to_thread(_download, url, 60)
                    size = await asyncio.to_thread(_process_one, enc, skey, out_abs)
                    conn.execute("UPDATE messages SET media_local_path=? WHERE msg_id=?",
                                 (out_rel, msg_id))
                    conn.commit()
                    ok += 1
                    log.append(f"    [+] {msg_id} ({size//1024} KB)")
                except Exception as e:
                    fail += 1
                    log.append(f"    [-] {msg_id}: {e}")
                if progress_cb:
                    progress_cb({
                        "ok": ok, "fail": fail, "skipped": skipped,
                        "total": total, "current": msg_id,
                    })

    try:
        await asyncio.wait_for(s.close(), timeout=10)
    except Exception:
        pass

    log.append(f"[+] done: ok={ok} fail={fail} skipped={skipped}")
    return {"total": total, "ok": ok, "fail": fail,
            "skipped": skipped, "log": log}


async def main():
    """CLI:
      backfill all pending:  `python -m extractor.video_downloader`
      backfill N:            `python -m extractor.video_downloader N`
      reset DB+files:        `python -m extractor.video_downloader --reset`
    """
    if len(sys.argv) > 1 and sys.argv[1] == "--reset":
        conn = get_db()
        n = reset_local_paths(conn)
        print(f"[*] cleared media_local_path on {n} rows")
        if os.path.isdir(VIDEOS_DIR):
            files = [f for f in os.listdir(VIDEOS_DIR) if f.endswith(".mp4")]
            for f in files:
                try: os.unlink(os.path.join(VIDEOS_DIR, f))
                except OSError: pass
            print(f"[*] deleted {len(files)} mp4 files")
        return

    limit = None
    if len(sys.argv) > 1:
        try: limit = int(sys.argv[1])
        except ValueError: pass

    def cb(p):
        print(f"  → ok={p['ok']} fail={p['fail']} skipped={p['skipped']} / {p['total']}", flush=True)

    result = await backfill(limit=limit, progress_cb=cb)
    print("\n=== log (tail) ===")
    for line in result["log"][-15:]:
        print(line)
    print(f"\n=== summary ===")
    print(f"  total={result['total']} ok={result['ok']} fail={result['fail']} skipped={result['skipped']}")


if __name__ == "__main__":
    asyncio.run(main())
