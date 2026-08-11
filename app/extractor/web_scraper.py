#!/usr/bin/env python3
"""Extract Douyin chat messages via web version using Playwright + DOM scraping."""
import asyncio
import base64
import json
import hashlib
import os
import re
import random
import sys
import time
from datetime import datetime, timedelta

# Fix Windows console encoding: allow unencodable chars (e.g. \xa0, emoji,
# decorative Unicode in nicknames) to be replaced instead of crashing print().
if sys.platform == 'win32':
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, 'reconfigure'):
            try:
                _stream.reconfigure(errors='replace')
            except Exception:
                pass

from playwright.async_api import async_playwright

from common import paths
from extractor.models import (
    init_db, get_db, upsert_user, upsert_conversation, update_conversation_stats,
)

CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "browser_profile")

# IM 用户信息接口：POST sec_user_ids=<JSON 数组> → {data: [{uid, nickname, unique_id, avatar_thumb}]}
# 只认 cookies，不需要 msToken/a_bogus（与 batch_play_info 同理）。
USER_INFO_API = "https://www.douyin.com/aweme/v1/web/im/user/info/"
BATCH_USER_INFO = 20

# ── DOM Selectors (from discovery) ──────────────────────────────
# Conversation list
SEL_CONV_LIST = 'div[class*="conversationConversationListwrapper"]'
SEL_CONV_ITEM = 'div[class*="conversationConversationItemwrapper"]'
SEL_CONV_TITLE = 'div[class*="conversationConversationItemtitle"]'
SEL_CONV_TIME = 'div[class*="ConversationItemTagNextToTitletimeStr"]'
SEL_CONV_PREVIEW = 'pre[class*="ConversationItemHinttextBox"]'

# Message area
SEL_MSG_LIST = 'div[class*="messageMessageListlist"]'
SEL_MSG_BOX = 'div[class*="messageMessageBoxmessageBox"]'
SEL_MSG_CONTENT_BOX = 'div[class*="messageMessageBoxcontentBox"]'
SEL_MSG_IS_SELF = 'messageMessageBoxisFromMe'  # class substring
SEL_MSG_TEXT = 'span[class*="TextMessageTextpureText"]'
SEL_MSG_TIME = 'div[class*="MessageBoxTimetimeLayout"]'
SEL_MSG_SHARE = 'div[class*="MessageItemShareAwemecontainer"]'
SEL_MSG_EMOJI = 'img[class*="MessageItemEmojiimage"]'
SEL_MSG_AVATAR = 'img[class*="avatar"]'

# ── 中文星期映射 ────────────────────────────────────────────────
WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


_HEIF_BRANDS = {b"heic", b"heix", b"mif1", b"msf1", b"hevc", b"hevx", b"heim", b"heis", b"hevm", b"hevs"}
_MP4_BRANDS = {b"mp42", b"mp41", b"isom", b"iso2", b"iso4", b"iso5", b"iso6", b"avc1", b"M4V ", b"qt  "}


def _detect_media_format(data):
    """从字节流头部识别媒体格式，返回 (kind, ext)。

    kind ∈ {'image', 'video', 'heif'}, ext 包含点号。
    HEIF 单独区分出来，因为浏览器不原生支持，下载后需要转 JPEG。
    """
    if data[:3] == b"\xff\xd8\xff":
        return ("image", ".jpg")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ("image", ".png")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("image", ".webp")
    if data[:3] == b"GIF":
        return ("image", ".gif")
    # ISO Base Media (MP4 / HEIF 共用)：bytes 4-8 是 "ftyp"，brand 在 8-12
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in _HEIF_BRANDS:
            return ("heif", ".heic")
        if brand in _MP4_BRANDS:
            return ("video", ".mp4")
        # 未知 brand：保守起见当 mp4 视频处理
        return ("video", ".mp4")
    return ("image", ".jpg")


def _heic_to_jpeg(heic_bytes):
    """将 HEIC 字节流转为 JPEG 字节流（浏览器不原生支持 HEIC）。"""
    import io
    from PIL import Image
    import pillow_heif

    pillow_heif.register_heif_opener()
    im = Image.open(io.BytesIO(heic_bytes))
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    out = io.BytesIO()
    im.save(out, "JPEG", quality=90)
    return out.getvalue()


def _fetch(url, timeout=20):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.douyin.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _conv_subdir(conv_id, peer_name=None):
    """返回会话专属子目录名。

    格式: '<对方昵称>_<uidA尾4位>'（好认 + 防同名冲突）：
      '人℃_1902'（对方昵称=人℃，conv_id 首段 uid 尾4位）
    没有昵称时退化为纯 ID 格式：
      'conv_0_1_62185797183_808834269460910'

    注意: 不同会话的"对方"可能同名（如两个"小羊莓莓"），所以必须带
    uidA（conv_id 第 3 段）尾4位做区分——uidB 是对端 uid 可能相同，
    不能用作唯一区分。
    peer_name 不传时尝试从数据库 conversations.name（已修正为对方昵称）查。
    同一个会话不同次抓取，subdir 相同，可保证新下载落同一目录。
    """
    if not conv_id:
        return ""
    # 区分短 ID：用 conv_id 第 3 段 uid（uidA）尾 4 位
    parts = str(conv_id).split(":")
    short_id = ""
    if len(parts) >= 3 and parts[2] and parts[2].isdigit() and len(parts[2]) >= 4:
        short_id = parts[2][-4:]
    # 名字来源：显式传入 > 数据库 conversations.name > 无
    name = peer_name
    if not name:
        try:
            from extractor.models import get_db
            conn = get_db()
            try:
                row = conn.execute(
                    "SELECT name FROM conversations WHERE conv_id = ?", (str(conv_id),)
                ).fetchone()
                if row and row[0] and str(row[0]).strip():
                    name = str(row[0]).strip()
            finally:
                conn.close()
        except Exception:
            name = None
    if name:
        safe = re.sub(r'[<>:"/\\|?*]+', "_", str(name)).strip().strip("._")
        if not safe:
            safe = "conv"
        if short_id:
            return f"{safe}_{short_id}"
        return f"{safe}"
    safe = re.sub(r'[<>:"/\\|?*]', '_', str(conv_id))
    return f"conv_{safe}"


def _save_voice(url, voice_dir, server_id):
    """下载语音消息音频（urllib 直连，无需浏览器 cookie）。

    Returns 相对路径（如 'voice/xxx.mpeg'）或 None。
    """
    import urllib.request

    os.makedirs(voice_dir, exist_ok=True)
    filename = f"{server_id}.mpeg"
    target = os.path.join(voice_dir, filename)
    rel = f"voice/{filename}"
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return rel
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read()
        if len(data) > 100:
            with open(target, "wb") as f:
                f.write(data)
            return rel
    except Exception:
        pass
    return None


def _save_emoji(url, emoji_dir):
    """下载表情包（普通 PNG/WEBP，无加密）。按 URL 路径哈希去重。

    Returns 相对路径（如 'emoji/abc.png'）或 None。
    """
    import hashlib
    from urllib.parse import urlparse

    path = urlparse(url).path
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".png"
    h = hashlib.md5(path.encode("utf-8")).hexdigest()[:16]
    filename = f"{h}{ext}"
    target = os.path.join(emoji_dir, filename)
    rel = f"emoji/{filename}"
    if os.path.exists(target):
        return rel
    data = _fetch(url)
    if len(data) < 100:
        return None
    with open(target, "wb") as f:
        f.write(data)
    return rel


