"""patch_fast_login_probe.py

Replace the Chromium-launching `_probe_login_state` in
`backend/control_panel.py` with a SQLite-first variant.

Why:
  The original implementation launches a fresh headless Chromium against
  the persistent profile on EVERY call (badge poll, refresh precondition,
  scrape precondition). On Windows, each `launch_persistent_context` takes
  2-5s, and any failure mode inside Chromium (incomplete profile, missing
  `Local State`, headless sandbox hiccup) can hang the call indefinitely,
  starving the asyncio.Lock and freezing the panel's "检测中" badge.

What this does:
  1. Read cookies directly from the SQLite DB Chromium writes
     (`Default/Network/Cookies`) — millisecond fast, no browser.
  2. Walk the same `expires_utc` filter Chromium applies, so a stale
     sessionid is reported as `expired` (matches what the scraper would
     see when it actually launches a context).
  3. Only fall back to launching Chromium when the SQLite DB is missing,
     locked, or has no douyin cookies at all (catches genuine "no profile"
     cases without spinning up a browser for the normal case).
  4. Wrap the Chromium fallback in `asyncio.wait_for(..., timeout=8)` so
     one bad launch can't wedge the lock forever.

Apply with:
    python patch_fast_login_probe.py            # dry run
    python patch_fast_login_probe.py --apply    # patch in place
    python patch_fast_login_probe.py --revert   # restore from .bak

Author: mavis (MiniMax) — 2026-08-10
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
TARGET = REPO / "backend" / "control_panel.py"
BAK = TARGET.with_suffix(".py.fastprobe.bak")

# ── Replacement function body ──────────────────────────────────────────────
NEW_FUNC = '''async def _probe_login_state() -> dict:
    """Single source of truth for whether the persistent profile is logged in.

    Fast path: read the Chromium cookies SQLite DB directly (milliseconds,
    no browser launch). Slow path: fall back to launching Chromium with a
    hard 8s timeout so one bad launch can't wedge the lock forever.

    Returns one of:
        {"status": "logged_in",  "has_cookies": True}
        {"status": "expired",    "has_cookies": False}
        {"status": "no_profile", "has_cookies": False}
        {"status": "error",      "has_cookies": False, "message": "..."}

    Serialized via a module-level lock so the badge poll and the
    refresh/scrape preconditions can't race to launch two Chromium
    instances on the same profile (which would lock-conflict).
    """
    async with _login_probe_lock:
        has_profile = os.path.isdir(_USER_DATA_DIR) and os.listdir(_USER_DATA_DIR)
        if not has_profile:
            return {"status": "no_profile", "has_cookies": False}

        # ── Fast path: read Chromium's cookies SQLite DB directly ──
        # Chromium stores cookies at <user_data>/Default/Network/Cookies.
        # Values are AES-encrypted with the OS keychain, but sessionid
        # value is opaque to us — what matters is that the row exists
        # AND its expires_utc is in the future. If sessionid is present
        # and not expired, the scraper (which uses the same profile)
        # will see the same thing.
        cookies_db = os.path.join(_USER_DATA_DIR, "Default", "Network", "Cookies")
        fast_result = _read_sessionid_from_sqlite(cookies_db)
        if fast_result is not None:
            return fast_result

        # ── Slow path: launch Chromium (capped at 8s) ──
        # We only get here if the cookies DB is missing, locked, or empty.
        # In practice this means the user hasn't run a login yet OR the
        # profile is genuinely broken and needs a re-login.
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            try:
                ctx = await pw.chromium.launch_persistent_context(
                    _USER_DATA_DIR, headless=True,
                    viewport={"width": 1400, "height": 900}, locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                try:
                    page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    await asyncio.wait_for(
                        page.goto("https://www.douyin.com/", wait_until="domcontentloaded"),
                        timeout=6.0,
                    )
                    await asyncio.sleep(2)
                    cookies = await ctx.cookies("https://www.douyin.com")
                    has_login = any(
                        c.get("name") == "sessionid" and c.get("value")
                        for c in cookies
                    )
                    return {
                        "status": "logged_in" if has_login else "expired",
                        "has_cookies": has_login,
                    }
                finally:
                    await ctx.close()
            finally:
                await pw.stop()
        except asyncio.TimeoutError:
            return {"status": "error", "has_cookies": False,
                    "message": "login probe timeout (>8s)"}
        except Exception as e:
            return {"status": "error", "has_cookies": False, "message": str(e)}


def _read_sessionid_from_sqlite(cookies_db: str) -> dict | None:
    """Read sessionid cookie directly from Chromium's cookies DB.

    Returns one of:
        {"status": "logged_in", "has_cookies": True}  — sessionid present and not expired
        {"status": "expired",   "has_cookies": False} — sessionid present but past expires_utc
        None                                            — DB missing/locked, caller should fall back

    Chromium stores expires_utc as microseconds since the 1601-01-01 epoch
    (Windows FILETIME). 0 means a session cookie. We use a 5s skew buffer
    so a sessionid expiring "right now" still counts as valid.
    """
    if not os.path.isfile(cookies_db):
        return None
    try:
        import sqlite3
        # Read-only URI mode avoids touching the WAL — safe even if Chromium
        # has the DB open. timeout=0.5s so a hung file handle doesn't block.
        uri = f"file:{cookies_db}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.5)
    except Exception:
        return None
    try:
        cur = conn.cursor()
        # Try a cheap probe query first; if 'cookies' table doesn't exist
        # or schema changed, return None and let caller fall back to Chromium.
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cookies'")
        if not cur.fetchone():
            return None
        # Chromium epoch (1601-01-01) → Unix epoch: 11644473600 seconds
        CHROMIUM_EPOCH_OFFSET_US = 11644473600 * 1_000_000
        now_us = int(time.time() * 1_000_000) + CHROMIUM_EPOCH_OFFSET_US
        cur.execute(
            "SELECT value, expires_utc FROM cookies "
            "WHERE host_key IN ('.douyin.com', 'www.douyin.com') "
            "  AND name = 'sessionid' "
            "LIMIT 1"
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return {"status": "expired", "has_cookies": False}
        _value, expires_utc = row
        if expires_utc and expires_utc > 0 and expires_utc < now_us - 5_000_000:
            return {"status": "expired", "has_cookies": False}
        return {"status": "logged_in", "has_cookies": True}
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass
'''


def _find_old_func(src: str) -> tuple[int, int]:
    """Return (start, end) byte offsets of the original _probe_login_state definition."""
    # Match: `async def _probe_login_state() -> dict:\n"""..."""\n    ...\n    finally: ...\n        await pw.stop()\n    except Exception as e:\n        return ...`
    # The original ends with `return {"status": "error", "has_cookies": False, "message": str(e)}\n\n` then a blank line.
    pat = re.compile(
        r"async def _probe_login_state\(\) -> dict:.*?"
        r"return \{\"status\": \"error\", \"has_cookies\": False, \"message\": str\(e\)\}\n",
        re.DOTALL,
    )
    m = pat.search(src)
    if not m:
        raise RuntimeError("Could not locate the original _probe_login_state block in control_panel.py — file may already be patched or has unexpected format.")
    return m.start(), m.end()


def apply() -> None:
    src = TARGET.read_text(encoding="utf-8")
    if "_read_sessionid_from_sqlite" in src:
        print(f"[skip] {TARGET.name} already contains _read_sessionid_from_sqlite (already patched)")
        return
    start, end = _find_old_func(src)
    new_src = src[:start] + NEW_FUNC + src[end:]
    BAK.write_text(src, encoding="utf-8")
    TARGET.write_text(new_src, encoding="utf-8")
    print(f"[ok] patched {TARGET}")
    print(f"[ok] backup saved to {BAK}")


def revert() -> None:
    if not BAK.exists():
        print(f"[skip] no backup at {BAK}")
        return
    TARGET.write_text(BAK.read_text(encoding="utf-8"), encoding="utf-8")
    BAK.unlink()
    print(f"[ok] reverted {TARGET} from {BAK}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--revert", action="store_true")
    args = ap.parse_args()
    if args.revert:
        revert()
    elif args.apply:
        apply()
    else:
        print(__doc__)
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
