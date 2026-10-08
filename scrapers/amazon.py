"""Amazon.in scraper: session warm-up (cookie flow) + server-rendered search results.

v2 "next level": 45 search queries spanning electronics, fashion, home, kitchen,
beauty, grocery, sports & general deal pages. Each check-cycle processes only a
rotating slice (default 7 queries), so the full sweep completes in ~7 cycles
while request volume stays gentle and throttling-free.
"""
import logging
import re

import requests
from bs4 import BeautifulSoup

from adapter import RetailerAdapter
from scraper_base import BaseScraper, parse_price

log = logging.getLogger("scraper.amazon")

CATEGORY_QUERIES = {
    "electronics": [
        "mobile phones under 30000", "wireless earbuds", "smartwatch deals",
        "laptop deals", "powerbank deals", "bluetooth speaker deals",
        "trimmer deals", "tablet deals", "smart tv deals", "headphones deals",
        "external hard disk deals", "ssd deals", "mouse keyboard deals",
        "monitor deals", "camera deals",
    ],
    "fashion": [
        "tshirt deals", "kurti deals", "shoes deals", "saree deals",
        "watch deals", "jeans deals", "handbag deals", "sunglasses deals",
        "sneakers deals", "kids clothing deals",
    ],
    "home": [
        "kitchen appliances deals", "home storage deals", "cookware deals",
        "bedsheets deals", "furniture deals", "mixer grinder deals",
        "air fryer deals", "vacuum cleaner deals", "water purifier deals",
    ],
    "beauty": ["beauty deals", "skincare deals", "perfume deals"],
    "grocery": ["grocery deals", "dry fruits deals"],
    "sports": ["fitness equipment deals", "cricket kit deals", "cycling deals"],
    "general": ["deals of the day", "lightning deals", "top deals today"],
}


