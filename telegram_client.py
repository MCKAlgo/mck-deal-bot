"""Telegram Bot API client: long-polling command listener + message sender."""
import logging
import threading
import time

import requests

log = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramBot:
    def __init__(self, token: str, on_command=None):
        self.token = token
        self.on_command = on_command  # callback(update_dict)
        self.offset = 0
        self.session = requests.Session()
        self.polling = False
        self._thread = None
        self._stop = threading.Event()
        self._fallback_notified = False

    # ---------------- low-level ----------------
    def api(self, method: str, **params):
        url = API.format(token=self.token, method=method)
        try:
            r = self.session.post(url, json=params, timeout=35)
            data = r.json()
            if not data.get("ok"):
                desc = data.get("description", "unknown")
                # 400 Bad Request often = HTML parse failure; caller may retry plain
                log.warning("Telegram %s failed: %s", method, desc[:120])
                return {"ok": False, "description": desc, "error_code": data.get("error_code")}
            return data
        except (requests.RequestException, ValueError) as e:
            log.warning("Telegram %s network error: %s", method, str(e)[:120])
            return {"ok": False, "description": str(e)}

    def get_me(self):
        return self.api("getMe")

    # ---------------- sending ----------------
    def send_message(self, chat_id, text: str, disable_preview: bool = True,
                     reply_markup: dict = None) -> bool:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_preview,
        }
        if reply_markup:
            import json as _json
            payload["reply_markup"] = _json.dumps(reply_markup)
        res = self.api("sendMessage", **payload)
        if not res.get("ok") and res.get("error_code") == 400:
            # HTML parse issue -> resend as plain text (strip tags crudely)
            import re as _re
            plain = _re.sub(r"<[^>]+>", "", text)
            plain = plain.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
            retry = {"chat_id": chat_id, "text": plain,
                     "disable_web_page_preview": disable_preview}
            if reply_markup:
                import json as _json
                retry["reply_markup"] = _json.dumps(reply_markup)
            res = self.api("sendMessage", **retry)
            if res.get("ok") and not self._fallback_notified:
                self._fallback_notified = True
        return bool(res.get("ok"))

    def send_photo(self, chat_id, photo_url: str, caption: str,
                   reply_markup: dict = None) -> bool:
        """Photo alert with rich caption; falls back to text if the image
        URL is rejected (deleted/blocked/hotlink-protected)."""
        import json as _json
        payload = {"chat_id": chat_id, "photo": photo_url,
                   "caption": caption[:1024], "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = _json.dumps(reply_markup)
        res = self.api("sendPhoto", **payload)
        if not res.get("ok"):
            log.info("sendPhoto failed (%s) - falling back to text card",
                     str(res.get("description"))[:80])
            return self.send_message(chat_id, caption, reply_markup=reply_markup)
        return True

    def broadcast(self, chats: list, text: str, disable_preview: bool = True,
                  reply_markup: dict = None) -> int:
        sent = 0
        for chat in chats:
            if self.send_message(chat["chat_id"], text, disable_preview,
                                 reply_markup=reply_markup):
                sent += 1
            time.sleep(0.4)  # respect ~1 msg/sec global limit
        return sent

    def broadcast_photo(self, chats: list, photo_url: str, caption: str,
                        reply_markup: dict = None) -> int:
        sent = 0
        for chat in chats:
            if self.send_photo(chat["chat_id"], photo_url, caption,
                               reply_markup=reply_markup):
                sent += 1
            time.sleep(0.6)
        return sent

    # ---------------- long polling ----------------
    def poll_forever(self):
        self.polling = True
        log.info("Telegram long-polling started")
        backoff = 5
        while not self._stop.is_set():
            try:
                res = self.api("getUpdates", offset=self.offset, timeout=25,
                               allowed_updates=["message", "edited_message"])
                updates = res.get("result", []) if res.get("ok") else []
                if res.get("ok"):
                    backoff = 5
                for upd in updates:
                    self.offset = max(self.offset, upd["update_id"] + 1)
                    if self.on_command:
                        try:
                            self.on_command(upd)
                        except Exception as e:
                            log.warning("command handler error: %s", str(e)[:140])
                if not res.get("ok"):
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 120)
            except Exception as e:
                log.warning("poll loop error: %s", str(e)[:140])
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)
        self.polling = False
        log.info("Telegram long-polling stopped")

    def start_polling(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self.poll_forever, daemon=True,
                                        name="tg-poller")
        self._thread.start()

    def stop_polling(self):
        self._stop.set()
