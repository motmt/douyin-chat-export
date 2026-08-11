"""patch_organize_media.py - 媒体文件按会话分目录（轻量补丁）

目的:
    解决 D 盘那个 1881 个 media 文件全堆在 data/media/{voice,images,emoji}/
    不知道哪个属于哪个会话的问题。

策略 (最简单的方案 - 不用 2D 分类):
    - 老文件保持原位: data/media/{voice,images,emoji}/xxx.mpeg  ← 不动
    - 新下载走新位置: data/media/conv_<safe_id>/{voice,images,emoji}/xxx.mpeg
    - DB 里 local_path 同步改成新相对路径
    - 老记录 local_path 保持原样 (相对路径还是 "voice/xxx.mpeg" 仍然有效)

变更范围:
    extractor/web_scraper.py:
      1. 新增 _conv_subdir(conv_id) 辅助函数
      2. _download_all_media_parallel() 改用 conv subdir
      3. _download_voice_files(messages, conv_id=None) 接收 conv_id 可选参数
      4. _download_image_files(messages, conv_id=None) 接收 conv_id 可选参数

兼容性:
    - 不删老文件
    - 不改 DB schema
    - 不动 call sites 也能跑 (conv_id=None 时退化为老路径)
    - 老相对路径 "voice/xxx.mpeg" / "images/xxx.jpg" / "emoji/xxx.png" 仍然有效

用法:
    python patch_organize_media.py                          # 默认补丁当前目录
    python patch_organize_media.py C:\path\to\project        # 指定项目根
    python patch_organize_media.py . --dry-run               # 只看不改
    python patch_organize_media.py . --revert                # 回滚
"""

import re
import sys
from pathlib import Path

TARGET_FILE = "extractor/web_scraper.py"


def _conv_subdir_code() -> str:
    """返回 _conv_subdir 辅助函数代码片段"""
    return '''

def _conv_subdir(conv_id):
    """返回会话专属子目录名，如 'conv_0_1_62185797183_808834269460910'。

    把冒号替换成下划线，避免 Windows 路径非法字符。
    同一个会话不同次抓取，subdir 相同，可保证新下载落同一目录。
    """
    if not conv_id:
        return ""
    safe = re.sub(r'[<>:"/\\\\|?*]', '_', str(conv_id))
    return f"conv_{safe}"'''


def patches():
    """返回 (anchor, old, new) 列表，按顺序应用。"""
    p1_old = '''    async def _download_voice_files(self, messages):
        """下载语音消息的音频文件到本地"""
        voice_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media", "voice")
        os.makedirs(voice_dir, exist_ok=True)'''
    p1_new = '''    async def _download_voice_files(self, messages, conv_id=None):
        """下载语音消息的音频文件到本地

        conv_id: 传入则把语音放到 data/media/conv_<id>/voice/ 子目录；
                 传入 None 退化为 data/media/voice/（兼容老代码路径）
        """
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        if conv_id:
            media_root = os.path.join(media_root, _conv_subdir(conv_id))
        voice_dir = os.path.join(media_root, "voice")
        os.makedirs(voice_dir, exist_ok=True)'''
    p1_anchor = '''    async def _download_voice_files(self, messages):'''

    p2_old = '''    async def _download_image_files(self, messages):
        """下载图片/表情包到本地。
        - 表情：直接保存（无加密），按 URL 路径哈希去重
        - 图片：从 origin_url 拉密文，AES-256-GCM 解密 (skey) 后保存
        """
        if not self.download_images:
            return
        media_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media")
        img_dir = os.path.join(media_root, "images")
        emoji_dir = os.path.join(media_root, "emoji")
        os.makedirs(img_dir, exist_ok=True)
        os.makedirs(emoji_dir, exist_ok=True)'''
    p2_new = '''    async def _download_image_files(self, messages, conv_id=None):
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
        os.makedirs(emoji_dir, exist_ok=True)'''
    p2_anchor = '''    async def _download_image_files(self, messages):'''

    p3_old = '''    async def _download_all_media_parallel(self, conv_id, max_concurrent=10):
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
            os.makedirs(d, exist_ok=True)'''
    p3_new = '''    async def _download_all_media_parallel(self, conv_id, max_concurrent=10):
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
            os.makedirs(d, exist_ok=True)'''
    p3_anchor = '''    async def _download_all_media_parallel(self, conv_id, max_concurrent=10):'''

    p4_old = '''                    if data and len(data) > 100:
                        local_path = os.path.join(voice_dir, f"{sid}.mpeg")
                        with open(local_path, "wb") as f:
                            f.write(bytes(data))
                        rel = f"voice/{sid}.mpeg"
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))'''
    p4_new = '''                    if data and len(data) > 100:
                        local_path = os.path.join(voice_dir, f"{sid}.mpeg")
                        with open(local_path, "wb") as f:
                            f.write(bytes(data))
                        rel = os.path.join(_conv_subdir(conv_id), "voice", f"{sid}.mpeg").replace("\\\\", "/")
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))'''
    p4_anchor = '''                    if data and len(data) > 100:
                        local_path = os.path.join(voice_dir, f"{sid}.mpeg")'''

    p5_old = '''                        h = md5(url.encode()).hexdigest()[:16]
                        ext = ".webp" if ".webp" in url.lower() else (".gif" if ".gif" in url.lower() else ".png")
                        local_path = os.path.join(emoji_dir, f"{h}{ext}")
                        with open(local_path, "wb") as f:
                            f.write(bytes(data))
                        rel = f"emoji/{h}{ext}"
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))'''
    p5_new = '''                        h = md5(url.encode()).hexdigest()[:16]
                        ext = ".webp" if ".webp" in url.lower() else (".gif" if ".gif" in url.lower() else ".png")
                        local_path = os.path.join(emoji_dir, f"{h}{ext}")
                        with open(local_path, "wb") as f:
                            f.write(bytes(data))
                        rel = os.path.join(_conv_subdir(conv_id), "emoji", f"{h}{ext}").replace("\\\\", "/")
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))'''
    p5_anchor = '''                        h = md5(url.encode()).hexdigest()[:16]
                        ext = ".webp" if ".webp" in url.lower() else (".gif" if ".gif" in url.lower() else ".png")'''

    p6_old = '''                        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
                        local_path = os.path.join(img_dir, f"{sid}.jpg")
                        with open(local_path, "wb") as f:
                            f.write(plaintext)
                        rel = f"images/{sid}.jpg"
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))'''
    p6_new = '''                        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
                        local_path = os.path.join(img_dir, f"{sid}.jpg")
                        with open(local_path, "wb") as f:
                            f.write(plaintext)
                        rel = os.path.join(_conv_subdir(conv_id), "images", f"{sid}.jpg").replace("\\\\", "/")
                        conn.execute("UPDATE messages SET local_path = ? WHERE server_id = ?", (rel, sid))'''
    p6_anchor = '''                        plaintext = cipher.decrypt_and_verify(ciphertext, tag)'''

    return [
        (p1_anchor, p1_old, p1_new),
        (p2_anchor, p2_old, p2_new),
        (p3_anchor, p3_old, p3_new),
        (p4_anchor, p4_old, p4_new),
        (p5_anchor, p5_old, p5_new),
        (p6_anchor, p6_old, p6_new),
    ]