class AmazonScraper(RetailerAdapter):
    platform = "amazon"
    warmed_up = False
    last_warmup = 0.0
    consecutive_failures = 0

    def warm_up(self, force: bool = False):
        """Hit homepage first so anti-bot cookies get set, then searches work."""
        import time as _time
        if not force and _time.time() - self.last_warmup < 600:
            return
        try:
            r = self.session.get("https://www.amazon.in/", timeout=22)
            self.warmed_up = True
            self.last_warmup = _time.time()
            log.info("[%s] warm-up status %s (cookies: %d)", self.platform, r.status_code, len(self.session.cookies))
        except requests.RequestException as e:
            log.warning("[%s] warm-up failed: %s", self.platform, str(e)[:90])

    def _flat_queries(self) -> list:
        return [(cat, q) for cat, queries in CATEGORY_QUERIES.items() for q in queries]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        from discovery import extract
        return extract("amazon", html, seed_url)

    def fetch_products(self, offset: int = 0, budget: int = 7, cats: list = None) -> list:
        if not self.warmed_up or self.consecutive_failures >= 1:
            self.warm_up(force=self.consecutive_failures >= 1)
        if cats:
            # discovered browse-node search URLs (catalogue universe)
            picked = [(c.get("name") or "catalogue", c["url"]) for c in cats]
            products = []
            for category, url in picked:
                html = self.fetch(url, referer="https://www.amazon.in/")
                if not html or len(html) < 3000:
                    self.consecutive_failures += 1
                    continue
                got = self._parse_search(html, category)
                if got:
                    self.consecutive_failures = 0
                products.extend(got)
                for d in self.discover_categories(html, url):
                    self._record_discovered(d["url"], d.get("name", ""),
                                            d.get("depth", 1))
        else:
            picked = self.rotate_slice(self._flat_queries(), offset, budget)
            log.info("[%s] cycle slice: %s", self.platform, [q for _, q in picked])
            products = []
            for category, q in picked:
                url = f"https://www.amazon.in/s?k={q.replace(' ', '+')}"
                html = self.fetch(url, referer="https://www.amazon.in/")
                if not html or len(html) < 3000:
                    self.consecutive_failures += 1
                    continue
                got = self._parse_search(html, category)
                if got:
                    self.consecutive_failures = 0
                products.extend(got)
                for d in self.discover_categories(html, url):
                    self._record_discovered(d["url"], d.get("name", ""),
                                            d.get("depth", 1))
        if not products:
            self.consecutive_failures += 1
            log.info("[%s] no products this cycle (failures=%d) - will re-warm next time",
                     self.platform, self.consecutive_failures)
        seen = set()
        out = []
        for p in products:
            key = p["url"]
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        log.info("[%s] scraped %d products", self.platform, len(out))
        return out

    def get_product_update(self, url: str):
        """Recheck a single product page (adaptive rotation tier)."""
        if "/dp/" not in url:
            return None
        html = self.fetch(url, referer="https://www.amazon.in/")
        if not html or len(html) < 3000:
            return None
        try:
            soup = BeautifulSoup(html, "lxml")
            price = 0.0
            off = soup.select_one("span.a-price span.a-offscreen")
            if off:
                price = parse_price(off.get_text())
            whole = soup.select_one("span.a-price-whole")
            if not price and whole:
                price = parse_price(whole.get_text())
            if price < 100:
                return None
            out = {"price": price}
            title = soup.select_one("#productTitle")
            if title and len(title.get_text(strip=True)) > 10:
                out["title"] = title.get_text(strip=True)[:240]
            was = soup.select_one("span.a-price.a-text-price span.a-offscreen")
            if was:
                mrp = parse_price(was.get_text())
                if mrp > price:
                    out["mrp"] = mrp
            alt = soup.select_one("span[data-hook='rating-out-of-text'], i.a-icon-star span.a-icon-alt")
            if alt:
                try:
                    r = float(alt.get_text().split()[0])
                    if 1 <= r <= 5:
                        out["rating"] = r
                except (ValueError, IndexError):
                    pass
            rev = soup.select_one("#acrCustomerReviewText")
            if rev:
                import re as _re
                rm = _re.search(r"([\d,]+)", rev.get_text())
                if rm:
                    out["reviews_count"] = int(rm.group(1).replace(",", ""))
            img = soup.select_one("img#landingImage, #imgTagWrapperId img")
            if img and img.get("src"):
                out["image_url"] = img.get("src")
            return out
        except Exception as e:
            log.debug("amazon product-page parse: %s", str(e)[:80])
            return None

    def _parse_search(self, html: str, category: str) -> list:
        soup = BeautifulSoup(html, "lxml")
        out = []
        for card in soup.select("div[data-component-type='s-search-result']")[:40]:
            try:
                asin = card.get("data-asin", "").strip()
                if not asin:
                    continue
                h2 = card.find("h2")
                title = ""
                if h2:
                    span = h2.find("span")
                    title = (span.get_text(" ", strip=True) if span else h2.get_text(" ", strip=True)) or ""
                if len(title) < 12:
                    continue
                price = 0.0
                whole = card.select_one("span.a-price-whole")
                if whole:
                    price = parse_price(whole.get_text())
                if not price:
                    off = card.select_one("span.a-price span.a-offscreen")
                    if off:
                        price = parse_price(off.get_text())
                if price < 100:
                    continue
                mrp = 0.0
                was = card.select_one("span.a-price.a-text-price span.a-offscreen")
                if was:
                    mrp = parse_price(was.get_text())
                if mrp <= price:
                    mrp = 0.0
                disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
                dm = re.search(r"\((\d{1,2})%\s*off\)", card.get_text(" ", strip=True))
                if dm:
                    disc = float(dm.group(1))
                rating = None
                alt = card.select_one("i.a-icon-star-small span.a-icon-alt, i.a-icon-star span.a-icon-alt")
                if alt:
                    try:
                        val = float(alt.get_text().split()[0])
                        rating = val if 1 <= val <= 5 else None
                    except (ValueError, IndexError):
                        pass
                reviews = None
                rm = re.search(r"([\d,]+)\s*ratings", card.get_text(" ", strip=True), re.I)
                if rm:
                    reviews = int(rm.group(1).replace(",", ""))
                img = card.select_one("img.s-image")
                image = img.get("src", "") if img else ""
                url = f"https://www.amazon.in/dp/{asin}"
                out.append(self.make_product(self.platform, title, price, url, mrp=mrp,
                                             discount_pct=disc, rating=rating, reviews=reviews,
                                             image_url=image, category=category))
            except Exception as e:
                log.debug("amazon card skip: %s", str(e)[:80])
        return out
