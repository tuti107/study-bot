"""
ngrokのURLを取得してLINE Webhookに自動設定し、親のLINEに通知する。
"""
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
PARENT_USER_ID = os.environ["PARENT_USER_ID"]


def get_ngrok_url(retries: int = 20, interval: float = 3.0) -> str:
    for i in range(retries):
        try:
            resp = requests.get("http://localhost:4040/api/tunnels", timeout=3)
            data = resp.json()
            tunnels = data.get("tunnels", [])
            print(f"  トンネル数: {len(tunnels)}, データ: {[t.get('proto') for t in tunnels]}")
            # httpsを優先、なければhttpも許容
            for proto in ("https", "http"):
                for t in tunnels:
                    if t.get("proto") == proto:
                        url = t["public_url"]
                        # httpをhttpsに変換
                        if url.startswith("http://"):
                            url = "https://" + url[7:]
                        return url
        except Exception as e:
            print(f"  エラー: {e}")
        print(f"ngrok待機中... ({i+1}/{retries})")
        time.sleep(interval)
    raise RuntimeError("ngrokのURLを取得できませんでした。")


def update_line_webhook(url: str) -> None:
    webhook_url = f"{url}/webhook"
    resp = requests.put(
        "https://api.line.me/v2/bot/channel/webhook/endpoint",
        headers={
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        },
        json={"endpoint": webhook_url},
        timeout=(5, 30),
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Webhook更新失敗: {resp.status_code} {resp.text}")
    print(f"Webhook更新完了: {webhook_url}")


def notify_parent(url: str) -> None:
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}",
        },
        json={
            "to": PARENT_USER_ID,
            "messages": [{"type": "text", "text": f"勉強Botが起動しました。\nURL: {url}"}],
        },
        timeout=(5, 30),
    )
    if resp.status_code != 200:
        print(f"親通知失敗: {resp.status_code} {resp.text}", file=sys.stderr)


if __name__ == "__main__":
    try:
        url = get_ngrok_url()
        update_line_webhook(url)
        notify_parent(url)
        print("起動完了")
    except Exception as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
