"""Retry the failed files (most are p9-sign SSL issues)."""
import sqlite3, urllib.request, ssl, os, sys, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

DB = r"D:\douyin-chat-export-complete\app\data\chat.db"
OUT = r"D:\fjj_media_export"
CONVS = [
    ("0:1:98880361157:7588998437715723322", "fjj"),
    ("0:1:98880361157:281069785987623",     "凤晶晶"),
]
TYPE_DIR = {2: "images", 3: "voice", 4: "videos"}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
REFERER = "https://www.douyin.com/"

def get_ext(url, t):
    p = urlparse(url).path.lower()
    for ext in (".webp",".jpeg",".jpg",".png",".gif",".mp4",".m4a",".mp3",".aac"):
        if p.endswith(ext): return ext
    return {2:".jpg",3:".m4a",4:".mp4"}.get(t,".bin")

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
LOCK = threading.Lock()
STATS = {"ok":0, "fail":0, "errors":[]}

def fetch(url, dst, attempts=3):
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": REFERER})
            with urllib.request.urlopen(req, timeout=30, context=CTX) as r:
                data = r.read()
            if len(data) < 50:
                raise RuntimeError(f"too small {len(data)}B")
            with open(dst, "wb") as f:
                f.write(data)
            with LOCK: STATS["ok"] += 1
            return True
        except Exception as e:
            if i == attempts - 1:
                with LOCK:
                    STATS["fail"] += 1
                    if len(STATS["errors"]) < 30:
                        STATS["errors"].append(f"{url[:80]}: {e}")
            else:
                time.sleep(0.5 * (i + 1))
    return False

def main():
    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    print(f"Retry mode, workers={workers}")
    conn = sqlite3.connect(DB)
    # 找出还没下载的 (size==0 或 不存在)
    tasks = []
    for conv_id, name in CONVS:
        for msg_type, type_dir in TYPE_DIR.items():
            sub = os.path.join(OUT, name, type_dir)
            if not os.path.isdir(sub):
                continue
            for row in conn.execute(
                "SELECT msg_id, media_url FROM messages "
                "WHERE conv_id=? AND msg_type=? AND media_url IS NOT NULL",
                (conv_id, msg_type),
            ):
                msg_id, url = row
                ext = get_ext(url, msg_type)
                dst = os.path.join(sub, f"{msg_id}{ext}")
                if not os.path.exists(dst) or os.path.getsize(dst) < 50:
                    tasks.append((url, dst))
    print(f"Pending: {len(tasks)}")
    if not tasks:
        return
    start = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fetch, u, d): (u, d) for u, d in tasks}
        for fut in as_completed(futs):
            done += 1
            if done % 20 == 0 or done == len(tasks):
                el = time.time() - start
                rate = done / max(el, 0.01)
                eta = (len(tasks) - done) / max(rate, 0.01)
                print(f"\r[{done}/{len(tasks)}] ok={STATS['ok']} fail={STATS['fail']} {rate:.1f}f/s ETA {eta:.0f}s   ", end="", flush=True)
    el = time.time() - start
    print()
    print(f"Done in {el:.1f}s: ok={STATS['ok']} fail={STATS['fail']}")
    for e in STATS["errors"][:10]:
        print(f"  - {e}")

if __name__ == "__main__":
    main()
