"""Telegram HTML formatting v2 - rich deal cards for photo captions.

Photo captions are capped at 1024 chars by Telegram, so the card is built
compact (~<= 1000 chars) and section-prioritised:
header -> price -> price intelligence -> cross-store -> why buy -> risks -> verdict.
"""
import html as html_mod


def esc(s) -> str:
    return html_mod.escape(str(s or ""))


PLATFORM_FLAGS = {
    "flipkart": "🟡 Flipkart",
    "amazon": "🟠 Amazon",
    "meesho": "🔴 Meesho",
    "croma": "🟣 Croma",
    "jiomart": "🔵 JioMart",
    "myntra": "🩷 Myntra",
}


def fmt_money(v) -> str:
    try:
        v = float(v)
        return f"₹{int(v):,}"
    except (TypeError, ValueError):
        return "₹?"


def _clip(text: str, limit: int = 990) -> str:
    """Hard-cap a caption at Telegram's limit, cutting at the last full line."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    nl = cut.rfind("\n")
    if nl > limit * 0.6:
        cut = cut[:nl]
    return cut.rstrip() + "\n…"


def fmt_deal_alert(product: dict, verdict: dict, group_info: dict, stats: dict) -> str:
    """Rich deal card. Returns HTML caption (<= ~1000 chars)."""
    from scorer import DEAL_ICON, VERDICT_LABEL
    price = product["current_price"]
    mrp = product.get("mrp") or 0
    stats = stats or {}
    lines = []

    # ---------- header + title ----------
    icon = DEAL_ICON.get(verdict.get("deal_type", "GOOD"), "🔥 DEAL")
    lines.append(f"<b>{icon} — {verdict['score']:.0f}/100</b>")
    lines.append(f"📦 <b>{esc(product['title'][:80])}</b>")

    # ---------- price block ----------
    pblock = f"💰 <b>{fmt_money(price)}</b>"
    if mrp > price:
        pblock += f"  <s>{fmt_money(mrp)}</s>"
        if product.get("discount_pct"):
            pblock += f"  ({int(product['discount_pct'])}% off)"
    lines.append(pblock)
    lines.append("")

    # ---------- price intelligence (PRIMARY evidence) ----------
    intel = []
    m90avg = stats.get("m90avg")
    if m90avg and m90avg > 0:
        below = (m90avg - price) / m90avg * 100
        arrow = "↓" if below >= 0 else "↑"
        intel.append(f"90D avg {fmt_money(m90avg)} → {arrow} {abs(below):.1f}%")
    if stats.get("m30low"):
        intel.append(f"30D low {fmt_money(stats['m30low'])}")
    if stats.get("wmin"):
        intel.append(f"7D low {fmt_money(stats['wmin'])}")
    if intel:
        lines.append("📉 <b>PRICE INTELLIGENCE</b>")
        lines.extend(f"• {i}" for i in intel)
    if any("ALL-TIME LOW" in r for r in verdict.get("reasons", [])):
        lines.append("🏆 <b>All-time low — lowest ever tracked</b>")
    if intel:
        lines.append("")

    # ---------- cross-store comparison ----------
    if group_info and group_info.get("size", 0) >= 2:
        lines.append("⚖️ <b>CROSS-STORE</b>")
        items = sorted(group_info["by_platform"].items(), key=lambda kv: kv[1]["price"])
        for plat, info in items[:3]:
            tag = PLATFORM_FLAGS.get(plat, plat)
            mark = " ⭐" if plat == group_info["cheapest_platform"] else ""
            lines.append(f"• {tag}: {fmt_money(info['price'])}{mark}")
        lines.append("")
    elif verdict.get("confidence", 0) < 100:
        lines.append("⚖️ Cross-store comparison: not available yet")
        lines.append("")

    # ---------- why buy ----------
    if verdict.get("reasons"):
        lines.append("💡 <b>WHY BUY</b>")
        for r in verdict["reasons"][:3]:
            lines.append(f"✓ {esc(r)}")
        lines.append("")

    # ---------- risks / why not ----------
    if verdict.get("warnings"):
        lines.append("⚠️ <b>WATCH OUT</b>")
        for w in verdict["warnings"][:2]:
            lines.append(f"• {esc(w)}")
        lines.append("")

    # ---------- verdict ----------
    lines.append(VERDICT_LABEL.get(verdict.get("verdict", "WATCH"),
                                   "🟡 VERDICT: WATCH"))
    lines.append(f"🎯 Score {verdict['score']:.0f}/100 · 📊 Confidence "
                 f"{verdict.get('confidence', 0)}%")
    return _clip("\n".join(lines))


def deal_buttons(product: dict, group_info: dict) -> dict:
    """Inline keyboard: BUY on this store + compare on next-best store."""
    from scorer import VERDICT_LABEL  # noqa: F401  (import guard symmetry)
    row = [{"text": f"🛒 BUY on {product['platform'].title()}",
            "url": product.get("url")}]
    if group_info and group_info.get("size", 0) >= 2:
        others = sorted(((k, v) for k, v in group_info["by_platform"].items()
                         if k != product["platform"] and v.get("price")),
                        key=lambda kv: kv[1]["price"])
        if others:
            k, v = others[0]
            row.append({"text": f"🔍 Compare on {k.title()}",
                        "url": v.get("url") or product.get("url")})
    return {"inline_keyboard": [row]}


def fmt_digest(deals: list, stats_summary: dict) -> str:
    lines = ["☀️ <b>DAILY BEST-DEAL BUY LIST</b> ☀️", ""]
    lines.append(f"🕐 {len(deals)} verified deals worth your money today")
    lines.append("")
    for i, d in enumerate(deals, 1):
        p = d["product"]
        plat = PLATFORM_FLAGS.get(p["platform"], p["platform"])
        price = fmt_money(p["current_price"])
        v = d["verdict"]
        lines.append(f"{i}. <b>{esc(p['title'][:58])}</b>")
        lines.append(f"   {plat} · <b>{price}</b> · {v['score']:.0f}/100 · "
                     f"conf {v.get('confidence', 0)}%")
        if v.get("reasons"):
            lines.append(f"   ↳ {esc(v['reasons'][0])}")
        lines.append(f"   🛒 {esc(p['url'])}")
        lines.append("")
    by = stats_summary.get("by_platform", {})
    lines.append("📊 <b>Tracker Status:</b>")
    lines.append(f"  • Products tracked: {stats_summary.get('products', 0):,}")
    lines.append(f"  • Price points recorded: {stats_summary.get('price_points', 0):,}")
    for plat, cnt in by.items():
        lines.append(f"  • {PLATFORM_FLAGS.get(plat, plat)}: {cnt:,} products")
    lines.append("")
    lines.append("🤖 Auto-generated by MCK Deal Engine · 24/7 monitoring")
    return "\n".join(lines)


def fmt_welcome(chat_type: str) -> str:
    lines = [
        "🤖 <b>MCK Deal Engine activated!</b> 🛍️",
        "",
        "I monitor <b>Flipkart · Amazon · Meesho</b> 24/7 with real price intelligence:",
        "  📉 7D / 30D / 90D price history on every product",
        "  ⚖️ Cross-store comparison for identical products",
        "  🚫 Fake-MRP & hype-discount detection",
        "  🎯 Deal Score + Data Confidence on every alert",
        "  📦 Product photo + BUY buttons on every card",
        "",
        "<b>Commands:</b>",
        "  /deals — top deals right now",
        "  /status — bot health & tracking stats",
        "  /mute 8 — pause alerts for 8 hours",
        "  /unmute — resume alerts",
        "  /help — full help",
        "",
    ]
    if chat_type == "private":
        lines.append("✅ This chat is now registered. Alerts start automatically!")
    else:
        lines.append("✅ This group is now registered. Alerts will be posted here!")
    lines.append("")
    lines.append("⏳ History builds over 24-72h — every alert gets sharper. "
                 "Only verified deals pass the gate; no spam, ever.")
    return "\n".join(lines)


def fmt_status(stats: dict, muted_until: str) -> str:
    lines = ["📊 <b>MCK Deal Engine Status</b>", ""]
    lines.append(f"🛍️ Products tracked: <b>{stats.get('products', 0):,}</b>")
    lines.append(f"📈 Price points recorded: <b>{stats.get('price_points', 0):,}</b>")
    lines.append(f"🔔 Alerts sent so far: <b>{stats.get('alerts_sent', 0):,}</b>")
    for plat, cnt in (stats.get("by_platform") or {}).items():
        lines.append(f"  • {PLATFORM_FLAGS.get(plat, plat)}: {cnt:,}")
    lines.append(f"🕒 Last check: {esc(stats.get('last_check') or 'starting...')}")
    if muted_until and muted_until != "0":
        lines.append(f"😴 Muted until: {esc(muted_until)}")
    else:
        lines.append("🟢 Status: RUNNING 24/7")
    return "\n".join(lines)


def fmt_help() -> str:
    return "\n".join([
        "🤖 <b>MCK Deal Engine — Help</b>",
        "",
        "I auto-check Flipkart, Amazon & Meesho 24/7, keep full price history,",
        "match identical products across stores, verify every deal against",
        "90-day price intelligence, and send only what passes the gate.",
        "",
        "<b>Alert tiers:</b>",
        "🔥🔥 90+ Exceptional · 🔥 80+ Best · 🟢 70+ Good",
        "🟡 60-69 New low / flash / watch · below 60 → silent",
        "",
        "<b>Commands:</b>",
        "/start — register this chat & activate alerts",
        "/deals — top 5 deals right now",
        "/status — tracking statistics",
        "/mute 8 — pause alerts for N hours (default 8)",
        "/unmute — resume alerts",
        "/help — this message",
    ])


def fmt_test() -> str:
    return "\n".join([
        "✅ <b>MCK Deal Engine v2 online!</b>",
        "",
        "New intelligence stack is live:",
        "  📉 History-first verification (7D/30D/90D)",
        "  ⚖️ Cross-store price wars detected",
        "  🚫 Fake-MRP radar",
        "  🎯 Deal Score + 📊 Data Confidence on every card",
        "  📦 Product photos + BUY buttons",
        "",
        "Alerts tiered: 🔥🔥 90+ · 🔥 80+ · 🟢 70+ · 🟡 60+ selective.",
    ])
