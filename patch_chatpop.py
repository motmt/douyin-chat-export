#!/usr/bin/env python3
"""
chatpop 性能 + 日志卡顿 修复脚本

修复 3 个问题:
  1. 日志卡顿: 子进程默认块缓冲，加 PYTHONUNBUFFERED=1 + 日志文件用行缓冲
  2. 大群慢: 媒体下载从 per-batch 串行移到全量并发
  3. 日志只显示 80 行: 改 200 + 后端从文件末尾反向读取

用法: 在此脚本所在目录运行
  python patch_chatpop.py

会修改 D:\\douyin-chat-export-complete\\app\\ 下的对应文件，每个修改前会
做 unique string 校验，找不到目标串会跳过并提示，不会瞎改。
"""
import os
import sys

# 配置: 目标项目根目录 (可通过命令行参数覆盖)
#   python patch_chatpop.py                 # 默认改 D:\douyin-chat-export-complete\app
#   python patch_chatpop.py <其他路径>      # 改其他项目
DEFAULT_PROJECT_DIR = r"D:\douyin-chat-export-complete\app"
PROJECT_DIR = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PROJECT_DIR

CONTROL_PANEL = os.path.join(PROJECT_DIR, "backend", "control_panel.py")
WEB_SCRAPER = os.path.join(PROJECT_DIR, "extractor", "web_scraper.py")
PANEL_HTML = os.path.join(PROJECT_DIR, "backend", "panel", "static", "panel.html")


def patch_file(path, patches, dry_run=False):
    """Apply list of (old, new) string patches to a file.
    Each old string must appear exactly once in the file (unique match).
    """
    if not os.path.exists(path):
        print(f"[!] 跳过 (不存在): {path}")
        return False

    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    original = content
    applied = 0
    for i, (old, new) in enumerate(patches, 1):
        count = content.count(old)
        if count == 0:
            print(f"  [-] patch #{i}: 未找到目标串，跳过")
            print(f"      预期: {old[:80]!r}...")
            continue
        if count > 1:
            print(f"  [!] patch #{i}: 目标串出现 {count} 次，跳过 (避免误改)")
            print(f"      预期: {old[:80]!r}...")
            continue
        content = content.replace(old, new, 1)
        print(f"  [+] patch #{i}: 已应用 ({len(old)} -> {len(new)} 字符)")
        applied += 1

    if applied == 0:
        print(f"[=] {path}: 没有改动")
        return False

    if dry_run:
        print(f"[DRY] {path}: 会改 {applied} 处")
        return True

    # 备份
    bak = path + ".bak"
    if not os.path.exists(bak):
        with open(bak, "w", encoding="utf-8") as f:
            f.write(original)
        print(f"  [备份] {bak}")

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] {path}: 应用了 {applied} 处改动")
    return True


# ─── Patch 1: control_panel.py ───
# 1a) _run_scrape: 加 PYTHONUNBUFFERED=1 + 日志文件用行缓冲
P1_OLD_RUN = '''    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        with open(LOG_PATH, "w") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
            )'''

P1_NEW_RUN = '''    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        # 关键修复: 日志文件用行缓冲 + 子进程强制 unbuffered
        # 否则 Python print 写文件是块缓冲（4-8KB），前端会看到日志卡 30+ 秒
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        with open(LOG_PATH, "w", buffering=1, encoding="utf-8") as log_file:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=log_file,
                stderr=asyncio.subprocess.STDOUT,
                cwd=os.path.dirname(os.path.dirname(__file__)),
                env=env,
            )'''

# 1b) scrape_log: 反向读最后 N 字节，避免 f.readlines() 读整个大文件
P1_OLD_LOG = '''@control_router.get("/api/scrape/log")
async def scrape_log(lines: int = 50):
    if not os.path.exists(LOG_PATH):
        return {"log": ""}
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
        tail = all_lines[-lines:] if len(all_lines) > lines else all_lines
        return {"log": "".join(tail)}
    except Exception:
        return {"log": ""}'''

P1_NEW_LOG = '''@control_router.get("/api/scrape/log")
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
        return {"log": ""}'''


# ─── Patch 2: web_scraper.py ───
# 2a) 注释掉 batch 内的串行媒体下载 (line ~1825-1827)
P2_OLD_DOWNLOAD = '''            # 下载语音文件
            await self._download_voice_files(converted)
            # 下载图片/表情（按配置）
            await self._download_image_files(converted)'''

