"""MCK Deal Intelligence Engine - genuine-deal scorer v2.

Deal Score (0-100) built from 9 weighted factors:
  price_advantage      25   vs next-best verified cross-store price
  historical_advantage 20   vs 90-day avg / 30d low / all-time low
  cross_store          15   cheapest among matched stores
  drop_strength        10   vs 7-day reference price
  product_quality      10   rating
  seller_quality        5   (neutral until seller tracking lands)
  review_confidence     5   review volume
  offer_reliability     5   MRP sanity vs history (fake-MRP detection)
  demand_popularity     5   review-based demand proxy

Data Confidence (0-100) is computed SEPARATELY: history depth, history span,
cross-store coverage, review volume. A high score with low confidence is
never presented as a premium deal.

Deal types: EXCEPTIONAL / BEST / GOOD / FLASH / PRICE_WAR / NEW_LOW / WATCH / NONE
Verdicts:   BUY NOW / BUY / WATCH / IGNORE
"""
import logging

log = logging.getLogger("scorer")

# configurable thresholds (overridable via config["gate"])
GATE_DEFAULTS = {
    "exceptional_min": 90,   # + confidence >= best_confidence_min -> EXCEPTIONAL
    "best_min": 80,          # + confidence >= best_confidence_min -> BEST DEAL label
    "strong_min": 70,        # >= this: always send
    "selective_min": 60,     # 60-69: send selectively (see bot gate)
    "best_confidence_min": 75,
    "watch_confidence_min": 60,
}

DEAL_ICON = {
    "EXCEPTIONAL": "🔥🔥 EXCEPTIONAL DEAL",
    "BEST": "🔥 BEST DEAL",
    "GOOD": "🟢 GOOD DEAL",
    "FLASH": "⚡ FLASH DEAL",
    "PRICE_WAR": "🏆 PRICE WAR",
    "NEW_LOW": "🟡 NEW LOW",
    "WATCH": "👀 WATCHLIST",
    "NONE": "📊 TRACKED",
}

VERDICT_LABEL = {
    "BUY NOW": "🟢 VERDICT: BUY NOW",
    "BUY": "🟢 VERDICT: BUY",
    "WATCH": "🟡 VERDICT: WATCH",
    "IGNORE": "⚪ VERDICT: IGNORE",
}


