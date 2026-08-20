# -*- coding: utf-8 -*-
"""打包 release zip：包含代码 + 启动脚本 + README（不含 venv/data/runtime/缓存）。

用法: python tools/build_release.py
输出: douyin-chat-export-release.zip（项目根）
"""
import os
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "douyin-chat-export-release.zip")

SKIP_DIRS = {
    'venv', 'data', 'runtime', '__pycache__', '.workbuddy', '_original',
    'node_modules', '.git', 'logs',
}
SKIP_FILES = {
    '.server.log', '.server2.log', '.server3.log', '.server4.log',
    '.server_fix.log', '.server_verify.log', '.server_test.log', '.server_test2.log',
    'douyin-chat-export-release.zip',
}
SKIP_EXT = {'.pyc', '.bak', '.rar'}


def should_skip(relpath: str) -> bool:
    parts = relpath.replace('\\', '/').split('/')
    if any(p in SKIP_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in SKIP_FILES:
        return True
    if any(name.endswith(e) for e in SKIP_EXT):
        return True
    return False


def main():
    if os.path.exists(OUT):
        os.remove(OUT)
    count = 0
    with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in ['README.md', '.gitignore', 'ATTRIBUTION.md']:
            fp = os.path.join(ROOT, f)
            if os.path.exists(fp):
                zf.write(fp, f)
                count += 1
        # tools/
        for root, dirs, files in os.walk(os.path.join(ROOT, 'tools')):
            dirs[:] = [d for d in dirs if not should_skip(os.path.relpath(os.path.join(root, d), ROOT))]
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, ROOT)
                if should_skip(rel):
                    continue
                zf.write(fp, rel.replace('\\', '/'))
                count += 1
        # app/
        for root, dirs, files in os.walk(os.path.join(ROOT, 'app')):
            dirs[:] = [d for d in dirs if not should_skip(os.path.relpath(os.path.join(root, d), ROOT))]
            for f in files:
                fp = os.path.join(root, f)
                rel = os.path.relpath(fp, ROOT)
                if should_skip(rel):
                    continue
                zf.write(fp, rel.replace('\\', '/'))
                count += 1
    size = os.path.getsize(OUT) / 1024 / 1024
    print(f'打包完成: {OUT} ({size:.1f} MB, {count} 个文件)')


if __name__ == '__main__':
    main()
