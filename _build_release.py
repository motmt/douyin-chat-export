# -*- coding: utf-8 -*-
"""打包 release zip：包含代码 + 启动脚本 + README（不含 venv/data/runtime/缓存）。"""
import os
import zipfile

ROOT = r'D:\douyin-chat-export-complete'
OUT = r'D:\douyin-chat-export-complete\douyin-chat-export-release.zip'

# 排除规则（目录名或文件名前缀）
SKIP_DIRS = {
    'venv', 'data', 'runtime', '__pycache__', '.workbuddy', '_original',
    'node_modules', '.git', 'logs',
}
SKIP_FILES = {
    '.server.log', '.server2.log', '.server3.log', '.server4.log',
}
SKIP_EXT = {'.pyc', '.bak', '.rar'}

def should_skip(relpath: str, is_dir: bool) -> bool:
    # relpath 统一为相对项目根
    parts = relpath.replace('\\', '/').split('/')
    if any(p in SKIP_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in SKIP_FILES:
        return True
    if any(name.endswith(e) for e in SKIP_EXT):
        return True
    return False

count = 0
with zipfile.ZipFile(OUT, 'w', zipfile.ZIP_DEFLATED) as zf:
    # 根 README 和 gitignore
    zf.write(os.path.join(ROOT, 'README.md'), 'README.md')
    zf.write(os.path.join(ROOT, '.gitignore'), '.gitignore')
    count += 2
    # tools/（安装/检查脚本）
    tools_root = os.path.join(ROOT, 'tools')
    for root, dirs, files in os.walk(tools_root):
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, ROOT)
            zf.write(fp, rel.replace('\\', '/'))
            count += 1
    # app 下所有内容（递归）
    app_root = os.path.join(ROOT, 'app')
    for root, dirs, files in os.walk(app_root):
        # 过滤目录（统一用相对根路径判断）
        dirs[:] = [d for d in dirs
                   if not should_skip(os.path.relpath(os.path.join(root, d), ROOT), True)]
        for f in files:
            fp = os.path.join(root, f)
            rel = os.path.relpath(fp, ROOT)
            if should_skip(rel, False):
                continue
            zf.write(fp, rel.replace('\\', '/'))
            count += 1

size = os.path.getsize(OUT) / 1024 / 1024
print(f'打包完成: {OUT} ({size:.1f} MB, {count} 个文件)')
