"""JioMart adapter (jiomart.com).

JioMart search/category pages hydrate a big embedded JSON payload on clean
IPs. This adapter walks any embedded state blobs (__NEXT_DATA__, window
assignments) for product-shaped nodes - the same technique as the Meesho
adapter. From datacenter IPs the CDN returns an edge block; the adapter
auto-activates from trusted/residential IPs.
"""
import json
import logging
import re

from bs4 import BeautifulSoup

from adapter import RetailerAdapter
from scraper_base import parse_price

log = logging.getLogger("scraper.jiomart")

SEED_CATEGORIES = [
    "https://www.jiomart.com/catalogsearch/result/?q=deals",
    "https://www.jiomart.com/catalogsearch/result/?q=electronics offers",
    "https://www.jiomart.com/catalogsearch/result/?q=kitchen offers",
    "https://www.jiomart.com/catalogsearch/result/?q=fashion offers",
]

PRICE_KEYS = ("finalPrice", "sellingPrice", "discountedPrice", "price",
              "offerPrice")
MRP_KEYS = ("mrp", "listPrice", "strikePrice", "basePrice", "markedPrice")


def _walk(node, found, depth=0):
    if depth > 40 or len(found) > 120:
        return
    if isinstance(node, dict):
        name = node.get("name") or node.get("productName") or node.get("title")
        price = None
        for k in PRICE_KEYS:
            v = node.get(k)
            if isinstance(v, (int, float)) and v > 0:
                price = float(v)
                break
            if isinstance(v, dict) and v.get("value"):
                price = parse_price(v["value"])
                if price:
                    break
        if name and price and isinstance(name, str) and 10 < len(name) < 300:
            mrp = None
            for k in MRP_KEYS:
                v = node.get(k)
                if isinstance(v, (int, float)) and v >= price:
                    mrp = float(v)
                    break
                if isinstance(v, dict) and v.get("value"):
                    cand = parse_price(v["value"])
                    if cand >= price:
                        mrp = cand
                        break
            image = ""
            imgs = node.get("images") or node.get("image") or []
            if isinstance(imgs, list) and imgs:
                first = imgs[0]
                image = first.get("url", "") if isinstance(first, dict) else str(first)
            url = node.get("url") or node.get("productUrl") or ""
            pid = node.get("sku") or node.get("productId") or node.get("id") or ""
            rating = node.get("rating") or node.get("averageRating")
            reviews = node.get("reviewCount") or node.get("ratingCount")
            found.append({"name": name, "price": price, "mrp": mrp, "image": image,
                          "url": url, "pid": str(pid), "rating": rating,
                          "reviews": reviews})
            return
        for v in node.values():
            _walk(v, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found, depth + 1)


class JioMartAdapter(RetailerAdapter):
    platform = "jiomart"
    blocked_count = 0

    def _flat_urls(self) -> list:
        return [("general", u) for u in SEED_CATEGORIES]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        from discovery import discover_generic
        return discover_generic(html, self.platform)

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
        picked = [(c, u) for c, u in (cats or [])] or self.rotate_slice(
            self._flat_urls(), offset, budget)
        products = []
        for category, url in picked:
            html = self.fetch(url)
            if not html:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] edge-blocked - auto-activates from trusted IPs",
                             self.platform)
                continue
            got = self._parse_state(html, category)
            products.extend(got)
            for d in self.discover_categories(html, url):
                self._record_discovered(d["url"], d.get("name", ""), d.get("depth", 1))
        seen, out = set(), []
        for p in products:
            k = p["url"] or p["title"].lower()[:80]
            if k in seen:
                continue
            seen.add(k)
            out.append(p)
        if out:
            log.info("[%s] scraped %d products", self.platform, len(out))
        return out

    def _parse_state(self, html: str, category: str) -> list:
        blobs = []
        # __NEXT_DATA__-style script tags
        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all("script", id=True):
            try:
                blobs.append(json.loads(tag.string or ""))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
        # window.__X__ = {...} assignments (embedded hydration payloads)
        for m in re.finditer(r"window\.[A-Za-z_$][\w$]*\s*=\s*(\{.*?\});?\s*</script>",
                             html, re.S):
            try:
                blobs.append(json.loads(m.group(1)))
            except (json.JSONDecodeError, ValueError):
                continue
        found = []
        for blob in blobs[:8]:
            _walk(blob, found)
        out = []
        seen_pids = set()
        for f in found[:80]:
            if f["pid"] and f["pid"] in seen_pids:
                continue
            if f["pid"]:
                seen_pids.add(f["pid"])
            price = f["price"]
            mrp = f.get("mrp") or 0
            disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
            url = f["url"] if f["url"].startswith("http") else f"https://www.jiomart.com/{f['url']}"
            try:
                reviews = int(f["reviews"]) if f.get("reviews") else None
            except (TypeError, ValueError):
                reviews = None
            out.append(self.make_product(
                self.platform, f["name"], price, url, mrp=mrp, discount_pct=disc,
                rating=float(f["rating"]) if f.get("rating") else None,
                reviews=reviews, image_url=f.get("image", ""), category=category))
        return out
