# douyin-chat-export complete Windows package

This folder is a complete portable package:

```text
D:\douyin-chat-export-complete
  app\    project source code
  tools\  Windows helper scripts
```

The package does not include private runtime data by default:

- `app\data\browser_profile` login cookies
- `app\data\chat.db` exported chat database
- `app\data\media` downloaded media

If you need to migrate an existing login state or exported chats, copy the old
`data` folder into `app\data` yourself.

## First run on a new Windows PC

Install these first:

- Chrome or Edge

Python can be installed automatically as a portable runtime if it is missing.

Then run scripts in this order:

```text
tools\00_install_deps_nju.bat
tools\01_cleanup_backend.bat
tools\02_start_server.bat
```

`00_install_deps_nju.bat` asks for a mode:

```text
0 = install Python dependencies only
1 = install portable Node.js + Python dependencies + frontend
```

Choose `1` for the first full setup. Node.js is downloaded into:

```text
runtime\nodejs
```

It uses the npmmirror Node.js binary mirror and does not require a system-wide
Node.js install.

Python dependencies are installed into:

```text
app\venv
```

The start/login scripts use this venv automatically, so dependencies do not get
mixed with another Python on the same Windows PC.

If system Python is missing, Python 3.12.10 is downloaded from Aliyun mirror and
installed into:

```text
runtime\python
```

Open:

```text
http://127.0.0.1:8001/
http://127.0.0.1:8001/panel
```

## Login

Use the panel login page first. If QR login in the panel does not work, run:

```text
tools\04_login_qr.bat
```

## Daily use

```text
tools\01_cleanup_backend.bat
tools\02_start_server.bat
```

## If viewer root shows Not Found

```text
tools\03_build_frontend.bat
tools\01_cleanup_backend.bat
tools\02_start_server.bat
```

## Check status

```text
tools\05_check_status.bat
```

## Fix Playwright browser missing

If login or cookie import says `Executable doesn't exist` under
`ms-playwright`, run:

```text
tools\07_fix_playwright_browser.bat
```

Playwright browser downloads use:

```text
https://npmmirror.com/mirrors/playwright
```

## Mirrors

Python packages use NJU PyPI:

```text
https://mirrors.nju.edu.cn/pypi/web/simple
```

npm packages use NJU npm:

```text
https://repo.nju.edu.cn/repository/npm/
```

