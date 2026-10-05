"""
api/index.py
Flask-based Vercel Serverless Telegram Bot Webhook
Downloads YouTube Shorts & Facebook Reels under 20MB and sends directly to Telegram.
Includes m.facebook.com routing, YouTube player_client rotation (mweb, tv),
and YOUTUBE_COOKIES support for seamless authenticated downloads.
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

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("telegram_webhook_bot")

# Initialize Flask app
app = Flask(__name__)

# Config & Limits
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
MAX_FILESIZE_BYTES = 20 * 1024 * 1024  # 20MB limit

DESKTOP_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

MOBILE_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}


def send_telegram_request(method: str, data: dict = None, files: dict = None, timeout: int = 40):
    if not TELEGRAM_BOT_TOKEN:
        logger.error("CRITICAL: TELEGRAM_BOT_TOKEN environment variable is not set in Vercel settings!")
        return None
    url = f"{TELEGRAM_API_BASE}/{method}"
    try:
        if files:
            res = requests.post(url, data=data, files=files, timeout=timeout)
        else:
            res = requests.post(url, json=data, timeout=timeout)
        return res.json()
    except Exception as e:
        logger.error(f"Error calling Telegram method {method}: {e}", exc_info=True)
        return None


def extract_url(text: str) -> str:
    """Extracts first http/https URL from text."""
    if not text:
        return ""
    match = re.search(r"https?://[^\s]+", text)
    return match.group(0) if match else ""


def resolve_facebook_url(raw_url: str) -> str:
    """
    Resolves Facebook /share/r/ or fb.watch links and converts to mobile
    https://m.facebook.com/watch/?v=<id>.
    """
    url = raw_url.strip().rstrip(").,;!?\"'")
    reel_id = None

    match_direct = re.search(r"/(?:reel|videos)/(\d+)", url)
    if match_direct:
        reel_id = match_direct.group(1)

    watch_direct = re.search(r"[?&]v=(\d+)", url)
    if watch_direct:
        reel_id = watch_direct.group(1)

    if not reel_id and ("facebook.com/share" in url or "fb.watch" in url):
        try:
            logger.info(f"Resolving Facebook share link: {url}")
            session = requests.Session()
            session.headers.update(MOBILE_HEADERS)
            resp = session.get(url, allow_redirects=True, timeout=10)
            final_url = resp.url

            match = re.search(r"/(?:reel|videos)/(\d+)", final_url)
            if match:
                reel_id = match.group(1)
            else:
                watch_m = re.search(r"[?&]v=(\d+)", final_url)
                if watch_m:
                    reel_id = watch_m.group(1)
                else:
                    html = resp.text
                    html_reel = re.search(r'/(?:reel|videos)/(\d+)', html)
                    if html_reel:
                        reel_id = html_reel.group(1)
                    else:
                        og_match = re.search(r'<meta property="og:url" content="([^"]+)"', html)
                        if og_match:
                            og_id = re.search(r'/(?:reel|videos)/(\d+)', og_match.group(1))
                            if og_id:
                                reel_id = og_id.group(1)
        except Exception as e:
            logger.warning(f"Error resolving Facebook share link {url}: {e}")

    if reel_id:
        target = f"https://m.facebook.com/watch/?v={reel_id}"
        logger.info(f"Facebook URL resolved to mobile watch endpoint: {target}")
        return target

    url = re.sub(r'[\?&](mibextid|sfnsn|feature|fbclid|si|igsh)=[^&]+', '', url)
    if url.endswith('?') or url.endswith('&'):
        url = url[:-1]

    return url


def handle_message(message: dict):
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = message.get("text", "").strip()

    logger.info(f"Received message: chat_id={chat_id} text={text}")

    if not chat_id:
        return

    if text.startswith("/start") or text.startswith("/help"):
        welcome_text = (
            "👋 Welcome to Reel & Shorts Downloader Bot!\n\n"
            "Send me any YouTube Shorts or Facebook Reel link (under 20MB), "
            "and I will download and send it to you as an MP4 Video or MP3 Audio."
        )
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": welcome_text
        })
        return

    found_url = extract_url(text)
    if not found_url:
        send_telegram_request("sendMessage", {
            "chat_id": chat_id,
            "text": "ℹ️ Please send a valid YouTube Shorts or Facebook Reels link starting with http/https.",
            "reply_to_message_id": message_id
        })
        return

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

    resolved_url = resolve_facebook_url(found_url) if "facebook.com" in domain or "fb.watch" in domain else found_url

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
        "text": f"🎬 Choose Download Format:\n{resolved_url}",
        "reply_to_message_id": message_id,
        "reply_markup": keyboard
    })


def handle_callback_query(callback_query: dict):
    cq_id = callback_query.get("id")
    data = callback_query.get("data", "")
    bot_message = callback_query.get("message", {})
    chat_id = bot_message.get("chat", {}).get("id")
    bot_msg_id = bot_message.get("message_id")

    logger.info(f"Received callback_query: data={data} chat_id={chat_id}")

    send_telegram_request("answerCallbackQuery", {"callback_query_id": cq_id})

    if data not in ["dl_mp4", "dl_mp3"]:
        return

    reply_to = bot_message.get("reply_to_message", {})
    target_url = extract_url(bot_message.get("text", "")) or extract_url(reply_to.get("text", ""))

    if not target_url:
        send_telegram_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": bot_msg_id,
            "text": "❌ Could not retrieve original video URL. Please send the link again."
        })
        return

    clean_target_url = resolve_facebook_url(target_url) if ("facebook.com" in target_url or "fb.watch" in target_url) else target_url
    logger.info(f"Target URL: {clean_target_url}")

    send_telegram_request("editMessageText", {
        "chat_id": chat_id,
        "message_id": bot_msg_id,
        "text": "⏳ Downloading and processing, please wait..."
    })

    chat_action = "upload_video" if data == "dl_mp4" else "upload_voice"
    send_telegram_request("sendChatAction", {
        "chat_id": chat_id,
        "action": chat_action
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
        'extractor_args': {
            'youtube': {'player_client': ['mweb', 'tv', 'tv_embedded']},
        }
    }

    # Automatically read YOUTUBE_COOKIES from Vercel Environment Variables
    cookies_env = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies_env:
        cookies_path = f"/tmp/{unique_id}_cookies.txt"
        with open(cookies_path, "w") as cf:
            cf.write(cookies_env)
        ydl_opts['cookiefile'] = cookies_path

    if is_audio:
        ydl_opts['format'] = 'bestaudio/best'
    else:
        ydl_opts['format'] = 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best'

    downloaded_files = []
    video_title = "Downloaded Media"

    try:
        logger.info(f"Extracting video: {clean_target_url} (audio={is_audio})")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_target_url, download=True)
            video_title = info.get("title", "Downloaded Media")

        found_files = glob.glob(f"/tmp/{unique_id}_*")
        media_files = [f for f in found_files if not f.endswith("_cookies.txt")]
        if not media_files:
            raise FileNotFoundError("Downloaded file was not found in storage.")
        downloaded_filepath = media_files[0]
        downloaded_files.append(downloaded_filepath)

        file_size = os.path.getsize(downloaded_filepath)
        if file_size > MAX_FILESIZE_BYTES:
            raise ValueError(f"File size ({file_size / (1024*1024):.1f}MB) exceeds the 20MB limit.")

        caption = f"🎬 {video_title}\n\n⚡ Downloaded via Telegram Downloader"

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

        send_telegram_request("deleteMessage", {
            "chat_id": chat_id,
            "message_id": bot_msg_id
        })

    except Exception as err:
        logger.error(f"Error in download/delivery: {err}", exc_info=True)
        err_str = str(err)
        if "sign in to confirm you’re not a bot" in err_str.lower() or "bot check triggered" in err_str.lower():
            text_resp = (
                "⚠️ YouTube Bot Verification on Cloud IP:\n\n"
                "YouTube blocks cloud servers (AWS/Vercel) from downloading without a session.\n\n"
                "💡 How to enable YouTube Shorts in 1 minute:\n"
                "1. Export your cookies from youtube.com using the free Chrome/Firefox extension 'Get cookies.txt LOCALLY'.\n"
                "2. In Vercel -> Project Settings -> Environment Variables, add:\n"
                "   Key: YOUTUBE_COOKIES\n"
                "   Value: (paste your cookies text)\n\n"
                "✨ Facebook Reels work 100% without cookies!"
            )
        else:
            text_resp = f"❌ Failed to process video:\n{err_str}"

        send_telegram_request("editMessageText", {
            "chat_id": chat_id,
            "message_id": bot_msg_id,
            "text": text_resp
        })

    finally:
        for f in downloaded_files:
            try:
                if os.path.exists(f):
                    os.remove(f)
            except Exception:
                pass
        for extra in glob.glob(f"/tmp/{unique_id}_*"):
            try:
                if os.path.exists(extra):
                    os.remove(extra)
            except Exception:
                pass


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def webhook_handler(path=""):
    if request.method == "GET":
        token_configured = bool(TELEGRAM_BOT_TOKEN)
        return jsonify({
            "status": "healthy",
            "bot": "Telegram Video Downloader Webhook",
            "telegram_token_configured": token_configured,
            "received_path": path
        }), 200

    try:
        update = request.get_json(force=True, silent=True)
        logger.info(f"Incoming Telegram webhook update on path '{path}': {update}")

        if not update:
            logger.warning("Empty or non-JSON webhook body received.")
            return jsonify({"ok": True}), 200

        if not TELEGRAM_BOT_TOKEN:
            logger.error("CRITICAL: TELEGRAM_BOT_TOKEN is missing in Vercel Environment Variables!")

        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback_query(update["callback_query"])

    except Exception as e:
        logger.error(f"Webhook processing exception: {e}", exc_info=True)

    return jsonify({"ok": True}), 200


@app.errorhandler(404)
def handle_not_found(e):
    return webhook_handler(path=request.path)


handler = app
application = app

if __name__ == "__main__":
    app.run(port=5000)
