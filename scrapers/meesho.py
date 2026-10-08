"""Meesho scraper: __NEXT_DATA__ JSON walk.

NOTE: Meesho sits behind aggressive Cloudflare protection which blocks many
datacenter IPs (HTTP 403). From residential IPs / Indian servers the plain
browser-like request below usually succeeds. The bot auto-retries on every
cycle, so Meesho results start flowing the moment the site lets us through -
no config change needed.
"""
import json
import logging
import re

from bs4 import BeautifulSoup

from adapter import RetailerAdapter
from scraper_base import parse_price

log = logging.getLogger("scraper.meesho")

CATEGORY_URLS = {
    "electronics": [
        "https://www.meesho.com/electronic-store/pl/3vn",
        "https://www.meesho.com/search?q=earphones",
        "https://www.meesho.com/search?q=smartwatch",
        "https://www.meesho.com/search?q=mobile accessories",
    ],
    "fashion": [
        "https://www.meesho.com/fashion-store/pl/3fz",
        "https://www.meesho.com/search?q=kurti",
        "https://www.meesho.com/search?q=tshirt",
        "https://www.meesho.com/search?q=saree",
        "https://www.meesho.com/search?q=shoes",
    ],
    "home": [
        "https://www.meesho.com/home-essentials/pl/3kk",
        "https://www.meesho.com/search?q=kitchen",
        "https://www.meesho.com/search?q=home decor",
    ],
    "general": [
        "https://www.meesho.com/",
        "https://www.meesho.com/search?q=toys",
        "https://www.meesho.com/search?q=beauty",
        "https://www.meesho.com/search?q=watches",
    ],
}

PRICE_KEYS = ("discountedPrice", "finalPrice", "price", "sellingPrice")
MRP_KEYS = ("mrp", "MRP", "strikePrice", "listPrice", "basePrice")


def _walk(node, found, depth=0):
    if depth > 40:
        return
    if isinstance(node, dict):
        keys = set(node.keys())
        name = node.get("name") or node.get("title") or node.get("productTitle")
        price = None
        for k in PRICE_KEYS:
            if k in node and isinstance(node[k], (int, float)) and node[k] > 0:
                price = float(node[k])
                break
        if name and price and isinstance(name, str) and 10 < len(name) < 300:
            mrp = None
            for k in MRP_KEYS:
                if k in node and isinstance(node[k], (int, float)) and node[k] >= price:
                    mrp = float(node[k])
                    break
            image = ""
            if isinstance(node.get("images"), list) and node["images"]:
                first = node["images"][0]
                image = first.get("url", "") if isinstance(first, dict) else str(first)
            elif isinstance(node.get("image"), dict):
                image = node["image"].get("url", "")
            url = node.get("url") or node.get("productUrl") or ""
            pid = node.get("productId") or node.get("id") or ""
            rating = node.get("rating") or node.get("avgRating")
            reviews = node.get("ratingCount") or node.get("reviewCount") or node.get("totalReviews")
            found.append({"name": name, "price": price, "mrp": mrp, "image": image,
                          "url": url, "pid": str(pid), "rating": rating, "reviews": reviews})
            return  # do not recurse into leaf product nodes
        for v in node.values():
            _walk(v, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found, depth + 1)


class MeeshoScraper(RetailerAdapter):
    platform = "meesho"
    blocked_count = 0

    def _flat_urls(self) -> list:
        return [(cat, u) for cat, urls in CATEGORY_URLS.items() for u in urls]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        return []  # Meesho renders categories via JS; discovery needs a browser

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
        products = []
        self.begin_sweep()
        if cats:
            picked = [(c.get("name") or "catalogue", c["url"]) for c in cats]
        else:
            picked = self.rotate_slice(self._flat_urls(), offset, budget)
        for category, url in picked:
            if self.circuit_open():
                break
            html = self.fetch(url)
            if not html:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] still Cloudflare-blocked (403) - will retry next cycle; "
                             "auto-activates from residential/IN IPs", self.platform)
                continue
            got = self._parse_next_data(html, category)
            products.extend(got)
        seen = set()
        out = []
        for p in products:
            key = p["url"] or p["title"].lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        if out:
            log.info("[%s] scraped %d products", self.platform, len(out))
        return out

    def _parse_next_data(self, html: str, category: str) -> list:
        soup = BeautifulSoup(html, "lxml")
        tag = soup.find("script", id="__NEXT_DATA__")
        if not tag:
            return []
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, ValueError, TypeError):
            return []
        found = []
        _walk(data, found)
        out = []
        seen_ids = set()
        for f in found[:80]:
            if f["pid"] and f["pid"] in seen_ids:
                continue
            if f["pid"]:
                seen_ids.add(f["pid"])
            price = f["price"]
            mrp = f.get("mrp") or 0
            disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
            url = f["url"] if f["url"].startswith("http") else f"https://www.meesho.com/{f['url']}"
            if not f["pid"] and "/p/" not in url:
                url = ""
            if not url:
                url = f"https://www.meesho.com/search?q={f['name'][:40]}"
            try:
                reviews = int(f["reviews"]) if f.get("reviews") else None
            except (TypeError, ValueError):
                reviews = None
            out.append(self.make_product(
                self.platform, f["name"], price, url, mrp=mrp, discount_pct=disc,
                rating=float(f["rating"]) if f.get("rating") else None,
                reviews=reviews, image_url=f.get("image", ""), category=category))
        return out
