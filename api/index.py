"""
api/index.py - Telegram Bot Webhook
"""
import os
import re
import glob
import uuid
import logging
from urllib.parse import urlparse
from flask import Flask, request, jsonify
import requests
import yt_dlp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_webhook_bot")
app = Flask(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_FILESIZE_BYTES = 20 * 1024 * 1024

DESKTOP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

def send_telegram_request(method: str, data: dict = None, files: dict = None, timeout: int = 40):
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is missing!")
        return None
    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        if files:
            res = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            res = requests.post(url, json=data, timeout=timeout)
        return res.json()
    except Exception as e:
        logger.error(f"Telegram error {method}: {e}")
        return None

def extract_url(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"https?://[^\s]+", text)
    return m.group(0) if m else ""

def resolve_facebook_url(raw_url: str) -> str:
    url = raw_url.strip().rstrip(").,;!?\"'")
    reel_id = None
    m = re.search(r"/(?:reel|videos)/(\d+)", url) or re.search(r"[?&]v=(\d+)", url)
    if m:
        reel_id = m.group(1)
    if not reel_id and ("facebook.com/share" in url or "fb.watch" in url):
        try:
            resp = requests.get(url, headers={'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X)'}, allow_redirects=True, timeout=10)
            m2 = re.search(r"/(?:reel|videos)/(\d+)", resp.url) or re.search(r"[?&]v=(\d+)", resp.url)
            if m2:
                reel_id = m2.group(1)
        except Exception:
            pass
    if reel_id:
        return f"https://m.facebook.com/watch/?v={reel_id}"
    return url

def handle_message(message: dict):
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = message.get("text", "").strip()

    if not chat_id:
        return
    if text.startswith("/start") or text.startswith("/help"):
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "👋 Welcome to Reel & Shorts Downloader Bot!\n\nSend any YouTube Shorts or Facebook Reel link (under 20MB) to download as MP4 or MP3."
        })
        return

    found_url = extract_url(text)
    if not found_url:
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "ℹ️ Please send a valid link starting with http/https.",
            "reply_to_message_id": message_id
        })
        return

    clean_url = resolve_facebook_url(found_url) if "facebook.com" in found_url or "fb.watch" in found_url else found_url
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "🎥 MP4 Video", "callback_data": "dl_mp4"},
                {"text": "🎵 MP3 Audio", "callback_data": "dl_mp3"}
            ]
        ]
    }
    send_telegram_request("sendMessage", {
        "chat_id": chat_id,
        "text": f"🎬 Choose Download Format:\n{clean_url}",
        "reply_to_message_id": message_id,
        "reply_markup": keyboard
    })

