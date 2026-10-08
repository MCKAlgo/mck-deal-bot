"""Catalogue discovery engine - finds deeper subcategories automatically.

Instead of hand-maintained category lists, the engine seeds a few root
categories per retailer and then DISCOVERS the full tree from listing-page
HTML itself:

Flipkart  - listing pages embed links to deeper sids
            (/mobiles/pr?sid=tyy,io3 -> /accessories/pr?sid=tyy,io3,abc...)
            each discovered sid becomes a new listing to crawl, so the
            category universe expands breadth-first, automatically.
Amazon    - search pages embed department browse-nodes (/b/?node=NNN and
            rh=n%3ANNN facet links) which become filtered search URLs.

Discovered categories go into the DB `categories` table and feed the
rotating sweep, so coverage grows every cycle without manual edits.
"""
import logging
import re
from urllib.parse import unquote, urljoin

log = logging.getLogger("discovery")

# Flipkart: any listing link carrying a sid (deeper = longer sid)
FK_LISTING_RE = re.compile(
    r'href="(https?://www\.flipkart\.com/[a-z0-9-]+/pr\?sid=([a-z0-9,]+)[^"]*)"',
    re.I)
FK_REL_LISTING_RE = re.compile(
    r'href="(/[^"]*?/pr\?sid=([a-z0-9,]+)[^"]*)"', re.I)
FK_NAME_RE = re.compile(r'<a[^>]*href="[^"]*sid=([a-z0-9,]+)[^"]*"[^>]*>'
                        r'([^<]{3,60})</a>', re.I)

# Amazon: browse-node links + department facets
AMZ_NODE_RE = re.compile(r'href="(/(?:b|gp/browse)\?[^"]*?node=(\d{5,15})[^"]*)"',
                         re.I)
AMZ_RH_RE = re.compile(r'rh=n%3A(\d{5,15})')
AMZ_NODE_NAME_RE = re.compile(
    r'<a[^>]*href="[^"]*node=(\d{5,15})[^"]*"[^>]*>\s*(?:<span[^>]*>)?'
    r'([^<]{3,50})', re.I)


def discover_flipkart(html: str, seed_sid: str = "") -> list:
    """Returns {url, name, depth} for subcategories DEEPER than seed_sid."""
    out = {}
    if not html:
        return []
    for pattern in (FK_LISTING_RE, FK_REL_LISTING_RE):
        for href, sid in pattern.findall(html):
            sid = sid.strip(",").lower()
            if not sid or sid == seed_sid.lower():
                continue
            if seed_sid and not sid.startswith(seed_sid.lower() + ","):
                continue  # only deeper branches of this seed
            url = href if href.startswith("http") else f"https://www.flipkart.com{href}"
            url = url.split("&page=")[0]  # strip pagination
            if url not in out:
                depth = sid.count(",") + 1
                out[url] = {"url": url, "name": sid, "depth": depth}
    # attach human names where available
    for sid, name in FK_NAME_RE.findall(html):
        name = name.strip()
        for item in out.values():
            if item["name"] == sid and name and not name[0].isdigit():
                item["name"] = name[:60]
    return list(out.values())[:40]


def discover_amazon(html: str, base: str = "https://www.amazon.in") -> list:
    """Returns {url, name, depth} for department browse-nodes found on a page."""
    out = {}
    if not html:
        return []
    for href, node in AMZ_NODE_RE.findall(html):
        url = f"{base}{href}"
        if node not in {u.rsplit("node=", 1)[-1].split("&")[0] for u in out}:
            out[url] = {"url": url, "name": f"node-{node}", "depth": 1}
    for node, name in AMZ_NODE_NAME_RE.findall(html):
        name = name.strip()
        for item in out.values():
            if item["url"].rsplit("node=", 1)[-1].split("&")[0] == node and len(name) > 2:
                item["name"] = name[:60]
    # rh facets also reveal nodes: build search URLs for them
    for node in AMZ_RH_RE.findall(html)[:20]:
        url = f"{base}/s?rh=n%3A{node}"
        out.setdefault(url, {"url": url, "name": f"node-{node}", "depth": 1})
    return list(out.values())[:30]


def discover_generic(html: str, platform: str) -> list:
    """Last-resort generic discovery: JSON-LD ItemList / breadcrumb URLs.
    Used by adapters for retailers without a bespoke extractor."""
    out = []
    if not html:
        return []
    for m in re.finditer(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S | re.I):
        blob = m.group(1)
        if '"ItemList"' not in blob and '"BreadcrumbList"' not in blob:
            continue
        for um in re.finditer(r'"url"\s*:\s*"([^"]+)"', blob):
            u = um.group(1)
            if any(k in u for k in ("/c/", "/pl/", "/s?", "/b?")):
                out.append({"url": u, "name": "", "depth": 1})
    return out[:20]


def extract(platform: str, html: str, seed_url: str = "") -> list:
    """Platform router used by adapters."""
    if platform == "flipkart":
        seed_sid = ""
        m = re.search(r"sid=([a-z0-9,]+)", seed_url or "", re.I)
        if m:
            seed_sid = m.group(1)
        return discover_flipkart(html, seed_sid)
    if platform == "amazon":
        return discover_amazon(html)
    return discover_generic(html, platform)
