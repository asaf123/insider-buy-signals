"""
Weekly Insider Buying Signal Report
- Pulls this week's insider Form 4 purchases from Dataroma's real-time feed
- Groups transactions by symbol and ranks them by total open-market buy $
- Flags cluster buying (multiple insiders) and officer/director buying
  (the classic "insider conviction" signal) vs. passive 10%-holder stakes
- Looks up market cap per symbol (stockanalysis.com) and buckets results
  into Large/Mid/Small/Micro cap sections
- Picks a "Top Signal of the Week" and writes a static HTML report
- Appends a snapshot to history/ so trends can be tracked over time

Run: python scraper.py
"""

import json
import math
import re
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from bs4 import BeautifulSoup

BASE_URL = "https://www.dataroma.com/m/ins/ins.php"
QUERY = "t=w&am=0&sym=&po=1&so=&tp=&rid=&o=a&d=d"
MARKET_CAP_URL = "https://stockanalysis.com/stocks/{symbol}/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_PAGES = 10          # safety cap (100 rows/page)
PER_TIER_N = 8           # symbols shown per cap-size section

ROOT = Path(__file__).parent
HISTORY_DIR = ROOT / "history"
REPORT_FILE = ROOT / "report.html"
LATEST_JSON = ROOT / "latest.json"

OFFICER_DIRECTOR_KEYWORDS = [
    "director", "ceo", "cfo", "coo", "cto", "president", "chief",
    "chairman", "officer", "evp", "svp", "vp",
]

CAP_TIERS = ["Large Cap", "Mid Cap", "Small Cap", "Micro Cap", "Unclassified"]
CAP_TIER_SUBTITLES = {
    "Large Cap": "≥ $10B",
    "Mid Cap": "$2B – $10B",
    "Small Cap": "$300M – $2B",
    "Micro Cap": "< $300M",
    "Unclassified": "market cap unavailable",
}
MARKET_CAP_MULTIPLIERS = {"T": 1e12, "B": 1e9, "M": 1e6, "K": 1e3, "": 1}
MARKET_CAP_RE = re.compile(r'Market Cap</a>.*?>\s*([\d,.]+)\s*([TBMK]?)\s*<', re.S)


def fetch_page(page_num):
    url = f"{BASE_URL}?{QUERY}&L={page_num}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_rows(html):
    """Returns (parsed_rows, raw_row_count). raw_row_count includes rows
    skipped for having no ticker symbol (private placements in unlisted
    entities) — needed to detect whether a page was full (100 raw rows)
    so pagination doesn't stop early just because some rows got filtered."""
    soup = BeautifulSoup(html, "html.parser")
    grid = soup.find("table", id="grid")
    if not grid:
        return [], 0
    body = grid.find("tbody")
    if not body:
        return [], 0

    raw_trs = body.find_all("tr")
    rows = []
    for tr in raw_trs:
        cells = tr.find_all("td")
        if len(cells) < 10:
            continue
        try:
            symbol = cells[1].get_text(strip=True)
            security = cells[2].get_text(strip=True)
            reporting_name = cells[3].get_text(strip=True)
            relationship = cells[4].get_text(strip=True)
            trans_date = cells[5].get_text(strip=True)
            tran_code_cell = cells[6]
            code_suffix = tran_code_cell.find("span")
            code_suffix = code_suffix.get_text(strip=True) if code_suffix else ""
            shares = cells[7].get_text(strip=True)
            price = cells[8].get_text(strip=True)
            amount_raw = cells[9].get_text(strip=True)
            dir_ind = cells[10].get_text(strip=True) if len(cells) > 10 else ""

            amount = float(amount_raw.replace(",", "") or 0)
            try:
                shares_num = float(shares.replace(",", ""))
            except ValueError:
                shares_num = 0.0
        except (ValueError, IndexError):
            continue

        if not symbol or symbol.upper() == "NONE":
            continue

        rows.append({
            "symbol": symbol,
            "security": security,
            "reporting_name": reporting_name,
            "relationship": relationship,
            "trans_date": trans_date,
            "open_market": code_suffix == "",   # blank code = plain open-market "Purchase"
            "shares": shares,
            "shares_num": shares_num,
            "price": price,
            "amount": amount,
            "dir_ind": dir_ind,
        })
    return rows, len(raw_trs)


