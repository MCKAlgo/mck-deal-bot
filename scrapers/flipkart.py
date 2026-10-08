"""Flipkart scraper: __INITIAL_STATE__ JSON walk + HTML card fallback.

v2 "next level": 13 category listings spanning electronics, fashion, home,
books, sports & offers store. Each check-cycle processes a rotating slice
(default 4 listings x page 1+2), completing the full sweep in ~4 cycles.
"""
import json
import logging
import re

from bs4 import BeautifulSoup

from adapter import RetailerAdapter
from scraper_base import parse_price

log = logging.getLogger("scraper.flipkart")

# Seed roots: the discovery engine expands these into the full tree
# automatically (see discovery.py) - no manual category maintenance.
SEED_CATEGORIES = [
    "https://www.flipkart.com/mobiles/pr?sid=tyy,io3&sort=discount_desc",
    "https://www.flipkart.com/electronics/pr?sid=tyy&sort=discount_desc",
    "https://www.flipkart.com/audio-video/pr?sid=0pm&sort=discount_desc",
    "https://www.flipkart.com/computers/pr?sid=6bo&sort=discount_desc",
    "https://www.flipkart.com/cameras/pr?sid=jek&sort=discount_desc",
    "https://www.flipkart.com/clothing-and-accessories/pr?sid=clo&sort=discount_desc",
    "https://www.flipkart.com/footwear/pr?sid=oto&sort=discount_desc",
    "https://www.flipkart.com/watches/pr?sid=r18&sort=discount_desc",
    "https://www.flipkart.com/home-kitchen/pr?sid=hpk&sort=discount_desc",
    "https://www.flipkart.com/home-appliances/pr?sid=j9e&sort=discount_desc",
    "https://www.flipkart.com/books/pr?sid=bks&sort=discount_desc",
    "https://www.flipkart.com/sports/pr?sid=abc&sort=discount_desc",
    "https://www.flipkart.com/offers-store",
]

# Kept for backwards compatibility with tools that import it
CATEGORY_URLS = SEED_CATEGORIES


def _walk(node, found):
    """Recursively walk JSON for Flipkart product-summary nodes."""
    if isinstance(node, dict):
        pid = node.get("productId") or node.get("listingId")
        if isinstance(pid, str) and len(pid) >= 8:
            pricing = node.get("pricing") or {}
            titles = node.get("titles") or node.get("title") or {}
            media = node.get("media") or {}
            rating_node = node.get("rating") or {}
            price = mrp = 0
            if isinstance(pricing, dict):
                fp = pricing.get("finalPrice")
                if isinstance(fp, dict):
                    price = parse_price(fp.get("value"))
                m = pricing.get("mrp")
                if isinstance(m, dict):
                    mrp = parse_price(m.get("value"))
                if not price:
                    price = parse_price(pricing.get("finalPrice") or pricing.get("price"))
                if not mrp:
                    mrp = parse_price(pricing.get("mrp"))
            title = ""
            if isinstance(titles, dict):
                title = titles.get("title") or ""
                if titles.get("subtitle"):
                    title = f"{title} {titles['subtitle']}"
            elif isinstance(titles, str):
                title = titles
            image = ""
            if isinstance(media, dict):
                images = media.get("images") or []
                if images and isinstance(images[0], dict):
                    image = images[0].get("url") or ""
                elif images and isinstance(images[0], str):
                    image = images[0]
            rating = None
            reviews = None
            if isinstance(rating_node, dict):
                rating = rating_node.get("averageStarRating") or rating_node.get("avgStarRating")
                reviews = rating_node.get("count") or rating_node.get("ratingCount")
            if price > 0 and title:
                found.append({"pid": pid, "title": title, "price": price, "mrp": mrp,
                              "image": image, "rating": rating, "reviews": reviews})
            return
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found)


