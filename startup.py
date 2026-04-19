"""
Bot・ngrok を起動し、LINE Webhookを自動更新する起動スクリプト。
"""
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).parent
LOG_DIR = BASE / "logs"
LOG_DIR.mkdir(exist_ok=True)

LOGFILE = LOG_DIR / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
PYTHON = BASE / "venv" / "Scripts" / "python.exe"
NGROK = BASE / "ngrok.exe"


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line)
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def cleanup_old_logs(days: int = 7) -> None:
    cutoff = datetime.now().timestamp() - days * 86400
    for p in LOG_DIR.glob("bot_*.log"):
        if p.stat().st_mtime < cutoff:
            p.unlink()
            log(f"古いログを削除: {p.name}")


def start_bot() -> subprocess.Popen:
    log("Bot起動中...")
    proc = subprocess.Popen(
        [str(PYTHON), str(BASE / "bot.py")],
        stdout=open(LOGFILE, "a", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        cwd=str(BASE),
    )
    log(f"Bot PID: {proc.pid}")
    return proc


def start_ngrok() -> subprocess.Popen:
    log("ngrok起動中...")
    proc = subprocess.Popen(
        [str(NGROK), "http", "5000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(BASE),
    )
    log(f"ngrok PID: {proc.pid}")
    return proc


def update_webhook() -> None:
    log("LINE Webhook更新中...")
    result = subprocess.run(
        [str(PYTHON), str(BASE / "update_webhook.py")],
        capture_output=True,
        text=True,
        cwd=str(BASE),
    )
    if result.returncode == 0:
        log(result.stdout.strip())
    else:
        log(f"Webhook更新エラー: {result.stderr.strip()}")


if __name__ == "__main__":
    log("=== StudyBot 起動開始 ===")
    cleanup_old_logs()
    start_bot()
    time.sleep(4)
    start_ngrok()
    time.sleep(10)
    update_webhook()
    log("=== 起動完了 ===")
