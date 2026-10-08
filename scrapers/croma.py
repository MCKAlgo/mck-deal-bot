"""Croma adapter (croma.com) - Demandware/SFCC storefront.

Croma search & category pages are server-rendered on clean IPs with the
standard Demandware grid markup (pdp-link tiles + price value spans) and
usually application/ld+json Product blocks. Akamai blocks datacenter IPs
(HTTP 403) - the adapter auto-activates once the host IP is trusted, same
behaviour as the Meesho adapter.
"""
import json
import logging
import re

from bs4 import BeautifulSoup

from adapter import RetailerAdapter
from scraper_base import parse_price

log = logging.getLogger("scraper.croma")

SEED_CATEGORIES = [
    "https://www.croma.com/televisions/c/1001",
    "https://www.croma.com/audio-devices/c/1002",
    "https://www.croma.com/mobile-phones/c/1003",
    "https://www.croma.com/laptops-computers/c/1004",
    "https://www.croma.com/home-appliances/c/1005",
    "https://www.croma.com/kitchen-appliances/c/1006",
    "https://www.croma.com/personal-care/c/1007",
    "https://www.croma.com/cameras-accessories/c/1008",
]


class CromaAdapter(RetailerAdapter):
    platform = "croma"
    blocked_count = 0

    def _flat_urls(self) -> list:
        return [(c, u) for c, u in zip(
            ["electronics"] * 4 + ["home"] * 2 + ["beauty"] + ["electronics"],
            SEED_CATEGORIES)]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        from discovery import discover_generic
        return discover_generic(html, self.platform)

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
        picked = [(c, u) for c, u in (cats or [])] or self.rotate_slice(
            self._flat_urls(), offset, budget)
        products = []
        self.begin_sweep()
        for category, url in picked:
            if self.circuit_open():
                break
            html = self.fetch(url)
            if not html:
                self.blocked_count += 1
                if self.blocked_count % 15 == 1:
                    log.info("[%s] blocked (403) - auto-activates from trusted IPs",
                             self.platform)
                continue
            got = self._parse_listing(html, category)
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

    def _parse_listing(self, html: str, category: str) -> list:
        out = []
        # 1) JSON-LD Product blocks (most reliable)
        for m in re.finditer(
                r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                html, re.S | re.I):
            try:
                data = json.loads(m.group(1).strip())
            except (json.JSONDecodeError, ValueError):
                continue
            for node in (data if isinstance(data, list) else [data]):
                if not isinstance(node, dict) or node.get("@type") != "Product":
                    continue
                p = self._product_from_jsonld(node, category)
                if p:
                    out.append(p)
        # 2) Demandware tile fallback
        if len(out) < 5:
            out.extend(self._parse_tiles(html, category, out))
        return out[:60]

    def _product_from_jsonld(self, node: dict, category: str) -> dict:
        title = node.get("name") or ""
        offers = node.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        price = parse_price((offers or {}).get("price"))
        if not title or len(title) < 10 or price <= 0:
            return None
        url = node.get("url") or ""
        image = node.get("image") or ""
        if isinstance(image, list):
            image = image[0] if image else ""
        mrp = 0.0
        sp = (offers or {}).get("highPrice") or (offers or {}).get("listPrice")
        if sp:
            mrp = parse_price(sp)
        if mrp <= price:
            mrp = 0.0
        rating = None
        ar = node.get("aggregateRating") or {}
        try:
            rating = float(ar.get("ratingValue")) if ar else None
        except (TypeError, ValueError):
            rating = None
        reviews = None
        try:
            reviews = int(float(ar.get("reviewCount"))) if ar else None
        except (TypeError, ValueError):
            reviews = None
        disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
        return self.make_product(self.platform, title, price, url, mrp=mrp,
                                 discount_pct=disc, rating=rating, reviews=reviews,
                                 image_url=image, category=category)

    def _parse_tiles(self, html: str, category: str, seed: list) -> list:
        soup = BeautifulSoup(html, "lxml")
        out = list(seed)
        seen = {p["title"].lower()[:70] for p in out}
        for tile in soup.select("li.product-item, div.product-tile")[:60]:
            try:
                a = tile.select_one("a.link, a.pdp-link, a[href*='/p/']")
                if not a:
                    continue
                title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
                if len(title) < 10 or title.lower()[:70] in seen:
                    continue
                price_el = tile.select_one(".price .value, .pdp-price, span.price")
                if not price_el:
                    continue
                price = parse_price(price_el.get_text())
                if price < 100:
                    continue
                strike = tile.select_one(".price .strike-through, s, del")
                mrp = parse_price(strike.get_text()) if strike else 0.0
                if mrp <= price:
                    mrp = 0.0
                disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0
                href = a.get("href", "")
                url = href if href.startswith("http") else f"https://www.croma.com{href}"
                img = tile.find("img")
                image = (img.get("src") or img.get("data-src") or "") if img else ""
                out.append(self.make_product(self.platform, title, price, url,
                                             mrp=mrp, discount_pct=disc,
                                             image_url=image, category=category))
                seen.add(title.lower()[:70])
            except Exception as e:
                log.debug("croma tile skip: %s", str(e)[:70])
        return out