class FlipkartScraper(RetailerAdapter):
    platform = "flipkart"

    def _flat_urls(self) -> list:
        return [("catalogue", u) for u in SEED_CATEGORIES]

    def discover_categories(self, html: str = "", seed_url: str = "") -> list:
        from discovery import extract
        return extract("flipkart", html, seed_url)

    def fetch_products(self, offset: int = 0, budget: int = 4, cats: list = None) -> list:
        products = []
        self.begin_sweep()

        def _valid(html: str) -> bool:
            return "/p/itm" in html or "productId" in html

        if cats:
            picked = [(c.get("name") or "catalogue", c["url"]) for c in cats]
        else:
            picked = self.rotate_slice(self._flat_urls(), offset, budget)
        log.info("[%s] cycle slice: %s", self.platform,
                 [u.split('?')[0].rsplit('/', 1)[-1] for _, u in picked])
        for category, url in picked:
            if self.circuit_open():
                break
            # page 2 doubles yield when Flipkart serves real pages
            page_urls = [url + "&page=2", url] if "?" in url else [url]
            for page_url in page_urls:
                if self.circuit_open():
                    break
                html = self.fetch(page_url, validate=_valid)
                if not html:
                    continue
                got = self._parse_json_state(html, category)
                if len(got) < 5:
                    got = self._parse_html_cards(html, category, got)
                for p in got:
                    p.setdefault("category", category)
                products.extend(got)
                # catalogue discovery piggybacks on already-fetched HTML (free)
                for d in self.discover_categories(html, url):
                    self._record_discovered(d["url"], d.get("name", ""),
                                            d.get("depth", 1))
        # de-dup by pid/url title
        seen = set()
        out = []
        for p in products:
            key = p["url"] or p["title"].lower()[:80]
            if key in seen:
                continue
            seen.add(key)
            out.append(p)
        log.info("[%s] scraped %d products", self.platform, len(out))
        return out

    def get_product_update(self, url: str):
        """Recheck a single product page (adaptive rotation tier)."""
        if "/p/" not in url and "pid=" not in url:
            return None

        def _valid(html: str) -> bool:
            return "/p/itm" in html or "productId" in html

        html = self.fetch(url, validate=_valid)
        if not html:
            return None
        got = self._parse_json_state(html, "recheck")
        if not got:
            return None
        first = got[0]
        out = {"price": first["current_price"]}
        if first.get("mrp"):
            out["mrp"] = first["mrp"]
        if first.get("rating"):
            out["rating"] = first["rating"]
        if first.get("reviews_count"):
            out["reviews_count"] = first["reviews_count"]
        if first.get("image_url"):
            out["image_url"] = first["image_url"]
        out["title"] = first["title"]
        return out

    def _parse_json_state(self, html: str, category: str) -> list:
        m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;?\s*</script>", html, re.S)
        if not m:
            return []
        try:
            state = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            return []
        found = []
        _walk(state, found)
        out = []
        for f in found[:60]:
            price = f["price"]
            mrp = f["mrp"] or 0
            disc = round((mrp - price) / mrp * 100, 1) if mrp > price else 0
            url = f"https://www.flipkart.com/product?pid={f['pid']}"
            try:
                reviews = int(f["reviews"]) if f.get("reviews") else None
            except (TypeError, ValueError):
                reviews = None
            out.append(self.make_product(
                self.platform, f["title"], price, url, mrp=mrp, discount_pct=disc,
                rating=float(f["rating"]) if f.get("rating") else None,
                reviews=reviews, image_url=f.get("image", ""), category=category))
        return out

    def _parse_html_cards(self, html: str, category: str, seed: list) -> list:
        soup = BeautifulSoup(html, "lxml")
        out = list(seed)
        seen_titles = {p["title"].lower()[:70] for p in out}
        cards = soup.select("a[href*='/p/itm']")
        for a in cards[:120]:
            try:
                href = a.get("href", "")
                if "/p/itm" not in href:
                    continue
                container = a.parent
                for _ in range(3):
                    if container is None:
                        break
                    text_len = len(container.get_text(" ", strip=True))
                    if text_len > 120:
                        break
                    container = container.parent
                block = container if container else a
                title = (a.get("title") or a.get_text(" ", strip=True) or "").strip()
                if not title:
                    img = a.find("img")
                    title = (img.get("alt") or img.get("title") or "") if img else ""
                title = title.strip()
                if len(title) < 12:
                    continue
                tkey = title.lower()[:70]
                if tkey in seen_titles:
                    continue
                block_text = block.get_text(" ", strip=True)
                prices = re.findall(r"(?:₹|&#8377;)\s*([\d,]+)", block_text)
                nums = [parse_price(x) for x in prices]
                nums = [n for n in nums if 100 <= n <= 500000]
                if not nums:
                    continue
                price = min(nums)
                mrp = max(nums) if len(nums) > 1 and max(nums) > price else 0
                dm = re.search(r"(\d{1,2})%\s*off", block_text)
                disc = float(dm.group(1)) if dm else (round((mrp - price) / mrp * 100, 1) if mrp > price else 0)
                rm = re.search(r"([0-4]\.\d)\s*★?", block_text)
                rating = float(rm.group(1)) if rm and 1 <= float(rm.group(1)) <= 5 else None
                revm = re.search(r"([\d,]{3,})\s*(?:Ratings|Reviews)", block_text)
                reviews = int(revm.group(1).replace(",", "")) if revm else None
                img = a.find("img")
                image = (img.get("src") or img.get("data-src") or "") if img else ""
                url = href if href.startswith("http") else f"https://www.flipkart.com{href}"
                out.append(self.make_product(self.platform, title, price, url, mrp=mrp,
                                             discount_pct=disc, rating=rating, reviews=reviews,
                                             image_url=image, category=category))
                seen_titles.add(tkey)
            except Exception as e:  # never kill the loop for one card
                log.debug("card parse skip: %s", str(e)[:80])
        return out
