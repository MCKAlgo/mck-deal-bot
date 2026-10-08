"""Ajio adapter (Reliance fashion).

Ajio hydrates listing pages with a `window.__myx = {...}` state blob that
contains the product grid (pims). Behind Akamai - blocks datacenter IPs
with 403; auto-activates from clean/residential IPs without config change.
"""
import logging

from adapter import RetailerAdapter
from scrapers.state_walk import (dedupe, discover_links, extract_state,
                                 walk_products)

log = logging.getLogger("scraper.ajio")

CATEGORY_URLS = [
    "https://www.ajio.com/men/tshirts/c/830216016",
    "https://www.ajio.com/men/shirts/c/830216017",
    "https://www.ajio.com/men/jeans/c/830216001",
    "https://www.ajio.com/men/sports-shoes/c/830221701",
    "https://www.ajio.com/men/watches/c/830221714",
    "https://www.ajio.com/women/kurtas/c/830312026",
    "https://www.ajio.com/women/sarees/c/830312046",
    "https://www.ajio.com/women/heels/c/830313853",
    "https://www.ajio.com/bags/c/830305301",
    "https://www.ajio.com/men/backpacks/c/830216074",
]

SEARCH_URLS = [
    "https://www.ajio.com/search/?text=smartwatch",
    "https://www.ajio.com/search/?text=earbuds",
    "https://www.ajio.com/search/?text=sneakers",
    "https://www.ajio.com/search/?text=handbag",
    "https://www.ajio.com/search/?text=sunglasses",
    "https://www.ajio.com/search/?text=bluetooth%20speaker",
]

SPEC = {
    "name": ("fname", "name", "productName", "title"),
    "price": ("effPrice", "finalPrice", "sellingPrice", "price", "discountedPrice"),
    "mrp": ("mrpAmount", "mrp", "wasPriceData", "originalPrice", "listPrice", "strikePrice"),
    "url": ("url", "productUrl", "seoUrl", "productPageUrl"),
    "image": ("imageURL", "image", "imageUrl", "imgSrc"),
    "rating": ("rating", "avgRating", "ratingValue"),
    "reviews": ("ratingCount", "reviewCount", "reviewsCount"),
    "availability": ("availability", "inStock", "isSellable", "sellable"),
}


class AjioAdapter(RetailerAdapter):
    platform = "ajio"
    blocked_count = 0

    def __init__(self):
        super().__init__()
        # Ajio rejects default gzip/br-less GETs sometimes; nudge headers
        self.session.headers["Referer"] = "https://www.ajio.com/"

    def _flat_urls(self):
        out = []
        for u in CATEGORY_URLS:
            parts = u.split("/")
            out.append((parts[3] if len(parts) > 3 else "fashion", u))
        for u in SEARCH_URLS:
            out.append(("search", u))
        return out

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        return discover_links(html, "ajio") if html else []

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
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
            if not html:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] blocked (Akamai 403) - retrying each cycle; "
                             "auto-activates from residential/IN IPs", self.platform)
                continue
            got = self._parse_listing(html, category)
            for d in discover_links(html, "ajio")[:6]:
                self._record_discovered(d["url"], d["name"], d["depth"])
            products.extend(got)
        return products

    # ------------------------------------------------------------- parsing
    def _parse_listing(self, html: str, category: str) -> list:
        state = extract_state(html, [
            "window.__myx", 'id="__myx"', "window.__prelam",
            "window.__INITIAL_STATE__",
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
                url = "https://www.ajio.com" + url
            if "/p/" not in url:
                url = f"https://www.ajio.com/search/?text={f['name'][:40]}"
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
        state = extract_state(html, ["window.__myx", "window.__INITIAL_STATE__"])
        if not state:
            return None
        found = []
        walk_products(state, SPEC, found)
        if not found:
            return None
        # on a PDP the main product is the entry matching the url path
        path = url.split("ajio.com")[-1]
        f = next((x for x in found if x["url"] and path in x["url"]), found[0])
        return {
            "price": f["price"],
            "mrp": f.get("mrp") or None,
            "rating": f.get("rating"),
            "reviews_count": f.get("reviews"),
            "image_url": f.get("image", ""),
            "availability": f.get("availability"),
        }