def fetch_all_rows():
    all_rows = []
    seen_first_key = None
    for page in range(1, MAX_PAGES + 1):
        html = fetch_page(page)
        rows, raw_row_count = parse_rows(html)
        if raw_row_count == 0:
            break
        # Dataroma repeats the last page if you go past the end — stop if we loop.
        first_key = (rows[0]["symbol"], rows[0]["reporting_name"], rows[0]["amount"]) if rows else None
        if first_key is not None and first_key == seen_first_key:
            break
        seen_first_key = first_key
        all_rows.extend(rows)
        if raw_row_count < 100:
            break
        time.sleep(1)  # be polite between page fetches
    return all_rows


def is_officer_or_director(relationship):
    rel = relationship.lower()
    return any(kw in rel for kw in OFFICER_DIRECTOR_KEYWORDS)


def fetch_market_cap(symbol):
    """Scrapes the market cap shown on stockanalysis.com's stock page.
    Returns dollars as a float, or None if the symbol isn't found there
    (OTC/foreign listings, trusts, BDCs, etc.)."""
    url = MARKET_CAP_URL.format(symbol=symbol.lower())
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    m = MARKET_CAP_RE.search(html)
    if not m:
        return None
    value, suffix = m.groups()
    try:
        value = float(value.replace(",", ""))
    except ValueError:
        return None
    return value * MARKET_CAP_MULTIPLIERS.get(suffix, 1)


def cap_tier(market_cap):
    if market_cap is None:
        return "Unclassified"
    if market_cap >= 10_000_000_000:
        return "Large Cap"
    if market_cap >= 2_000_000_000:
        return "Mid Cap"
    if market_cap >= 300_000_000:
        return "Small Cap"
    return "Micro Cap"


def attach_market_caps(ranked):
    for i, entry in enumerate(ranked, 1):
        cap = fetch_market_cap(entry["symbol"])
        entry["market_cap"] = cap
        entry["cap_tier"] = cap_tier(cap)
        print(f"  [{i:>3}/{len(ranked)}] ${entry['symbol']:<8} "
              f"{entry['cap_tier']:<13} {fmt_money(cap) if cap else 'n/a'}")
        time.sleep(0.4)  # be polite between requests
    return ranked


def group_and_rank(rows):
    groups = defaultdict(lambda: {
        "security": "",
        "rows": [],
        "insiders": set(),
        "officer_director_insiders": set(),
        "total_amount": 0.0,
        "open_market_amount": 0.0,
        "open_market_shares": 0.0,
    })

    for r in rows:
        g = groups[r["symbol"]]
        g["security"] = r["security"] or g["security"]
        g["rows"].append(r)
        g["insiders"].add(r["reporting_name"])
        g["total_amount"] += r["amount"]
        if r["open_market"]:
            g["open_market_amount"] += r["amount"]
            g["open_market_shares"] += r["shares_num"]
            if is_officer_or_director(r["relationship"]):
                g["officer_director_insiders"].add(r["reporting_name"])

    ranked = []
    for symbol, g in groups.items():
        avg_price = (g["open_market_amount"] / g["open_market_shares"]
                     if g["open_market_shares"] > 0 else None)
        ranked.append({
            "symbol": symbol,
            "security": g["security"],
            "total_amount": round(g["total_amount"]),
            "open_market_amount": round(g["open_market_amount"]),
            "avg_buy_price": round(avg_price, 2) if avg_price is not None else None,
            "num_transactions": len(g["rows"]),
            "num_insiders": len(g["insiders"]),
            "officer_director_count": len(g["officer_director_insiders"]),
            "cluster_buy": len(g["insiders"]) >= 2,
            "insider_grade": len(g["officer_director_insiders"]) >= 1,
            "top_rows": sorted(g["rows"], key=lambda r: -r["amount"])[:5],
        })

    # Primary ranking: total insider $ actually spent in the open market.
    ranked.sort(key=lambda x: x["open_market_amount"], reverse=True)
    return ranked


