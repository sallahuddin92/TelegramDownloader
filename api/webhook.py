"""
api/webhook.py
Production-ready Vercel Python Serverless Telegram Bot Webhook
Downloads YouTube Shorts & Facebook Reels under 20MB and sends directly to Telegram.
"""

import os
import re
import json
import glob
import uuid
import logging
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests
import yt_dlp
from fp.fp import FreeProxy

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_webhook_bot")

# Config & Limits
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_FILESIZE_BYTES = 20 * 1024 * 1024  # 20MB Telegram bot API limit


def send_telegram_request(method: str, data: dict = None, files: dict = None, timeout: int = 40):
    """Helper to send requests to Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN is not set.")
        return None
    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        if files:
            res = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            res = requests.post(url, json=data, timeout=timeout)
        return res.json()
    except Exception as e:
        logger.error(f"Error calling Telegram method {method}: {e}")
        return None


def get_proxy():
    """
    Attempts to scrape a free working HTTPS proxy using free-proxy.
    Falls back gracefully to direct connection (None) if scraping times out or fails.
    """
    try:
        logger.info("Scraping proxy via free-proxy...")
        proxy = FreeProxy(country_id=None, timeout=2.0, rand=True, https=True).get()
        if proxy:
            logger.info(f"Obtained proxy: {proxy}")
            return proxy
    except Exception as e:
        logger.warning(f"Free-proxy lookup failed: {e}. Falling back to direct connection.")
    return None


def extract_url(text: str) -> str:
    """Extracts first http/https URL from text."""
    if not text:
        return ""
    match = re.search(r"https?://[^\s]+", text)
    return match.group(0) if match else ""


def handle_message(message: dict):
    """Processes incoming text messages."""
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = message.get("text", "").strip()

    if not chat_id:
        return

    # Handle /start or /help commands
    if text.startswith("/start") or text.startswith("/help"):
        welcome_text = (
            "👋 *Welcome to Reel & Shorts Downloader Bot\!*

"
            "Send me any *YouTube Shorts* or *Facebook Reel* link \(under 20MB\), "
            "and I will download and send it to you as an *MP4 Video* or *MP3 Audio*\."
        )
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": welcome_text,
            "parse_mode": "MarkdownV2"
        })
        return

    # Check for URLs
    found_url = extract_url(text)
    if not found_url:
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "ℹ️ Please send a valid YouTube Shorts or Facebook Reels link starting with http/https.",
            "reply_to_message_id": message_id
        })
        return

    # Validate domain
    parsed = urlparse(found_url)
    domain = parsed.netloc.lower()
    allowed_domains = ["youtube.com", "www.youtube.com", "youtu.be", "m.youtube.com",
                       "facebook.com", "www.facebook.com", "fb.watch", "m.facebook.com"]
    if not any(d in domain for d in allowed_domains):
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "⚠️ Unsupported URL. Only YouTube Shorts and Facebook Reels are supported.",
            "reply_to_message_id": message_id
        })
        return

    # Present Inline Keyboard with MP4 / MP3 options
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
        "text": f"🎬 *Choose Download Format:*\n`{found_url}`",
        "parse_mode": "Markdown",
        "reply_to_message_id": message_id,
        "reply_markup": keyboard
    })


def handle_callback_query(callback_query: dict):
    """Processes button clicks from inline keyboards."""
    cq_id = callback_query.get("id")
    data = callback_query.get("data", "")
    bot_message = callback_query.get("message", {})
    chat_id = bot_message.get("chat", {}).get("id")
    bot_msg_id = bot_message.get("message_id")

    # Acknowledge callback immediately to remove loading spinner in Telegram
    send_telegram_request("answerCallbackQuery", {"callback_query_id": cq_id})

    if data not in ["dl_mp4", "dl_mp3"]:
        return

    # Extract target URL from the original message that was replied to
    reply_to = bot_message.get("reply_to_message", {})
    target_url = extract_url(reply_to.get("text", "")) or extract_url(reply_to.get("caption", ""))

    if not target_url:
        target_url = extract_url(bot_message.get("text", ""))

    if not target_url:
        send_telegram_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": bot_msg_id,
            "text": "❌ Could not retrieve original video URL. Please send the link again."
        })
        return

    # 1. Wait State: Edit button message
    send_telegram_request("editMessageText", {
        "chat_id": chat_id,
        "message_id": bot_msg_id,
        "text": "⏳ Downloading and processing, please wait..."
    })

    # Trigger chat action
    chat_action = "upload_video" if data == "dl_mp4" else "upload_voice"
    send_telegram_request("sendChatAction", {
        "chat_id": chat_id,
        "action": chat_action
    })

    # 2. Download Engine
    unique_id = str(uuid.uuid4())[:8]
    output_template = f"/tmp/{unique_id}_%(id)s.%(ext)s"
    proxy = get_proxy()

    is_audio = (data == "dl_mp3")

    ydl_opts = {
        'nocheckcertificate': True,
        'quiet': True,
        'no_warnings': True,
        'max_filesize': MAX_FILESIZE_BYTES,
        'noplaylist': True,
        'socket_timeout': 15,
        'outtmpl': output_template,
    }

    if is_audio:
        ydl_opts['format'] = 'bestaudio/best'
    else:
        ydl_opts['format'] = 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'

    if proxy:
        ydl_opts['proxy'] = proxy

    downloaded_files = []

    try:
        logger.info(f"Starting extraction for URL: {target_url} (audio={is_audio})")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(target_url, download=True)
            video_title = info.get("title", "Downloaded Media")

        found_files = glob.glob(f"/tmp/{unique_id}_*")
        if not found_files:
            raise FileNotFoundError("File was not saved to /tmp.")

        downloaded_filepath = found_files[0]
        downloaded_files.append(downloaded_filepath)

        file_size = os.path.getsize(downloaded_filepath)
        logger.info(f"Downloaded file: {downloaded_filepath} ({file_size} bytes)")

        if file_size > MAX_FILESIZE_BYTES:
            raise ValueError(f"File size ({file_size / (1024*1024):.1f}MB) exceeds the 20MB Telegram limit.")

        caption = f"🎬 {video_title}\n\n⚡ Downloaded via @TelegramDownloader"

        # 3. Delivery via Telegram API
        if is_audio:
            with open(downloaded_filepath, "rb") as audio_file:
                files = {"audio": (os.path.basename(downloaded_filepath), audio_file, "audio/mpeg")}
                data_payload = {
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "title": video_title[:64]
                }
                send_telegram_request("sendAudio", data=data_payload, files=files, timeout=50)
        else:
            with open(downloaded_filepath, "rb") as vid_file:
                files = {"video": (os.path.basename(downloaded_filepath), vid_file, "video/mp4")}
                data_payload = {
                    "chat_id": chat_id,
                    "caption": caption[:1024],
                    "supports_streaming": "true"
                }
                send_telegram_request("sendVideo", data=data_payload, files=files, timeout=50)

        # 4. Delete the "⏳ Downloading..." message
        send_telegram_request("deleteMessage", {
            "chat_id": chat_id,
            "message_id": bot_msg_id
        })

    except Exception as err:
        logger.error(f"Error in download/delivery: {err}", exc_info=True)
        send_telegram_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": bot_msg_id,
            "text": f"❌ Failed to process video:\n{str(err)}"
        })

    finally:
        for f in downloaded_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
                    logger.info(f"Deleted /tmp file: {f}")
            except Exception:
                pass

        for extra in glob.glob(f"/tmp/{unique_id}_*"):
            try:
                if os.path.exists(extra):
                    os.remove(extra)
            except Exception:
                pass


class handler(BaseHTTPRequestHandler):
    """Vercel Python Serverless Function entrypoint."""

    def _send_json(self, status: int, data: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def do_GET(self):
        """Health check endpoint."""
        self._send_json(200, {
            "status": "healthy",
            "bot": "Telegram Video Downloader Webhook",
            "supported_types": ["YouTube Shorts", "Facebook Reels"]
        })

    def do_POST(self):
        """Telegram Webhook handler."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            if not body:
                self._send_json(200, {"ok": True})
                return

            update = json.loads(body)

            if "message" in update:
                handle_message(update["message"])
            elif "callback_query" in update:
                handle_callback_query(update["callback_query"])

            self._send_json(200, {"ok": True})

        except Exception as e:
            logger.error(f"Webhook processing error: {e}", exc_info=True)
            self._send_json(200, {"ok": False, "error": str(e)})
