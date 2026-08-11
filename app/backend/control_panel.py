"""Control panel for managing scraper, viewer, and export."""
import asyncio
import json
import os
import sys
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel

from backend import database
from common import config as _cfg, paths
from backend.panel import notify as _notify
from backend.panel.scheduler import parse_cron as _parse_cron, next_cron_run as _next_cron_run

control_router = APIRouter(prefix="/panel")

# The panel single-page app lives in backend/panel/static/panel.html and is
# loaded once at import; panel_page() serves it verbatim.
_PANEL_HTML_PATH = os.path.join(os.path.dirname(__file__), "panel", "static", "panel.html")
with open(_PANEL_HTML_PATH, encoding="utf-8") as _f:
    PANEL_HTML = _f.read()


# ── Persistent config (data/panel_config.json) — implemented in common.config ──
def _load_config():
    return _cfg.load_config()


def _save_config(cfg):
    _cfg.save_config(cfg)


# ── Scrape job state ──
_scrape_state = {
    "status": "idle",  # idle | running | completed | failed
    "started_at": None,
    "finished_at": None,
    "message": "",
    "process": None,
}

# ── Export state ──
_export_state = {
    "status": "idle",
    "file_path": None,
    "message": "",
}

# ── Scheduler state ──
_scheduler_state = {
    "enabled": False,
    "schedule": "",
    "task": None,
    "next_run": None,
}

# ── Conversation discovery (refresh conv list) state ──
_discover_state = {
    "status": "idle",  # idle | running | completed | failed
    "message": "",
    "process": None,
    "started_at": None,
    "finished_at": None,
}

# ── Media backfill state ──
_backfill_state = {
    "status": "idle",  # idle | running | completed | failed
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

_video_backfill_state = {
    "status": "idle",
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "skipped": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

# 语音专用补下载状态（用户要求独立按钮，与媒体补下载分开）
_voice_backfill_state = {
    "status": "idle",  # idle | running | completed | failed
    "total": 0,
    "done": 0,
    "ok": 0,
    "failed": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
}

LOG_PATH = paths.SCRAPE_LOG
DISCOVER_LOG_PATH = paths.DISCOVER_LOG
CONV_LIST_PATH = paths.CONVERSATIONS_LIST


async def restore_schedule_on_startup():
    """从 panel_config.json 恢复定时任务（容器重启后自动恢复）。"""
    cfg = _load_config()
    cron = cfg.get("schedule", "").strip()
    if not cron:
        return
    parsed = _parse_cron(cron)
    if not parsed:
        print(f"[scheduler] 配置中的 cron 表达式无效: {cron}", flush=True)
        return
    next_run = _next_cron_run(parsed)
    _scheduler_state["enabled"] = True
    _scheduler_state["schedule"] = cron
    _scheduler_state["next_run"] = next_run
    _scheduler_state["task"] = asyncio.create_task(
        _cron_loop(parsed, incremental=True)
    )
    from datetime import datetime
    next_str = datetime.fromtimestamp(next_run).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[scheduler] 已恢复定时任务: {cron}, 下次执行: {next_str}", flush=True)


class ScrapeRequest(BaseModel):
    incremental: bool = True
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class ExportRequest(BaseModel):
    format: str = "jsonl"
    filter: str = ""
    conversations: list[str] | None = None  # selected nicknames; overrides filter


class ScheduleRequest(BaseModel):
    enabled: bool
    cron: str = ""  # cron expression: "0 0 * * *" or shorthand
    incremental: bool = True
    conversations: list[str] | None = None  # selected nicknames for scheduled scrape


class CustomFilterAction(BaseModel):
    action: str  # "add" | "remove"
    value: str


class CookieImportRequest(BaseModel):
    cookies: str  # JSON array from DevTools or "key=value; key=value" string


class PasswordRequest(BaseModel):
    password: str = ""  # empty = remove password


class SelectedUpdate(BaseModel):
    section: str  # "scraper" | "export" | "schedule"
    conversations: list[str]


@control_router.post("/api/password")
async def set_password(req: PasswordRequest):
    import hashlib
    cfg = _load_config()
    if req.password:
        cfg["password_hash"] = hashlib.sha256(req.password.encode()).hexdigest()
        _save_config(cfg)
        return {"status": "ok", "message": "密码已设置"}
    else:
        cfg.pop("password_hash", None)
        _save_config(cfg)
        return {"status": "ok", "message": "密码已清除"}


@control_router.get("/api/password/status")
async def password_status():
    cfg = _load_config()
    return {"has_password": bool(cfg.get("password_hash"))}


# ── Notifications (Server酱 / sct.ftqq.com) ──

class NotifyKeyRequest(BaseModel):
    sendkey: str = ""  # empty = remove


# Server酱 notification helpers live in backend/panel/notify.py.
_send_serverchan_sync = _notify.send_serverchan_sync
_build_failure_desp = _notify.build_failure_desp
_notify_on_failure = _notify.notify_on_failure


@control_router.post("/api/notify/serverchan")
async def set_notify_key(req: NotifyKeyRequest):
    cfg = _load_config()
    key = req.sendkey.strip()
    if key:
        cfg["notify_serverchan_key"] = key
        _save_config(cfg)
        return {"status": "ok", "message": "SendKey 已保存"}
    cfg.pop("notify_serverchan_key", None)
    _save_config(cfg)
    return {"status": "ok", "message": "SendKey 已清除"}


@control_router.get("/api/notify/serverchan/status")
async def notify_status():
    cfg = _load_config()
    return {"has_key": bool(cfg.get("notify_serverchan_key"))}


@control_router.post("/api/notify/test")
async def notify_test():
    cfg = _load_config()
    sendkey = (cfg.get("notify_serverchan_key") or "").strip()
    if not sendkey:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "未配置 SendKey"},
        )
    ok, msg = await asyncio.to_thread(
        _send_serverchan_sync, sendkey,
        "抖音聊天导出 · 测试通知",
        "如果你收到这条消息，说明 Server酱 配置正常。",
    )
    return {"status": "ok" if ok else "error", "message": msg}


# ── Media download toggle + backfill ──

class DownloadImagesToggle(BaseModel):
    enabled: bool


@control_router.get("/api/config/download-images")
async def get_download_images():
    return {"enabled": bool(_load_config().get("download_images"))}


@control_router.post("/api/config/download-images")
async def set_download_images(req: DownloadImagesToggle):
    cfg = _load_config()
    cfg["download_images"] = bool(req.enabled)
    _save_config(cfg)
    return {"status": "ok", "enabled": cfg["download_images"]}


@control_router.get("/api/media/backfill/status")
async def backfill_status():
    return {
        "status": _backfill_state["status"],
        "total": _backfill_state["total"],
        "done": _backfill_state["done"],
        "ok": _backfill_state["ok"],
        "failed": _backfill_state["failed"],
        "message": _backfill_state["message"],
        "started_at": _backfill_state["started_at"],
        "finished_at": _backfill_state["finished_at"],
    }


@control_router.post("/api/media/backfill")
async def backfill_start():
    if _backfill_state["status"] == "running":
        return JSONResponse({"error": "Backfill already running"}, status_code=409)
    # Mark running synchronously before spawning so two rapid POSTs can't both
    # pass the 409 check (the coroutine sets it too, but that races).
    _backfill_state["status"] = "running"
    asyncio.create_task(_run_backfill())
    return {"status": "started"}