def _r0(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def score_product(product: dict, stats: dict, group_info: dict, rules: dict,
                  gate: dict = None) -> dict:
    price = _r0(product.get("current_price"))
    mrp = _r0(product.get("mrp"))
    rating = product.get("rating")
    reviews = product.get("reviews_count") or 0
    gate = {**GATE_DEFAULTS, **(gate or {})}
    stats = stats or {}

    empty = {"score": 0.0, "confidence": 0, "deal_type": "NONE", "verdict": "IGNORE",
             "reasons": [], "warnings": [], "triggered": [], "genuine": False,
             "components": {}}
    if price <= 0:
        return empty

    amin = _r0(stats.get("amin")) or None
    amax = _r0(stats.get("amax")) or None
    cnt = int(stats.get("cnt") or 0)
    wmin = _r0(stats.get("wmin")) or None
    m30low = _r0(stats.get("m30low")) or None
    m90avg = _r0(stats.get("m90avg")) or None
    d7_ref = _r0(stats.get("d7_ref")) or None
    first_ts = stats.get("first_ts")

    comp = {}
    reasons = []          # WHY BUY (history + cross-store first!)
    warnings = []         # WHY NOT / risks
    triggered = []
    genuine = True

    # ---------- cross-store context ----------
    next_best = None
    next_best_platform = None
    is_best = False
    group_size = 0
    if group_info and group_info.get("size", 0) >= 2:
        group_size = group_info["size"]
        byp = group_info.get("by_platform", {})
        is_best = group_info.get("cheapest_platform") == product["platform"]
        others = sorted(((k, _r0(v.get("price")), v.get("url"))
                         for k, v in byp.items()
                         if k != product["platform"] and _r0(v.get("price")) > 0),
                        key=lambda x: x[1])
        if others:
            next_best_platform, next_best, _ = others[0]

    # ---------- 1. price_advantage (25) ----------
    if next_best and next_best > price:
        save_amt = next_best - price
        save_pct = save_amt / next_best * 100
        if save_pct >= 20:
            comp["price_advantage"] = 25
        elif save_pct >= 10:
            comp["price_advantage"] = 20
        elif save_pct >= 5:
            comp["price_advantage"] = 15
        elif save_pct >= 2:
            comp["price_advantage"] = 10
        else:
            comp["price_advantage"] = 6
        reasons.append(f"💰 ₹{int(save_amt):,} cheaper than next-best store "
                       f"({next_best_platform.title()} ₹{int(next_best):,})")
    elif next_best:
        comp["price_advantage"] = 0
        warnings.append(f"Cheaper on {next_best_platform.title()} (₹{int(next_best):,})")
    else:
        comp["price_advantage"] = 10  # neutral: no verified cross-store data

    # ---------- 2. historical_advantage (20) ----------
    atl = False
    below30 = False
    below90_pct = 0.0
    if cnt >= 3 and m90avg and m90avg > 0:
        below90_pct = (m90avg - price) / m90avg * 100
        if below90_pct >= 20:
            comp["historical_advantage"] = 20
        elif below90_pct >= 12:
            comp["historical_advantage"] = 16
        elif below90_pct >= 7:
            comp["historical_advantage"] = 12
        elif below90_pct >= 3:
            comp["historical_advantage"] = 8
        elif below90_pct >= 0:
            comp["historical_advantage"] = 4
        else:
            comp["historical_advantage"] = 0
        if amin and price <= amin:
            atl = True
            comp["historical_advantage"] = min(20, comp["historical_advantage"] + 4)
        if m30low and price <= m30low:
            below30 = True
            comp["historical_advantage"] = min(20, comp["historical_advantage"] + 2)
        comp["historical_advantage"] = min(20, comp["historical_advantage"])
    elif cnt >= 2:
        comp["historical_advantage"] = 8
        if amin and price <= amin:
            atl = True
            comp["historical_advantage"] = 12
    else:
        comp["historical_advantage"] = 6  # neutral: history still building

    # primary evidence lines (history FIRST, per design)
    if atl and cnt >= 3:
        triggered.append("all_time_low") if rules.get("all_time_low", True) else None
        reasons.append(f"🏆 New ALL-TIME LOW — lowest price in {cnt} observations")
    elif below90_pct >= 3 and m90avg:
        reasons.append(f"📉 {below90_pct:.1f}% below 90-day average (₹{int(m90avg):,})")
    if below30 and not atl and m30low and m30low < price * 1.01:
        reasons.append(f"📉 At/below 30-day low (₹{int(m30low):,})")

    # ---------- 3. cross_store (15) ----------
    if group_size >= 2:
        if is_best:
            comp["cross_store"] = 15
            if "cross_platform_win" not in triggered and rules.get("cross_platform_win", True):
                triggered.append("cross_platform_win")
        elif next_best and price <= next_best * 1.03:
            comp["cross_store"] = 10
        else:
            comp["cross_store"] = 0
    else:
        comp["cross_store"] = 7  # neutral

    # ---------- 4. drop_strength (10) ----------
    drop7_pct = 0.0
    if cnt >= 3 and d7_ref and d7_ref > price:
        drop7_pct = (d7_ref - price) / d7_ref * 100
        if drop7_pct >= 15:
            comp["drop_strength"] = 10
        elif drop7_pct >= 10:
            comp["drop_strength"] = 8
        elif drop7_pct >= 5:
            comp["drop_strength"] = 6
        elif drop7_pct >= 2:
            comp["drop_strength"] = 4
        else:
            comp["drop_strength"] = 0
        if drop7_pct >= 10:
            reasons.append(f"⚡ Sharp {drop7_pct:.1f}% drop vs last week (₹{int(d7_ref):,})")
    else:
        comp["drop_strength"] = 0
    if rules.get("drop_500_7d", True) and d7_ref and (d7_ref - price) >= 500 and cnt >= 3:
        triggered.append("drop_500_7d")
    if drop7_pct >= 15 and cnt >= 3:
        triggered.append("flash_drop")

    # ---------- 5. product_quality (10) ----------
    if rating is not None:
        r = float(rating)
        if r >= 4.3:
            comp["product_quality"] = 10
        elif r >= 4.0:
            comp["product_quality"] = 8
        elif r >= 3.7:
            comp["product_quality"] = 6
        elif r >= 3.3:
            comp["product_quality"] = 4
        else:
            comp["product_quality"] = 2
            warnings.append(f"Low rating {r}★")
            if r < 3.0:
                genuine = False
    else:
        comp["product_quality"] = 5  # neutral

    # ---------- 6. seller_quality (5) ----------
    comp["seller_quality"] = 3  # neutral until seller tracking exists

    # ---------- 7. review_confidence (5) ----------
    if reviews >= 5000:
        comp["review_confidence"] = 5
    elif reviews >= 1000:
        comp["review_confidence"] = 4
    elif reviews >= 300:
        comp["review_confidence"] = 3
    elif reviews >= 50:
        comp["review_confidence"] = 2
    else:
        comp["review_confidence"] = 1

    # ---------- 8. offer_reliability (5) - fake-MRP radar ----------
    fake_mrp = False
    if mrp > 0:
        if cnt >= 5 and m90avg and mrp >= m90avg * 3:
            fake_mrp = True
            comp["offer_reliability"] = 0
            warnings.append("MRP looks inflated vs real selling history")
        elif mrp >= price * 8:
            fake_mrp = True
            comp["offer_reliability"] = 0
            warnings.append("MRP looks inflated — treat % OFF with caution")
            genuine = False
        elif mrp >= price * 1.05:
            comp["offer_reliability"] = 5
        else:
            comp["offer_reliability"] = 2
    else:
        comp["offer_reliability"] = 2.5

    # ---------- 9. demand_popularity (5) ----------
    if reviews >= 100000:
        comp["demand"] = 5
    elif reviews >= 10000:
        comp["demand"] = 4
    elif reviews >= 2000:
        comp["demand"] = 3
    elif reviews >= 500:
        comp["demand"] = 2
    elif reviews >= 100:
        comp["demand"] = 1
    else:
        comp["demand"] = 0.5

    if rating and reviews and rating >= 4.0 and reviews >= 100:
        reasons.append(f"✅ Trusted: {rating}★ from {int(reviews):,} buyers")

    # ---------- MRP%-off is SECONDARY evidence only ----------
    disc = _r0(product.get("discount_pct"))
    if disc >= 50 and not fake_mrp:
        reasons.append(f"🏷️ {int(disc)}% off MRP (verified reference)")
    if disc >= 60 and not fake_mrp and rules.get("discount_60", True):
        triggered.append("discount_60")

    if price < 99:
        genuine = False
        warnings.append("Suspiciously low price — verify before buying")

    # ---------- Data Confidence (separate from score) ----------
    conf = 25.0
    if cnt < 3:
        pass
    elif cnt <= 5:
        conf += 15
    elif cnt <= 15:
        conf += 30
    elif cnt <= 40:
        conf += 40
    else:
        conf += 45
    if first_ts:
        span = _history_span_days(first_ts)
        if span >= 14:
            conf += 10
    if group_size >= 2:
        conf += 10
    if reviews >= 500:
        conf += 10
    confidence = int(max(0, min(100, conf)))

    # ---------- total ----------
    score = max(0.0, min(100.0, sum(comp.values())))

    # ---------- deal type + verdict ----------
    e_min, b_min = gate["exceptional_min"], gate["best_min"]
    c_min = gate["best_confidence_min"]
    if score >= e_min and confidence >= c_min:
        deal_type = "EXCEPTIONAL"
    elif score >= e_min:
        deal_type = "GOOD"  # premium score but weak data
        warnings.append("Premium score but limited data — confidence low")
    elif score >= b_min and confidence >= c_min:
        deal_type = "BEST"
    elif score >= b_min:
        deal_type = "GOOD"
        warnings.append("Strong price but limited data — confidence low")
    elif score >= gate["strong_min"]:
        deal_type = "GOOD"
    elif score >= gate["selective_min"]:
        if "flash_drop" in triggered:
            deal_type = "FLASH"
            triggered.append("flash_deal")
        elif is_best and next_best and next_best > 0 and (next_best - price) / next_best >= 0.08:
            deal_type = "PRICE_WAR"
            triggered.append("price_war")
        elif atl and confidence < gate["watch_confidence_min"]:
            deal_type = "NEW_LOW"
            triggered.append("new_low")
            warnings.append("Historical low but short price history")
        else:
            deal_type = "WATCH"
            triggered.append("watch_tier")
    else:
        if atl and confidence < gate["watch_confidence_min"]:
            deal_type = "NEW_LOW"   # interesting even at low score
        else:
            deal_type = "NONE"
        if score < 55:
            score = min(score, 55) if atl else score

    if deal_type == "EXCEPTIONAL":
        verdict = "BUY NOW"
    elif deal_type in ("BEST",):
        verdict = "BUY NOW"
    elif deal_type == "GOOD":
        verdict = "BUY"
    elif deal_type in ("FLASH", "PRICE_WAR", "NEW_LOW", "WATCH"):
        verdict = "WATCH"
    else:
        verdict = "IGNORE"

    if not triggered and score > 55 and deal_type not in ("NEW_LOW",):
        score = min(score, 55)  # no concrete rule fired -> keep out of premium bands

    return {
        "score": round(score, 1),
        "confidence": confidence,
        "deal_type": deal_type,
        "verdict": verdict,
        "reasons": reasons,
        "warnings": warnings,
        "triggered": triggered,
        "genuine": genuine,
        "components": {k: round(v, 1) for k, v in comp.items()},
    }


def _history_span_days(first_ts: str) -> int:
    try:
        from datetime import datetime
        return (datetime.now().astimezone() - datetime.fromisoformat(first_ts)).days
    except (ValueError, TypeError):
        return 0
