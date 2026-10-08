"""Shared embedded-state extractor + product walker for JS-hydrated stores.

Many Indian retail sites (Ajio, TataCliq, Nykaa, JioMart...) embed their
product data as a JSON blob inside the HTML:

    <script>window.__myx = { ... };</script>
    <script>window.__PRELOADED_STATE__ = JSON.parse("{ ...escaped... }")</script>
    <script type="application/ld+json">{ "@type": "Product", ... }</script>

This module reliably pulls those blobs out (brace matching, JSON.parse
support) and walks them for product-shaped dicts, so each retailer adapter
only declares *which* keys it uses.
"""
import json
import logging
import re

from scraper_base import parse_price

log = logging.getLogger("state_walk")


# ---------------------------------------------------------------- extraction
def _balanced_json(text: str, start: int) -> str:
    """Return the balanced {...}/[...] JSON substring starting at `start`."""
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth, in_str, esc = 0, False, False
    for i in range(start, min(len(text), start + 4_000_000)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c in "{[":
            depth += 1
        elif c in "}]":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return ""


def extract_state(html: str, markers: list):
    """Find the first parseable JSON state blob after any of `markers`.

    Handles both `MARKER = {...}` and `MARKER = JSON.parse("...")` forms.
    Returns a python object (dict/list) or None.
    """
    for marker in markers:
        idx = html.find(marker)
        if idx < 0:
            continue
        seg = html[idx:idx + 800]
        # JSON.parse("...") form -> unescape the string literal
        m = re.match(re.escape(marker) + r"\s*=\s*JSON\.parse\(", seg)
        if m:
            q = html.find('"', idx + len(marker))
            if q < 0:
                continue
            # balanced quote scan (respect escapes)
            end, i = q + 1, q + 1
            while i < len(html):
                if html[i] == "\\":
                    i += 2
                    continue
                if html[i] == '"':
                    end = i
                    break
                i += 1
            try:
                raw = json.loads(html[q:end + 1])
                data = json.loads(raw)
                if isinstance(data, (dict, list)):
                    return data
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
        # plain `MARKER = { ... }` form -> brace matching
        brace = html.find("{", idx + len(marker) - 1)
        if brace < 0:
            continue
        blob = _balanced_json(html, brace)
        if not blob:
            continue
        try:
            data = json.loads(blob)
            if isinstance(data, (dict, list)):
                return data
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return None


def extract_all_ldjson(html: str) -> list:
    """Parse every application/ld+json block in the page."""
    out = []
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
                         html, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
        blocks = data if isinstance(data, list) else [data]
        for b in blocks:
            if isinstance(b, dict):
                out.append(b)
    return out


# ---------------------------------------------------------------- walking
def _num(v):
    """Price-ish value -> float. Handles 123, '1,23,456', {'value': 123}."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v > 0 else None
    if isinstance(v, dict):
        for k in ("value", "amount", "price", "finalPrice", "sellingPrice", "mrp"):
            if k in v:
                return _num(v[k])
        return None
    if isinstance(v, str):
        p = parse_price(v)
        return p if p > 0 else None
    return None


def _clean_str(v):
    return v.strip() if isinstance(v, str) else ""


def walk_products(node, spec, found, depth=0):
    """Recursively collect product-shaped dicts from arbitrary JSON.

    spec keys (all are tuples of candidate names):
      name / price / mrp / url / image / rating / reviews / availability
    A dict is treated as a leaf product when it yields BOTH a name and price.
    """
    if depth > 45 or len(found) > 400:
        return
    if isinstance(node, dict):
        name = None
        for k in spec["name"]:
            if k in node and isinstance(node[k], str) and 10 < len(node[k]) < 300:
                name = node[k].strip()
                break
        price = None
        for k in spec["price"]:
            if k in node:
                price = _num(node[k])
                if price:
                    break
        if name and price:
            mrp = None
            for k in spec["mrp"]:
                if k in node:
                    v = _num(node[k])
                    if v and v >= price:
                        mrp = v
                        break
            url = ""
            for k in spec["url"]:
                if k in node and isinstance(node[k], str):
                    url = node[k]
                    break
            image = ""
            for k in spec["image"]:
                if k in node and isinstance(node[k], str) and node[k]:
                    image = node[k]
                    break
                if k in node and isinstance(node[k], list) and node[k]:
                    first = node[k][0]
                    image = _clean_str(first.get("url", "")) if isinstance(first, dict) else _clean_str(first)
                    if image:
                        break
            rating = None
            for k in spec["rating"]:
                if k in node:
                    v = _num(node[k])
                    if v and 0 < v <= 5:
                        rating = v
                        break
            reviews = None
            for k in spec["reviews"]:
                if k in node:
                    v = _num(node[k])
                    if v:
                        reviews = int(v)
                        break
            availability = None
            for k in spec.get("availability", ()):
                if k in node:
                    v = node[k]
                    if isinstance(v, bool):
                        availability = "InStock" if v else "OutOfStock"
                    elif isinstance(v, str):
                        low = v.lower()
                        if any(x in low for x in ("outofstock", "out_of_stock", "unavailable")):
                            availability = "OutOfStock"
                        elif any(x in low for x in ("instock", "in_stock", "sellable", "active", "available")):
                            availability = "InStock"
                    break
            found.append({
                "name": name, "price": price, "mrp": mrp or 0.0,
                "url": url, "image": image, "rating": rating,
                "reviews": reviews, "availability": availability,
            })
            return  # leaf
        for v in node.values():
            walk_products(v, spec, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            walk_products(item, spec, found, depth + 1)


def dedupe(found: list, limit: int = 80) -> list:
    seen, out = set(), []
    for f in found:
        key = (f["url"] or f["name"].lower()[:70])
        if key in seen:
            continue
        seen.add(key)
        out.append(f)
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------- link discovery
HREF_RE = re.compile(r'<a[^>]+href="([^"]+)"[^>]*>', re.I)

CATEGORY_PATTERNS = {
    "nykaa": re.compile(r"^https?://www\.nykaa\.com/[a-z0-9\-]+(?:/[a-z0-9\-]+)*/c/\w+", re.I),
    "ajio": re.compile(r"^https?://www\.ajio\.com/(?:[^/]+/)*[^/]+/c/\w+", re.I),
    "tatacliq": re.compile(r"^https?://www\.tatacliq\.com/[^/]+/c-\w+", re.I),
}


def discover_links(html: str, platform: str, base: str = "https://www.") -> list:
    """Pull internal category listing links out of a listing page."""
    pat = CATEGORY_PATTERNS.get(platform)
    if not pat:
        return []
    host = {"nykaa": "www.nykaa.com", "ajio": "www.ajio.com",
            "tatacliq": "www.tatacliq.com"}.get(platform, "")
    out, seen = [], set()
    for m in HREF_RE.finditer(html):
        href = m.group(1).split("?")[0].split("#")[0]
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = f"https://{host}{href}"
        if not href.startswith("http"):
            continue
        if host and host not in href:
            continue
        if not pat.match(href) or href in seen:
            continue
        seen.add(href)
        label = href.rstrip("/").split("/")[-1].replace("-", " ")[:60]
        out.append({"url": href, "name": label, "depth": 1})
        if len(out) >= 30:
            break
    return out
