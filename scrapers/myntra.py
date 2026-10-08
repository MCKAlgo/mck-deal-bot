"""Myntra adapter (fashion - Flipkart Group).

Myntra hydrates listing pages with a `window.__myx = {...}` state blob
(same family as Ajio). Behind Akamai - serves a 483-byte bot-challenge
shell to datacenter IPs; the adapter auto-activates the moment the host
IP is trusted (residential / Indian IPs, some cloud ranges).
"""
import logging

from adapter import RetailerAdapter
from scrapers.state_walk import (dedupe, discover_links, extract_state,
                                 walk_products)

log = logging.getLogger("scraper.myntra")

CATEGORY_URLS = [
    "https://www.myntra.com/men-tshirts",
    "https://www.myntra.com/men-casual-shirts",
    "https://www.myntra.com/men-jeans",
    "https://www.myntra.com/men-sports-shoes",
    "https://www.myntra.com/men-watches",
    "https://www.myntra.com/women-kurtas",
    "https://www.myntra.com/women-sarees",
    "https://www.myntra.com/women-heels",
    "https://www.myntra.com/women-tshirts",
    "https://www.myntra.com/backpacks",
    "https://www.myntra.com/watches",
    "https://www.myntra.com/kids-clothing",
]

SEARCH_URLS = [
    "https://www.myntra.com/smartwatch?f=Categories%3ASmart%20Watches%3A%3A421",
    "https://www.myntra.com/earbuds",
    "https://www.myntra.com/sneakers",
    "https://www.myntra.com/handbags",
]

SPEC = {
    "name": ("productName", "name", "title", "fname"),
    "price": ("price", "finalPrice", "discountedPrice", "sellingPrice"),
    "mrp": ("mrp", "mrpAmount", "originalPrice", "listPrice", "strikePrice"),
    "url": ("url", "productUrl", "landingPageUrl", "seoUrl"),
    "image": ("image", "imageURL", "imageUrl", "src"),
    "rating": ("rating", "avgRating", "ratingValue"),
    "reviews": ("ratingCount", "reviewCount", "reviewsCount"),
    "availability": ("availability", "inStock", "isSellable"),
}


class MyntraAdapter(RetailerAdapter):
    platform = "myntra"
    blocked_count = 0

    def _flat_urls(self):
        return [(u.rstrip("/").rsplit("/", 1)[-1][:24], u) for u in CATEGORY_URLS] + \
               [("search", u) for u in SEARCH_URLS]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        # Myntra category pages are /slug style (no /c/); collect from __myx
        if not html:
            return []
        out = []
        for m in __import__("re").finditer(
                r'href="(https://www\.myntra\.com/[a-z0-9\-]{4,60})"', html):
            out.append({"url": m.group(1), "name": m.group(1).rsplit("/", 1)[-1],
                        "depth": 1})
            if len(out) >= 30:
                break
        return out

    def fetch_products(self, offset: int = 0, budget: int = 8, cats: list = None) -> list:
        products = []
        self.begin_sweep()
        if cats:
            picked = [(c.get("name") or "fashion", c["url"]) for c in cats]
        else:
            picked = self.rotate_slice(self._flat_urls(), offset, budget)
        for category, url in picked:
            if self.circuit_open():
                break
            html = self.fetch(url)
            if not html or len(html) < 30000:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] shell/blocked (Akamai) - retrying each cycle; "
                             "auto-activates from residential/IN IPs", self.platform)
                continue
            got = self._parse_listing(html, category)
            for d in self.discover_categories(html)[:6]:
                self._record_discovered(d["url"], d["name"], d["depth"])
            products.extend(got)
        return products

    def _parse_listing(self, html: str, category: str) -> list:
        state = extract_state(html, ["window.__myx", "window.__INITIAL_STATE__"])
        found = []
        if state:
            walk_products(state, SPEC, found)
        out = []
        for f in dedupe(found):
            url = f["url"]
            if url.startswith("/"):
                url = "https://www.myntra.com" + url
            elif not url.startswith("http"):
                url = "https://www.myntra.com/" + url
            if "/p/" not in url:
                continue  # Myntra product URLs always carry /p/<id>
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

    def get_product_update(self, url: str):
        html = self.fetch(url)
        if not html or len(html) < 30000:
            return None
        state = extract_state(html, ["window.__myx", "window.__INITIAL_STATE__"])
        if not state:
            return None
        found = []
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
