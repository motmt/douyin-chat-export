#!/bin/bash
set -e

MODE="${MODE:-all}"
UVICORN_PID=""
SCHEDULER_PID=""

cleanup() {
    echo "[entrypoint] Shutting down..."
    [ -n "$UVICORN_PID" ] && kill "$UVICORN_PID" 2>/dev/null
    [ -n "$SCHEDULER_PID" ] && kill "$SCHEDULER_PID" 2>/dev/null
    wait
    exit 0
}
trap cleanup SIGTERM SIGINT

# Ensure DB schema exists
python -c "from extractor.models import init_db; init_db()"

echo "[entrypoint] MODE=$MODE"

# Start web server
if [ "$MODE" = "web" ] || [ "$MODE" = "all" ]; then
    if [ "$MODE" = "web" ]; then
        # Foreground 鈥?this is the only service
        echo "[entrypoint] Starting web server (foreground)..."
        exec python -E start_server.py --host 0.0.0.0 --port 8001
    else
        # Background 鈥?scraper/scheduler will also run
        echo "[entrypoint] Starting web server (background)..."
        python -E start_server.py --host 0.0.0.0 --port 8001 &
        UVICORN_PID=$!
        sleep 1
    fi
fi

# Start scraper / scheduler
if [ "$MODE" = "scraper" ] || [ "$MODE" = "all" ]; then
    if [ -n "$SCRAPER_SCHEDULE" ]; then
        echo "[entrypoint] Starting scheduler (schedule: $SCRAPER_SCHEDULE)..."
        python -u scheduler.py &
        SCHEDULER_PID=$!
    else
        if [ "$MODE" = "scraper" ]; then
            # Run once and exit
            echo "[entrypoint] Running scraper once..."
            CMD="python -u extract.py"
            [ "$SCRAPER_INCREMENTAL" = "true" ] && CMD="$CMD --incremental"
            [ -n "$SCRAPER_FILTER" ] && CMD="$CMD --filter \"$SCRAPER_FILTER\""
            eval $CMD
            echo "[entrypoint] Scraper finished."
            exit 0
        fi
        # MODE=all without schedule: just start web, no auto-scrape
        # (scraping can be triggered from control panel)
    fi
fi

# Wait for background processes
echo "[entrypoint] Services running. Waiting..."
wait