def pick_top_signal(ranked):
    """
    The 'top pick' is the highest open-market $ symbol that also has at least
    one officer/director buying (not just a passive 10%-holder placement) —
    that combination is the closest thing to a classic conviction signal.
    Falls back to the #1 $ symbol if nothing qualifies.
    """
    for entry in ranked:
        if entry["insider_grade"] and entry["open_market_amount"] > 0:
            return entry
    return ranked[0] if ranked else None


def save_history(ranked, top_signal):
    HISTORY_DIR.mkdir(exist_ok=True)
    week_str = datetime.now().strftime("%Y-%m-%d")
    snapshot = {
        "generated_at": datetime.now().isoformat(),
        "week": week_str,
        "top_signal": top_signal["symbol"] if top_signal else None,
        "rankings": ranked,
    }
    with open(HISTORY_DIR / f"{week_str}.json", "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    with open(LATEST_JSON, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2)
    return snapshot


def fmt_money(n):
    if n is None:
        return "n/a"
    if n >= 1_000_000_000_000:
        return f"${n/1_000_000_000_000:.2f}T"
    if n >= 1_000_000_000:
        return f"${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${n/1_000:.0f}K"
    return f"${n:,.0f}"


def generate_html(ranked, top_signal, total_buy_count, total_buy_amount):
    week_str = datetime.now().strftime("%B %d, %Y")

    def badge_html(entry):
        badges = ""
        if entry["insider_grade"]:
            badges += '<span class="badge grade">Officer/Director Buying</span>'
        if entry["cluster_buy"]:
            badges += f'<span class="badge cluster">Cluster · {entry["num_insiders"]} insiders</span>'
        if not entry["insider_grade"] and not entry["cluster_buy"]:
            badges += ('<span class="badge holder" title="Buyer owns 10%+ of the company '
                       'but holds no officer/director role — a large shareholder, not an '
                       'executive">10%+ Owner (not an exec)</span>')
        return badges

    def rows_html(entry):
        out = ""
        for r in entry["top_rows"]:
            tag = "" if r["open_market"] else '<span class="nonmkt">non-open-market</span>'
            out += f"""
            <div class="txn">
                <span class="txn-name">{r['reporting_name']}</span>
                <span class="txn-rel">{r['relationship']}</span>
                <span class="txn-amt">{fmt_money(r['amount'])}</span>
                {tag}
            </div>"""
        return out

    def card_html(rank, e, max_amount):
        bar_width = int((e["open_market_amount"] / max_amount) * 100) if e["open_market_amount"] else 2
        cap_label = fmt_money(e["market_cap"]) if e["market_cap"] else "n/a"
        avg_price_label = f"${e['avg_buy_price']:,.2f}/share" if e["avg_buy_price"] is not None else "n/a"
        return f"""
        <div class="card">
            <div class="card-header">
                <span class="rank">#{rank}</span>
                <span class="ticker">${e['symbol']}</span>
                <span class="security">{e['security']}</span>
                <span class="mcap">mkt cap {cap_label}</span>
            </div>
            <div class="bar-wrap"><div class="bar" style="width:{bar_width}%"></div></div>
            <div class="amount-row">
                <span class="amount">{fmt_money(e['open_market_amount'])} open-market buys</span>
                <span class="total">({fmt_money(e['total_amount'])} incl. non-open-market)</span>
            </div>
            <div class="avg-price-row">Avg. buy price: <strong>{avg_price_label}</strong></div>
            <div class="badges">{badge_html(e)}</div>
            <div class="txns">{rows_html(e)}</div>
        </div>"""

    tier_sections = ""
    for tier in CAP_TIERS:
        tier_entries = [e for e in ranked if e["cap_tier"] == tier][:PER_TIER_N]
        if not tier_entries:
            continue
        max_amount = max((e["open_market_amount"] for e in tier_entries), default=1) or 1
        cards = "".join(card_html(i, e, max_amount) for i, e in enumerate(tier_entries, 1))
        tier_sections += f"""
        <div class="tier-section">
            <div class="tier-title">{tier} <span>{CAP_TIER_SUBTITLES[tier]}</span></div>
            <div class="grid">{cards}</div>
        </div>"""

    if top_signal:
        top_reason = (
            "Selected as the highest open-market insider buying by an officer or "
            "director this week — a real cash purchase by someone running the "
            "company, not just a fund taking a passive stake."
            if top_signal["insider_grade"] else
            "No officer/director open-market buying qualified this week, so this is "
            "simply the largest total insider purchase amount."
        )
        cap_label = fmt_money(top_signal["market_cap"]) if top_signal["market_cap"] else "n/a"
        top_html = f"""
        <div class="top-pick">
            <div class="top-pick-label">Top Signal of the Week</div>
            <div class="top-pick-ticker">${top_signal['symbol']} <span>{top_signal['security']}</span></div>
            <div class="top-pick-tier">{top_signal['cap_tier']} · mkt cap {cap_label}</div>
            <div class="top-pick-amount">{fmt_money(top_signal['open_market_amount'])} in open-market insider buying</div>
            <div class="top-pick-avg">Avg. buy price: {f"${top_signal['avg_buy_price']:,.2f}/share" if top_signal['avg_buy_price'] is not None else "n/a"}</div>
            <div class="top-pick-reason">{top_reason}</div>
        </div>"""
    else:
        top_html = '<div class="top-pick"><div class="top-pick-reason">No qualifying purchases this week.</div></div>'

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Insider Buy Signals — {week_str}</title>
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#0f1117; color:#e8eaed; padding:30px 20px; }}
  .header {{ text-align:center; margin-bottom:28px; }}
  .header h1 {{ font-size:2.1rem; color:#fff; }}
  .header p {{ color:#8b949e; margin-top:8px; }}
  .stats {{ display:flex; justify-content:center; gap:24px; margin-bottom:32px; flex-wrap:wrap; }}
  .stat {{ background:#1c1f2e; border-radius:12px; padding:14px 28px; text-align:center; }}
  .stat-num {{ font-size:1.7rem; font-weight:800; color:#3fb950; }}
  .stat-label {{ font-size:0.78rem; color:#8b949e; margin-top:3px; }}
  .top-pick {{ max-width:700px; margin:0 auto 40px; background:linear-gradient(135deg,#132b1c,#161b22); border:1px solid #238636; border-radius:16px; padding:26px 30px; text-align:center; }}
  .top-pick-label {{ font-size:0.75rem; text-transform:uppercase; letter-spacing:1.5px; color:#3fb950; margin-bottom:10px; }}
  .top-pick-ticker {{ font-size:2rem; font-weight:800; color:#fff; }}
  .top-pick-ticker span {{ font-size:1rem; font-weight:400; color:#8b949e; display:block; margin-top:4px; }}
  .top-pick-tier {{ display:inline-block; color:#8b949e; font-size:0.8rem; background:#21262d; border-radius:20px; padding:3px 12px; margin-bottom:10px; }}
  .top-pick-amount {{ font-size:1.15rem; color:#3fb950; margin:2px 0 4px; font-weight:700; }}
  .top-pick-avg {{ color:#8b949e; font-size:0.85rem; margin-bottom:10px; }}
  .top-pick-reason {{ color:#c9d1d9; font-size:0.9rem; line-height:1.5; }}
  .tier-section {{ max-width:1200px; margin:0 auto 36px; }}
  .tier-title {{ font-size:1.15rem; font-weight:800; color:#fff; margin-bottom:14px; padding-left:4px; }}
  .tier-title span {{ font-size:0.8rem; font-weight:400; color:#8b949e; margin-left:8px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); gap:18px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:14px; padding:20px; }}
  .card-header {{ display:flex; align-items:baseline; gap:10px; margin-bottom:12px; flex-wrap:wrap; }}
  .rank {{ color:#8b949e; font-size:0.85rem; }}
  .ticker {{ font-size:1.3rem; font-weight:800; color:#58a6ff; }}
  .security {{ color:#8b949e; font-size:0.85rem; }}
  .mcap {{ color:#8b949e; font-size:0.75rem; margin-left:auto; }}
  .bar-wrap {{ background:#21262d; border-radius:6px; height:8px; margin-bottom:10px; }}
  .bar {{ background:linear-gradient(90deg,#238636,#3fb950); height:8px; border-radius:6px; }}
  .amount-row {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px; flex-wrap:wrap; gap:6px; }}
  .amount {{ font-weight:700; color:#3fb950; }}
  .total {{ font-size:0.78rem; color:#8b949e; }}
  .avg-price-row {{ font-size:0.8rem; color:#8b949e; margin-bottom:12px; }}
  .avg-price-row strong {{ color:#c9d1d9; }}
  .badges {{ display:flex; gap:6px; flex-wrap:wrap; margin-bottom:12px; }}
  .badge {{ font-size:0.72rem; border-radius:20px; padding:3px 10px; border:1px solid; }}
  .badge.grade {{ color:#3fb950; border-color:#23863655; background:#23863622; }}
  .badge.cluster {{ color:#58a6ff; border-color:#1f6feb55; background:#1f6feb22; }}
  .badge.holder {{ color:#d29922; border-color:#9e6a0355; background:#9e6a0322; }}
  .txns {{ border-top:1px solid #21262d; padding-top:10px; }}
  .txn {{ display:flex; justify-content:space-between; align-items:center; gap:8px; font-size:0.82rem; padding:4px 0; flex-wrap:wrap; }}
  .txn-name {{ color:#c9d1d9; font-weight:600; }}
  .txn-rel {{ color:#8b949e; flex:1; text-align:left; padding-left:8px; }}
  .txn-amt {{ color:#3fb950; font-weight:700; }}
  .nonmkt {{ font-size:0.68rem; color:#d29922; }}
  .footer {{ text-align:center; color:#8b949e; font-size:0.78rem; margin-top:50px; max-width:700px; margin-left:auto; margin-right:auto; line-height:1.6; }}
</style>
</head>
<body>
<div class="header">
  <h1>📊 Weekly Insider Buy Signals</h1>
  <p>{week_str} — ranked by open-market insider buying, sourced from Dataroma</p>
</div>
<div class="stats">
  <div class="stat"><div class="stat-num">{total_buy_count}</div><div class="stat-label">Buy Transactions</div></div>
  <div class="stat"><div class="stat-num">{fmt_money(total_buy_amount)}</div><div class="stat-label">Total $ Bought</div></div>
  <div class="stat"><div class="stat-num">{len(ranked)}</div><div class="stat-label">Symbols With Buys</div></div>
</div>
{top_html}
{tier_sections}
<div class="footer">
  Data source: dataroma.com real-time Form 4 insider filings, purchases only, trailing 7 days.
  Market caps from stockanalysis.com. "Open-market" excludes grants/awards and other
  non-cash acquisition codes. "Officer/Director Buying" flags at least one insider with
  an executive or board relationship (not just a 10%+ holder). This is a data summary of
  public SEC filings, not investment advice.<br>
  Generated {datetime.now().strftime("%Y-%m-%d %H:%M")}
</div>
</body>
</html>"""

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html)


def run():
    print("Fetching this week's insider purchases from Dataroma...")
    rows = fetch_all_rows()
    print(f"  Parsed {len(rows)} purchase transactions.")

    if not rows:
        print("No data returned — aborting.")
        return

    ranked = group_and_rank(rows)
    top_signal = pick_top_signal(ranked)
    total_buy_amount = sum(r["amount"] for r in rows)

    print("\nLooking up market caps...")
    attach_market_caps(ranked)  # mutates entries in place, incl. top_signal (same dict refs)

    save_history(ranked, top_signal)
    generate_html(ranked, top_signal, len(rows), total_buy_amount)

    print(f"\nTop signal: ${top_signal['symbol']}  ({fmt_money(top_signal['open_market_amount'])})" if top_signal else "No top signal.")
    print(f"Report saved -> {REPORT_FILE}")
    try:
        import os
        os.startfile(REPORT_FILE)
    except Exception:
        pass


if __name__ == "__main__":
    run()
