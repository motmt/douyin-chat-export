# 抖音聊天记录导出工具 (Douyin Chat Export)

从抖音网页版完整导出私信聊天记录，本地 Web 面板浏览、搜索、导出，支持图片/语音/视频/表情媒体下载。

## ✨ 功能

- **完整采集**：调用抖音 IM 接口抓取，突破网页虚拟列表滚动上限，可导出完整历史
- **本地面板**：`http://127.0.0.1:8001/panel`，浏览、搜索、筛选聊天记录
- **媒体下载**：图片、语音、表情按会话分目录（`data/media/<对方昵称>_<短ID>/`），视频走签名解析
- **多格式导出**：JSON / JSONL（ChatLab 格式），按会话分文件夹（`data/exports/<对方昵称>/`）
- **多账号支持**：同名会话按会话 ID 精确区分，各自独立导出
- **定时采集**：内置 APScheduler，可按 cron 定时增量抓取
- **登录方式**：扫码登录 / Cookie 导入

## 🚀 快速开始（Windows）

```bat
setup.bat    REM 第一次：自动创建 venv + 安装依赖 + 下载 Chromium
start.bat    REM 启动服务，自动打开面板
stop.bat     REM 停止服务并清理残留进程
```

首次使用流程：

1. 双击 `setup.bat`（需要已安装 Python 3.11+ 且在 PATH 中）
2. 双击 `start.bat`，浏览器自动打开 `http://127.0.0.1:8001/panel`
3. 面板里「刷新会话列表」→ 勾选会话 → 「采集」
4. 采集完成后可浏览消息、下载媒体、导出记录

## 📁 目录结构

```
app/
├── start.bat / stop.bat / setup.bat   # Windows 一键脚本
├── start_server.py                    # 服务入口（uvicorn）
├── extract.py                         # 采集入口
├── extractor/                         # 核心：web_scraper 采集 / exporter 导出 / video_downloader 视频
├── backend/                           # FastAPI 后端 + 控制面板
├── frontend/                          # 前端源码（Vue3 + Vite）
└── data/                              # 运行时数据（不入库）
    ├── chat.db                        # SQLite 数据库
    ├── media/<昵称>_<ID>/{voice,images,emoji,videos}/   # 媒体文件（按会话分目录）
    └── exports/<昵称>/<日期>.jsonl    # 导出文件
```

## 🛠 开发

```bash
cd app/frontend
npm install
npm run build      # 构建前端到 dist/
cd ..
python -m uvicorn backend.main:app --host 127.0.0.1 --port 8001
```

## 📄 导出格式

- JSONL 首行为 header（含 `meta.name = 与<对方昵称>的对话`），随后为 member/message 行
- 支持 ChatLab 0.0.2 格式
- 媒体消息：图片嵌入 CDN URL 或本地 base64，语音/视频转为文字标签

## ⚠️ 说明

- 仅供个人学习与数据备份使用，请遵守抖音平台规则与相关法律法规
- 采集频率过高可能触发风控，建议使用「增量采集」与定时任务
- Playwright Chromium 需要约 300MB 磁盘空间

## 🔗 相关

- 基于 [TeamBreakerr/douyin-chat-export](https://github.com/TeamBreakerr/douyin-chat-export) 二次开发增强