def _save_image(origin_url, skey_hex, server_id, img_dir, video_dir=None):
    """下载并解密媒体（图片或视频），按实际格式存到对应目录。

    抖音 IM `awe_type=2702/2703/2704` 既可能是图片也可能是视频，
    必须解密后看 magic bytes 才能知道。AES-256-GCM:
    - key = skey hex (32 bytes)
    - IV  = 密文前 12 字节
    - 密文+tag = 剩余部分

    Returns 相对路径（如 'images/xxx.jpg' 或 'videos/xxx.mp4'）或 None。
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if video_dir is None:
        video_dir = os.path.join(os.path.dirname(img_dir), "videos")
    os.makedirs(video_dir, exist_ok=True)

    # 已存在？
    for d, prefix in ((img_dir, "images"), (video_dir, "videos")):
        for ext in (".jpg", ".png", ".webp", ".gif", ".mp4"):
            t = os.path.join(d, f"{server_id}{ext}")
            if os.path.exists(t):
                return f"{prefix}/{server_id}{ext}"

    cipher = _fetch(origin_url)
    if len(cipher) < 28:
        return None
    key = bytes.fromhex(skey_hex)
    iv = cipher[:12]
    body = cipher[12:]
    plain = AESGCM(key).decrypt(iv, body, None)
    kind, ext = _detect_media_format(plain)
    if kind == "heif":
        # 浏览器不支持 HEIC，转为 JPEG
        plain = _heic_to_jpeg(plain)
        kind, ext = "image", ".jpg"
    target_dir = video_dir if kind == "video" else img_dir
    prefix = "videos" if kind == "video" else "images"
    filename = f"{server_id}{ext}"
    with open(os.path.join(target_dir, filename), "wb") as f:
        f.write(plain)
    return f"{prefix}/{filename}"


class WebChatScraper:
    def __init__(self, discovery_mode=False, name_filter=None, incremental=False, download_images=False):
        self.discovery_mode = discovery_mode
        self.name_filter = name_filter
        self.incremental = incremental
        self.download_images = download_images
        self.pw = None
        self.context = None
        self.page = None
        self._db_conn = None  # 持久数据库连接
        self._last_known_timestamp = 0  # 跨批次时间戳继承

    async def _safe_eval(self, js, arg=None, timeout=30, default=None):
        """带超时的 page.evaluate 封装。

        页面主线程被风控冻结 / JS 死循环 / CDP 连接卡住时，原生 evaluate 会
        永久挂起 → 采集卡死。统一在这里加 asyncio.wait_for 硬超时兜底。
        """
        try:
            if arg is None:
                return await asyncio.wait_for(self.page.evaluate(js), timeout=timeout)
            return await asyncio.wait_for(self.page.evaluate(js, arg), timeout=timeout)
        except asyncio.TimeoutError:
            print(f"  [!] evaluate 超时 (> {timeout}s)，跳过")
            return default
        except Exception:
            return default

    async def launch(self):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        init_db()
        self._db_conn = get_db()

        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=os.environ.get("HEADLESS", "false").lower() == "true",
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        print("[+] 浏览器已启动")

        # Fallback: if no sessionid in profile (or sessionid value is empty), try loading from JSON backup
        cookies = await self.context.cookies()
        if not any(c["name"] == "sessionid" and c["value"] for c in cookies):
            import json, os as _os
            backup_file = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "data", "cookies_backup.json")
            if _os.path.exists(backup_file):
                with open(backup_file, "r", encoding="utf-8") as f:
                    backup_cookies = json.load(f)
                if any(c["name"] == "sessionid" for c in backup_cookies):
                    print("[*] 从 JSON 备份加载 cookies...")
                    # Filter to douyin.com cookies only
                    dy_cookies = [c for c in backup_cookies if "douyin.com" in c.get("domain", "") or "byted" in c.get("domain", "")]
                    await self.context.add_cookies(dy_cookies)
                    print(f"[+] 已加载 {len(dy_cookies)} 条 cookies 从备份")
                    # 验证 sessionid 是否真的写进去了
                    after = await self.context.cookies()
                    sess = next((c["value"][:16] + "..." for c in after if c["name"] == "sessionid" and c["value"]), "NOT FOUND")
                    print(f"[*] sessionid 当前值（前16位）: {sess}")

    async def wait_for_login(self):
        await self.page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        print("[*] 正在检测登录状态...")

        for attempt in range(180):
            # Use Playwright cookie API to check HttpOnly cookies too
            cookies = await self.context.cookies("https://www.douyin.com")
            cookie_names = {c["name"] for c in cookies}
            logged_in = "sessionid" in cookie_names
            if logged_in:
                print("[+] 已检测到登录状态")
                return True
            if attempt == 0:
                print("[*] 未检测到登录，请在浏览器中扫码登录... (最多 3 分钟)")
            await asyncio.sleep(1)

        print("[-] 登录超时")
        return False

    async def navigate_to_chat(self):
        print("[*] 正在导航至私信页面...")

        for attempt in range(3):
            try:
                await asyncio.wait_for(
                    self.page.goto(CHAT_URL, wait_until="domcontentloaded"), timeout=30
                )
            except asyncio.TimeoutError:
                print(f"[!] 页面加载超时(>30s)，第 {attempt+1} 次重试...")
                continue
            try:
                await self.page.wait_for_selector(SEL_CONV_ITEM, timeout=20000)
                print(f"[+] 当前页面: {self.page.url}")
                return
            except Exception as e:
                await self._dump_chat_page_diagnostics(reason=str(e)[:80])
                if attempt < 2:
                    print(f"[!] 等待会话列表超时，第 {attempt+1} 次重试...")
                    await asyncio.sleep(3)
                else:
                    print("[!] 等待会话列表超时（已重试 3 次），页面可能未完全加载")

        await asyncio.sleep(1)
        print(f"[+] 当前页面: {self.page.url}")

    async def _dump_chat_page_diagnostics(self, reason: str = ""):
        """打印当前 chat 页的诊断信息，用于排查 selector 命中失败的原因。"""
        try:
            diag = await self._safe_eval("""() => {
                const out = {
                    url: location.href,
                    title: document.title,
                    pathname: location.pathname,
                    has_conv_store: !!window.conversationStore,
                    has_user_store: !!window.userInfoStore,
                    has_im_module: !!window['__VMOK_@pc-im/im:1.0.0.562__'] ||
                                   Object.keys(window).some(k => k.includes('pc-im')),
                    list_wrappers: document.querySelectorAll('div[class*="conversationConversationListwrapper"]').length,
                    item_wrappers: document.querySelectorAll('div[class*="conversationConversationItemwrapper"]').length,
                    body_text_first200: (document.body && document.body.innerText || '').slice(0, 200),
                };
                // Detect login wall / captcha
                out.has_qr = !!document.querySelector('img[src*="qrcode"], canvas[class*="qrcode"], div[class*="qrcode"], div[class*="QrCode"]');
                out.has_captcha = !!document.querySelector('iframe[src*="captcha"], div[class*="captcha"], div[class*="verify"]');
                out.has_login_btn = !!document.querySelector('button[class*="login"], div[class*="login-button"]');
                // Top-level classes of body's first ~10 children to spot rename
                const top = [];
                if (document.body) {
                    for (const c of document.body.children) {
                        if (top.length >= 10) break;
                        top.push((c.className || '').toString().split(/\\s+/).slice(0, 3).join(' '));
                    }
                }
                out.body_top_children_classes = top;
                // Sample any class names that look related so we can spot a rename
                const related = new Set();
                document.querySelectorAll('div[class]').forEach(el => {
                    const cls = el.className;
                    if (typeof cls !== 'string') return;
                    for (const c of cls.split(/\\s+/)) {
                        const lc = c.toLowerCase();
                        if (lc.includes('conversation') || lc.includes('chatlist') || lc.includes('messagelist')) {
                            related.add(c);
                        }
                    }
                });
                out.related_classes = [...related].slice(0, 25);
                return out;
            }""")
        except Exception as e:
            print(f"[!] 诊断失败: {e}")
            return

        if reason:
            print(f"[!] 会话列表未找到 (原因: {reason})")
        print(f"[*] URL: {diag.get('url')}")
        print(f"[*] 标题: {diag.get('title')}")
        print(f"[*] conversationStore={diag.get('has_conv_store')} userInfoStore={diag.get('has_user_store')} IM SDK={diag.get('has_im_module')}")
        print(f"[*] DOM 命中: list={diag.get('list_wrappers')} item={diag.get('item_wrappers')}")
        if diag.get("has_qr"):
            print("[!] 页面有二维码 → 登录态实际无效，请重新扫码或导入新 Cookie")
        if diag.get("has_captcha"):
            print("[!] 页面有验证码/滑块 → 触发了风控，需要人工通过")
        if diag.get("has_login_btn"):
            print("[!] 页面有登录按钮 → 大概率未登录")
        if not diag.get("has_conv_store") and not diag.get("has_im_module"):
            print("[!] IM SDK 完全未加载 → 可能账号无 PC IM 权限，或 JS chunk 被拦截")
        related = diag.get("related_classes") or []
        if related and diag.get("item_wrappers", 0) == 0:
            print(f"[*] 相关类名: {', '.join(related)}")
            print("[*] 如果出现 conversationItemwrapper 之类的新名字，可能是抖音改了类名")
        snippet = (diag.get("body_text_first200") or "").replace("\n", " ").strip()
        if snippet:
            print(f"[*] 正文片段: {snippet}")

    # ── Time Parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_time_label(label: str) -> int:
        """将 DOM 时间标签转为 Unix 秒时间戳。"""
        if not label or not label.strip():
            return 0

        label = label.strip()
        now = datetime.now()

        # "X分钟前"
        m = re.match(r"(\d+)\s*分钟前", label)
        if m:
            return int((now - timedelta(minutes=int(m.group(1)))).timestamp())

        # "X小时前"
        m = re.match(r"(\d+)\s*小时前", label)
        if m:
            return int((now - timedelta(hours=int(m.group(1)))).timestamp())

        # "刚刚"
        if label == "刚刚":
            return int(now.timestamp())

        # "昨天 HH:MM"
        m = re.match(r"昨天\s*(\d{1,2}):(\d{2})", label)
        if m:
            yesterday = now - timedelta(days=1)
            dt = yesterday.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp())

        # "前天 HH:MM"
        m = re.match(r"前天\s*(\d{1,2}):(\d{2})", label)
        if m:
            day = now - timedelta(days=2)
            dt = day.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp())

        # "星期X HH:MM"
        m = re.match(r"星期([一二三四五六日天])\s*(\d{1,2}):(\d{2})", label)
        if m:
            target_wd = WEEKDAY_MAP.get(m.group(1), 0)
            current_wd = now.weekday()
            days_back = (current_wd - target_wd) % 7
            if days_back == 0:
                days_back = 7  # 同一天指上周
            day = now - timedelta(days=days_back)
            dt = day.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
            return int(dt.timestamp())

        # "YYYY/MM/DD HH:MM" or "YYYY/MM/DD"
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", label)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour = int(m.group(4)) if m.group(4) else 0
            minute = int(m.group(5)) if m.group(5) else 0
            try:
                dt = datetime(year, month, day, hour, minute)
                return int(dt.timestamp())
            except ValueError:
                pass

        # "MM/DD HH:MM" or "MM/DD"
        m = re.match(r"(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$", label)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            hour = int(m.group(3)) if m.group(3) else 0
            minute = int(m.group(4)) if m.group(4) else 0
            year = now.year
            try:
                dt = datetime(year, month, day, hour, minute)
                if dt > now:
                    dt = dt.replace(year=year - 1)
                return int(dt.timestamp())
            except ValueError:
                pass

        # "HH:MM" (今天)
        m = re.match(r"^(\d{1,2}):(\d{2})$", label)
        if m:
            dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp())

        # 无法解析
        return 0

    # ── Discovery ──────────────────────────────────────────────────

    async def run_discovery(self, duration=60):
        print(f"\n{'='*60}")
        print(f"  发现模式 — 分析 DOM 结构 ({duration}s)")
        print(f"{'='*60}\n")

        await self.navigate_to_chat()
        await asyncio.sleep(2)

        dom_info = await self._dump_dom_structure()
        debug_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        filepath = os.path.join(debug_dir, f"dom_structure_{int(time.time()*1000)}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(dom_info, f, ensure_ascii=False, indent=2)

        print(f"\n[*] 监听 {duration} 秒，请在浏览器中操作...")
        for i in range(duration):
            await asyncio.sleep(1)
            if i > 0 and i % 15 == 0:
                print(f"  [{i}/{duration}s]")
                await self._dump_dom_structure()

    async def _dump_dom_structure(self):
        dom_info = await self._safe_eval("""() => {
            const result = { title: document.title, url: location.href, im_elements: {}, conv_containers: [], msg_containers: [] };
            document.querySelectorAll('*').forEach(el => {
                const cls = typeof el.className === 'string' ? el.className : '';
                const lower = cls.toLowerCase();
                if (lower.match(/session|conversation|chat|message|im-|inbox|msg|bubble/)) {
                    const key = el.tagName.toLowerCase() + '.' + cls.split(' ')[0]?.substring(0, 40);
                    if (!result.im_elements[key]) result.im_elements[key] = { count: 0, sample_text: '', class: cls, children: 0 };
                    result.im_elements[key].count++;
                    if (!result.im_elements[key].sample_text) result.im_elements[key].sample_text = el.textContent?.trim().substring(0, 80) || '';
                    result.im_elements[key].children = Math.max(result.im_elements[key].children, el.children.length);
                }
            });
            return result;
        }""")

        print(f"  [DOM] IM 元素类型: {len(dom_info.get('im_elements', {}))}")
        for key, info in sorted(dom_info.get("im_elements", {}).items()):
            if info["count"] >= 2:
                print(f"    {key} x{info['count']}  text: {info['sample_text'][:50]}")
        return dom_info

    # ── Extraction ─────────────────────────────────────────────────

    async def extract_all(self):
        await self.navigate_to_chat()
        await asyncio.sleep(2)

        print("[*] 正在加载会话列表...")
        conversations = await self._load_all_conversations()
        print(f"[+] 共发现 {len(conversations)} 个会话")

        if not conversations:
            print("[-] 未找到会话")
            # Save debug screenshot
            debug_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "debug_no_conv.png")
            try:
                await self.page.screenshot(path=debug_path)
                print(f"[*] 调试截图已保存: {debug_path}")
            except Exception:
                pass
            return

        if self.name_filter:
            # Support comma-separated multiple filters
            filter_parts = [f.strip() for f in self.name_filter.split(",") if f.strip()]
            filtered = [c for c in conversations
                        if any(fp in c.get("nickname", "") or fp in c["name"] for fp in filter_parts)]
            print(f"[*] 过滤后: {len(filtered)} 个会话匹配 \"{self.name_filter}\"")
            if not filtered:
                print(f"[-] 没有匹配的会话。全部会话名称:")
                for c in conversations:
                    print(f"    - {c.get('nickname', '')} ({c['name']})")
                return
            conversations = filtered

        for i, conv in enumerate(conversations):
            display_name = conv.get("nickname") or conv["name"]
            print(f"\n[{i+1}/{len(conversations)}] {display_name} (最后活跃: {conv['time']})")
            try:
                # 单会话兜底超时（默认 25 分钟）：即使个别环节意外挂起，也不能
                # 拖死整个采集进程（否则面板状态永远停在"进行中"）。
                await asyncio.wait_for(self._extract_conversation(i, conv), timeout=25 * 60)
            except asyncio.TimeoutError:
                print(f"  [!] 会话「{display_name}」处理超时 (>25 分钟)，跳过继续下一个")
            except Exception as e:
                print(f"  [!] 错误: {e}")
                import traceback
                traceback.print_exc()
            await asyncio.sleep(0.5 + random.random())

        conn = self._db_conn
        stats = {
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        }

        print(f"\n{'='*60}")
        print(f"  提取完成!")
        print(f"  会话: {stats['conversations']}")
        print(f"  消息: {stats['messages']}")
        print(f"  用户: {stats['users']}")
        print(f"{'='*60}")

    async def list_conversations(self):
        """Navigate to chat and return all discovered conversations (no extraction).

        Used by the control panel's "refresh conversation list" action so the
        user can pick which conversations to scrape/export.
        """
        await self.navigate_to_chat()
        await asyncio.sleep(2)

        print("[*] 正在加载会话列表...")
        conversations = await self._load_all_conversations()
        print(f"[+] 共发现 {len(conversations)} 个会话")
        return conversations

    async def _load_all_conversations(self):
        """Scroll the conversation list and accumulate all items with dedup.

        抖音会话列表是"虚拟滚动 + 滚动到底懒加载更多"：滚到底后 scrollHeight 会
        继续增长（加载下一批），若在增长前判定"到底"就会漏掉后续项（曾连续只扫出
        45 个）。策略：
          1. 小步滚动（300px ≈ 4 项），给虚拟滚动渲染留足余量，避免中间项从未进 DOM；
          2. 滚动到底后等待加载稳定（sleep + 复查 scrollHeight），确认真正到底；
          3. 多遍扫描直到某遍零新增才收敛（最多 4 遍），兜底任何遗漏。
        """
        # 先滚到顶部，保证从头开始收集
        await self._safe_eval(f"""() => {{
            const list = document.querySelector('{SEL_CONV_LIST}');
            if (list) {{
                const scrollable = list.querySelector('[style*="overflow"]') || list;
                scrollable.scrollTop = 0;
            }}
        }}""")
        await asyncio.sleep(0.6)

        seen = {}  # key -> conv info (保持插入顺序 = 列表自上而下)

        def _scroll_js(delta):
            return f"""() => {{
                const list = document.querySelector('{SEL_CONV_LIST}');
                if (!list) return true;
                const scrollable = list.querySelector('[style*="overflow"]') || list;
                const before = scrollable.scrollTop;
                const max = scrollable.scrollHeight - scrollable.clientHeight;
                scrollable.scrollTop += {delta};
                return {{
                    moved: scrollable.scrollTop !== before,
                    at_bottom: max <= 0 || scrollable.scrollTop >= max - 2,
                }};
            }}"""

        def _snapshot():
            return self._safe_eval(f"""() => {{
                const items = document.querySelectorAll('{SEL_CONV_ITEM}');
                return Array.from(items).map(el => {{
                    const titleEl = el.querySelector('{SEL_CONV_TITLE}');
                    const timeEl = el.querySelector('{SEL_CONV_TIME}');
                    const previewEl = el.querySelector('{SEL_CONV_PREVIEW}');
                    let nickname = '';
                    if (titleEl) {{
                        const innerTitle = titleEl.querySelector('div[class*="conversationConversationItemtitle"]');
                        nickname = (innerTitle && innerTitle !== titleEl)
                            ? innerTitle.textContent.trim()
                            : titleEl.childNodes[0]?.textContent?.trim() || '';
                    }}
                    return {{
                        name: titleEl ? titleEl.textContent.trim() : '',
                        nickname: nickname,
                        time: timeEl ? timeEl.textContent.trim() : '',
                        preview: previewEl ? previewEl.textContent.trim() : '',
                    }};
                }});
            }}""")

        def _absorb(convs):
            added = 0
            for c in convs:
                key = c.get("nickname") or c.get("name")
                if key and key not in seen:
                    seen[key] = c
                    added += 1
            return added

        # ── 多遍扫描：每遍从顶到底；某遍零新增 → 收敛 ──
        MAX_PASSES = 4
        for pass_no in range(1, MAX_PASSES + 1):
            # 每遍开始都回顶部，保证覆盖
            await self._safe_eval(f"""() => {{
                const list = document.querySelector('{SEL_CONV_LIST}');
                if (list) {{
                    const scrollable = list.querySelector('[style*="overflow"]') || list;
                    scrollable.scrollTop = 0;
                }}
            }}""")
            await asyncio.sleep(0.5)

            pass_added = 0
            stable = 0
            for _ in range(200):
                convs = await _snapshot()
                added = _absorb(convs)
                pass_added += added
                if added:
                    stable = 0
                    print(f"  已加载 {len(seen)} 个会话...")
                else:
                    stable += 1

                st = await self._safe_eval(_scroll_js(300))

                if st.get("at_bottom"):
                    if stable >= 2:
                        # 到底了：等 1s 让"加载更多"完成（scrollHeight 可能增长），
                        # 复查一次；若新增则继续滚，否则本遍结束。
                        await asyncio.sleep(1.0)
                        convs2 = await _snapshot()
                        added2 = _absorb(convs2)
                        if added2:
                            pass_added += added2
                            stable = 0
                            print(f"  到底后加载更多，补充到 {len(seen)} 个...")
                            continue
                        break
                else:
                    # 未到底但多轮无新增（异常），给次机会后退出本遍，防死循环
                    if stable >= 5:
                        print(f"  [!] 未到底但连续 5 轮无新增，提前结束本遍")
                        break

                await asyncio.sleep(0.35)

            print(f"  [*] 第 {pass_no} 遍扫描完成：共 {len(seen)} 个会话 (本遍新增 {pass_added})")
            if pass_added == 0:
                break  # 收敛：两遍结果一致

        # 回到顶部，后续点击流程从熟悉的起点开始
        await self._safe_eval(f"""() => {{
            const list = document.querySelector('{SEL_CONV_LIST}');
            if (list) {{
                const scrollable = list.querySelector('[style*="overflow"]') || list;
                scrollable.scrollTop = 0;
            }}
        }}""")
        await asyncio.sleep(0.5)

        all_convs = list(seen.values())
        for c in all_convs:
            c["name"] = c["name"].replace('\xa0', ' ').strip()
            c["nickname"] = c.get("nickname", "").replace('\xa0', ' ').strip()

        return all_convs

    async def _ensure_conv_list_loaded(self):
        """Wait for conversation list to load.

        On a freshly loaded chat page, the list renders at the top by default,
        so we don't scroll here — _find_and_click_conversation handles scrolling
        as needed. Scrolling unnecessarily can race with virtual-scroll
        re-renders and break subsequent clicks.
        """
        try:
            await self.page.wait_for_selector(SEL_CONV_ITEM, timeout=20000)
        except Exception:
            return 0
        await asyncio.sleep(1)
        count = await self._safe_eval(f"""() =>
            document.querySelectorAll('{SEL_CONV_ITEM}').length
        """)
        return count

    async def _find_and_click_conversation(self, target_name):
        """Find a conversation by name and click it.

        JS does the matching (with whitespace/nbsp normalization, so Windows
        vs. Linux discrepancies don't break `in` checks), but the ACTUAL
        click uses Playwright's element handle — JS `.click()` only fires a
        `click` event, while React listens for `pointerdown`/`mousedown`,
        so a JS click was identified but wouldn't activate the conversation.
        """
        async def _try_match():
            """Return (matched_index, matched_text, debug_names) or (-1, '', names)."""
            result = await self._safe_eval(f"""(targetName) => {{
                const normalize = s => s.replace(/[\\s\\u00a0]+/g, ' ').trim();
                const target = normalize(targetName);
                const items = document.querySelectorAll('{SEL_CONV_ITEM}');
                const debugNames = [];

                for (let i = 0; i < items.length; i++) {{
                    const item = items[i];
                    const titleEl = item.querySelector('{SEL_CONV_TITLE}');
                    if (!titleEl) {{ debugNames.push(''); continue; }}

                    const innerTitle = titleEl.querySelector('{SEL_CONV_TITLE}');
                    let nickname = '';
                    if (innerTitle) {{
                        nickname = normalize(innerTitle.textContent);
                    }} else {{
                        for (const node of titleEl.childNodes) {{
                            const t = node.textContent?.trim();
                            if (t) {{ nickname = normalize(t); break; }}
                        }}
                    }}

                    const fullText = normalize(titleEl.textContent);
                    debugNames.push(nickname || fullText.substring(0, 20));

                    if (nickname === target ||
                        (nickname && target.includes(nickname)) ||
                        (nickname && nickname.includes(target)) ||
                        fullText.includes(target)) {{
                        return {{index: i, text: nickname || fullText, names: debugNames}};
                    }}
                }}

                return {{index: -1, text: '', names: debugNames}};
            }}""", target_name)
            return result

        async def _click_index(idx, text):
            items = await self.page.query_selector_all(SEL_CONV_ITEM)
            if idx < len(items):
                await items[idx].click()
                return {"found": True, "text": text}
            return None

        # First attempt: match current DOM (don't disturb scroll state)
        m = await _try_match()
        if m["index"] >= 0:
            clicked = await _click_index(m["index"], m["text"])
            if clicked:
                return clicked

        all_debug_names = list(m.get("names", []))

        # Not found in current view; scroll from top, incrementally.
        await self._safe_eval(f"""() => {{
            const list = document.querySelector('{SEL_CONV_LIST}');
            if (list) {{
                const scrollable = list.querySelector('[style*="overflow"]') || list;
                scrollable.scrollTop = 0;
            }}
        }}""")
        await asyncio.sleep(0.5)

        for _ in range(30):
            m = await _try_match()
            if m["index"] >= 0:
                clicked = await _click_index(m["index"], m["text"])
                if clicked:
                    return clicked

            for n in m.get("names", []):
                if n and n not in all_debug_names:
                    all_debug_names.append(n)

            reached_bottom = await self._safe_eval(f"""() => {{
                const list = document.querySelector('{SEL_CONV_LIST}');
                if (!list) return true;
                const scrollable = list.querySelector('[style*="overflow"]') || list;
                const before = scrollable.scrollTop;
                scrollable.scrollTop += 300;
                return scrollable.scrollTop === before;
            }}""")
            await asyncio.sleep(0.4)
            if reached_bottom:
                # 到底：等 1s 让懒加载更多完成，再复查一轮，防漏目标会话
                await asyncio.sleep(1.0)
                m = await _try_match()
                if m["index"] >= 0:
                    clicked = await _click_index(m["index"], m["text"])
                    if clicked:
                        return clicked
                break

        return {"found": False, "count": len(all_debug_names), "names": all_debug_names[:20]}

    async def _download_voice_files(self, messages, conv_id=None):
        """下载语音消息的音频文件到本地

        conv_id: 传入则把语音放到 data/media/conv_<id>/voice/ 子目录；
                 传入 None 退化为 data/media/voice/（兼容老代码路径）
        """
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        if conv_id:
            media_root = os.path.join(media_root, _conv_subdir(conv_id))
        voice_dir = os.path.join(media_root, "voice")
        os.makedirs(voice_dir, exist_ok=True)

        voice_msgs = []
        for m in messages:
            if m.get("msg_type") != "other":
                continue
            cj_str = m.get("content_json", "")
            if not cj_str or "resource_url" not in cj_str:
                continue
            try:
                cj = json.loads(cj_str)
                if cj.get("resource_url") and cj.get("duration"):
                    urls = cj["resource_url"].get("url_list", [])
                    if urls:
                        voice_msgs.append((m, urls[0], cj.get("duration", 0)))
            except (json.JSONDecodeError, KeyError):
                continue

        if not voice_msgs:
            return

        # 批量下载（通过浏览器 fetch 以携带 cookie）
        for m, url, duration in voice_msgs:
            server_id = m.get("server_id", "unknown")
            filename = f"{server_id}.mpeg"
            local_path = os.path.join(voice_dir, filename)
            rel_path = f"voice/{filename}"

            if os.path.exists(local_path):
                m["local_path"] = rel_path
                continue

            try:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                if len(data) > 100:
                    with open(local_path, "wb") as f:
                        f.write(data)
                    m["local_path"] = rel_path
                    dur_sec = round(duration / 1000)
                    print(f"  [voice] 已下载语音 {dur_sec}s: {filename}")
                else:
                    print(f"  [voice] 下载失败（空响应 {len(data)}B）: {server_id}")
            except Exception as e:
                print(f"  [voice] 下载失败: {server_id}: {e}")

    async def _download_image_files(self, messages, conv_id=None):
        """下载图片/表情包到本地。
        - 表情：直接保存（无加密），按 URL 路径哈希去重
        - 图片：从 origin_url 拉密文，AES-256-GCM 解密 (skey) 后保存

        conv_id: 传入则放到 data/media/conv_<id>/{images,emoji}/；None 走老路径
        """
        if not self.download_images:
            return
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        if conv_id:
            media_root = os.path.join(media_root, _conv_subdir(conv_id))
        img_dir = os.path.join(media_root, "images")
        emoji_dir = os.path.join(media_root, "emoji")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(emoji_dir, exist_ok=True)

        ok, fail = 0, 0
        for m in messages:
            mt = m.get("msg_type")
            if mt == "emoji":
                url = m.get("image_src")
                if not url:
                    continue
                try:
                    rel = _save_emoji(url, emoji_dir)
                    if rel:
                        m["local_path"] = rel; ok += 1
                except Exception as e:
                    fail += 1
                    print(f"  [media] emoji 失败: {e}")
            elif mt == "image":
                try:
                    cj = json.loads(m.get("content_json", "") or "{}")
                    ru = cj.get("resource_url") or {}
                    skey = ru.get("skey")
                    origin = (ru.get("origin_url_list") or [None])[0]
                    if not (skey and origin):
                        continue
                    rel = _save_image(origin, skey, m.get("server_id", "unknown"), img_dir)
                    if rel:
                        m["local_path"] = rel; ok += 1
                except Exception as e:
                    fail += 1
                    print(f"  [media] image 失败: {e}")
            elif mt == "video":
                # awe_type=0 的视频：只下载 poster（封面图）。真正的视频文件需要
                # 反查 vid→URL 才能拿，留待后续；poster 用 poster.skey 解密。
                try:
                    cj = json.loads(m.get("content_json", "") or "{}")
                    poster = cj.get("poster") or {}
                    skey = poster.get("skey")
                    origin = (poster.get("origin_url_list") or [None])[0]
                    if not (skey and origin):
                        continue
                    rel = _save_image(origin, skey, m.get("server_id", "unknown"), img_dir)
                    if rel:
                        m["local_path"] = rel; ok += 1
                except Exception as e:
                    fail += 1
                    print(f"  [media] video 封面失败: {e}")
        if ok or fail:
            print(f"  [media] 图片/表情/视频封面 已下载 {ok} 个 (失败 {fail})")

    async def _download_all_media_parallel(self, conv_id, max_concurrent=10):
        """全量并发下载一个会话的所有媒体文件。

        性能优化: 之前每个 batch 调一次 _download_voice_files/_download_image_files，
        1000 条 batch × 50 条媒体 × 1-3s/条 = 几十秒到几分钟。改为抓完所有消息后，
        一次性收集所有待下载项，用 asyncio.Semaphore 限流并发，可提速 5-10 倍。

        媒体按会话分子目录: data/media/conv_<safe_id>/{voice,images,emoji}/
        老文件保持原位不动。
        """
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        if conv_id:
            media_root = os.path.join(media_root, _conv_subdir(conv_id))
        voice_dir = os.path.join(media_root, "voice")
        img_dir = os.path.join(media_root, "images")
        emoji_dir = os.path.join(media_root, "emoji")
        for d in (voice_dir, img_dir, emoji_dir):
            os.makedirs(d, exist_ok=True)

        conn = self._db_conn
        # 查所有还没下载的 (msg_id, raw_data, msg_type)
        rows = conn.execute(
            "SELECT msg_id, raw_data, msg_type, media_local_path FROM messages "
            "WHERE conv_id = ?",
            (conv_id,),
        ).fetchall()

        voice_tasks = []   # (server_id, url)
        image_tasks = []   # (server_id, origin, skey)
        emoji_tasks = []   # (server_id, url)

        media_base = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        for server_id, cj_str, msg_type, local_path in rows:
            if not cj_str:
                continue
            # 跳过已经下载过的（local_path 非空且文件存在）
            # 注意: local_path 是相对 data/media 的路径（含 conv_ 前缀），
            # 直接拼 media_base，不能拼 media_root（已含 conv 子目录，会双重嵌套）。
            if local_path and not local_path.startswith("http"):
                full = os.path.join(media_base, local_path.replace("/", os.sep))
                if os.path.exists(full):
                    continue
            try:
                cj = json.loads(cj_str)
            except Exception:
                continue

            # msg_type 是 INTEGER: 0=other(语音), 2=emoji, 3=image
            if msg_type in (0, "0", "other"):
                # 语音消息: msg_type=other 但 cj 有 resource_url + duration
                # 语音用 urllib 直连下载（不依赖浏览器 fetch，更可靠）
                ru = cj.get("resource_url") or {}
                if ru.get("url_list") and cj.get("duration"):
                    voice_tasks.append((server_id, ru["url_list"][0]))
            elif msg_type in (3, "3", "image"):
                ru = cj.get("resource_url") or {}
                skey = ru.get("skey")
                origin = (ru.get("origin_url_list") or [None])[0]
                if skey and origin:
                    image_tasks.append((server_id, origin, skey))
            elif msg_type in (2, "2", "emoji"):
                url_obj = cj.get("url")
                if isinstance(url_obj, dict):
                    ul = url_obj.get("url_list", [])
                    if ul and isinstance(ul[0], str):
                        emoji_tasks.append((server_id, ul[0]))

        total = len(voice_tasks) + len(image_tasks) + len(emoji_tasks)
        if total == 0:
            print(f"  [media] 没有待下载的媒体")
            return

        print(f"  [media] 全量并发下载 {total} 个文件 (语音 {len(voice_tasks)} / 图片 {len(image_tasks)} / 表情 {len(emoji_tasks)})，并发={max_concurrent}")
        start = time.time()

        # 语音：用 urllib 直连下载（不依赖浏览器 fetch，cookie 无关更可靠）
        voice_ok = voice_fail = 0
        if voice_tasks:
            loop = asyncio.get_event_loop()
            v_sem = asyncio.Semaphore(max_concurrent)

            async def _voice_one(sid, url):
                nonlocal voice_ok, voice_fail
                async with v_sem:
                    rel = await loop.run_in_executor(None, _save_voice, url, voice_dir, sid)
                    if rel:
                        rel_full = os.path.join(_conv_subdir(conv_id), rel).replace("\\", "/")
                        conn.execute(
                            "UPDATE messages SET media_local_path = ? WHERE msg_id = ?",
                            (rel_full, sid),
                        )
                        voice_ok += 1
                    else:
                        voice_fail += 1

            await asyncio.gather(*(_voice_one(s, u) for s, u in voice_tasks), return_exceptions=True)
            conn.commit()
            print(f"  [media] 语音下载: {voice_ok} 成功, {voice_fail} 失败")

        # 统一任务列表: (kind, sid, url, skey)
        # 性能优化: 之前每个文件一次 page.evaluate + Array.from 传回百万级数组，
        # CDP 序列化开销巨大。改为按批(10个)一次 evaluate，JS 端并发 fetch，
        # base64 传回（比数组小 ~4 倍且无逐元素序列化），Python 端解码落盘。
        # 注意: 语音(voice)不在此列表——用 urllib 直连（_save_voice）更可靠。
        all_tasks = (
            [("emoji", sid, url, None) for sid, url in emoji_tasks] +
            [("image", sid, origin, skey) for sid, origin, skey in image_tasks]
        )

        done_counter = [0]
        failed_counter = [0]

        async def _fetch_batch(items):
            """一次 evaluate 并发下载一批 URL，返回 (b64data, None) 列表。

            每个 URL 的 fetch 都带 30s AbortController 超时（否则某个 CDN 连接
            永久挂起 → evaluate 永不返回 → 整个采集卡死）；外层再套 60s 硬超时。
            """
            urls = [it[2] for it in items]
            return await asyncio.wait_for(self.page.evaluate("""async (urls) => {
                const out = new Array(urls.length).fill(null);
                await Promise.all(urls.map(async (u, idx) => {
                    const ctrl = new AbortController();
                    const timer = setTimeout(() => ctrl.abort(), 30000);
                    try {
                        const r = await fetch(u, {credentials: 'include', signal: ctrl.signal});
                        if (!r.ok) return;
                        const buf = await r.arrayBuffer();
                        // chunk 拼接避免大文件 String.fromCharCode 爆栈
                        const bytes = new Uint8Array(buf);
                        let bin = '';
                        const CHUNK = 0x8000;
                        for (let i = 0; i < bytes.length; i += CHUNK) {
                            bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
                        }
                        out[idx] = btoa(bin);
                    } catch (e) { out[idx] = null; }
                    finally { clearTimeout(timer); }
                }));
                return out;
            }""", urls), timeout=60)

        async def _process_batch(items):
            b64s = await _fetch_batch(items)
            for (kind, sid, url, skey), b64 in zip(items, b64s):
                try:
                    if not b64:
                        failed_counter[0] += 1
                        continue
                    raw = base64.b64decode(b64)
                    if kind == "voice":
                        if len(raw) <= 100:
                            failed_counter[0] += 1
                            continue
                        local_path = os.path.join(voice_dir, f"{sid}.mpeg")
                        with open(local_path, "wb") as f:
                            f.write(raw)
                        rel = os.path.join(_conv_subdir(conv_id), "voice", f"{sid}.mpeg").replace("\\", "/")
                        conn.execute("UPDATE messages SET media_local_path = ? WHERE msg_id = ?", (rel, sid))
                    elif kind == "emoji":
                        if len(raw) <= 50:
                            failed_counter[0] += 1
                            continue
                        from hashlib import md5
                        h = md5(url.encode()).hexdigest()[:16]
                        ext = ".webp" if ".webp" in url.lower() else (".gif" if ".gif" in url.lower() else ".png")
                        local_path = os.path.join(emoji_dir, f"{h}{ext}")
                        with open(local_path, "wb") as f:
                            f.write(raw)
                        rel = os.path.join(_conv_subdir(conv_id), "emoji", f"{h}{ext}").replace("\\", "/")
                        conn.execute("UPDATE messages SET media_local_path = ? WHERE msg_id = ?", (rel, sid))
                    else:  # image: AES-256-GCM 解密
                        if len(raw) < 16:
                            failed_counter[0] += 1
                            continue
                        from Crypto.Cipher import AES
                        b = raw
                        raw_key = skey.encode() if isinstance(skey, str) else skey
                        if len(raw_key) < 32:
                            raw_key = (raw_key + b"0" * 32)[:32]
                        tag = b[-16:]
                        ciphertext = b[12:-16]
                        iv = b[:12]
                        cipher = AES.new(raw_key[:32], AES.MODE_GCM, nonce=iv)
                        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
                        local_path = os.path.join(img_dir, f"{sid}.jpg")
                        with open(local_path, "wb") as f:
                            f.write(plaintext)
                        rel = os.path.join(_conv_subdir(conv_id), "images", f"{sid}.jpg").replace("\\", "/")
                        conn.execute("UPDATE messages SET media_local_path = ? WHERE msg_id = ?", (rel, sid))
                except Exception:
                    failed_counter[0] += 1
                done_counter[0] += 1
                if done_counter[0] % 50 == 0:
                    print(f"  [media] 进度 {done_counter[0]}/{total} (失败 {failed_counter[0]})")

        # 分批：每批 10 个 URL 一次 evaluate；批次间并发（受 sem 限制）
        BATCH = 10
        sem = asyncio.Semaphore(max(1, max_concurrent // BATCH))
        batches = [all_tasks[i:i + BATCH] for i in range(0, len(all_tasks), BATCH)]

        async def _run_batch(items):
            async with sem:
                try:
                    await _process_batch(items)
                except Exception as e:
                    print(f"  [media] 批次失败: {e}")
                    failed_counter[0] += len(items)
                    done_counter[0] += len(items)

        await asyncio.gather(*(_run_batch(b) for b in batches), return_exceptions=True)
        conn.commit()
        elapsed = time.time() - start
        print(f"  [media] 全量下载完成: {voice_ok + (len(all_tasks) - failed_counter[0])}/{total} 成功, "
              f"失败 {voice_fail + failed_counter[0]}, 耗时 {elapsed:.1f}s")

    async def _extract_and_save_user_info(self, conv_id):
        """从 userInfoStore 提取用户信息（昵称、头像、unique_id），下载头像到本地。"""
        # 纯 JS evaluate 也带 15s 超时：页面主线程卡死时 evaluate 可能永不返回
        try:
            users = await asyncio.wait_for(self.page.evaluate("""() => {
                const result = [];
                try {
                    const uis = window.userInfoStore;
                    if (!uis) return result;

                    // 当前登录用户
                    const me = uis.curLoginUserInfo;
                    if (me) {
                        result.push({
                            uid: String(me.uid || ''),
                            nickname: me.nickname || '',
                            unique_id: me.uniqueId || '',
                            avatar_url: me.avatarUrl || me.avatar300Url || '',
                        });
                    }

                    // usersInfoMap (MobX observable)
                    const uim = uis.usersInfoMap;
                    if (uim && uim.data_) {
                        for (const [k, v] of uim.data_.entries()) {
                            const u = v.value_ || v;
                            if (!u || !u.nickname) continue;
                            let avatarUrl = '';
                            if (u.avatar_thumb && u.avatar_thumb.url_list && u.avatar_thumb.url_list.length > 0) {
                                avatarUrl = u.avatar_thumb.url_list[0];
                            }
                            result.push({
                                uid: String(u.uid || k),
                                nickname: u.nickname || '',
                                unique_id: u.unique_id || '',
                                avatar_url: avatarUrl,
                            });
                        }
                    }
                } catch(e) {}
                return result;
            }"""), timeout=15)
        except asyncio.TimeoutError:
            print(f"  [!] 读取 userInfoStore 超时 (>15s)，跳过用户信息提取")
            return

        if not users:
            print(f"  [*] 未能从 userInfoStore 获取用户信息")
            return

        saved = await self._save_users(users)
        print(f"  [*] 已保存 {saved} 个用户信息")

    async def _save_users(self, users):
        """下载头像到本地并落库。users: [{uid, nickname, unique_id, avatar_url}]。

        性能优化：头像下载改为并发（Semaphore=8），且单头像 15s 硬超时——
        之前串行 + 40s 超时，遇网络差时 N 个头像卡 N×40s（曾有 3 分钟无输出）。
        """
        avatar_dir = paths.AVATARS_DIR
        os.makedirs(avatar_dir, exist_ok=True)

        conn = self._db_conn
        saved = 0
        avatar_tasks = []
        for u in users:
            uid = u.get("uid", "")
            if not uid:
                continue

            nickname = u.get("nickname", "")
            unique_id = u.get("unique_id", "")
            avatar_url = u.get("avatar_url", "")

            local_avatar = None
            if avatar_url:
                ext = "jpg"
                if ".webp" in avatar_url:
                    ext = "webp"
                elif ".png" in avatar_url:
                    ext = "png"
                local_path = os.path.join(avatar_dir, f"{uid}.{ext}")
                if not os.path.exists(local_path):
                    avatar_tasks.append((u, uid, nickname, unique_id, avatar_url, local_path, ext))
                else:
                    local_avatar = f"avatars/{uid}.{ext}"

            upsert_user(conn, uid, nickname=nickname,
                        avatar_url=local_avatar or avatar_url,
                        unique_id=unique_id)
            saved += 1

        # 并发下载头像（最多 8 个同时），单头像 15s 硬超时，失败静默跳过
        if avatar_tasks:
            sem = asyncio.Semaphore(8)

            async def _dl_one(task):
                u, uid, nickname, unique_id, avatar_url, local_path, ext = task
                async with sem:
                    try:
                        resp = await asyncio.wait_for(self.page.evaluate("""async (url) => {
                            const ctrl = new AbortController();
                            const timer = setTimeout(() => ctrl.abort(), 10000);
                            try {
                                const r = await fetch(url, {credentials: 'include', signal: ctrl.signal});
                                if (!r.ok) return null;
                                const buf = await r.arrayBuffer();
                                return Array.from(new Uint8Array(buf));
                            } catch { return null; }
                            finally { clearTimeout(timer); }
                        }""", avatar_url), timeout=15)
                        if resp and len(resp) > 100:
                            with open(local_path, "wb") as f:
                                f.write(bytes(resp))
                            rel = f"avatars/{uid}.{ext}"
                            conn.execute(
                                "UPDATE users SET avatar_url = ? WHERE uid = ?", (rel, uid)
                            )
                            print(f"  [*] 已保存头像: {nickname} ({uid})")
                    except Exception as e:
                        print(f"  [!] 下载头像失败 {nickname}: {e}")

            await asyncio.gather(*(_dl_one(t) for t in avatar_tasks), return_exceptions=True)

        conn.commit()
        return saved

    async def _resolve_sender_identities(self, sec_by_uid):
        """按 sec_uid 批量补全发送者昵称/头像。

        `userInfoStore` 只缓存 SDK 渲染过的用户——单聊够用，群聊远远不够：纯 API
        模式压根不渲染历史消息，绝大多数群成员因此从来没进过 users 表，前端只能
        回退成会话名，于是所有人都显示成群名（issue #24）。

        消息 protobuf 的 field 14 是该条消息发送者的 sec_uid，拿它调 IM 的用户信息
        接口即可补全。接口只认 cookies（不需要 msToken/a_bogus），sec_user_ids 是
        JSON 数组，一次可查多个。
        """
        known = self._known_user_uids()
        pending = {uid: sec for uid, sec in sec_by_uid.items() if uid not in known}
        if not pending:
            return

        print(f"  [*] 补全 {len(pending)} 个发送者的昵称/头像...")
        sec_list = list(pending.values())
        total = 0
        for i in range(0, len(sec_list), BATCH_USER_INFO):
            batch = sec_list[i:i + BATCH_USER_INFO]
            try:
                users = await asyncio.wait_for(self.page.evaluate("""async (args) => {
                    const [api, secs] = args;
                    const ctrl = new AbortController();
                    const timer = setTimeout(() => ctrl.abort(), 30000);
                    try {
                        const r = await fetch(api, {
                            method: 'POST',
                            credentials: 'include',
                            headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                            body: 'sec_user_ids=' + encodeURIComponent(JSON.stringify(secs)),
                            signal: ctrl.signal,
                        });
                        if (!r.ok) return [];
                        const j = await r.json();
                        return (j.data || []).map(u => ({
                            uid: String(u.uid || ''),
                            nickname: u.nickname || '',
                            unique_id: u.unique_id || '',
                            avatar_url: (u.avatar_thumb && u.avatar_thumb.url_list
                                         && u.avatar_thumb.url_list[0]) || '',
                        })).filter(u => u.uid && u.nickname);
                    } finally { clearTimeout(timer); }
                }""", [USER_INFO_API, batch]), timeout=40)
            except Exception as e:
                print(f"  [!] 补全发送者失败 (batch {i // BATCH_USER_INFO + 1}): {e}")
                continue
            if users:
                total += await self._save_users(users)
            await asyncio.sleep(0.3)

        print(f"  [*] 已补全 {total}/{len(pending)} 个发送者信息")

    def _known_user_uids(self):
        """users 表里已有昵称的 uid（没昵称的等同于没有，需要补全）。"""
        return {
            row[0] for row in self._db_conn.execute(
                "SELECT uid FROM users WHERE nickname IS NOT NULL AND nickname != ''"
            )
        }

    async def _extract_and_save_conv_avatar(self, conv_id):
        """从当前激活会话的列表项 DOM 抓取头像，下载到本地，写入会话表。"""
        try:
            avatar_url = await asyncio.wait_for(self.page.evaluate(f"""() => {{
                const active = document.querySelector('{SEL_CONV_ITEM}[class*="curConversation"]');
                if (!active) return '';
                const img = active.querySelector('img');
                return img ? img.src : '';
            }}"""), timeout=10)
        except asyncio.TimeoutError:
            print(f"  [!] 读取会话头像 URL 超时 (>10s)，跳过")
            return
        if not avatar_url or not avatar_url.startswith('http'):
            return

        avatar_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media", "avatars")
        os.makedirs(avatar_dir, exist_ok=True)

        ext = "jpg"
        if ".webp" in avatar_url:
            ext = "webp"
        elif ".png" in avatar_url:
            ext = "png"
        safe_id = conv_id.replace(':', '_').replace('/', '_')
        filename = f"conv_{safe_id}.{ext}"
        local_path = os.path.join(avatar_dir, filename)

        try:
            resp = await asyncio.wait_for(self.page.evaluate("""async (url) => {
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 30000);
                try {
                    const r = await fetch(url, {signal: ctrl.signal});
                    if (!r.ok) return null;
                    const buf = await r.arrayBuffer();
                    return Array.from(new Uint8Array(buf));
                } catch { return null; }
                finally { clearTimeout(timer); }
            }""", avatar_url), timeout=40)
            if resp and len(resp) > 100:
                with open(local_path, "wb") as f:
                    f.write(bytes(resp))
                rel_path = f"avatars/{filename}"
                self._db_conn.execute(
                    "UPDATE conversations SET avatar_url = ? WHERE conv_id = ?",
                    (rel_path, conv_id),
                )
                self._db_conn.commit()
                print(f"  [*] 已保存会话头像")
        except Exception as e:
            print(f"  [!] 下载会话头像失败: {e}")

    async def _extract_conversation(self, conv_index, conv_info):
        """Click a conversation and extract all its messages."""
        conv_name = conv_info["name"]
        # 优先使用纯昵称（不含火花天数和时间）
        clean_name = conv_info.get("nickname") or conv_name
        # Will try to get real conversation ID from fiber data after clicking
        conv_id = hashlib.md5(conv_name.encode()).hexdigest()[:16]

        # 确保会话列表完整加载（处理上一个会话 reload 后的状态）
        await self._ensure_conv_list_loaded()

        # ── 优化：点击会话前就装请求拦截器 ──
        # SDK 首次打开会话会发 get_by_conversation 请求，此时拦截就能拿到 short_id，
        # 完全跳过"清缓存+整页重载"（那个流程慢且脆弱，曾卡在重新加载聊天页面）。
        # 捕获失败才回退到 _acquire_short_id 的旧逻辑。
        self._captured_api_cursor = None
        cursor_captured_event = asyncio.Event()
        short_capture_listener = None

        async def pre_capture(request):
            nonlocal short_capture_listener
            if "get_by_conversation" in request.url and request.method == "POST":
                try:
                    body = request.post_data_buffer
                    if body:
                        result = await self._safe_eval("""(bytes) => {
                            function dv(buf, pos) {
                                let r = 0n, s = 0n;
                                while (pos < buf.length) {
                                    const b = buf[pos++]; r |= BigInt(b & 0x7F) << s;
                                    if (!(b & 0x80)) break; s += 7n;
                                }
                                return [r, pos];
                            }
                            function ef(buf, tf) {
                                let pos = 0;
                                while (pos < buf.length) {
                                    let tag; [tag, pos] = dv(buf, pos);
                                    const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                    if (wt === 0) { let v; [v, pos] = dv(buf, pos); }
                                    else if (wt === 2) { let len; [len, pos] = dv(buf, pos); len = Number(len); if (fn === tf) return buf.slice(pos, pos + len); pos += len; }
                                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                                }
                                return null;
                            }
                            function efStr(buf, tf) {
                                let pos = 0;
                                while (pos < buf.length) {
                                    let tag; [tag, pos] = dv(buf, pos);
                                    const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                    if (wt === 0) { let v; [v, pos] = dv(buf, pos); }
                                    else if (wt === 2) {
                                        let len; [len, pos] = dv(buf, pos); len = Number(len);
                                        if (fn === tf) return new TextDecoder().decode(buf.slice(pos, pos + len));
                                        pos += len;
                                    }
                                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                                }
                                return null;
                            }
                            const data = new Uint8Array(bytes);
                            const f8 = ef(data, 8);
                            if (!f8) return null;
                            const f301 = ef(f8, 301);
                            if (!f301) return null;
                            const reqConvId = efStr(f301, 1);
                            let cursor = null;
                            let pos = 0;
                            while (pos < f301.length) {
                                let tag; [tag, pos] = dv(f301, pos);
                                const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                if (wt === 0) { let v; [v, pos] = dv(f301, pos); if (fn === 3) cursor = v.toString(); }
                                else if (wt === 2) { let len; [len, pos] = dv(f301, pos); pos += Number(len); }
                                else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                            }
                            return { convId: reqConvId, cursor: cursor };
                        }""", list(body))
                        if result and result.get("cursor") and result["cursor"] != "0":
                            # 注意：不能按 conv_id 严格匹配——拦截器在点击前安装，
                            # 此时真实 conv_id（0:1:uidA:uidB）还没拿到（要点击后才从
                            # store 读），闭包里的 conv_id 是 md5 hash，永远匹配不上。
                            # 点击会话后 SDK 只会为当前活跃会话发 get_by_conversation，
                            # 直接接受第一个带 cursor 的请求即可。
                            self._captured_api_cursor = result["cursor"]
                            req_conv_id = result.get("convId", "")
                            print(f"  [*] 点击时已捕获 API cursor: {result['cursor']} (conv_id={req_conv_id})")
                            cursor_captured_event.set()
                except Exception:
                    pass

        self.page.on("request", pre_capture)
        short_capture_listener = pre_capture

        result = await self._find_and_click_conversation(clean_name)
        if not result.get("found"):
            dbg = result.get("names", [])
            print(f"  [!] 无法找到会话「{clean_name}」，跳过 (DOM中有 {result.get('count', 0)} 个会话: {dbg})")
            try:
                self.page.remove_listener("request", pre_capture)
            except Exception:
                pass
            return
        print(f"  [*] 已点击会话: {result.get('text', '')}")

        # 点击后 SDK 会拉取消息 → 给 8s 捕获 short_id（不重载页面）
        pre_short_id = None
        if conv_id and not conv_id.isdigit():
            try:
                await asyncio.wait_for(cursor_captured_event.wait(), timeout=8)
            except asyncio.TimeoutError:
                print(f"  [*] 点击后 8s 内未捕获到 short_id，回退到清缓存+重载流程")
            pre_short_id = self._captured_api_cursor
            if pre_short_id:
                print(f"  [*] 已通过点击捕获 short_id: {pre_short_id}（无需重载页面）")

        try:
            self.page.remove_listener("request", pre_capture)
        except Exception:
            pass

        await asyncio.sleep(2)

        try:
            active_name = await asyncio.wait_for(self.page.evaluate(f"""() => {{
                const active = document.querySelector('{SEL_CONV_ITEM}[class*="curConversation"]');
                if (!active) return '';
                const title = active.querySelector('{SEL_CONV_TITLE}');
                return title ? title.textContent.trim() : '';
            }}"""), timeout=10)
        except asyncio.TimeoutError:
            active_name = ''
        print(f"  [*] 当前活跃会话: {active_name or '(未检测到)'}")

        # Try to get real conversation ID from IM SDK
        try:
            real_conv_id = await asyncio.wait_for(self.page.evaluate("""() => {
                const cs = window.conversationStore;
                return cs && cs.curConversationId ? String(cs.curConversationId) : null;
            }"""), timeout=10)
        except asyncio.TimeoutError:
            real_conv_id = None
        if real_conv_id:
            conv_id = real_conv_id
            print(f"  [*] 真实会话ID: {conv_id}")

        # 全量模式：清除该会话的旧消息，避免残留已撤回或已删除的消息。
        # 但 DELETE 是先 commit 的，而后面的抓取可能一条都拿不到（回退到滚动模式却读不出
        # 内容、重新加载后找不到会话、中途抛异常）——那样一次失败的全量抓取就把历史记录
        # 清空了。所以先快照到临时表，结束时发现库里是空的就还原回去。
        backed_up = 0
        if not self.incremental:
            backed_up = self._backup_conv_messages(conv_id)
            cur = self._db_conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
            if cur.rowcount > 0:
                print(f"  [*] 全量模式：已清除该会话旧消息 {cur.rowcount} 条")

        try:
            upsert_conversation(self._db_conn, conv_id, name=clean_name)
            self._db_conn.commit()

            # 从激活的会话列表项抓取并保存会话头像
            await self._extract_and_save_conv_avatar(conv_id)

            # 提取用户信息（昵称、头像、unique_id）
            await self._extract_and_save_user_info(conv_id)

            mode_str = "增量" if self.incremental else "全量"
            print(f"  [*] 开始{mode_str}导出 (纯API模式)...")

            # ── 纯 API 模式：拿到 short_id → API 直取全部消息 ──
            # short_id 是 imapi 分页请求的 field 3。代码历史上误称它 "cursor"，实际是
            # conversation_short_id（每会话固定）；真正翻页靠 field 5 的时间戳。
            # 优先用点击时捕获的 short_id（无需重载页面）；没有才走 _acquire_short_id。
            if pre_short_id:
                short_id = pre_short_id
            else:
                short_id = await self._acquire_short_id(conv_id, clean_name)

            if not short_id:
                print(f"  [!] 未能获取 short_id，跳过该会话（其它会话不受影响；已有消息会保留）")
                return

            total_saved = await self._api_fetch_all_messages(
                conv_id, short_id, incremental=self.incremental
            )
            print(f"  [+] 共保存 {total_saved} 条消息")
        finally:
            self._restore_conv_messages_if_empty(conv_id, backed_up)

    async def _acquire_short_id(self, conv_id, clean_name):
        """拿到 imapi 分页请求需要的 short_id（field 3）。

        - 群聊：纯数字 conv_id 本身就是 short_id（实测请求里 f3==conv_id），不依赖 SDK
          任何时序，直接返回。旧代码对群聊也走"清缓存偷请求"，而群聊 SDK 有缓存时压根
          不发 get_by_conversation → 偷不到 → 抓不到（issue #24/#25 根因）。
        - 单聊：conv_id 形如 '0:1:uidA:uidB'，short_id 是另一个数字，既不在前端 store 里
          也算不出来，直接查接口又过不了 secsdk 签名——只能从 SDK 自己发的（已签名的）
          get_by_conversation 请求里偷。带重试；偷不到就放弃该会话（不再有滚动兜底）。
        """
        if conv_id.isdigit():
            print(f"  [*] 群聊 short_id = conv_id ({conv_id})")
            return conv_id

        for attempt in range(3):
            short_id = await self._steal_short_id_from_sdk(conv_id, clean_name)
            if short_id:
                return short_id
            # 陌生人会话只有一条系统提示、消息列表不可滚动 → 结构上没有可翻页的历史，
            # 再重试也偷不到。这类会话在"全量重抓"里成批出现，省掉多余的清缓存+重载。
            si = await self._get_scroll_info()
            if si and not si.get("scrollable"):
                print(f"  [*] 会话无可翻页历史（消息列表不可滚动），停止重试")
                break
            if attempt < 2:
                print(f"  [!] 第 {attempt + 1}/3 次未捕获到 short_id，重试...")
        return None

    async def _steal_short_id_from_sdk(self, conv_id, clean_name):
        """清缓存 → 重载 → 点会话，逼 SDK 重新从 API 拉取，拦它发出的
        get_by_conversation 请求，从请求体偷 short_id（f8→301→3）。

        请求体的 protobuf 解析是脆弱逆向逻辑，逐字保留自旧实现，勿改。
        返回 short_id 字符串，偷不到返回 None。
        """
        # 1. 清除 SDK 本地缓存（localStorage/sessionStorage/IndexedDB，保留 cookies 以维持登录）
        print(f"  [*] 清除 SDK 本地缓存...")
        await self._clear_sdk_cache()

        # 2. 安装请求拦截器（在重新加载之前，确保捕获 SDK 的第一个 API 请求）
        self._captured_api_cursor = None
        cursor_captured_event = asyncio.Event()


        async def capture_api_request(request):
            if "get_by_conversation" in request.url and request.method == "POST":
                try:
                    body = request.post_data_buffer
                    if body:
                        result = await self._safe_eval("""(bytes) => {
                            function dv(buf, pos) {
                                let r = 0n, s = 0n;
                                while (pos < buf.length) {
                                    const b = buf[pos++]; r |= BigInt(b & 0x7F) << s;
                                    if (!(b & 0x80)) break; s += 7n;
                                }
                                return [r, pos];
                            }
                            function ef(buf, tf) {
                                let pos = 0;
                                while (pos < buf.length) {
                                    let tag; [tag, pos] = dv(buf, pos);
                                    const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                    if (wt === 0) { let v; [v, pos] = dv(buf, pos); }
                                    else if (wt === 2) { let len; [len, pos] = dv(buf, pos); len = Number(len); if (fn === tf) return buf.slice(pos, pos + len); pos += len; }
                                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                                }
                                return null;
                            }
                            // 提取字符串字段 (wire type 2)
                            function efStr(buf, tf) {
                                let pos = 0;
                                while (pos < buf.length) {
                                    let tag; [tag, pos] = dv(buf, pos);
                                    const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                    if (wt === 0) { let v; [v, pos] = dv(buf, pos); }
                                    else if (wt === 2) {
                                        let len; [len, pos] = dv(buf, pos); len = Number(len);
                                        if (fn === tf) return new TextDecoder().decode(buf.slice(pos, pos + len));
                                        pos += len;
                                    }
                                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                                }
                                return null;
                            }
                            const data = new Uint8Array(bytes);
                            const f8 = ef(data, 8);
                            if (!f8) return null;
                            const f301 = ef(f8, 301);
                            if (!f301) return null;
                            // 提取 conv_id (field 1, string) 和 cursor (field 3, varint)
                            const reqConvId = efStr(f301, 1);
                            let cursor = null;
                            let pos = 0;
                            while (pos < f301.length) {
                                let tag; [tag, pos] = dv(f301, pos);
                                const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                if (wt === 0) { let v; [v, pos] = dv(f301, pos); if (fn === 3) cursor = v.toString(); }
                                else if (wt === 2) { let len; [len, pos] = dv(f301, pos); pos += Number(len); }
                                else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                            }
                            return { convId: reqConvId, cursor: cursor };
                        }""", list(body))
                        if result and result.get("cursor") and result["cursor"] != "0":
                            req_conv_id = result.get("convId", "")
                            if req_conv_id == conv_id:
                                self._captured_api_cursor = result["cursor"]
                                print(f"  [*] 捕获到 API cursor: {result['cursor']} (conv_id 匹配)")
                                cursor_captured_event.set()
                            else:
                                print(f"  [!] 忽略不匹配的 API 请求: conv_id={req_conv_id} (期望 {conv_id})")
                except Exception:
                    pass


        self.page.on("request", capture_api_request)
        try:
            # 3. 重新加载聊天页面（SDK 内存缓存随页面销毁而清除）
            #    30s 硬超时：页面被风控/加载异常时 goto 可能长时间不返回
            print(f"  [*] 重新加载聊天页面...")
            try:
                await asyncio.wait_for(
                    self.page.goto(CHAT_URL, wait_until="domcontentloaded"), timeout=30
                )
            except asyncio.TimeoutError:
                print(f"  [!] 重新加载聊天页面超时 (>30s)，继续尝试点击")
            await self._ensure_conv_list_loaded()

            # 4. 重新点击目标会话（触发 SDK 从 API 加载消息）
            print(f"  [*] 重新点击会话: {clean_name}...")
            result = await self._find_and_click_conversation(clean_name)
            if not result.get("found"):
                dbg = result.get("names", [])
                print(f"  [!] 重新加载后未找到会话「{clean_name}」 (DOM: {result.get('count', 0)} items: {dbg})")
                return None
            print(f"  [*] 已重新点击: {result.get('text', '')}")

            # 5. 等待 SDK 发出 API 请求（缓存已清，应该很快）
            print(f"  [*] 等待 SDK 发出 API 请求...")
            try:
                await asyncio.wait_for(cursor_captured_event.wait(), timeout=15)
            except asyncio.TimeoutError:
                # 15 秒内没捕获到，尝试轻微滚动触发（每轮 evaluate 也带 5s 超时）
                print(f"  [*] 未立即捕获到，尝试滚动触发...")
                for i in range(50):
                    try:
                        await asyncio.wait_for(
                            self.page.evaluate("""() => {
                                const el = document.querySelector('[class*="messageMessageListlist"]');
                                if (el) el.scrollTop += 3000;
                            }"""), timeout=5)
                    except asyncio.TimeoutError:
                        pass
                    await asyncio.sleep(0.3)
                    if self._captured_api_cursor:
                        break
            return self._captured_api_cursor
        finally:
            self.page.remove_listener("request", capture_api_request)


    def _backup_conv_messages(self, conv_id):
        """把该会话的消息快照到临时表，返回条数。

        TEMP 表跟随连接存在，不受中途 commit 影响，正好用来兜住"全量抓取先删后抓"
        的窗口期。
        """
        conn = self._db_conn
        conn.execute("DROP TABLE IF EXISTS temp.msg_backup")
        conn.execute(
            "CREATE TEMP TABLE msg_backup AS SELECT * FROM messages WHERE conv_id = ?",
            (conv_id,),
        )
        return conn.execute("SELECT COUNT(*) FROM temp.msg_backup").fetchone()[0]

    def _restore_conv_messages_if_empty(self, conv_id, backed_up):
        """全量抓取一条都没入库时，把快照的旧消息放回去。

        一个会话在抖音那边真的被清空、导致抓到 0 条的概率，远低于抓取本身出问题。
        宁可留着旧记录（要清空有面板的删除功能），也不能让一次失败的抓取把历史抹掉。
        """
        if not backed_up:
            return
        conn = self._db_conn
        remaining = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
        ).fetchone()[0]
        if remaining == 0:
            conn.execute("INSERT INTO messages SELECT * FROM temp.msg_backup")
            update_conversation_stats(conn, conv_id)
            print(f"  [!] 本次全量抓取一条消息都没拿到，已还原原有的 {backed_up} 条旧消息")
        conn.execute("DROP TABLE IF EXISTS temp.msg_backup")
        conn.commit()


    async def _clear_sdk_cache(self):
        """清除 IM SDK 的本地缓存（localStorage/sessionStorage/IndexedDB），保留 cookies。

        SDK 会在 IndexedDB 和 localStorage 中缓存消息数据。
        清除后重新加载页面，SDK 将被迫从 API 重新拉取消息。
        """
        try:
            await asyncio.wait_for(self.page.evaluate("""async () => {
            // 清除 localStorage 和 sessionStorage
            try { localStorage.clear(); } catch(e) {}
            try { sessionStorage.clear(); } catch(e) {}

            // 删除所有 IndexedDB 数据库
            try {
                const dbs = await indexedDB.databases();
                for (const db of dbs) {
                    if (db.name) {
                        indexedDB.deleteDatabase(db.name);
                    }
                }
            } catch(e) {
                // indexedDB.databases() 可能不支持，逐个尝试已知的数据库名
                const knownDbs = ['im_sdk', 'im_db', 'douyin_im', 'bytedance_im'];
                for (const name of knownDbs) {
                    try { indexedDB.deleteDatabase(name); } catch(e2) {}
                }
            }
        }"""), timeout=15)
        except asyncio.TimeoutError:
            print(f"  [!] 清除 SDK 缓存超时 (>15s)，继续")
        print(f"  [*] 已清除 localStorage/sessionStorage/IndexedDB")

    async def _get_scroll_info(self):
        """获取滚动容器的详细状态。"""
        return await self._safe_eval(f"""() => {{
            const el = document.querySelector('{SEL_MSG_LIST}');
            if (!el) return null;
            // 找到真正可滚动的元素（可能是 msg list 本身或其父/子元素）
            let scrollEl = el;
            if (el.scrollHeight <= el.clientHeight) {{
                // 尝试父元素
                if (el.parentElement && el.parentElement.scrollHeight > el.parentElement.clientHeight) {{
                    scrollEl = el.parentElement;
                }}
            }}
            return {{
                scrollTop: scrollEl.scrollTop,
                scrollHeight: scrollEl.scrollHeight,
                clientHeight: scrollEl.clientHeight,
                scrollable: scrollEl.scrollHeight > scrollEl.clientHeight,
                tagName: scrollEl.tagName,
                className: (scrollEl.className || '').substring(0, 60),
            }};
        }}""")

    async def _inject_api_tools(self):
        """注入 protobuf 编解码 + IM API 调用工具到页面中。"""
        await self._safe_eval("""() => {
            if (window.__imApi) return; // 已注入
            // ── protobuf 编码 ──
            function encodeVarint(value) {
                const bytes = [];
                let v = typeof value === 'bigint' ? value : BigInt(value);
                do {
                    let b = Number(v & 0x7Fn);
                    v >>= 7n;
                    if (v > 0n) b |= 0x80;
                    bytes.push(b);
                } while (v > 0n);
                if (bytes.length === 0) bytes.push(0);
                return new Uint8Array(bytes);
            }
            function encodeTag(fn, wt) { return encodeVarint((fn << 3) | wt); }
            function encodeString(fn, s) {
                const e = new TextEncoder().encode(s);
                return concatArrays([encodeTag(fn, 2), encodeVarint(e.length), e]);
            }
            function encodeVarintField(fn, v) { return concatArrays([encodeTag(fn, 0), encodeVarint(v)]); }
            function encodeBytes(fn, d) { return concatArrays([encodeTag(fn, 2), encodeVarint(d.length), d]); }
            function concatArrays(arrs) {
                const t = arrs.reduce((s, a) => s + a.length, 0);
                const r = new Uint8Array(t); let o = 0;
                for (const a of arrs) { r.set(a, o); o += a.length; }
                return r;
            }

            // ── protobuf 解码 ──
            function decodeVarint(buf, pos) {
                let result = 0, shift = 0;
                while (pos < buf.length) {
                    const b = buf[pos++]; result |= (b & 0x7F) << shift;
                    if ((b & 0x80) === 0) break; shift += 7; if (shift > 35) break;
                }
                return [result, pos];
            }
            function decodeVarintBig(buf, pos) {
                let result = 0n, shift = 0n;
                while (pos < buf.length) {
                    const b = buf[pos++]; result |= BigInt(b & 0x7F) << shift;
                    if ((b & 0x80) === 0) break; shift += 7n;
                }
                return [result, pos];
            }
            function extractField(buf, targetField) {
                let pos = 0;
                while (pos < buf.length) {
                    let tag; [tag, pos] = decodeVarint(buf, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (wt === 0) { let v; [v, pos] = decodeVarintBig(buf, pos); }
                    else if (wt === 2) { let len; [len, pos] = decodeVarint(buf, pos); if (fn === targetField) return buf.slice(pos, pos + len); pos += len; }
                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                }
                return null;
            }

            function buildRequest(convId, cursor, timestamp) {
                const inner = concatArrays([
                    encodeString(1, convId), encodeVarintField(2, 1),
                    encodeVarintField(3, cursor), encodeVarintField(4, 1),
                    encodeVarintField(5, timestamp), encodeVarintField(6, 50),
                ]);
                const queryMsg = encodeBytes(301, inner);
                return concatArrays([
                    encodeVarintField(1, 301), encodeVarintField(2, 10027),
                    encodeString(3, '0.1.6'), encodeString(4, ''),
                    encodeVarintField(5, 3), encodeVarintField(6, 0),
                    encodeString(7, 'fef1a80:p/lzg/store'),
                    encodeBytes(8, queryMsg), encodeString(9, '0'),
                    encodeString(11, 'douyin_pc'), encodeString(14, '360000'),
                    encodeVarintField(18, 1), encodeString(21, 'douyin_pc'),
                ]);
            }

            // 通用 protobuf 递归解析器（返回所有字段）
            function parseProto(buf, depth) {
                if (!depth) depth = 0;
                const fields = {}; let pos = 0;
                while (pos < buf.length) {
                    let tag; [tag, pos] = decodeVarint(buf, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (fn === 0 || fn > 200) break;
                    if (wt === 0) {
                        let v; [v, pos] = decodeVarintBig(buf, pos);
                        fields['f'+fn] = v.toString();
                    } else if (wt === 2) {
                        let len; [len, pos] = decodeVarint(buf, pos);
                        if (pos + len > buf.length) break;
                        const slice = buf.slice(pos, pos+len);
                        // 尝试解码为 UTF-8 文本
                        let text = null;
                        try { text = new TextDecoder('utf-8', {fatal:true}).decode(slice); } catch {}
                        if (text !== null && text.length < 5000) {
                            fields['f'+fn] = text;
                        } else if (depth < 3 && len > 4) {
                            // 尝试递归解析为嵌套 protobuf
                            try {
                                const sub = parseProto(slice, depth + 1);
                                if (Object.keys(sub).length > 0) fields['f'+fn] = sub;
                            } catch {}
                        }
                        pos += len;
                    } else if (wt === 1) { pos += 8; }
                    else if (wt === 5) { pos += 4; }
                    else break;
                }
                return fields;
            }

            function parseMessage(buf) {
                const r = {}; let pos = 0;
                while (pos < buf.length) {
                    let tag; [tag, pos] = decodeVarint(buf, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (fn === 0 || fn > 500) break;
                    if (wt === 0) { let v; [v, pos] = decodeVarintBig(buf, pos);
                        if (fn===3) r.server_id=v.toString(); else if (fn===4) r.created_at_us=v.toString();
                        else if (fn===5) r.order=v.toString(); else if (fn===7) r.sender_uid=v.toString();
                        else if (fn===6) r.type_code=Number(v); else if (fn===11) r.is_recalled=Number(v);
                        else if (fn===12) r.visible=Number(v);
                    } else if (wt === 2) { let len; [len, pos] = decodeVarint(buf, pos);
                        const slice = buf.slice(pos, pos+len);
                        if (fn===1) r.conv_id=new TextDecoder().decode(slice);
                        else if (fn===8) { try { r.content_json=new TextDecoder().decode(slice); } catch {} }
                        // Field 14: 发送者 sec_uid。群聊补全昵称/头像的唯一线索——
                        // IM 用户信息接口只认 sec_uid，不认 f7 的数字 uid。
                        else if (fn===14) { try { r.sender_sec_uid=new TextDecoder().decode(slice); } catch {} }
                        else if (fn===18) {
                            // Field 18: 引用/回复消息
                            // 结构: f1=被引用消息server_id, f2=JSON(content, nickname, refmsg_sec_uid, refmsg_content)
                            try {
                                const refProto = parseProto(slice, 0);
                                if (refProto.f1 && refProto.f2) {
                                    const refJson = JSON.parse(refProto.f2);
                                    r._ref_msg = {
                                        server_id: refProto.f1,
                                        content: refJson.content || '',
                                        nickname: refJson.nickname || '',
                                        sec_uid: refJson.refmsg_sec_uid || '',
                                        refmsg_content: refJson.refmsg_content || '',
                                    };
                                }
                            } catch {}
                        }
                        pos += len;
                    } else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                }
                return r;
            }

            function parseResponse(data) {
                const f6 = extractField(data, 6);
                if (!f6) return { msgs: [], hasMore: 0, nextTs: null };
                const f301 = extractField(f6, 301);
                if (!f301) return { msgs: [], hasMore: 0, nextTs: null };
                let pos = 0; const msgs = []; let nextTs = null, hasMore = 0;
                while (pos < f301.length) {
                    let tag; [tag, pos] = decodeVarint(f301, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (wt === 0) { let v; [v, pos] = decodeVarintBig(f301, pos);
                        if (fn===2) nextTs=v.toString(); if (fn===3) hasMore=Number(v);
                    } else if (wt === 2) { let len; [len, pos] = decodeVarint(f301, pos);
                        if (fn===1) msgs.push(parseMessage(f301.slice(pos, pos+len)));
                        pos += len;
                    } else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                }
                return { msgs, nextTs, hasMore };
            }

            // ── API 调用 ──
            window.__imApi = {
                buildRequest, parseResponse,
                call: async function(convId, cursor, timestamp, retries = 3) {
                    for (let attempt = 0; attempt < retries; attempt++) {
                        try {
                            const result = await new Promise((resolve, reject) => {
                                const reqBody = buildRequest(convId, BigInt(cursor), BigInt(timestamp));
                                const xhr = new XMLHttpRequest();
                                xhr.open('POST', 'https://imapi.douyin.com/v1/message/get_by_conversation');
                                xhr.setRequestHeader('Content-Type', 'application/x-protobuf');
                                xhr.setRequestHeader('Accept', 'application/x-protobuf');
                                xhr.responseType = 'arraybuffer';
                                xhr.withCredentials = true;
                                xhr.timeout = 30000;
                                xhr.onload = () => resolve({ status: xhr.status, data: new Uint8Array(xhr.response) });
                                xhr.onerror = () => reject(new Error('XHR failed'));
                                xhr.ontimeout = () => reject(new Error('XHR timeout'));
                                xhr.send(reqBody.buffer);
                            });
                            return result;
                        } catch (e) {
                            if (attempt < retries - 1) {
                                const wait = (attempt + 1) * 3000;
                                console.log('[imApi] call attempt ' + (attempt+1) + '/' + retries + ' failed: ' + e.message + ', retry in ' + (wait/1000) + 's');
                                await new Promise(r => setTimeout(r, wait));
                            } else {
                                throw e;
                            }
                        }
                    }
                },
                fetchBatch: async function(convId, cursor, timestamp, maxPages) {
                    const allMsgs = [];
                    let ts = timestamp;
                    let hasMore = 1;
                    let consecutiveErrors = 0;
                    let emptyStreak = 0;
                    for (let i = 0; i < maxPages && hasMore; i++) {
                        try {
                            const r = await this.call(convId, cursor, ts);
                            if (r.status !== 200) {
                                console.log('[imApi] page ' + i + ': HTTP ' + r.status);
                                consecutiveErrors++;
                                emptyStreak = 0;
                                if (consecutiveErrors >= 3) break;
                                await new Promise(r => setTimeout(r, 3000));
                                continue;
                            }
                            consecutiveErrors = 0;
                            const p = this.parseResponse(r.data);
                            if (!p.msgs || p.msgs.length === 0) {
                                // 空页可能是限流/抖动导致的"瞬时空响应"，也可能真的到了
                                // 历史尽头。连续 3 次空页才判定到底，避免漏掉后续消息。
                                emptyStreak++;
                                if (emptyStreak >= 3) {
                                    console.log('[imApi] page ' + i + ': 连续 3 次空页，判定已到历史起点');
                                    hasMore = 0;
                                    break;
                                }
                                await new Promise(r => setTimeout(r, 2000));
                                continue;
                            }
                            emptyStreak = 0;
                            for (const m of p.msgs) allMsgs.push(m);
                            hasMore = p.hasMore;
                            ts = p.nextTs;
                            // 轻微节流：每 5 页休息 150ms，降低触发限流的概率
                            // （翻页太快会触发风控，导致后续请求返回空/错误而漏消息）
                            if (i % 5 === 4) await new Promise(r => setTimeout(r, 150));
                        } catch (e) {
                            console.log('[imApi] page ' + i + ' error: ' + e.message);
                            consecutiveErrors++;
                            emptyStreak = 0;
                            if (consecutiveErrors >= 3) {
                                return { msgs: allMsgs, nextTs: ts, hasMore, error: e.message };
                            }
                            await new Promise(r => setTimeout(r, 3000));
                        }
                    }
                    return { msgs: allMsgs, nextTs: ts, hasMore };
                },
            };
        }""")

    async def _api_fetch_all_messages(self, conv_id, cursor, incremental=False):
        """用 API 直接获取全部历史消息。

        `cursor` 是 short_id（imapi 请求 field 3，每会话固定，非分页游标）；真正翻页靠
        请求 field 5 的时间戳，本方法从 9999999999999999 开始逐批往旧推。
        """
        await self._inject_api_tools()

        api_cursor = cursor
        next_ts = "9999999999999999"
        print(f"  [*] API 直取模式: cursor={api_cursor}, 从最新开始向旧获取")

        # 3. 增量模式：获取已有消息的最旧时间戳
        existing_count = 0
        existing_oldest_ts = None
        if incremental:
            row = self._db_conn.execute(
                "SELECT COUNT(*), MIN(timestamp) FROM messages WHERE conv_id = ?", (conv_id,)
            ).fetchone()
            existing_count = row[0] or 0
            existing_oldest_ts = row[1] if row[1] else None
            if existing_count:
                print(f"  [*] 增量模式: 已有 {existing_count} 条消息")

        # 4. 循环分页获取所有消息
        sec_by_uid = {}  # sender_uid -> sec_uid，抓完后批量补全昵称/头像
        total_saved = 0
        total_fetched = 0
        batch_num = 0
        zero_saved_streak = 0  # 连续 saved=0 的批次计数
        pages_per_batch = 30  # 每批 30 页 ≈ 1500 条。50 页一次 evaluate 翻页太快，
        # 易触发 imapi 限流导致空页/错误而漏消息；30 页 + JS 端每 5 页 150ms 节流更稳。
        has_more = True
        start_time = time.time()

        while has_more:
            batch_num += 1

            # 带重试的批量 API 调用
            batch_result = None
            for attempt in range(3):
                try:
                    # 120s 硬超时：JS 端 fetchBatch 最多 50 页，每页 XHR 30s 超时，
                    # 若某个请求/页面永久挂起，这里也必须能退出，否则采集卡死。
                    batch_result = await asyncio.wait_for(
                        self.page.evaluate("""async (args) => {
                            const [convId, cursor, ts, maxPages] = args;
                            return await window.__imApi.fetchBatch(convId, cursor, ts, maxPages);
                        }""", [conv_id, api_cursor, next_ts, pages_per_batch]),
                        timeout=120,
                    )
                    break
                except asyncio.TimeoutError:
                    print(f"  [!] batch #{batch_num} 超时 (>120s)，标记该会话失败，继续下一个")
                    has_more = False
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = (attempt + 1) * 5
                        print(f"  [!] batch #{batch_num} 失败 (attempt {attempt+1}/3): {e}")
                        print(f"  [*] 等待 {wait}s 后重试...")
                        await asyncio.sleep(wait)
                    else:
                        print(f"  [!] batch #{batch_num} 连续 3 次失败，停止")
                        has_more = False

            if batch_result and batch_result.get("error"):
                print(f"  [!] batch #{batch_num} JS 端报错: {batch_result['error']}")

            # 空结果 ≠ 到底！JS 端可能因错误提前返回 {msgs:[], hasMore:1}——
            # 此时必须用 nextTs 继续翻页，否则漏掉剩余消息。
            # 只有服务端明确 hasMore=0，或 next_ts 不再前进（死循环保护），才停止。
            if not batch_result or not batch_result.get("msgs"):
                js_has_more = bool(batch_result and batch_result.get("hasMore"))
                if js_has_more and next_ts and next_ts != "0":
                    print(f"  [!] batch #{batch_num} 返回空但 hasMore=1（可能是限流），"
                          f"用 nextTs={next_ts} 继续翻页防漏")
                    # 避免 next_ts 不前进导致死循环：连续 3 批空且 next_ts 不变才放弃
                    if getattr(self, "_empty_streak_conv", None) == conv_id and getattr(self, "_empty_streak_ts", None) == next_ts:
                        self._empty_streak = getattr(self, "_empty_streak", 0) + 1
                        if self._empty_streak >= 3:
                            print(f"  [!] 连续 3 批空结果且 nextTs 未前进，放弃该会话防死循环")
                            break
                    else:
                        self._empty_streak = 1
                    self._empty_streak_conv = conv_id
                    self._empty_streak_ts = next_ts
                    continue
                if has_more:
                    print(f"  [*] API 返回空结果，停止")
                break

            msgs = batch_result["msgs"]
            has_more = batch_result.get("hasMore", 0) == 1
            next_ts = batch_result.get("nextTs", next_ts)
            total_fetched += len(msgs)

            # 前3批打印引用消息统计
            if batch_num <= 3:
                ref_count = sum(1 for m in msgs if m.get("_ref_msg"))
                if ref_count:
                    print(f"  [debug] 发现 {ref_count} 条引用/回复消息")

            # 过滤掉不属于目标会话的消息（防止 cursor 错误导致拉到其他会话的数据）
            filtered_msgs = []
            for m in msgs:
                msg_conv_id = m.get("conv_id", "")
                if msg_conv_id and msg_conv_id != conv_id:
                    continue
                filtered_msgs.append(m)
            if len(filtered_msgs) < len(msgs):
                print(f"  [!] 过滤掉 {len(msgs) - len(filtered_msgs)} 条不属于当前会话的消息")
            msgs = filtered_msgs

            # 转换 API 消息格式 → _store_messages 期望的格式
            converted = []
            for m in msgs:
                if m.get("sender_uid") and m.get("sender_sec_uid"):
                    sec_by_uid.setdefault(m["sender_uid"], m["sender_sec_uid"])
                content_json = m.get("content_json", "")
                # 解析 content JSON
                text = ""
                msg_type = "other"
                awe_type = -1
                image_src = None
                try:
                    cj = json.loads(content_json)
                    awe_type = cj.get("aweType", -1)
                    text = cj.get("text", "") or cj.get("description", "")
                    if awe_type in (500, 501, 507, 508, 510, 514, 516):
                        # 表情包/贴纸
                        msg_type = "emoji"
                        if not text:
                            text = cj.get("display_name") or "[表情]"
                        # URL 在 cj.url.url_list[0]
                        url_obj = cj.get("url")
                        if isinstance(url_obj, dict):
                            url_list = url_obj.get("url_list", [])
                            if url_list:
                                image_src = url_list[0] if isinstance(url_list[0], str) else None
                    elif awe_type in (2702, 2703, 2704):
                        # 图片消息
                        msg_type = "image"
                        if not text:
                            text = "[图片]"
                        # URL 在 cj.resource_url.large_url_list[0]
                        ru = cj.get("resource_url") or {}
                        for key in ("large_url_list", "medium_url_list", "origin_url_list", "thumb_url_list"):
                            ul = ru.get(key, [])
                            if ul and isinstance(ul[0], str):
                                image_src = ul[0]
                                break
                    elif awe_type == 700 or awe_type == 0:
                        msg_type = "text"
                    elif awe_type == 701 or awe_type == 703:
                        msg_type = "text"
                    elif awe_type in (11054, 11055, 11063, 11066, 11067, 11069, 11070):
                        # 分享视频/直播
                        msg_type = "share"
                        if not text:
                            text = cj.get("push_detail") or "[分享]"
                        # 封面图 在 cj.cover_url.url_list[0]
                        cover = cj.get("cover_url")
                        if isinstance(cover, dict):
                            ul = cover.get("url_list", [])
                            if ul and isinstance(ul[0], str):
                                image_src = ul[0]
                    elif awe_type in (11029, 10500, 10401):
                        # 分享商品/评论
                        msg_type = "share"
                        # aweType=10500: 引用视频评论，comment 字段包含评论内容
                        comment = cj.get("comment", "")
                        aweme_title = cj.get("aweme_title", "")
                        if comment:
                            text = comment
                        elif not text:
                            text = cj.get("push_detail") or aweme_title or "[分享]"
                    elif awe_type in (800, 801, 803):
                        msg_type = "share"
                        if not text:
                            text = "[分享]"
                    elif awe_type >= 100000:
                        msg_type = "other"
                        text = text or cj.get("push_detail") or "[系统消息]"
                    elif cj.get("resource_url") and cj.get("duration"):
                        # 语音消息：有 resource_url 和 duration
                        msg_type = "other"  # 保持 type=0，前端会检测 resource_url
                        dur_sec = round(cj["duration"] / 1000)
                        text = text or f"[语音 {dur_sec}秒]"
                    elif cj.get("video", {}).get("vid") and cj.get("poster", {}).get("origin_url_list"):
                        # 视频消息：awe_type=0 的视频走单独路径，cj.video.vid + cj.poster
                        # 真正的视频流要 vid → 加密 URL 反查（待办），目前只下载 poster 封面图
                        msg_type = "video"
                        try:
                            dur_sec = round(float(cj.get("duration") or 0))
                        except (TypeError, ValueError):
                            dur_sec = 0
                        text = f"[视频 {dur_sec}秒]" if dur_sec else "[视频]"
                        urls = cj.get("poster", {}).get("origin_url_list") or []
                        if urls and isinstance(urls[0], str):
                            image_src = urls[0]
                    elif text:
                        msg_type = "text"
                    else:
                        msg_type = "other"
                        text = cj.get("push_detail") or cj.get("display_name") or content_json[:200]
                except (json.JSONDecodeError, AttributeError):
                    text = content_json
                    msg_type = "text"

                if not text and msg_type == "text":
                    text = content_json

                # 时间戳：serverId 是 snowflake ID，高32位是 Unix 秒时间戳
                server_id_int = int(m.get("server_id", "0"))
                timestamp_sec = server_id_int >> 32 if server_id_int > 0 else 0

                # order 用于排序：created_at_us 是单调递增的，用作排序键
                created_at_us = int(m.get("created_at_us", "0"))

                # 引用/回复消息
                ref_msg = m.get("_ref_msg")
                ref_msg_json = json.dumps(ref_msg, ensure_ascii=False) if ref_msg else None

                converted.append({
                    "server_id": m.get("server_id", ""),
                    "content": text,
                    "msg_type": msg_type,
                    "awe_type": awe_type,
                    "is_self": False,  # API 不直接给出，后面可从 sender_uid 判断
                    "sender_uid": m.get("sender_uid", ""),
                    "sender_sec_uid": m.get("sender_sec_uid", ""),
                    "sender_name": "",  # API 不返回名字，抓完后按 sec_uid 批量补全
                    "conversation_id": m.get("conv_id", conv_id),
                    "created_at": datetime.utcfromtimestamp(timestamp_sec).isoformat() + "Z" if timestamp_sec > 0 else "",
                    "order_high": created_at_us >> 32,
                    "order_low": created_at_us & 0xFFFFFFFF,
                    "image_src": image_src,
                    "visible": m.get("visible", 0),
                    "is_recalled": m.get("is_recalled", 0),
                    "content_json": content_json,
                    "ref_msg": ref_msg_json,
                })

            # 性能修复: 不再每个 batch 都串行下载媒体
            # 改为全量消息抓完后再并发下载（见 _download_all_media_parallel）
            # 这样 500 人群从 N×batch×单条延迟 变成 1×总延迟/并发数
            # await self._download_voice_files(converted)
            # await self._download_image_files(converted)

            newly_inserted = self._store_messages(converted, conv_id, batch_seq_start=0)
            total_saved += newly_inserted

            elapsed = time.time() - start_time
            speed = total_fetched / elapsed if elapsed > 0 else 0
            # 计算时间范围
            if converted:
                times = [c["created_at"] for c in converted if c["created_at"]]
                oldest_time = min(times)[:19] if times else "?"
            else:
                oldest_time = "?"

            print(
                f"  [*] batch #{batch_num}: fetched={len(msgs)} saved={newly_inserted} "
                f"total={total_fetched}/{total_saved} oldest={oldest_time} "
                f"speed={speed:.0f}msg/s elapsed={elapsed:.1f}s hasMore={has_more}"
            )

            # 增量模式：连续 2 批 saved=0 说明已追上历史，停止
            if incremental and existing_count > 0:
                # 关键优化：本批消息的最早时间戳（秒）已不晚于已有最旧消息 →
                # 后面的翻页只会拉到已入库的旧消息，直接停止，省掉全量回捞。
                # 用"已抓到的消息时间"而非 nextTs 判断，边界更可靠，不易漏。
                try:
                    if converted:
                        times = [c["created_at"] for c in converted if c["created_at"]]
                        if times:
                            batch_oldest = min(times)
                            dt = datetime.fromisoformat(batch_oldest.replace("Z", "+00:00"))
                            if existing_oldest_ts and int(dt.timestamp()) <= existing_oldest_ts:
                                print(f"  [*] 增量模式: 本批最早 {batch_oldest[:19]} <= 已有最旧 "
                                      f"{existing_oldest_ts}，提前停止")
                                break
                except (ValueError, TypeError, AttributeError):
                    pass
                if newly_inserted == 0:
                    zero_saved_streak += 1
                    if zero_saved_streak >= 2:
                        print(f"  [*] 增量模式: 连续 {zero_saved_streak} 批无新消息，已追上历史记录")
                        break
                else:
                    zero_saved_streak = 0

            if not has_more:
                print(f"  [*] 已到达聊天记录起点")
                break

        # 性能修复: 全量并发下载所有媒体（之前每个 batch 串行，大群超慢）
        try:
            await self._download_all_media_parallel(conv_id)
        except Exception as e:
            print(f"  [!] 全量并发下载媒体失败: {e}")

        # 5. 补全发送者身份（群聊必需）。失败不能影响已抓到的消息。
        try:
            await self._resolve_sender_identities(sec_by_uid)
        except Exception as e:
            print(f"  [!] 补全发送者信息失败: {e}")

        # 6. 归一化 seq（临时表 + 索引关联 UPDATE，一次完成）
        # 性能: 关联子查询逐行重算 ROW_NUMBER 是 O(n²)，万级消息会卡几分钟。
        # 临时表算好 rn 并建主键索引，关联 UPDATE 走索引（O(n log n)）。
        print(f"  [*] 归一化消息序号 (按服务端排序)...")
        _c = self._db_conn
        _c.execute("DROP TABLE IF EXISTS temp.seq_map")
        _c.execute(
            """CREATE TEMP TABLE seq_map (
                 msg_id TEXT PRIMARY KEY,
                 rn INTEGER
               )"""
        )
        _c.execute(
            """INSERT INTO seq_map (msg_id, rn)
               SELECT msg_id, ROW_NUMBER() OVER (ORDER BY seq) AS rn
               FROM messages WHERE conv_id = ?""",
            (conv_id,),
        )
        _c.execute(
            """UPDATE messages SET seq = (
                SELECT rn FROM seq_map WHERE seq_map.msg_id = messages.msg_id
            ) WHERE conv_id = ?""",
            (conv_id,),
        )
        _c.execute("DROP TABLE IF EXISTS temp.seq_map")
        self._db_conn.commit()

        elapsed = time.time() - start_time
        print(f"  [*] API 获取完成: {total_fetched} 条消息, {total_saved} 条新增, 耗时 {elapsed:.1f}s")
        return total_saved

    @staticmethod
    def _make_msg_id(conv_id, msg):
        """生成消息ID，优先使用 serverId（稳定唯一）。"""
        server_id = msg.get("server_id")
        if server_id:
            return f"srv_{server_id}"
        # fallback: 基于内容 hash
        image_src = msg.get("image_src") or ""
        content = msg.get("content", "")
        is_self = msg.get("is_self", False)
        sender = msg.get("sender_uid", "") or msg.get("sender", "")
        msg_hash = hashlib.md5(
            f"{conv_id}:{content}:{is_self}:{sender}:{image_src}".encode()
        ).hexdigest()
        return f"web_{msg_hash}"

    def _store_messages(self, messages, conv_id, batch_seq_start=0):
        """Store a batch of messages to the database immediately. Returns count of newly inserted rows."""
        conn = self._db_conn
        msg_type_map = {"text": 1, "emoji": 2, "image": 3, "share": 4, "other": 0, "video": 5}

        # 批量收集行，避免逐条 execute 的 Python↔SQLite 往返开销
        rows = []
        ref_updates = []   # (ref_msg, msg_id) 已存在且缺 ref_msg 的消息
        user_rows = []     # (uid, nickname) 去重收集
        seen_users = set()

        for idx, msg in enumerate(messages):
            content = msg.get("content", "")
            if not content:
                continue

            msg_id = self._make_msg_id(conv_id, msg)

            # Sender: use real UID from fiber data
            sender_uid = msg.get("sender_uid", "")
            sender_name = msg.get("sender_name", "")
            if msg.get("is_self"):
                sender_name = sender_name or "__self__"
            if not sender_uid:
                sender_uid = hashlib.md5(
                    (sender_name or "unknown").encode()
                ).hexdigest()[:12]

            msg_type = msg_type_map.get(msg.get("msg_type", "text"), 0)

            # 图片/表情/分享/视频 都记录 media_url (ensure it's a string)
            raw_media = msg.get("image_src") if msg.get("msg_type") in ("image", "emoji", "share", "video") else None
            media_url = str(raw_media) if raw_media and isinstance(raw_media, str) else None
            local_path = msg.get("local_path")

            # Timestamp: use precise createdAt from fiber (ISO string → Unix seconds)
            timestamp = 0
            created_at = msg.get("created_at")
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    timestamp = int(dt.timestamp())
                except (ValueError, AttributeError):
                    pass

            # Seq: use orderInConversation (high << 32 | low) for precise ordering
            order_high = msg.get("order_high", 0) or 0
            order_low = msg.get("order_low", 0) or 0
            unsigned_low = order_low if order_low >= 0 else order_low + (1 << 32)
            seq = order_high * (1 << 32) + unsigned_low if (order_high or order_low) else (batch_seq_start + idx)

            ref_msg = msg.get("ref_msg")
            rows.append((
                msg_id, conv_id, sender_uid, sender_name, content, msg_type,
                media_url, local_path, timestamp, seq,
                json.dumps(msg, ensure_ascii=False), ref_msg,
            ))
            if ref_msg:
                ref_updates.append((ref_msg, msg_id))

            if sender_uid and sender_name and sender_name != "__self__" and sender_uid not in seen_users:
                seen_users.add(sender_uid)
                user_rows.append((sender_uid, sender_name))

        if not rows:
            update_conversation_stats(conn, conv_id)
            conn.commit()
            return 0

        # 批量 INSERT OR IGNORE；用 total_changes 差值统计真正新增的行数
        before = conn.total_changes
        conn.executemany(
            """INSERT OR IGNORE INTO messages
               (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
                media_url, media_local_path, timestamp, seq, raw_data, ref_msg)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        newly_inserted = conn.total_changes - before

        # 已存在的消息：补全 ref_msg（仅当新数据包含引用信息且旧值为空）
        if ref_updates:
            conn.executemany(
                "UPDATE messages SET ref_msg = ? WHERE msg_id = ? AND (ref_msg IS NULL OR ref_msg = '')",
                ref_updates,
            )

        # 批量 upsert 用户
        if user_rows:
            conn.executemany(
                """INSERT INTO users (uid, nickname, avatar_url, unique_id)
                   VALUES (?, ?, NULL, NULL)
                   ON CONFLICT(uid) DO UPDATE SET
                     nickname=COALESCE(excluded.nickname, nickname)""",
                user_rows,
            )

        update_conversation_stats(conn, conv_id)
        conn.commit()

        # 每 1000 条做一次 WAL checkpoint，防止 WAL 文件膨胀
        self._commit_counter = getattr(self, "_commit_counter", 0) + len(messages)
        if self._commit_counter >= 1000:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._commit_counter = 0

        return newly_inserted

    async def close(self):
        if self._db_conn:
            try:
                self._db_conn.commit()
                self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        print("[+] 浏览器已关闭")