def handle_callback_query(callback_query: dict):
    cq_id = callback_query.get("id")
    data = callback_query.get("data", "")
    bot_message = callback_query.get("message", {})
    chat_id = bot_message.get("chat", {}).get("id")
    bot_msg_id = bot_message.get("message_id")

    send_telegram_request("answerCallbackQuery", {"callback_query_id": cq_id})
    if data not in ["dl_mp4", "dl_mp3"]:
        return

    reply_to = bot_message.get("reply_to_message", {})
    target_url = extract_url(bot_message.get("text", "")) or extract_url(reply_to.get("text", ""))
    clean_target_url = resolve_facebook_url(target_url) if ("facebook.com" in target_url or "fb.watch" in target_url) else target_url

    send_telegram_request("editMessageText", {
        "chat_id": chat_id,
        "message_id": bot_msg_id,
        "text": "⏳ Downloading and processing, please wait..."
    })

    unique_id = str(uuid.uuid4())[:8]
    output_template = f"/tmp/{unique_id}_%(id)s.%(ext)s"
    is_audio = (data == "dl_mp3")

    ydl_opts = {
        'nocheckcertificate': True,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': MAX_FILESIZE_BYTES,
        'noplaylist': True,
        'socket_timeout': 30,
        'outtmpl': output_template,
        'http_headers': DESKTOP_HEADERS,
    }

    proxy_env = os.environ.get("PROXY", "").strip()
    if proxy_env:
        ydl_opts['proxy'] = proxy_env
        logger.info("Custom proxy enabled for yt-dlp")

    po_token_env = os.environ.get("YOUTUBE_PO_TOKEN", "").strip()
    yt_extractor_args = {
        'player_client': ['ios', 'android', 'tv_embedded', 'tv', 'mweb']
    }
    if po_token_env:
        yt_extractor_args['po_token'] = [f"web+{po_token_env}"]
        yt_extractor_args['player_client'] = ['web', 'tv_embedded', 'tv', 'mweb']
        logger.info("YOUTUBE_PO_TOKEN loaded")

    cookies_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_env:
        cookies_clean = cookies_env.replace('\\r\\n', '\n').replace('\\n', '\n').replace('\\t', '\t')
        if "# Netscape HTTP Cookie File" not in cookies_clean:
            cookies_clean = "# Netscape HTTP Cookie File\n# YouTube Session\n\n" + cookies_clean
        cookies_path = f"/tmp/{unique_id}_cookies.txt"
        with open(cookies_path, "w", encoding="utf-8") as cf:
            cf.write(cookies_clean)
        ydl_opts['cookiefile'] = cookies_path
        logger.info(f"YOUTUBE_COOKIES loaded ({len(cookies_clean)} chars)")
    else:
        logger.info("No YOUTUBE_COOKIES found in env")

    ydl_opts['extractor_args'] = {'youtube': yt_extractor_args}

    if is_audio:
        ydl_opts['format'] = 'bestaudio/best/b'
    else:
        ydl_opts['format'] = 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best/b'

    downloaded_files = []
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_target_url, download=True)
            video_title = info.get("title", "Downloaded Media")

        media_files = [f for f in glob.glob(f"/tmp/{unique_id}_*") if not f.endswith("_cookies.txt")]
        if not media_files:
            raise FileNotFoundError("Video could not be downloaded to storage.")
        downloaded_filepath = media_files[0]
        downloaded_files.append(downloaded_filepath)

        file_size = os.path.getsize(downloaded_filepath)
        if file_size > MAX_FILESIZE_BYTES:
            raise ValueError(f"File size ({file_size / (1024*1024):.1f}MB) exceeds the 20MB limit.")

        caption = f"🎬 {video_title}\n\n⚡ Downloaded via Telegram Downloader"
        if is_audio:
            with open(downloaded_filepath, "rb") as af:
                send_telegram_request("sendAudio", data={"chat_id": chat_id, "caption": caption[:1024], "title": video_title[:64]}, files={"audio": (os.path.basename(downloaded_filepath), af, "audio/mpeg")})
        else:
            with open(downloaded_filepath, "rb") as vf:
                send_telegram_request("sendVideo", data={"chat_id": chat_id, "caption": caption[:1024], "supports_streaming": "true"}, files={"video": (os.path.basename(downloaded_filepath), vf, "video/mp4")})

        send_telegram_request("deleteMessage", {"chat_id": chat_id, "message_id": bot_msg_id})

    except Exception as err:
        logger.error(f"Download failure: {err}")
        send_telegram_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": bot_msg_id,
            "text": f"❌ YouTube Error: {str(err)}"
        })

    finally:
        for f in downloaded_files:
            try:
                os.remove(f)
            except Exception:
                pass
        for extra in glob.glob(f"/tmp/{unique_id}_*"):
            try:
                os.remove(extra)
            except Exception:
                pass

@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def webhook_handler(path=""):
    if request.method == "GET":
        return jsonify({"status": "healthy", "cookies_detected": bool(os.environ.get("YOUTUBE_COOKIES"))}), 200
    try:
        update = request.get_json(force=True, silent=True)
        if update:
            if "message" in update:
                handle_message(update["message"])
            elif "callback_query" in update:
                handle_callback_query(update["callback_query"])
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return jsonify({"ok": True}), 200

handler = app
application = app