async def _run_backfill():
    """Download all historical image/emoji media that has a URL but no local file.

    - 表情 (msg_type=2): 直接下载 media_url
    - 图片 (msg_type=3): 从 raw_data 取 origin_url + skey，AES-GCM 解密后保存

    媒体按会话分子目录: data/media/conv_<safe_id>/{images,emoji}/（与采集一致）。
    """
    _backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "message": "扫描数据库...", "started_at": time.time(), "finished_at": None,
    })
    try:
        # Imports inside try: a failed import (e.g. playwright missing) must set
        # status='failed', not leave it stuck at 'running' (409 on every retry).
        from extractor.web_scraper import _save_emoji, _save_image, _save_voice, _conv_subdir
        from backend.database import get_db

        media_base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        os.makedirs(media_base, exist_ok=True)

        conn = get_db()
        # 媒体补下载范围: 表情(2) + 图片(3) + 语音(msg_type=0 且带 resource_url)。
        # 语音消息的 msg_type 是 0(other)，靠 raw_data 里的 resource_url+duration 识别。
        rows = conn.execute(
            """SELECT msg_id, conv_id, msg_type, media_url, raw_data FROM messages
               WHERE (msg_type IN (2, 3)
                      OR (msg_type = 0 AND raw_data LIKE '%resource_url%'))
                 AND (media_local_path IS NULL OR media_local_path = '')"""
        ).fetchall()
        _backfill_state["total"] = len(rows)
        _backfill_state["message"] = f"待下载 {len(rows)} 条"

        # 并发下载（Semaphore=10）：原串行 8000 条要十几小时，并发可提速 5-10 倍。
        # SQLite 连接不能跨线程共享，改为每个任务完成后由主协程统一落库。
        sem = asyncio.Semaphore(10)

        def _download_one(msg_id, conv_id, msg_type, url, raw):
            """线程池内执行下载（_save_emoji/_save_image/_save_voice 是同步函数）。"""
            conv_dir = _conv_subdir(conv_id) if conv_id else ""
            img_dir = os.path.join(media_base, conv_dir, "images")
            emoji_dir = os.path.join(media_base, conv_dir, "emoji")
            voice_dir = os.path.join(media_base, conv_dir, "voice")
            os.makedirs(img_dir, exist_ok=True)
            os.makedirs(emoji_dir, exist_ok=True)
            rel = None
            if msg_type == 2:
                if url:
                    r = _save_emoji(url, emoji_dir)
                    if r:
                        rel = os.path.join(conv_dir, r).replace("\\", "/") if conv_dir else r
            elif msg_type == 3:
                try:
                    data = json.loads(raw) if raw else {}
                    cj = json.loads(data.get("content_json") or "{}")
                except Exception:
                    cj = {}
                ru = cj.get("resource_url") or {}
                skey = ru.get("skey")
                origin = (ru.get("origin_url_list") or [None])[0]
                if skey and origin:
                    r = _save_image(origin, skey, str(msg_id), img_dir)
                    if r:
                        rel = os.path.join(conv_dir, r).replace("\\", "/") if conv_dir else r
            elif msg_type == 0:
                # 语音: raw_data.content_json.resource_url.url_list[0]
                try:
                    data = json.loads(raw) if raw else {}
                    cj = json.loads(data.get("content_json") or "{}")
                except Exception:
                    cj = {}
                ru = cj.get("resource_url") or {}
                ul = ru.get("url_list") or []
                if ul and isinstance(ul[0], str):
                    r = _save_voice(ul[0], voice_dir, str(msg_id))
                    if r:
                        rel = os.path.join(conv_dir, r).replace("\\", "/") if conv_dir else r
            return msg_id, rel

        async def _task(item):
            async with sem:
                return await asyncio.to_thread(_download_one, *item)

        # 分批并发：每批 50 个，批内并发，批间更新状态 + 落库
        pending = rows
        for start in range(0, len(pending), 50):
            batch = pending[start:start + 50]
            results = await asyncio.gather(*(_task(it) for it in batch), return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    _backfill_state["failed"] += 1
                    _backfill_state["done"] += 1
                    continue
                msg_id, rel = res
                if rel:
                    try:
                        conn.execute(
                            "UPDATE messages SET media_local_path = ? WHERE msg_id = ?",
                            (rel, msg_id),
                        )
                        conn.commit()
                        _backfill_state["ok"] += 1
                    except Exception:
                        _backfill_state["failed"] += 1
                else:
                    _backfill_state["failed"] += 1
                _backfill_state["done"] += 1
            _backfill_state["message"] = (
                f"下载中 {_backfill_state['done']}/{len(pending)} "
                f"(成功 {_backfill_state['ok']}，失败 {_backfill_state['failed']})"
            )
        conn.close()

        _backfill_state["status"] = "completed"
        _backfill_state["message"] = f"完成: 成功 {_backfill_state['ok']}，失败 {_backfill_state['failed']}"
    except Exception as e:
        _backfill_state["status"] = "failed"
        _backfill_state["message"] = f"错误: {e}"
    finally:
        _backfill_state["finished_at"] = time.time()


# ── 语音回填：独立按钮，只下载语音消息 ──

@control_router.get("/api/media/voice/backfill/status")
async def voice_backfill_status():
    return dict(_voice_backfill_state)


@control_router.post("/api/media/voice/backfill")
async def voice_backfill_start():
    if _voice_backfill_state["status"] == "running":
        return JSONResponse({"error": "Voice backfill already running"}, status_code=409)
    _voice_backfill_state["status"] = "running"
    asyncio.create_task(_run_voice_backfill())
    return {"status": "started"}


async def _run_voice_backfill():
    """只下载语音消息（msg_type=0 且带 resource_url）到各会话 voice/ 子目录。

    与媒体补下载分开，方便只补语音时不用重下图片/表情。
    并发下载，同 media backfill 的线程池方案。
    """
    _voice_backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "message": "扫描数据库...", "started_at": time.time(), "finished_at": None,
    })
    try:
        from extractor.web_scraper import _save_voice, _conv_subdir
        from backend.database import get_db

        media_base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        os.makedirs(media_base, exist_ok=True)

        conn = get_db()
        rows = conn.execute(
            """SELECT msg_id, conv_id, raw_data FROM messages
               WHERE msg_type = 0 AND raw_data LIKE '%resource_url%'
                 AND (media_local_path IS NULL OR media_local_path = '')"""
        ).fetchall()
        _voice_backfill_state["total"] = len(rows)
        _voice_backfill_state["message"] = f"待下载 {len(rows)} 条语音"

        sem = asyncio.Semaphore(10)

        def _download_one(msg_id, conv_id, raw):
            """线程池内执行下载（_save_voice 是同步函数）。"""
            try:
                data = json.loads(raw) if raw else {}
                cj = json.loads(data.get("content_json") or "{}")
            except Exception:
                cj = {}
            ul = (cj.get("resource_url") or {}).get("url_list") or []
            if not ul or not isinstance(ul[0], str):
                return msg_id, None
            conv_dir = _conv_subdir(conv_id) if conv_id else ""
            voice_dir = os.path.join(media_base, conv_dir, "voice")
            r = _save_voice(ul[0], voice_dir, str(msg_id))
            if r:
                rel = os.path.join(conv_dir, r).replace("\\", "/") if conv_dir else r
                return msg_id, rel
            return msg_id, None

        async def _task(item):
            async with sem:
                return await asyncio.to_thread(_download_one, *item)

        pending = rows
        for start in range(0, len(pending), 50):
            batch = pending[start:start + 50]
            results = await asyncio.gather(*(_task(it) for it in batch), return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    _voice_backfill_state["failed"] += 1
                    _voice_backfill_state["done"] += 1
                    continue
                msg_id, rel = res
                if rel:
                    try:
                        conn.execute(
                            "UPDATE messages SET media_local_path = ? WHERE msg_id = ?",
                            (rel, msg_id),
                        )
                        conn.commit()
                        _voice_backfill_state["ok"] += 1
                    except Exception:
                        _voice_backfill_state["failed"] += 1
                else:
                    _voice_backfill_state["failed"] += 1
                _voice_backfill_state["done"] += 1
            _voice_backfill_state["message"] = (
                f"下载中 {_voice_backfill_state['done']}/{len(pending)} "
                f"(成功 {_voice_backfill_state['ok']}，失败 {_voice_backfill_state['failed']})"
            )
        conn.close()

        _voice_backfill_state["status"] = "completed"
        _voice_backfill_state["message"] = (
            f"完成: 成功 {_voice_backfill_state['ok']}，失败 {_voice_backfill_state['failed']}"
        )
    except Exception as e:
        _voice_backfill_state["status"] = "failed"
        _voice_backfill_state["message"] = f"错误: {e}"
    finally:
        _voice_backfill_state["finished_at"] = time.time()


# ── 视频回填：调 batch_play_info 解析签名 URL 后落地 mp4 ──

@control_router.get("/api/media/videos/status")
async def video_backfill_status():
    return {
        "status": _video_backfill_state["status"],
        "total": _video_backfill_state["total"],
        "done": _video_backfill_state["done"],
        "ok": _video_backfill_state["ok"],
        "failed": _video_backfill_state["failed"],
        "skipped": _video_backfill_state["skipped"],
        "message": _video_backfill_state["message"],
        "started_at": _video_backfill_state["started_at"],
        "finished_at": _video_backfill_state["finished_at"],
    }


@control_router.get("/api/media/videos/pending")
async def video_backfill_pending():
    # Reuse the same Python filter as the backfill itself so the count matches
    # what will actually be processed (excludes text replies that quote a video).
    from extractor.video_downloader import pending_videos
    from backend.database import get_db
    conn = get_db()
    rows = pending_videos(conn)
    conn.close()
    return {"pending": len(rows)}


@control_router.post("/api/media/videos/backfill")
async def video_backfill_start():
    if _video_backfill_state["status"] == "running":
        return JSONResponse({"error": "video backfill already running"}, status_code=409)
    # Mark running synchronously before spawning to avoid the check-then-act race.
    _video_backfill_state["status"] = "running"
    asyncio.create_task(_run_video_backfill())
    return {"status": "started"}


async def _run_video_backfill():
    _video_backfill_state.update({
        "status": "running", "total": 0, "done": 0, "ok": 0, "failed": 0,
        "skipped": 0, "message": "启动浏览器解析视频 URL...",
        "started_at": time.time(), "finished_at": None,
    })

    def _cb(p):
        _video_backfill_state["total"] = p.get("total", _video_backfill_state["total"])
        _video_backfill_state["ok"] = p.get("ok", 0)
        _video_backfill_state["failed"] = p.get("fail", 0)
        _video_backfill_state["skipped"] = p.get("skipped", 0)
        _video_backfill_state["done"] = (
            _video_backfill_state["ok"] + _video_backfill_state["failed"] + _video_backfill_state["skipped"]
        )
        cur = p.get("current", "")
        _video_backfill_state["message"] = (
            f"已下载 {_video_backfill_state['ok']}，失败 {_video_backfill_state['failed']}，"
            f"跳过 {_video_backfill_state['skipped']} / {_video_backfill_state['total']}（{cur[-12:] if cur else ''}）"
        )

    try:
        # Import inside try so a failed import sets status='failed', not stuck 'running'.
        from extractor.video_downloader import backfill as run_backfill
        result = await run_backfill(progress_cb=_cb)
        _video_backfill_state["status"] = "completed"
        _video_backfill_state["message"] = (
            f"完成：成功 {result['ok']}，失败 {result['fail']}，跳过 {result['skipped']} / {result['total']}"
        )
    except Exception as e:
        _video_backfill_state["status"] = "failed"
        _video_backfill_state["message"] = f"错误: {e}"
    finally:
        _video_backfill_state["finished_at"] = time.time()


@control_router.get("", response_class=HTMLResponse)
@control_router.get("/", response_class=HTMLResponse)
async def panel_page():
    return PANEL_HTML


@control_router.get("/api/status")
async def panel_status():
    stats = database.get_stats()
    from backend.database import get_db
    conn = get_db()
    row = conn.execute("SELECT MAX(last_message_time) FROM conversations").fetchone()
    last_time = row[0] if row and row[0] else 0
    convs = conn.execute("SELECT name FROM conversations ORDER BY last_message_time DESC").fetchall()
    conn.close()

    cfg = _load_config()

    return {
        "conversations": stats["conversations"],
        "messages": stats["messages"],
        "users": stats["users"],
        "last_message_time": last_time,
        "conversation_names": [c[0] for c in convs if c[0]],
        "custom_filters": cfg.get("custom_filters", []),
        "scrape": {
            "status": _scrape_state["status"],
            "started_at": _scrape_state["started_at"],
            "finished_at": _scrape_state["finished_at"],
            "message": _scrape_state["message"],
        },
        "export": {
            "status": _export_state["status"],
            "file_path": _export_state["file_path"],
            "message": _export_state["message"],
        },
        "scheduler": {
            "enabled": _scheduler_state["enabled"],
            "schedule": _scheduler_state["schedule"],
            "next_run": _scheduler_state["next_run"],
        },
    }


@control_router.post("/api/scrape")
async def start_scrape(req: ScrapeRequest):
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scrape already running"}, status_code=409)

    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    # Selected conversations (checkbox list) take precedence over free-text filter
    effective_filter = ",".join(req.conversations) if req.conversations else req.filter

    cmd = [sys.executable, "-u", "extract.py"]
    if req.incremental:
        cmd.append("--incremental")
    if effective_filter:
        cmd.extend(["--filter", effective_filter])
    if _load_config().get("download_images"):
        cmd.append("--download-images")

    _scrape_state["status"] = "running"
    _scrape_state["started_at"] = time.time()
    _scrape_state["finished_at"] = None
    _scrape_state["message"] = f"{'增量' if req.incremental else '全量'}采集"
    if req.conversations:
        _scrape_state["message"] += f" ({len(req.conversations)} 个会话)"
    elif req.filter:
        _scrape_state["message"] += f" (过滤: {req.filter})"

    # Persist selection so it's remembered next time
    if req.conversations is not None:
        cfg = _load_config()
        cfg["scraper_selected"] = list(req.conversations)
        _save_config(cfg)

    asyncio.create_task(_run_scrape(cmd))
    return {"status": "started", "message": _scrape_state["message"]}


def _kill_process_tree(proc) -> None:
    """递归杀掉进程树（含 Playwright 的 Chromium 子进程）。

    只 kill/terminate 主进程会让 Chromium 变孤儿继续运行，占用 browser_profile 锁，
    导致下次采集/刷新会话列表卡死。Windows 用 taskkill /T 递归杀。
    注意：subprocess.run 是同步阻塞调用，调用方若是 async 端点须用 asyncio.to_thread。
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        import subprocess as _subprocess
        _subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True, timeout=15,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


async def _run_scrape(cmd):
    # Reset here (not in start_scrape) so BOTH the manual and cron paths clear a
    # prior manual-stop flag; otherwise a scheduled scrape after a manual Stop
    # would be mislabeled '已停止' and its failure notification suppressed.
    _scrape_state["stopped"] = False
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        # 关键修复: 子进程 stdout 直接重定向到日志文件（不用管道）。
        # 之前用 asyncio 管道逐行读取：Windows Proactor 上 wait_for 反复取消
        # readline 可能损坏 IOCP 状态 → 事件循环被阻塞 → 整个服务无响应（卡死）。
        # 现在子进程用 -u + PYTHONUNBUFFERED 逐行实时写文件，uvicorn 只等
        # proc.wait()，完全不碰管道，物理上不可能阻塞事件循环。
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        log_file = open(LOG_PATH, "w", buffering=1, encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
                env=env,
            )
            _scrape_state["process"] = proc

            # 看门狗：等进程结束（每 10s 检查一次），连续 10 分钟无日志输出则
            # 判定卡死并强制终止。不碰管道，只读日志文件 mtime。
            try:
                last_mtime = 0.0
                while proc.returncode is None:
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                        break
                    except asyncio.TimeoutError:
                        pass
                    # 停止标记：taskkill 已由 stop_scrape 触发，直接等进程回收
                    if _scrape_state.get("stopped"):
                        continue
                    try:
                        cur_mtime = os.path.getmtime(LOG_PATH)
                    except OSError:
                        cur_mtime = 0.0
                    if cur_mtime <= last_mtime:
                        if not _scrape_state.get("_stale_since"):
                            _scrape_state["_stale_since"] = time.time()
                        elif time.time() - _scrape_state["_stale_since"] > 10 * 60:
                            print("[!] 采集无输出超 10 分钟，判定卡死，强制终止进程")
                            await asyncio.to_thread(_kill_process_tree, proc)
                            break
                    else:
                        _scrape_state["_stale_since"] = None
                        last_mtime = cur_mtime
            finally:
                _scrape_state["_stale_since"] = None
        finally:
            try:
                log_file.close()
            except Exception:
                pass

        if _scrape_state.get("stopped"):
            # User-initiated stop: SIGTERM makes returncode nonzero, but this is
            # not a failure — don't report failed or push a WeChat notification.
            _scrape_state["status"] = "idle"
            _scrape_state["message"] = "已停止"
        elif proc.returncode == 0:
            _scrape_state["status"] = "completed"
            _scrape_state["message"] = "采集完成"
        else:
            _scrape_state["status"] = "failed"
            _scrape_state["message"] = f"采集失败 (exit code {proc.returncode})"
    except Exception as e:
        _scrape_state["status"] = "failed"
        _scrape_state["message"] = f"采集错误: {e}"
    finally:
        _scrape_state["finished_at"] = time.time()
        _scrape_state["process"] = None
        if _scrape_state["status"] == "failed" and not _scrape_state.get("stopped"):
            asyncio.create_task(_notify_on_failure(
                "抖音聊天导出 · 采集失败",
                _build_failure_desp(_scrape_state["message"], LOG_PATH),
            ))


@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 200, max_bytes: int = 200_000):
    """读取日志尾部，避免对大文件全量 readlines() 拖慢轮询。
    默认只读最后 ~200KB，前端 JS 端再按行截断展示。
    """
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        file_size = os.path.getsize(LOG_PATH)
        with open(LOG_PATH, "rb") as f:
            if file_size > max_bytes:
                f.seek(file_size - max_bytes)
                f.readline()  # 丢掉第一行（可能不完整）
            data = f.read().decode("utf-8", errors="replace")
        return {"log": data}
    except Exception:
        return {"log": ""}


@control_router.get("/api/conversations/refresh/log")
async def discover_log(lines: int = 80):
    if not os.path.exists(DISCOVER_LOG_PATH):
        return {"log": ""}
    try:
        with open(DISCOVER_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}


@control_router.post("/api/scrape/stop")
async def stop_scrape():
    proc = _scrape_state.get("process")
    if proc and proc.returncode is None:
        _scrape_state["stopped"] = True  # tell _run_scrape this was intentional
        # 关键修复: 不能只 terminate() 杀 extract.py 主进程——Playwright 的
        # Chromium 是它的子进程，主进程被杀后 Chromium 变成孤儿进程继续运行，
        # 占用 browser_profile 锁 → 下次采集/刷新会话列表就卡死（profile 锁冲突）。
        # _kill_process_tree 用 taskkill /T 递归杀整个进程树（含 Chromium）；
        # 它是同步阻塞的，放线程池执行避免卡住事件循环（否则前端请求全挂起）。
        await asyncio.to_thread(_kill_process_tree, proc)
        _scrape_state["status"] = "idle"
        _scrape_state["message"] = "已停止"
        return {"status": "stopped"}
    return {"status": "not_running"}


@control_router.post("/api/custom-filter")
async def manage_custom_filter(req: CustomFilterAction):
    cfg = _load_config()
    filters = cfg.get("custom_filters", [])
    if req.action == "add" and req.value and req.value not in filters:
        filters.append(req.value)
    elif req.action == "remove" and req.value in filters:
        filters.remove(req.value)
    cfg["custom_filters"] = filters
    _save_config(cfg)
    return {"custom_filters": filters}


# ── Conversation discovery / selection ────────────────────────────

def _read_conv_list():
    if not os.path.exists(CONV_LIST_PATH):
        return {"discovered_at": 0, "items": []}
    try:
        with open(CONV_LIST_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"discovered_at": 0, "items": []}


_login_probe_lock = asyncio.Lock()


async def _probe_login_state() -> dict:
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

        # ── Fast path 2: cookie 导入的备份 (data/cookies_backup.json) ──
        # 刚导入的 cookies 可能还没 flush 进 Chromium 的 sqlite（或复用登录
        # context 未落盘），但 web_scraper.launch() 会从该备份加载，视为已登录。
        backup_result = _read_sessionid_from_backup()
        if backup_result is not None:
            return backup_result

        # ── Slow path: launch Chromium (capped at 8s) ──
        # We only get here if the cookies DB is missing, locked, or empty.
        # In practice this means the user hasn't run a login yet OR the
        # profile is genuinely broken and needs a re-login.
        # 关键：采集/发现会话运行中时，extract.py 子进程正占用同一个
        # browser_profile。此时再 launch Chromium 会 profile 锁冲突，无限等待
        # （正是"刷新面板/进查看页就卡死"的根源）。采集运行中直接按备份判断，
        # 不再启动浏览器。
        if _scrape_state.get("status") == "running" or _discover_state.get("status") == "running":
            return {"status": "unknown", "has_cookies": False,
                    "message": "采集进行中，浏览器正忙，登录状态以备份为准"}
        try:
            from playwright.async_api import async_playwright
            pw = await async_playwright().start()
            try:
                async def _launch_and_probe():
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
                # 整个 launch+probe 流程 20s 硬超时，杜绝 profile 锁冲突导致的永久卡死
                return await asyncio.wait_for(_launch_and_probe(), timeout=20)
            finally:
                await pw.stop()
        except asyncio.TimeoutError:
            return {"status": "error", "has_cookies": False,
                    "message": "login probe timeout (>20s)"}
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


def _read_sessionid_from_backup() -> dict | None:
    """检查 cookie 导入备份 (data/cookies_backup.json) 里是否有有效的 sessionid。

    刚导入的 cookies 可能尚未 flush 进 Chromium 的 Cookies sqlite（尤其复用登录
    context 时），但 web_scraper.launch() 会从该备份加载 cookies 完成登录，
    因此这里认为有备份=已登录，避免前端误报"未检测到登录态"。

    返回 {"status": "logged_in", "has_cookies": True} 或 None（无备份/无 sessionid）。
    """
    try:
        backup_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "cookies_backup.json"
        )
        if not os.path.isfile(backup_path):
            return None
        with open(backup_path, "r", encoding="utf-8") as f:
            backup_cookies = json.load(f)
        if not isinstance(backup_cookies, list):
            return None
        for c in backup_cookies:
            if not isinstance(c, dict):
                continue
            if c.get("name") == "sessionid" and str(c.get("value", "")).strip():
                return {"status": "logged_in", "has_cookies": True}
        return None
    except Exception:
        return None


@control_router.post("/api/conversations/refresh")
async def refresh_conversations():
    """Run a lightweight scrape that only enumerates the conversation list."""
    if _discover_state["status"] == "running":
        return JSONResponse({"error": "Refresh already running"}, status_code=409)
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "Scraper is running — stop it first"}, status_code=409)

    # Pre-check: don't spawn the 3-minute browser wait if we already know
    # there's no usable session. Uses the same Playwright probe as the
    # login badge so the two never disagree.
    probe = await _probe_login_state()
    if not probe["has_cookies"]:
        return JSONResponse(
            {"error": "未检测到登录态，请先扫码登录或导入 Cookie"},
            status_code=400,
        )

    _discover_state["status"] = "running"
    _discover_state["message"] = "正在加载会话列表..."
    _discover_state["started_at"] = time.time()
    _discover_state["finished_at"] = None

    cmd = [sys.executable, "-u", "extract.py", "--list-conversations"]
    asyncio.create_task(_run_discover(cmd))
    return {"status": "started"}


async def _run_discover(cmd):
    proc = None
    try:
        os.makedirs(os.path.dirname(DISCOVER_LOG_PATH), exist_ok=True)
        # 与 _run_scrape 一致：子进程直接写日志文件，不碰管道（避免 Windows
        # Proactor 上 wait_for 取消 readline 导致事件循环阻塞）
        log_file = open(DISCOVER_LOG_PATH, "w", buffering=1, encoding="utf-8")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )
            _discover_state["process"] = proc
            try:
                await proc.wait()
            finally:
                _discover_state["stopped"] = False
        finally:
            try:
                log_file.close()
            except Exception:
                pass

        if proc.returncode == 0:
            data = _read_conv_list()
            count = len(data.get("items", []))
            _discover_state["status"] = "completed"
            _discover_state["message"] = f"发现 {count} 个会话"
        elif proc.returncode == 2:
            _discover_state["status"] = "failed"
            _discover_state["message"] = "未检测到登录态，请先扫码或导入 Cookie"
        else:
            _discover_state["status"] = "failed"
            _discover_state["message"] = f"刷新失败 (exit {proc.returncode})"
    except Exception as e:
        _discover_state["status"] = "failed"
        _discover_state["message"] = f"刷新错误: {e}"
        # Best-effort: kill any lingering subprocess so it doesn't pin the state
        _kill_process_tree(proc)
    finally:
        _discover_state["finished_at"] = time.time()
        _discover_state["process"] = None
        # Defensive: ensure status is never left at "running" when this coroutine exits
        if _discover_state["status"] == "running":
            _discover_state["status"] = "failed"
            _discover_state["message"] = _discover_state["message"] or "刷新中断"


@control_router.get("/api/conversations/refresh/status")
async def refresh_status():
    data = _read_conv_list()
    # 关联数据库 conv_id：同名会话（如两个"小羊莓莓"）各带自己的 conv_id，
    # 前端才能区分并精确导出/采集。按昵称匹配 conversations.name。
    items = data.get("items", [])
    try:
        from backend.database import get_db
        conn = get_db()
        try:
            rows = conn.execute(
                "SELECT conv_id, name FROM conversations WHERE name IS NOT NULL AND name != ''"
            ).fetchall()
            by_name: dict[str, list[str]] = {}
            for r in rows:
                by_name.setdefault(r["name"], []).append(r["conv_id"])
            for it in items:
                nick = it.get("nickname") or ""
                cids = by_name.get(nick, [])
                if cids:
                    it["conv_id"] = cids[0]
                    # 同名会话保留完整列表（前端可区分）
                    it["conv_ids"] = cids
        finally:
            conn.close()
    except Exception:
        pass
    return {
        "status": _discover_state["status"],
        "message": _discover_state["message"],
        "started_at": _discover_state["started_at"],
        "finished_at": _discover_state["finished_at"],
        "discovered_at": data.get("discovered_at", 0),
        "items": items,
    }


@control_router.post("/api/conversations/refresh/stop")
async def refresh_stop():
    proc = _discover_state.get("process")
    if proc and proc.returncode is None:
        # 同样用进程树终止（防孤儿 Chromium 锁 profile）；同步阻塞放线程池
        _discover_state["stopped"] = True
        await asyncio.to_thread(_kill_process_tree, proc)
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已停止"
        return {"status": "stopped"}
    # No live process — if state is still "running", force-reset (was stuck)
    if _discover_state["status"] == "running":
        _discover_state["status"] = "idle"
        _discover_state["message"] = "已重置"
        _discover_state["finished_at"] = time.time()
        return {"status": "reset"}
    return {"status": "not_running"}


@control_router.get("/api/conversations/selected")
async def get_selected():
    cfg = _load_config()
    return {
        "scraper": cfg.get("scraper_selected", []),
        "export": cfg.get("export_selected", []),
        "schedule": cfg.get("schedule_selected", []),
    }


@control_router.post("/api/conversations/selected")
async def set_selected(req: SelectedUpdate):
    if req.section not in ("scraper", "export", "schedule"):
        return JSONResponse({"error": "invalid section"}, status_code=400)
    cfg = _load_config()
    cfg[f"{req.section}_selected"] = list(req.conversations)
    _save_config(cfg)
    return {"status": "ok", "selected": cfg[f"{req.section}_selected"]}


@control_router.post("/api/schedule")
async def set_schedule(req: ScheduleRequest):
    # Cancel existing scheduled task
    if _scheduler_state["task"] and not _scheduler_state["task"].done():
        _scheduler_state["task"].cancel()
        _scheduler_state["task"] = None

    _scheduler_state["enabled"] = req.enabled
    _scheduler_state["schedule"] = req.cron if req.enabled else ""
    _scheduler_state["next_run"] = None

    # Always persist the schedule selection so the cron loop + UI stay in sync
    cfg = _load_config()
    if req.conversations is not None:
        cfg["schedule_selected"] = list(req.conversations)

    if req.enabled and req.cron:
        parsed = _parse_cron(req.cron)
        if not parsed:
            return JSONResponse({"error": "无效的 cron 表达式（分 时 日 月 周）"}, status_code=400)

        next_run = _next_cron_run(parsed)
        _scheduler_state["next_run"] = next_run
        _scheduler_state["task"] = asyncio.create_task(
            _cron_loop(parsed, req.incremental)
        )
        cfg["schedule"] = req.cron
        _save_config(cfg)
        return {"status": "enabled", "cron": req.cron, "next_run": next_run}

    cfg["schedule"] = ""
    _save_config(cfg)
    return {"status": "disabled"}


# Cron parsing (_parse_cron / _next_cron_run) lives in backend/panel/scheduler.py.


async def _cron_loop(parsed: list, incremental: bool):
    """Run scrape on cron schedule."""
    try:
        while True:
            next_run = _next_cron_run(parsed)
            _scheduler_state["next_run"] = next_run
            wait_secs = next_run - time.time()
            if wait_secs > 0:
                await asyncio.sleep(wait_secs)
            if _scrape_state["status"] != "running":
                cmd = [sys.executable, "-u", "extract.py"]
                if incremental:
                    cmd.append("--incremental")
                cfg = _load_config()
                if cfg.get("download_images"):
                    cmd.append("--download-images")
                # Preferred: schedule_selected (checkbox picks).
                # Fallback: custom_filters (legacy).
                # Fallback: all DB conversations (scrape everything we know).
                filters = cfg.get("schedule_selected") or cfg.get("custom_filters") or []
                if not filters:
                    from backend.database import get_db
                    conn = get_db()
                    convs = conn.execute("SELECT name FROM conversations WHERE name IS NOT NULL AND name != ''").fetchall()
                    conn.close()
                    filters = [c[0] for c in convs]
                if filters:
                    cmd.extend(["--filter", ",".join(filters)])
                _scrape_state["status"] = "running"
                _scrape_state["started_at"] = time.time()
                _scrape_state["finished_at"] = None
                filter_desc = f" (过滤: {','.join(filters[:5])}{'...' if len(filters) > 5 else ''})" if filters else " (全部会话)"
                _scrape_state["message"] = f"定时{'增量' if incremental else '全量'}采集{filter_desc}"
                await _run_scrape(cmd)
            # Wait at least 61 seconds to avoid re-trigger in same minute
            await asyncio.sleep(61)
    except asyncio.CancelledError:
        pass


@control_router.post("/api/export")
async def start_export(req: ExportRequest):
    if _export_state["status"] == "running":
        return JSONResponse({"error": "Export already running"}, status_code=409)

    _export_state["status"] = "running"
    _export_state["message"] = "正在导出..."

    # Persist selection
    if req.conversations is not None:
        cfg = _load_config()
        cfg["export_selected"] = list(req.conversations)
        _save_config(cfg)

    convs = list(req.conversations) if req.conversations else None
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _do_export, req.format, req.filter, convs)
    return {
        "status": _export_state["status"],
        "message": _export_state["message"],
        "file_path": _export_state["file_path"],
    }


def _do_export(fmt: str, filter_name: str, conversations: list | None):
    try:
        from extractor.exporter import ChatLabExporter
        import re
        import zipfile

        ext = ".json" if fmt == "json" else ".jsonl"
        data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
        # 统一导出目录: data/exports/<对方真实昵称>/<日期><ext>，按会话分文件夹
        exports_dir = os.path.join(data_dir, "exports")
        os.makedirs(exports_dir, exist_ok=True)
        today = time.strftime("%Y-%m-%d")

        def _safe_dirname(name: str) -> str:
            safe = re.sub(r'[<>:"/\\|?*]+', "_", str(name or "")).strip().strip(".")
            return safe or "未知会话"

        # 标准化目标：list of dict {name, conv_id}（兼容旧格式：纯字符串名字）
        targets_raw = []
        if conversations:
            targets_raw = conversations
        elif filter_name:
            targets_raw = [filter_name]
        else:
            targets_raw = [None]

        targets = []
        for t in targets_raw:
            if isinstance(t, dict):
                targets.append({"name": t.get("name") or t.get("nickname") or "", "conv_id": t.get("conv_id") or None})
            elif t is None:
                targets.append({"name": None, "conv_id": None})
            else:
                targets.append({"name": t, "conv_id": None})

        if len(targets) <= 1:
            # 单会话 → exports/<对方昵称>/<日期><ext>
            t = targets[0]
            exporter = ChatLabExporter(conv_name=t["name"], output_format=fmt, conv_id=t["conv_id"])
            # 先导到临时文件拿 peer_name（export() 返回对方昵称）
            tmp_path = os.path.join(exports_dir, f"_tmp{ext}")
            peer = exporter.export(tmp_path)
            if not os.path.exists(tmp_path):
                raise RuntimeError(f"未找到会话: {t['name'] or '(any)'}")
            conv_dir = os.path.join(exports_dir, _safe_dirname(peer or t["name"] or "对话"))
            os.makedirs(conv_dir, exist_ok=True)
            final_path = os.path.join(conv_dir, f"{today}{ext}")
            os.replace(tmp_path, final_path)
            _export_state["file_path"] = os.path.relpath(final_path, data_dir).replace("\\", "/")
            size_mb = os.path.getsize(final_path) / (1024 * 1024)
            _export_state["message"] = f"导出完成 → {_safe_dirname(peer or '')}/ ({size_mb:.1f} MB)"
        else:
            # 多会话 → exports/<对方昵称>/<日期><ext>，各自独立目录
            produced = []
            skipped = []
            seen_dirs = {}  # 目录名冲突计数（同名会话）
            for t in targets:
                exporter = ChatLabExporter(conv_name=t["name"], output_format=fmt, conv_id=t["conv_id"])
                tmp_path = os.path.join(exports_dir, f"_tmp{ext}")
                try:
                    peer = exporter.export(tmp_path)
                    if os.path.exists(tmp_path):
                        base_dir = _safe_dirname(peer or t["name"] or "对话")
                        # 同名会话（两个小羊莓莓）目录加序号区分
                        if base_dir in seen_dirs:
                            seen_dirs[base_dir] += 1
                            base_dir = f"{base_dir}_{seen_dirs[base_dir]}"
                        else:
                            seen_dirs[base_dir] = 1
                        conv_dir = os.path.join(exports_dir, base_dir)
                        os.makedirs(conv_dir, exist_ok=True)
                        final_path = os.path.join(conv_dir, f"{today}{ext}")
                        os.replace(tmp_path, final_path)
                        produced.append((t["name"], final_path, conv_dir))
                    else:
                        skipped.append(t["name"])
                except Exception as e:
                    skipped.append(t["name"])
                    print(f"[-] 导出 {t['name']} 失败: {e}")

            if not produced:
                raise RuntimeError(
                    f"没有成功导出的会话" + (f"（未找到: {'、'.join(skipped)}）" if skipped else "")
                )

            # 打包 zip 供一次性下载（zip 内按 <昵称>/<日期><ext> 组织）
            zip_path = os.path.join(exports_dir, f"export_{today}.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for _, path, conv_dir in produced:
                    arcname = os.path.join(os.path.basename(conv_dir), os.path.basename(path))
                    zf.write(path, arcname=arcname)

            _export_state["file_path"] = os.path.relpath(zip_path, data_dir).replace("\\", "/")
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            msg = f"导出完成 ({len(produced)} 个会话, {size_mb:.1f} MB)"
            if skipped:
                msg += f"；未找到: {'、'.join(skipped)}"
            _export_state["message"] = msg

        _export_state["status"] = "completed"
    except Exception as e:
        _export_state["status"] = "failed"
        _export_state["message"] = f"导出失败: {e}"


@control_router.get("/api/export/download")
async def download_export():
    if not _export_state["file_path"]:
        return JSONResponse({"error": "No export file"}, status_code=404)
    path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", _export_state["file_path"]
    )
    if not os.path.exists(path):
        return JSONResponse({"error": "File not found"}, status_code=404)
    return FileResponse(path, filename=_export_state["file_path"])


# ── Login (in-container headless with screenshot) ──

import base64

_USER_DATA_DIR = paths.BROWSER_PROFILE

_login_state = {
    "status": "idle",  # idle | starting | waiting_scan | logged_in | failed
    "screenshot": None,  # base64 png
    "message": "",
    "countdown": 0,
    "_context": None,
    "_pw": None,
}


@control_router.get("/api/login/check")
async def login_check():
    """Check login by actually opening browser and reading cookies."""
    return await _probe_login_state()


@control_router.post("/api/login/start")
async def login_start():
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "已在登录流程中"}, status_code=409)
    # If scraper is running, reject
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "请先停止采集再登录"}, status_code=409)

    _login_state["status"] = "starting"
    _login_state["screenshot"] = None
    _login_state["message"] = "正在启动浏览器..."
    asyncio.create_task(_login_flow())
    return {"status": "started"}


@control_router.get("/api/login/status")
async def login_status():
    return {
        "status": _login_state["status"],
        "screenshot": _login_state["screenshot"],
        "message": _login_state["message"],
        "countdown": _login_state["countdown"],
    }


class MouseAction(BaseModel):
    action: str  # click, mousedown, mousemove, mouseup
    x: float
    y: float


class KeyAction(BaseModel):
    action: str  # press, type
    key: str = ""
    text: str = ""


@control_router.post("/api/login/mouse")
async def login_mouse(req: MouseAction):
    """Forward mouse events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        mouse = page.mouse
        if req.action == "click":
            await mouse.click(req.x, req.y)
        elif req.action == "mousedown":
            await mouse.move(req.x, req.y)
            await mouse.down()
        elif req.action == "mousemove":
            await mouse.move(req.x, req.y)
        elif req.action == "mouseup":
            await mouse.up()
        else:
            return JSONResponse({"error": f"Unknown action: {req.action}"}, status_code=400)

        # Take a fresh screenshot after interaction
        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()

        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/keyboard")
async def login_keyboard(req: KeyAction):
    """Forward keyboard events to the headless browser page."""
    ctx = _login_state.get("_context")
    if not ctx or _login_state["status"] not in ("waiting_scan",):
        return JSONResponse({"error": "No active login session"}, status_code=400)

    try:
        page = ctx.pages[0] if ctx.pages else None
        if not page:
            return JSONResponse({"error": "No page"}, status_code=400)

        kb = page.keyboard
        if req.action == "type" and req.text:
            await kb.type(req.text)
        elif req.action == "press" and req.key:
            await kb.press(req.key)
        else:
            return JSONResponse({"error": "Invalid keyboard action"}, status_code=400)

        await asyncio.sleep(0.15)
        png = await page.screenshot(type="png")
        _login_state["screenshot"] = base64.b64encode(png).decode()
        return {"status": "ok"}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@control_router.post("/api/login/cancel")
async def login_cancel():
    await _login_cleanup()
    _login_state["status"] = "idle"
    _login_state["message"] = "已取消"
    _login_state["screenshot"] = None
    return {"status": "cancelled"}


@control_router.post("/api/login/clear")
async def login_clear():
    """Clear browser profile to force re-login."""
    import shutil
    if os.path.isdir(_USER_DATA_DIR):
        shutil.rmtree(_USER_DATA_DIR, ignore_errors=True)
    return {"status": "cleared"}


def _validate_cookie_entries(parsed: list[dict]) -> tuple[list[str], list[str]]:
    """Pre-flight check on parsed cookies. Returns (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    sids = [c for c in parsed if c["name"] == "sessionid"]
    if not sids:
        errors.append("Cookie 中未包含 sessionid，请确保已登录后再导出（cookie-editor 需全选导出）")
        return errors, warnings

    sid = sids[0]
    value = (sid.get("value") or "").strip()
    if not value:
        errors.append("sessionid 的值为空")
    elif len(value) < 16:
        warnings.append(f"sessionid 长度异常 ({len(value)} 字节)，可能被截断")

    domain = (sid.get("domain") or "").lstrip(".")
    if domain and domain != "douyin.com" and not domain.endswith(".douyin.com"):
        errors.append(
            f"sessionid 的 domain 是 .{domain}（应为 .douyin.com）"
            "—— 可能在子站点（iesdouyin.com 等）导出了，请回到 www.douyin.com 重导"
        )

    exp = sid.get("expires")
    if exp and exp > 0 and exp < time.time():
        errors.append("sessionid 已过期（expirationDate 在过去），请重新登录后再导出")

    if len(parsed) < 3:
        warnings.append(
            f"只解析出 {len(parsed)} 个 cookie，抖音通常需要 10+ 个才能完整工作，"
            "建议在 cookie-editor 里全选后再导出"
        )
    return errors, warnings


@control_router.post("/api/login/cookie-import")
async def login_cookie_import(req: CookieImportRequest):
    """Import cookies from browser DevTools or document.cookie string."""
    if _scrape_state["status"] == "running":
        return JSONResponse({"error": "采集进行中，请先停止"}, status_code=409)
    if _login_state["status"] in ("starting", "waiting_scan"):
        return JSONResponse({"error": "登录流程进行中，请先取消"}, status_code=409)

    raw = req.cookies.strip()
    if not raw:
        return JSONResponse({"error": "Cookie 数据为空"}, status_code=400)

    parsed: list[dict] = []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            for c in data:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                entry: dict = {
                    "name": c["name"],
                    "value": str(c.get("value", "")),
                    "domain": c.get("domain", ".douyin.com"),
                    "path": c.get("path", "/"),
                }
                exp = c.get("expirationDate") or c.get("expires")
                if exp:
                    entry["expires"] = float(exp)
                if c.get("httpOnly") is not None:
                    entry["httpOnly"] = bool(c["httpOnly"])
                if c.get("secure") is not None:
                    entry["secure"] = bool(c["secure"])
                # cookie-editor exports sameSite as lowercase enum.
                # Map "no_restriction" → "None" (cross-site allowed) — must NOT downgrade to Lax,
                # since some Douyin auth cookies require cross-site delivery for IM API calls.
                ss = (c.get("sameSite") or "").strip().lower()
                ss_map = {"no_restriction": "None", "none": "None",
                          "lax": "Lax", "strict": "Strict"}
                if ss in ss_map:
                    entry["sameSite"] = ss_map[ss]
                    # Playwright requires Secure=true when SameSite=None
                    if entry["sameSite"] == "None":
                        entry["secure"] = True
                parsed.append(entry)
        else:
            return JSONResponse({"error": "JSON 格式需为数组"}, status_code=400)
    except (json.JSONDecodeError, ValueError):
        for pair in raw.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, value = pair.split("=", 1)
            parsed.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".douyin.com",
                "path": "/",
            })

    if not parsed:
        return JSONResponse({"error": "未能解析出任何 Cookie"}, status_code=400)

    errors, warnings = _validate_cookie_entries(parsed)
    if errors:
        return JSONResponse({"error": "；".join(errors)}, status_code=400)

    # Session cookies (no expirationDate) get dropped on browser restart,
    # so the next login probe wouldn't see them. Pin a 30-day default.
    default_exp = time.time() + 30 * 86400
    for c in parsed:
        if "expires" not in c:
            c["expires"] = default_exp

    # ── 性能/稳定性优化: 导入 cookie 不再每次都新开浏览器 + 打开抖音首页 ──
    # 1) 若登录流程的浏览器上下文还在，直接复用它（秒级），避免重复启动 Chromium；
    # 2) 否则新开浏览器，但只 goto about:blank（不加载抖音首页，省 10s+ 网络等待），
    #    add_cookies 后直接用 context.cookies() 验证 sessionid，无需页面导航；
    # 3) 整个流程加 30s 硬超时，杜绝"卡着"（launch/goto 无超时是卡住的根源）。
    # 4) 验证通过后同步写一份 data/cookies_backup.json —— web_scraper.launch()
    #    在 profile 无 sessionid 时会从该备份加载，导入从此双保险。
    # 5) 采集/发现运行中：profile 正被 extract.py 占用，再启动 Chromium 会锁冲突
    #    卡死。此时跳过浏览器验证，直接写备份文件（下次采集 launch 时生效）。
    if _scrape_state.get("status") == "running" or _discover_state.get("status") == "running":
        try:
            backup_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "data", "cookies_backup.json"
            )
            os.makedirs(os.path.dirname(backup_path), exist_ok=True)
            with open(backup_path, "w", encoding="utf-8") as f:
                json.dump(parsed, f, ensure_ascii=False, indent=2)
            msg = (f"采集进行中，Cookie 已保存为备份（{len(parsed)} 条），"
                   f"将在下次采集时自动生效")
            if warnings:
                msg += "（注意：" + "；".join(warnings) + "）"
            return {"status": "ok", "message": msg, "count": len(parsed),
                    "warnings": warnings, "deferred": True}
        except Exception as e:
            return JSONResponse({"error": f"写 cookies 备份失败: {e}"}, status_code=500)

    try:
        from playwright.async_api import async_playwright

        ctx = _login_state.get("_context")
        close_ctx = False
        if ctx is None or ctx.is_closed():
            os.makedirs(_USER_DATA_DIR, exist_ok=True)
            pw = await async_playwright().start()

            async def _launch():
                nonlocal ctx, close_ctx
                ctx = await pw.chromium.launch_persistent_context(
                    _USER_DATA_DIR,
                    headless=True,
                    viewport={"width": 1400, "height": 900},
                    locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                )
                close_ctx = True

            try:
                await asyncio.wait_for(_launch(), timeout=30)
            except asyncio.TimeoutError:
                try:
                    await pw.stop()
                except Exception:
                    pass
                return JSONResponse(
                    {"error": "导入超时（浏览器启动超过 30s）。若上次浏览器未正常关闭，"
                              "请先到「登录」页点击取消/清空，或稍后再试"},
                    status_code=504,
                )
        else:
            pw = None

        try:
            # 打开空白页（极快），让 cookie 存储初始化并关联当前 origin
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await asyncio.wait_for(
                    page.goto("about:blank", wait_until="domcontentloaded"), timeout=10
                )
            except asyncio.TimeoutError:
                pass  # 空白页失败不致命，add_cookies 不依赖页面

            await ctx.add_cookies(parsed)
            cookies = await ctx.cookies("https://www.douyin.com")
            ok = "sessionid" in {c["name"] for c in cookies}
            all_cookies = await ctx.cookies()  # everything regardless of url, for diagnostics
        finally:
            if close_ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
                try:
                    await pw.stop()
                except Exception:
                    pass

        if ok:
            # 双保险：同步写 cookies_backup.json（web_scraper.launch 会读取）
            try:
                backup_path = os.path.join(
                    os.path.dirname(os.path.dirname(__file__)), "data", "cookies_backup.json"
                )
                os.makedirs(os.path.dirname(backup_path), exist_ok=True)
                with open(backup_path, "w", encoding="utf-8") as f:
                    json.dump(parsed, f, ensure_ascii=False, indent=2)
                print(f"[+] cookies 已备份到 {backup_path} ({len(parsed)} 条)")
            except Exception as e:
                print(f"[!] 写 cookies_backup.json 失败: {e}")

            msg = f"成功导入 {len(parsed)} 个 Cookie"
            if warnings:
                msg += "（注意：" + "；".join(warnings) + "）"
            return {"status": "ok", "message": msg, "count": len(parsed),
                    "warnings": warnings}
        # Verification failed — diagnose why so the user knows what to fix.
        sid_other = [c for c in all_cookies if c["name"] == "sessionid"]
        if sid_other:
            wrong_domain = sid_other[0].get("domain", "?")
            return JSONResponse(
                {"error": f"sessionid 被加载到 domain={wrong_domain}，"
                          f"对 www.douyin.com 不生效。请确认 cookie 的 domain 是 .douyin.com"},
                status_code=400,
            )
        return JSONResponse(
            {"error": "sessionid 导入后无法在 douyin.com 读取到，"
                      "可能已被服务端注销，请重新登录后再导出"},
            status_code=400,
        )
    except Exception as e:
        return JSONResponse({"error": f"导入失败: {e}"}, status_code=500)


async def _login_cleanup():
    try:
        if _login_state["_context"]:
            await _login_state["_context"].close()
    except Exception:
        pass
    try:
        if _login_state["_pw"]:
            await _login_state["_pw"].stop()
    except Exception:
        pass
    _login_state["_context"] = None
    _login_state["_pw"] = None


async def _login_flow():
    """In-container: open headless browser, screenshot the page for QR scanning."""
    try:
        from playwright.async_api import async_playwright

        os.makedirs(_USER_DATA_DIR, exist_ok=True)
        pw = await async_playwright().start()
        _login_state["_pw"] = pw

        try:
            # 启动浏览器 30s 硬超时：有残留 Chromium 锁 profile 时会卡在这里
            ctx = await asyncio.wait_for(
                pw.chromium.launch_persistent_context(
                    _USER_DATA_DIR,
                    headless=True,
                    viewport={"width": 1400, "height": 900},
                    locale="zh-CN",
                    args=["--disable-blink-features=AutomationControlled"],
                ),
                timeout=30,
            )
        except asyncio.TimeoutError:
            _login_state["status"] = "failed"
            _login_state["message"] = "启动浏览器超时 (>30s)，可能有残留进程占用，请重试"
            await _login_cleanup()
            return
        _login_state["_context"] = ctx
        await ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # Navigate to Douyin
        _login_state["message"] = "正在打开抖音..."
        try:
            await asyncio.wait_for(
                page.goto("https://www.douyin.com/", wait_until="domcontentloaded"),
                timeout=20,
            )
        except asyncio.TimeoutError:
            _login_state["message"] = "打开抖音超时，继续尝试..."
        await asyncio.sleep(2)

        # Check if already logged in
        cookies = await ctx.cookies("https://www.douyin.com")
        cookie_names = {c["name"] for c in cookies}
        if "sessionid" in cookie_names:
            _login_state["status"] = "logged_in"
            _login_state["message"] = "已登录，无需扫码"
            await _login_cleanup()
            return

        # Try to click login button
        _login_state["status"] = "waiting_scan"
        _login_state["message"] = "正在获取二维码..."
        try:
            login_btn = await page.wait_for_selector(
                'button:has-text("登录")', timeout=5000
            )
            if login_btn:
                await login_btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        # Poll: take screenshots and check cookies
        timeout_secs = 180
        for i in range(timeout_secs):
            if _login_state["status"] != "waiting_scan":
                break  # cancelled

            _login_state["countdown"] = timeout_secs - i

            # Screenshot
            png = await page.screenshot(type="png")
            _login_state["screenshot"] = base64.b64encode(png).decode()
            _login_state["message"] = f"请用抖音 APP 扫码 ({timeout_secs - i}s)"

            # Check login
            cookies = await ctx.cookies("https://www.douyin.com")
            cookie_names = {c["name"] for c in cookies}
            if "sessionid" in cookie_names:
                _login_state["status"] = "logged_in"
                _login_state["message"] = "登录成功！"
                _login_state["screenshot"] = None
                await _login_cleanup()
                return

            await asyncio.sleep(1)

        if _login_state["status"] == "waiting_scan":
            _login_state["status"] = "failed"
            _login_state["message"] = "扫码超时（3 分钟）"

    except Exception as e:
        _login_state["status"] = "failed"
        _login_state["message"] = f"登录错误: {e}"
    finally:
        await _login_cleanup()