REVERT_PATCHES = [
    ("def _conv_subdir(conv_id):", None, ""),  # remove helper if present
]


def apply_patches(project_root: Path, dry_run: bool = False, revert: bool = False):
    target = project_root / TARGET_FILE
    if not target.exists():
        print(f"❌ 找不到 {target}")
        return 1

    text = target.read_text(encoding="utf-8")
    original_text = text

    if revert:
        # Revert: 把 _conv_subdir 整段删掉，再把所有老代码还原
        if "def _conv_subdir(conv_id):" in text:
            text = re.sub(
                r"\n\ndef _conv_subdir\(conv_id\):.*?(?=\n\ndef |\nasync def |\nclass )",
                "\n",
                text,
                count=1,
                flags=re.DOTALL,
            )
            print("✓ 移除 _conv_subdir 辅助函数")
        # Reverse patches in reverse order
        for anchor, old, new in reversed(patches()):
            if new in text:
                text = text.replace(new, old, 1)
                print(f"✓ 回滚: {anchor[:60]}")
        if text != original_text:
            if not dry_run:
                target.write_text(text, encoding="utf-8")
                print(f"\n✓ 已回滚 {target}")
            else:
                print(f"\n[DRY-RUN] 会回滚 {target}")
        else:
            print("\n没有发现可回滚的改动")
        return 0

    # 1. 先插入 _conv_subdir 辅助函数（放在 _save_emoji 之前）
    helper = _conv_subdir_code().lstrip("\n")
    if "def _conv_subdir(conv_id):" in text:
        print("• _conv_subdir 辅助函数已存在，跳过")
    else:
        anchor = "def _save_emoji(url, emoji_dir):"
        if anchor in text:
            text = text.replace(anchor, helper + "\n\n\n" + anchor, 1)
            print("✓ 插入 _conv_subdir 辅助函数")
        else:
            print(f"❌ 找不到锚点 {anchor}，无法插入辅助函数")
            return 1

    # 2. 应用 7 个改动
    applied = 0
    for anchor, old, new in patches():
        if new in text:
            print(f"• 已应用: {anchor[:60]}")
            continue
        if old not in text:
            print(f"⚠ 跳过 (找不到 old): {anchor[:60]}")
            continue
        text = text.replace(old, new, 1)
        applied += 1
        print(f"✓ 应用: {anchor[:60]}")

    if text != original_text:
        if not dry_run:
            target.write_text(text, encoding="utf-8")
            print(f"\n✓ 已写入 {target}，共 {applied} 处改动")
        else:
            print(f"\n[DRY-RUN] 会写入 {target}，共 {applied} 处改动")
    else:
        print(f"\n没有需要改的地方 (可能已经补丁过)")

    return 0


def main():
    import argparse
    ap = argparse.ArgumentParser(description="媒体按会话分目录补丁")
    ap.add_argument("project_root", nargs="?", default=".", help="项目根目录")
    ap.add_argument("--dry-run", action="store_true", help="只看不改")
    ap.add_argument("--revert", action="store_true", help="回滚补丁")
    args = ap.parse_args()

    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        print(f"❌ 项目根不存在: {project_root}")
        return 1

    return apply_patches(project_root, dry_run=args.dry_run, revert=args.revert)


if __name__ == "__main__":
    sys.exit(main())