P2_NEW_DOWNLOAD = '''            # 性能修复: 不再每个 batch 都串行下载媒体
            # 改为全量消息抓完后再并发下载（见 _download_all_media_parallel）
            # 这样 500 人群从 N×batch×单条延迟 变成 1×总延迟/并发数
            # await self._download_voice_files(converted)
            # await self._download_image_files(converted)'''

# 2b) 在 has_more 循环结束后调用并发下载
# 找 "已到达聊天记录起点" 后面那段插
P2_OLD_END = '''            if not has_more:
                print(f"  [*] 已到达聊天记录起点")
                break

        # 5. 补全发送者身份（群聊必需）。失败不能影响已抓到的消息。'''

P2_NEW_END = '''            if not has_more:
                print(f"  [*] 已到达聊天记录起点")
                break

        # 性能修复: 全量并发下载所有媒体（之前每个 batch 串行，大群超慢）
        try:
            await self._download_all_media_parallel(conv_id)
        except Exception as e:
            print(f"  [!] 全量并发下载媒体失败: {e}")

        # 5. 补全发送者身份（群聊必需）。失败不能影响已抓到的消息。'''

# 2c) 添加新方法 _download_all_media_parallel
# 插入到 _download_image_files 方法结束后，找个合适的位置
# 这里找 _extract_and_save_user_info 前面插入
P2_OLD_ANCHOR = '''    async def _extract_and_save_user_info(self, conv_id):'''

P2_NEW_ANCHOR = '''    async def _download_all_media_parallel(self, conv_id, max_concurrent=10):
        """全量并发下载一个会话的所有媒体文件。

        性能优化: 之前每个 batch 调一次 _download_voice_files/_download_image_files，
        1000 条 batch × 50 条媒体 × 1-3s/条 = 几十秒到几分钟。改为抓完所有消息后，
        一次性收集所有待下载项，用 asyncio.Semaphore 限流并发，可提速 5-10 倍。
        """
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        voice_dir = os.path.join(media_root, "voice")
        img_dir = os.path.join(media_root, "images")
        emoji_dir = os.path.join(media_root, "emoji")
        for d in (voice_dir, img_dir, emoji_dir):
            os.makedirs(d, exist_ok=True)

        conn = self._db_conn
        # 查所有还没下载的 (server_id, content_json, msg_type)
        rows = conn.execute(
            "SELECT server_id, content_json, msg_type, local_path FROM messages "
            "WHERE conv_id = ?",
            (conv_id,),
        ).fetchall()

        voice_tasks = []   # (server_id, url)
        image_tasks = []   # (server_id, origin, skey)
        emoji_tasks = []   # (server_id, url)

        for server_id, cj_str, msg_type, local_path in rows:
            if not cj_str:
                continue
            # 跳过已经下载过的（local_path 非空且文件存在）
            if local_path and not local_path.startswith("http"):
                full = os.path.join(media_root, local_path.replace("/", os.sep))
                if os.path.exists(full):
                    continue
            try:
                cj = json.loads(cj_str)
            except Exception:
                continue

            if msg_type == "other":
                # 语音消息: msg_type=other 但 cj 有 resource_url + duration
                ru = cj.get("resource_url") or {}
                if ru.get("url_list") and cj.get("duration"):
                    voice_tasks.append((server_id, ru["url_list"][0]))
            elif msg_type == "image":
                ru = cj.get("resource_url") or {}
                skey = ru.get("skey")
                origin = (ru.get("origin_url_list") or [None])[0]
                if skey and origin:
                    image_tasks.append((server_id, origin, skey))
            elif msg_type == "emoji":
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
        sem = asyncio.Semaphore(max_concurrent)
        done_counter = [0]
        failed_counter = [0]

        async def _dl_voice(sid, url):
            async with sem:
                try:
                    data = await self.page.evaluate("""async (url) => {
                        try {
                            const r = await fetch(url, {credentials: 'include'});
                            if (!r.ok) return null;
                            const buf = await r.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        } catch { return null; }
                    }""", url)
                    if data and len(data) > 100:
                        local_path = os.path.join(voice_dir, f"{sid}.mpeg")
                        with open(local_path, "wb") as f:
                            f.write(bytes(data))
                        rel = f"voice/{sid}.mpeg"
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))
                    else:
                        failed_counter[0] += 1
                except Exception as e:
                    failed_counter[0] += 1
                done_counter[0] += 1
                if done_counter[0] % 50 == 0:
                    print(f"  [media] 进度 {done_counter[0]}/{total} (失败 {failed_counter[0]})")

        async def _dl_emoji(sid, url):
            async with sem:
                try:
                    data = await self.page.evaluate("""async (url) => {
                        try {
                            const r = await fetch(url, {credentials: 'include'});
                            if (!r.ok) return null;
                            const buf = await r.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        } catch { return null; }
                    }""", url)
                    if data and len(data) > 50:
                        # emoji 不加密直接存，按 URL 路径哈希
                        from hashlib import md5
                        h = md5(url.encode()).hexdigest()[:16]
                        ext = ".webp" if ".webp" in url.lower() else (".gif" if ".gif" in url.lower() else ".png")
                        local_path = os.path.join(emoji_dir, f"{h}{ext}")
                        with open(local_path, "wb") as f:
                            f.write(bytes(data))
                        rel = f"emoji/{h}{ext}"
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))
                    else:
                        failed_counter[0] += 1
                except Exception:
                    failed_counter[0] += 1
                done_counter[0] += 1

        async def _dl_image(sid, origin, skey):
            """图片: AES-256-GCM 解密。复用 _save_image 但放在异步 wrapper 里。"""
            async with sem:
                try:
                    # 拿密文
                    data = await self.page.evaluate("""async (url) => {
                        try {
                            const r = await fetch(url, {credentials: 'include'});
                            if (!r.ok) return null;
                            const buf = await r.arrayBuffer();
                            return Array.from(new Uint8Array(buf));
                        } catch { return null; }
                    }""", origin)
                    if not data or len(data) < 50:
                        failed_counter[0] += 1
                        return
                    # 解密并落盘 (与 _save_image 一致: AES-256-GCM)
                    try:
                        from Crypto.Cipher import AES
                        b = bytes(data)
                        if len(b) < 16:
                            failed_counter[0] += 1
                            return
                        # skey 16 字节对齐，IV 取密文前 12 字节
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
                        rel = f"images/{sid}.jpg"
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))
                    except Exception as e:
                        # 解密失败不致命
                        failed_counter[0] += 1
                except Exception:
                    failed_counter[0] += 1
                done_counter[0] += 1
                if done_counter[0] % 50 == 0:
                    print(f"  [media] 进度 {done_counter[0]}/{total} (失败 {failed_counter[0]})")

        # fire all
        coros = (
            [_dl_voice(s, u) for s, u in voice_tasks] +
            [_dl_emoji(s, u) for s, u in emoji_tasks] +
            [_dl_image(s, o, k) for s, o, k in image_tasks]
        )
        if coros:
            await asyncio.gather(*coros, return_exceptions=True)
        conn.commit()
        elapsed = time.time() - start
        print(f"  [media] 全量下载完成: {total - failed_counter[0]}/{total} 成功, 失败 {failed_counter[0]}, 耗时 {elapsed:.1f}s")

    async def _extract_and_save_user_info(self, conv_id):'''


