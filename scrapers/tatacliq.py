"""Tata CLiQ adapter (electronics + fashion generalist).

TataCliq hydrates pages from `window.__PRELOADED_STATE__ = JSON.parse("...")`
(escaped-JSON form - handled by the shared extractor). Also Akamai-protected;
403 from datacenter IPs, auto-activates from clean hosts.
"""
import logging

from adapter import RetailerAdapter
from scrapers.state_walk import (dedupe, discover_links, extract_state,
                                 walk_products)

log = logging.getLogger("scraper.tatacliq")

CATEGORY_URLS = [
    "https://www.tatacliq.com/mens-t-shirts/c-msh1207002",
    "https://www.tatacliq.com/mens-casual-shirts/c-msh1207006",
    "https://www.tatacliq.com/mens-sports-shoes/c-msh1206203",
    "https://www.tatacliq.com/womens-kurtas-kurtis/c-msh1207006",
    "https://www.tatacliq.com/mobiles/c-msh11c7001",
    "https://www.tatacliq.com/headphones-earphones/c-msh11c7003",
    "https://www.tatacliq.com/watches/c-msh1207004",
    "https://www.tatacliq.com/laptops/c-mth11c7003",
]

SEARCH_URLS = [
    "https://www.tatacliq.com/search/?searchCategory=all&text=smartwatch",
    "https://www.tatacliq.com/search/?searchCategory=all&text=earbuds",
    "https://www.tatacliq.com/search/?searchCategory=all&text=power%20bank",
    "https://www.tatacliq.com/search/?searchCategory=all&text=sneakers",
    "https://www.tatacliq.com/search/?searchCategory=all&text=handbag",
    "https://www.tatacliq.com/search/?searchCategory=all&text=air%20fryer",
]

SPEC = {
    "name": ("productTitle", "name", "title", "productName", "displayName"),
    "price": ("sellingPrice", "finalPrice", "offerPrice", "price", "discountedPrice",
              "effectivePrice"),
    "mrp": ("mrpFloat", "mrp", "mrpAmount", "listPrice", "strikePrice", "originalPrice"),
    "url": ("productPageUrl", "webURL", "url", "productUrl", "landingPageURL"),
    "image": ("productImage", "imageURL", "image", "imageUrl", "imgSrc"),
    "rating": ("productAvgRating", "avgRating", "rating"),
    "reviews": ("productReviewCount", "reviewCount", "ratingCount"),
    "availability": ("availability", "inStock", "sellable", "isSellable", "stockState"),
}


class TataCliqAdapter(RetailerAdapter):
    platform = "tatacliq"
    blocked_count = 0

    def _flat_urls(self):
        out = [(u.rstrip("/").split("/")[-1][:24], u) for u in CATEGORY_URLS]
        out += [("search", u) for u in SEARCH_URLS]
        return out

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        return discover_links(html, "tatacliq") if html else []

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
        products = []
        if cats:
            picked = [(c.get("name") or "general", c["url"]) for c in cats]
        else:
            picked = self.rotate_slice(self._flat_urls(), offset, budget)
        for category, url in picked:
            html = self.fetch(url)
            if not html:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] blocked (Akamai 403) - retrying each cycle; "
                             "auto-activates from residential/IN IPs", self.platform)
                continue
            got = self._parse_listing(html, category)
            for d in discover_links(html, "tatacliq")[:6]:
                self._record_discovered(d["url"], d["name"], d["depth"])
            products.extend(got)
        return products

    # ------------------------------------------------------------- parsing
    def _parse_listing(self, html: str, category: str) -> list:
        state = extract_state(html, [
            "window.__PRELOADED_STATE__", '"__PRELOADED_STATE__"',
            "window.__INITIAL_STATE__", "window.appState",
        ])
        found = []
        if state:
            walk_products(state, SPEC, found)
        out = []
        for f in dedupe(found):
            url = f["url"]
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://www.tatacliq.com" + url
            if "tatacliq.com" not in url:
                url = f"https://www.tatacliq.com/search/?searchCategory=all&text={f['name'][:40]}"
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
        if not html:
            return None
        state = extract_state(html, ["window.__PRELOADED_STATE__",
                                     "window.__INITIAL_STATE__"])
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
