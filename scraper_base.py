"""Base scraper: browser-like sessions, UA rotation, retries, price parsing."""
import logging
import random
import re
import time

import requests

log = logging.getLogger("scraper.base")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9,hi;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


def parse_price(text) -> float:
    """'₹1,29,999.00' / '1,29,999' -> 129999.0"""
    if text is None:
        return 0.0
    m = re.search(r"[\d][\d,\.]*", str(text))
    if not m:
        return 0.0
    raw = m.group(0).replace(",", "")
    try:
        return float(raw)
    except ValueError:
        return 0.0


def normalize_image_url(url) -> str:
    """Make a product image URL that Telegram's sendPhoto can actually fetch.

    Retailers serve images in several annoying shapes:
      - Flipkart templates:  http://rukmini1.flixcart.com/image/{@width}/{@height}/...
      - Protocol-relative:   //rukminim2.flixcart.com/image/...
      - Plain http:          http://rukmini1.flixcart.com/...  (Telegram prefers https)
      - Lazy placeholders:   data:image/gif;base64... / 1x1 pixels
    This returns a fetchable https URL (448x448 for Flipkart CDN), or ''.
    """
    if not url:
        return ""
    url = str(url).strip().replace("\\u002F", "/")
    if not url or url.startswith("data:"):
        return ""
    if url.startswith("//"):
        url = "https:" + url
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]
    # Flipkart CDN template placeholders -> real values
    # (?q= quality must stay 1-100, so {@quality} -> 70, NOT 448)
    url = re.sub(r"\{[^}]{0,12}qual[^}]{0,12}\}", "70", url)
    url = re.sub(r"\{[^}]{0,12}(hei|h)[^}]{0,12}\}", "448", url)
    url = re.sub(r"\{[^}]{0,12}wid[^}]{0,12}\}", "448", url)
    url = url.replace("{@width}", "448").replace("{@height}", "448")
    url = re.sub(r"\{[^}]{1,12}\}", "448", url)  # any leftover template variants
    # Flipkart often supports a size prefix swap; keep path but ensure /image/<w>/<h>/
    if not url.startswith("https://"):
        return ""
    if "{" in url:
        return ""
    return url


class BaseScraper:
    platform = "base"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(dict(BASE_HEADERS))
        self.rotate_ua()
        self.last_request_ts = 0.0
        self._circuit_open = False
        self._circuit_403 = 0

    def begin_sweep(self):
        """Reset per-cycle circuit breaker state."""
        self._circuit_open = False
        self._circuit_403 = 0

    def circuit_open(self) -> bool:
        """True once the site has hard-blocked us twice in a row this cycle -
        remaining URLs of this platform are skipped (retry next cycle)."""
        return self._circuit_open

    def rotate_ua(self):
        self.session.headers["User-Agent"] = random.choice(USER_AGENTS)

    def polite_delay(self, lo=1.2, hi=2.8):
        gap = time.time() - self.last_request_ts
        wait = random.uniform(lo, hi) - gap
        if wait > 0:
            time.sleep(wait)
        self.last_request_ts = time.time()

    def fetch(self, url: str, referer: str = None, timeout: int = 22,
              validate=None) -> str:
        """GET with retry + backoff + shell-page detection.
        validate: optional callable(html)->bool; if the response fails
        validation we rotate UA, clear cookies and retry once."""
        headers = {}
        if referer:
            headers["Referer"] = referer
            headers["Sec-Fetch-Site"] = "same-origin"
        for attempt in range(3):
            try:
                self.polite_delay()
                r = self.session.get(url, headers=headers, timeout=timeout)
                if r.status_code == 200 and len(r.text) > 2000:
                    if validate is None or validate(r.text):
                        return r.text
                    log.warning("[%s] shell/empty page detected (%d bytes), rotating UA",
                                self.platform, len(r.text))
                    self.rotate_ua()
                    self.session.cookies.clear()
                    continue
                if r.status_code in (429, 403, 503):
                    self._circuit_403 += 1
                    if self._circuit_403 >= 2:
                        self._circuit_open = True
                    log.warning("[%s] %s -> %s, backing off", self.platform, url[:80], r.status_code)
                    time.sleep(4 + attempt * 6)
                    self.rotate_ua()
                    continue
                log.warning("[%s] %s -> %s (%d bytes)", self.platform, url[:80], r.status_code, len(r.text))
                return ""
            except requests.RequestException as e:
                log.warning("[%s] fetch error %s: %s", self.platform, url[:80], str(e)[:90])
                time.sleep(3)
        return ""

    def fetch_products(self) -> list:
        """Override in subclass. Returns list of product dicts."""
        raise NotImplementedError

    @staticmethod
    def rotate_slice(items: list, cycle_offset: int, budget: int) -> list:
        """Rotating window over a list: each cycle covers a different slice,
        so the full sweep completes over several cycles without hammering."""
        if not items:
            return []
        budget = max(1, min(budget, len(items)))
        start = (max(0, cycle_offset) * budget) % len(items)
        return [items[(start + i) % len(items)] for i in range(budget)]

    @staticmethod
    def make_product(platform, title, price, url, mrp=0.0, discount_pct=0.0,
                     rating=None, reviews=None, image_url="", category="general"):
        return {
            "platform": platform,
            "title": (title or "").strip()[:240],
            "current_price": float(price or 0),
            "url": url,
            "mrp": float(mrp or 0),
            "discount_pct": float(discount_pct or 0),
            "rating": rating,
            "reviews_count": reviews,
            "image_url": normalize_image_url(image_url),
            "category": category,
        }