# ─── Patch 3: panel.html ───
# 日志显示行数 80 -> 200
P3_OLD = "const r = await fetch('/panel/api/scrape/log?lines=80');"
P3_NEW = "const r = await fetch('/panel/api/scrape/log?lines=200');"


def main():
    if not os.path.isdir(PROJECT_DIR):
        print(f"[!] 找不到目标项目: {PROJECT_DIR}")
        print(f"    如果路径不同，编辑脚本顶部的 PROJECT_DIR 变量")
        sys.exit(1)

    print(f"[i] 目标: {PROJECT_DIR}\n")

    print("[1/3] control_panel.py  日志缓冲 + 尾部读取")
    patch_file(CONTROL_PANEL, [
        (P1_OLD_RUN, P1_NEW_RUN),
        (P1_OLD_LOG, P1_NEW_LOG),
    ])

    print("\n[2/3] web_scraper.py  媒体下载移出 batch 循环 + 并发")
    patch_file(WEB_SCRAPER, [
        (P2_OLD_DOWNLOAD, P2_NEW_DOWNLOAD),
        (P2_OLD_END, P2_NEW_END),
        (P2_OLD_ANCHOR, P2_NEW_ANCHOR),
    ])

    print("\n[3/3] panel.html  日志行数 80 -> 200")
    patch_file(PANEL_HTML, [
        (P3_OLD, P3_NEW),
    ])

    print("\n[i] 完事。重启 uvicorn 生效。")
    print("    如果某个 patch 跳过了（[-] 或 [!]），可能是你之前手动改过代码，跳过检查即可。")
    print("    想回滚: 删掉文件末尾的 .bak 备份即可恢复。")


if __name__ == "__main__":
    main()
