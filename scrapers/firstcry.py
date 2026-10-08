"""FirstCry adapter (baby, kids & mother care).

FirstCry serves a JS-rendered SPA to plain HTTP clients (3.5 MB of
bundles, zero product data), so from datacenter IPs the adapter mostly
sees shells and keeps retrying quietly. Their robots.txt explicitly
allows AI/search crawlers, so when the site serves real HTML (clean
IPs / crawler-friendly ranges) we parse both JSON-LD and embedded
state blobs. Confirmed reachable category slugs are used as seeds.
"""
import logging
import re

from adapter import RetailerAdapter
from scrapers.state_walk import (dedupe, extract_all_ldjson, extract_state,
                                 walk_products)

log = logging.getLogger("scraper.firstcry")

CATEGORY_URLS = [
    "https://www.firstcry.com/kids-footwear",
    "https://www.firstcry.com/baby-footwear",
    "https://www.firstcry.com/boys-clothing/boy-clothing",
    "https://www.firstcry.com/girls-clothing",
    "https://www.firstcry.com/toys-games",
    "https://www.firstcry.com/baby-care",
    "https://www.firstcry.com/diapers",
    "https://www.firstcry.com/feeding",
]

SEARCH_URLS = [
    "https://www.firstcry.com/search?q=baby%20toys",
    "https://www.firstcry.com/search?q=diapers",
    "https://www.firstcry.com/search?q=baby%20lotion",
    "https://www.firstcry.com/search?q=kids%20shoes",
]

SPEC = {
    "name": ("productName", "productTitle", "name", "title"),
    "price": ("price", "sellingPrice", "finalPrice", "offerPrice", "SP"),
    "mrp": ("mrp", "MRP", "listPrice", "originalPrice", "strikePrice"),
    "url": ("productUrl", "productPageUrl", "url", "webURL"),
    "image": ("imageURL", "productImage", "image", "imageUrl"),
    "rating": ("avgRating", "productAvgRating", "rating"),
    "reviews": ("reviewCount", "productReviewCount", "ratingCount"),
    "availability": ("availability", "inStock", "isSellable"),
}


class FirstCryAdapter(RetailerAdapter):
    platform = "firstcry"
    blocked_count = 0

    def _flat_urls(self):
        return [(u.rstrip("/").rsplit("/", 1)[-1][:24], u) for u in CATEGORY_URLS] + \
               [("search", u) for u in SEARCH_URLS]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        if not html:
            return []
        out, seen = [], set()
        for m in re.finditer(r'href="(https://www\.firstcry\.com/[a-zA-Z0-9\-]{4,60})"', html):
            u = m.group(1)
            if u in seen:
                continue
            seen.add(u)
            out.append({"url": u, "name": u.rsplit("/", 1)[-1], "depth": 1})
            if len(out) >= 30:
                break
        return out

    def fetch_products(self, offset: int = 0, budget: int = 6, cats: list = None) -> list:
        products = []
        self.begin_sweep()
        if cats:
            picked = [(c.get("name") or "kids", c["url"]) for c in cats]
        else:
            picked = self.rotate_slice(self._flat_urls(), offset, budget)
        for category, url in picked:
            if self.circuit_open():
                break
            html = self.fetch(url)
            if not html or len(html) < 60000:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] JS shell / blocked - retrying each cycle; "
                             "auto-activates from trusted IPs", self.platform)
                continue
            got = self._parse_listing(html, category)
            for d in self.discover_categories(html)[:6]:
                self._record_discovered(d["url"], d["name"], d["depth"])
            products.extend(got)
        return products

    def _parse_listing(self, html: str, category: str) -> list:
        found = []
        # JSON-LD ItemList/Product blocks
        for block in extract_all_ldjson(html):
            btype = block.get("@type", "")
            if btype == "ItemList":
                for el in block.get("itemListElement", []):
                    item = el.get("item") if isinstance(el, dict) else None
                    if isinstance(item, dict):
                        self._ld_product(item, found)
            elif btype == "Product":
                self._ld_product(block, found)
        # embedded state
        if len(found) < 20:
            state = extract_state(html, ["window.__PRELOADED_STATE__",
                                         "window.__INITIAL_STATE__",
                                         '"__PRELOADED_STATE__"',
                                         "window.appState"])
            if state:
                walk_products(state, SPEC, found)
        out = []
        for f in dedupe(found):
            url = f["url"]
            if url.startswith("/"):
                url = "https://www.firstcry.com" + url
            if not url.startswith("http"):
                url = f"https://www.firstcry.com/search?q={f['name'][:40]}"
            price = f["price"]
            mrp = f.get("mrp") or 0
            disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
            try:
                reviews = int(f["reviews"]) if f.get("reviews") else None
            except (TypeError, ValueError):
                reviews = None
            out.append(self.make_product(
                self.platform, f["name"], price, url, mrp=mrp, discount_pct=disc,
                rating=f.get("rating"), reviews=reviews,
                image_url=f.get("image", ""), category=category))
        if out:
            log.info("[%s] scraped %d products", self.platform, len(out))
        return out

    def _ld_product(self, item: dict, found: list):
        from scraper_base import parse_price
        name = item.get("name")
        offers = item.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = parse_price(offers.get("price"))
        if not (name and price):
            return
        mrp = parse_price(offers.get("highPrice")) or 0
        image = item.get("image") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        found.append({
            "name": str(name), "price": price, "mrp": mrp,
            "url": item.get("url", "") or item.get("@id", ""),
            "image": image or "", "rating": None, "reviews": None,
            "availability": None,
        })

    def get_product_update(self, url: str):
        html = self.fetch(url)
        if not html or len(html) < 60000:
            return None
        found = []
        for block in extract_all_ldjson(html):
            if block.get("@type") == "Product":
                self._ld_product(block, found)
        if not found:
            state = extract_state(html, ["window.__PRELOADED_STATE__",
                                         "window.__INITIAL_STATE__"])
            if state:
                walk_products(state, SPEC, found)
        if not found:
            return None
        f = found[0]
        return {
            "price": f["price"],
            "mrp": f.get("mrp") or None,
            "rating": f.get("rating"),
            "reviews_count": f.get("reviews"),
            "image_url": f.get("image", ""),
            "availability": f.get("availability"),
        }
