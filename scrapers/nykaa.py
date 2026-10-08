"""Nykaa adapter (beauty & cosmetics).

Nykaa serves listing pages with JSON-LD (ItemList/Product) plus an embedded
__INITIAL_STATE__. Both are parsed here. Like Meesho/Croma, Nykaa sits
behind Akamai and blocks many datacenter IPs (HTTP 403) - the adapter
retries silently every cycle and auto-activates the moment the site lets
the host through (residential / Indian IPs, many cloud ranges).
"""
import logging

from adapter import RetailerAdapter
from scraper_base import parse_price
from scrapers.state_walk import (dedupe, discover_links, extract_all_ldjson,
                                 extract_state, walk_products)

log = logging.getLogger("scraper.nykaa")

SEARCH_URLS = [
    "https://www.nykaa.com/search/result?searchText=lipstick",
    "https://www.nykaa.com/search/result?searchText=serum",
    "https://www.nykaa.com/search/result?searchText=kajal",
    "https://www.nykaa.com/search/result?searchText=perfume",
    "https://www.nykaa.com/search/result?searchText=shampoo",
    "https://www.nykaa.com/search/result?searchText=sunscreen",
    "https://www.nykaa.com/search/result?searchText=face%20wash",
    "https://www.nykaa.com/search/result?searchText=moisturizer",
    "https://www.nykaa.com/search/result?searchText=foundation",
    "https://www.nykaa.com/search/result?searchText=hair%20oil",
    "https://www.nykaa.com/search/result?searchText=makeup%20kit",
    "https://www.nykaa.com/search/result?searchText=hair%20dryer",
]

CATEGORY_URLS = [
    "https://www.nykaa.com/makeup/c/10001",
    "https://www.nykaa.com/skin/c/8377",
    "https://www.nykaa.com/hair/c/8378",
]

SPEC = {
    "name": ("name", "productName", "title", "displayName"),
    "price": ("finalPrice", "sellingPrice", "price", "discountedPrice", "offerPrice"),
    "mrp": ("mrp", "originalPrice", "priceInfo", "strikePrice", "listPrice", "basePrice"),
    "url": ("url", "productUrl", "productPageUrl", "webURL", "landingPageUrl"),
    "image": ("image", "imageURL", "imageUrl", "imageSrc", "thumbnails"),
    "rating": ("avgRating", "rating", "productAvgRating"),
    "reviews": ("reviewCount", "ratingCount", "reviewsCount", "totalReviews"),
    "availability": ("availability", "inStock", "sellable", "stockState", "isSellable"),
}


class NykaaAdapter(RetailerAdapter):
    platform = "nykaa"
    blocked_count = 0

    def _flat_urls(self):
        return [("beauty", u) for u in SEARCH_URLS] + \
               [("beauty", u) for u in CATEGORY_URLS]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        return discover_links(html, "nykaa") if html else []

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
        products = []
        if cats:
            picked = [(c.get("name") or "beauty", c["url"]) for c in cats]
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
            for d in discover_links(html, "nykaa")[:6]:
                self._record_discovered(d["url"], d["name"], d["depth"])
            products.extend(got)
        return products

    # ------------------------------------------------------------- parsing
    def _parse_listing(self, html: str, category: str) -> list:
        found = []

        # 1) JSON-LD: ItemList + Product blocks (most reliable on Nykaa)
        for block in extract_all_ldjson(html):
            btype = block.get("@type", "")
            if btype == "ItemList":
                for el in block.get("itemListElement", []):
                    item = el.get("item") if isinstance(el, dict) else None
                    if isinstance(item, dict):
                        self._ld_product(item, found)
            elif btype == "Product":
                self._ld_product(block, found)

        # 2) embedded state walker (catches grid data ld+json misses)
        if len(found) < 20:
            state = extract_state(html, ["window.__INITIAL_STATE__",
                                         "window.__PRELOADED_STATE__",
                                         '"__PRELOADED_STATE__":'])
            if state:
                walk_products(state, SPEC, found)

        out = []
        for f in dedupe(found):
            url = f["url"]
            if url.startswith("//"):
                url = "https:" + url
            elif url.startswith("/"):
                url = "https://www.nykaa.com" + url
            if not url.startswith("http"):
                url = f"https://www.nykaa.com/search/result?searchText={f['name'][:40]}"
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

    # ------------------------------------------------------------- product refresh
    def get_product_update(self, url: str):
        html = self.fetch(url)
        if not html:
            return None
        found = []
        for block in extract_all_ldjson(html):
            if block.get("@type") == "Product":
                self._ld_product(block, found)
        if not found:
            state = extract_state(html, ["window.__INITIAL_STATE__",
                                         "window.__PRELOADED_STATE__"])
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
