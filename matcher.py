"""Cross-platform product matching - groups the same product listed on
Flipkart / Amazon / Meesho so prices can be compared side by side."""
import re

STOP_TOKENS = {
    "with", "for", "and", "the", "pack", "of", "free", "combo", "offer", "offers",
    "deal", "deals", "new", "best", "buy", "online", "at", "low", "price", "india",
    "genuine", "original", "quality", "product", "products", "in", "on", "by",
    "smart", "pro", "plus", "max", "mini", "lite", "ultra",  # keep these OUT of stop list? no -
}
# NOTE: pro/plus/max/mini are meaningful model words; keep them as MATCH tokens.
STOP_TOKENS -= {"pro", "plus", "max", "mini", "lite", "ultra"}
STOP_TOKENS |= {"black", "white", "blue", "red", "green", "grey", "gray", "pink",
                "yellow", "purple", "silver", "gold", "multi", "color", "colour"}

BRANDS = {
    "samsung", "redmi", "xiaomi", "poco", "realme", "oneplus", "vivo", "oppo",
    "motorola", "moto", "infinix", "tecno", "nokia", "apple", "iphone", "iqoo",
    "nothing", "boat", "boAt".lower(), "noise", "jbl", "sony", "bolt", "fire",
    "ptron", "mivi", "wecool", "crossbeats", "truke", "carlton", "nike", "adidas",
    "puma", "campus", "bata", "liberty", "levis", "levi", "allen", "solly",
    "prestige", "pigeon", "hawkins", "milton", "cello", "borosil", "solimo",
    "amazonbasics", "philips", "havells", "bajaj", "usha", "lg", "whirlpool",
    "godrej", "voltas", "daikin", "asus", "acer", "lenovo", "hp", "dell", "msi",
    "apple", "mi", "ammo", "zebronics", "artis", "portronics", "urbangear",
}

STORAGE_RE = re.compile(r"(\d{2,4})\s?(gb|tb)\b", re.I)
RAM_RE = re.compile(r"(\d{1,2})\s?gb\s?ram", re.I)
MODEL_RE = re.compile(r"\b([a-z]{0,6}\d{1,3}[a-z]{0,3}|\d{3,4}[a-z]{0,2})\b", re.I)

# Model tokens built from the NORMALIZED title by merging letter+digit token
# pairs: "WH-1000XM5" normalizes to "wh 1000xm5" -> merged "wh1000xm5".
# XM5 vs XM4 then produce DIFFERENT tokens (strong negative signal), while
# "WH-1000XM5" vs "WH1000XM5" produce the SAME token across stores.
MERGE_A_RE = re.compile(r"[a-z]{1,7}")
MERGE_B_RE = re.compile(r"\d[a-z0-9]{1,7}")
MODEL_STANDALONE_RE = re.compile(r"[a-z]{1,4}\d{2,5}[a-z]{0,3}")


def normalize(title: str) -> str:
    t = re.sub(r"[^\w\s]", " ", (title or "").lower())
    return re.sub(r"\s+", " ", t).strip()


def model_tokens(title: str) -> set:
    """Variant identifiers: 'Sony WH-1000XM5' -> {'wh1000xm5'},
    'iPhone 15' -> {'iphone15'}. Punctuation-insensitive, brand-aware
    (never swallows the brand into the token)."""
    toks = normalize(title).split()
    out = set()
    for tok in toks:
        if MODEL_STANDALONE_RE.fullmatch(tok):
            out.add(tok)
    for a, b in zip(toks, toks[1:]):
        if MERGE_A_RE.fullmatch(a) and MERGE_B_RE.fullmatch(b):
            out.add(a + b)
    return out


def tokens(title: str) -> set:
    t = normalize(title)
    raw = set(t.split()) - STOP_TOKENS
    out = set()
    for tok in raw:
        if len(tok) <= 1:
            continue
        out.add(tok)
    for m in STORAGE_RE.findall(normalize(title)):
        out.add(m[0] + m[1].lower())
    out |= model_tokens(title)
    return out


def brand_of(title: str) -> str:
    t = normalize(title)
    for tok in t.split():
        if tok in BRANDS:
            return tok
    first = t.split()[0] if t.split() else ""
    return first if len(first) >= 2 else ""


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def same_product(p1: dict, p2: dict) -> bool:
    """Heuristic: same brand + enough shared tokens + compatible price range.
    Model tokens decide near-misses: shared model (wh1000xm5 on both stores)
    loosens the token threshold; CONFLICTING models (XM5 vs XM4) reject even
    when the titles look similar."""
    if p1["platform"] == p2["platform"]:
        return False
    b1, b2 = brand_of(p1["title"]), brand_of(p2["title"])
    if not b1 or b1 != b2:
        return False
    t1, t2 = tokens(p1["title"]), tokens(p2["title"])
    m1, m2 = model_tokens(p1["title"]), model_tokens(p2["title"])
    if m1 and m2 and not (m1 & m2):
        return False  # same brand, different model numbers -> different products
    j = jaccard(t1, t2)
    if (m1 & m2) and j >= 0.18:
        return _price_compatible(p1, p2)
    if j < 0.30:
        return False
    return _price_compatible(p1, p2)


def _price_compatible(p1: dict, p2: dict) -> bool:
    pr1, pr2 = p1["current_price"], p2["current_price"]
    if pr1 <= 0 or pr2 <= 0:
        return False
    ratio = max(pr1, pr2) / max(1, min(pr1, pr2))
    return ratio <= 3.0  # too far apart to be same item


def build_groups(products: list) -> list:
    """Union-find grouping. Returns list of groups (each a list of product dicts)."""
    n = len(products)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # cap pairwise comparisons for performance
    limit = min(n, 500)
    for i in range(limit):
        for j in range(i + 1, limit):
            if same_product(products[i], products[j]):
                union(i, j)

    groups = {}
    for i in range(limit):
        groups.setdefault(find(i), []).append(products[i])
    return [g for g in groups.values() if g]


def group_summary(group: list) -> dict:
    """For a group of same-product listings: cheapest platform + comparison."""
    if not group:
        return {}
    cheapest = min(group, key=lambda p: p["current_price"])
    by_platform = {p["platform"]: {"price": p["current_price"], "url": p["url"],
                                   "title": p["title"]} for p in group}
    return {
        "size": len(group),
        "cheapest_platform": cheapest["platform"],
        "cheapest_price": cheapest["current_price"],
        "by_platform": by_platform,
    }
