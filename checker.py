import json
import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

with open("products.json") as f:
    config = json.load(f)

SETS               = config["sets"]
SITES              = config["sites"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE         = "last_state.json"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1",
]

def get_headers():
    return {
        "User-Agent":                random.choice(USER_AGENTS),
        "Accept-Language":           "en-IE,en-GB;q=0.9,en;q=0.8",
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Encoding":           "gzip, deflate, br",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control":             "max-age=0",
    }

def fetch(url, timeout=20, retries=3):
    for attempt in range(retries):
        try:
            time.sleep(random.uniform(1, 3))
            r = requests.get(url, headers=get_headers(), timeout=timeout)
            if r.status_code == 429:
                wait = (attempt + 1) * 10
                print(f"      ⏳ Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            return r
        except Exception as e:
            if attempt == retries - 1:
                print(f"      ⚠ Fetch failed after {retries} attempts: {e}")
                return None
            time.sleep((attempt + 1) * 5)
    return None

def is_blocked(r):
    if not r or r.status_code in (403, 503):
        return True
    return any(p in r.text.lower() for p in [
        "access denied", "please verify you are a human",
        "too many requests", "are you a robot", "ddos-guard",
    ])

def keywords_from_set(set_name):
    stopwords = {"the","a","an","and","or","for","of","in","to","&",
                 "pokemon","trading","card","game","tcg","pocket"}
    return [w.lower() for w in re.split(r"\W+", set_name)
            if w and w.lower() not in stopwords]

def text_matches(text, set_name):
    return all(w in text.lower() for w in keywords_from_set(set_name))

def absolute_url(href, domain):
    if not href:
        return None
    href = href.split("?")[0]
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://www.{domain}{href}"
    return None

OUT_SIGNALS = ["out of stock","sold out","unavailable",
               "notify me when available","currently unavailable",
               "temporarily out of stock"]
IN_SIGNALS  = ["add to cart","add to basket","add to trolley","add to bag","buy now"]

CARD_SELECTORS = [
    ".product-tile",".product-card",".product-item",
    "[class*='product-tile']","[class*='product-card']","[class*='product-item']",
    "article[class*='product']","li[class*='item']","li[class*='product']",
    ".grid-product",".collection-product",".item",
]

# ══════════════════════════════════════════════════════════════════════════════
# SEARCH + PRODUCT PAGE CHECKS
# ══════════════════════════════════════════════════════════════════════════════

def scrape_search_results(site, set_name):
    template = site.get("search_url", "")
    if not template:
        return []

    search_url = template.format(query=requests.utils.quote(set_name))
    r = fetch(search_url)

    if not r or is_blocked(r) or not r.ok:
        print(f"      ⚠ [{site['name']}] Search failed ({r.status_code if r else 'no response'})")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    domain = site["domain"]
    for tag in soup.select("nav,header,footer,script,style,noscript"):
        tag.decompose()

    products  = []
    seen_urls = set()

    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if not cards:
            continue
        for card in cards:
            card_text = card.get_text(" ", strip=True)
            if not text_matches(card_text, set_name):
                continue
            link  = card.find("a", href=True)
            url   = absolute_url(link["href"] if link else None, domain)
            if not url or url in seen_urls:
                continue
            title_tag = (card.find(["h2","h3","h4"]) or
                         card.find(class_=re.compile(r"title|name", re.I)) or
                         card.find("a"))
            title = title_tag.get_text(strip=True) if title_tag else card_text[:80]
            price_tag = card.find(class_=re.compile(r"price", re.I))
            price = ""
            if price_tag:
                m = re.search(r"€[\d,]+\.?\d*", price_tag.get_text())
                price = m.group(0) if m else ""
            seen_urls.add(url)
            products.append({"title": title, "url": url, "price": price})
        if products:
            break

    if not products:
        for a in soup.find_all("a", href=True):
            url = absolute_url(a["href"], domain)
            if not url or url in seen_urls:
                continue
            link_text = a.get_text(strip=True)
            if text_matches(link_text, set_name) and len(link_text) > 15:
                seen_urls.add(url)
                products.append({"title": link_text, "url": url, "price": ""})

    return products


def get_page_stock_status(site, url):
    r = fetch(url)

    if not r:
        return "unknown", ""
    if r.status_code in (404, 410):
        return "out_of_stock", ""
    if is_blocked(r) or not r.ok:
        return "unknown", ""

    soup   = BeautifulSoup(r.text, "html.parser")
    domain = site["domain"]
    text   = soup.get_text().lower()

    price = ""
    price_tag = soup.find(class_=re.compile(r"(^|\b)price(\b|$)", re.I))
    if price_tag:
        m = re.search(r"€[\d,]+\.?\d*", price_tag.get_text())
        price = m.group(0) if m else ""

    # Shopify sites
    if any(d in domain for d in ["eirehobbies","discarded","toyful"]):
        sold = (soup.find(class_=re.compile(r"sold.?out", re.I)) or
                soup.find("button", string=re.compile(r"sold out", re.I)) or
                soup.find(string=lambda t: t and "sold out" in t.lower()))
        atc  = soup.find("button", string=re.compile(r"add to cart", re.I))
        if sold:
            return "out_of_stock", price
        if atc and not atc.get("disabled"):
            return "in_stock", price
        return "unknown", price

    for sig in OUT_SIGNALS:
        if sig in text:
            return "out_of_stock", price
    for sig in IN_SIGNALS:
        if sig in text:
            return "in_stock", price
    return "unknown", price


def check_site_for_set(site, set_name):
    """Check one site for one set. Returns (site_name, set_name, products)."""
    print(f"  🔎 [{site['name']}] Searching '{set_name}'...")
    found = scrape_search_results(site, set_name)

    if not found:
        print(f"  ❌ [{site['name']}] Nothing found for '{set_name}'")
        return site["name"], set_name, []

    print(f"  📋 [{site['name']}] Found {len(found)} product(s) for '{set_name}', checking stock...")
    products = []
    for p in found:
        status, price = get_page_stock_status(site, p["url"])
        if not p["price"] and price:
            p["price"] = price
        p["status"] = status
        icon = {"in_stock":"✅","out_of_stock":"❌"}.get(status, "❓")
        print(f"    {icon} [{site['name']}] {p['title'][:55]} — {p.get('price') or 'no price'}")
        products.append(p)

    return site["name"], set_name, products


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

STATUS_EMOJI = {"in_stock":"✅","out_of_stock":"❌","unknown":"❓"}

def product_line(p):
    emoji = STATUS_EMOJI.get(p["status"], "❓")
    price = f" · {p['price']}" if p.get("price") else ""
    title = p["title"][:50] + ("…" if len(p["title"]) > 50 else "")
    return f"    {emoji} [{title}]({p['url']}){price}"

def send_telegram(text):
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id":                  TELEGRAM_CHAT_ID,
            "text":                     text,
            "parse_mode":               "Markdown",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    if r.ok:
        print("  ✈ Telegram sent")
    else:
        print(f"  ⚠ Telegram error: {r.status_code} {r.text[:200]}")

def send_alert(new_in_stock):
    lines = ["🚨 *Pokémon Restock Alert!*\n"]
    for set_name, sites in new_in_stock.items():
        lines.append(f"*{set_name}*")
        for site_name, products in sites.items():
            lines.append(f"  📦 {site_name}")
            for p in products:
                lines.append(product_line(p))
        lines.append("")
    lines.append(f"_Checked {datetime.now(timezone.utc).strftime('%H:%M UTC')}_")
    send_telegram("\n".join(lines))

def send_digest(all_results):
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    for set_name, sites in all_results.items():
        lines = [f"📊 *{set_name}* — {ts}\n"]
        has_content = False
        for site_name, products in sites.items():
            if not products:
                lines.append(f"  _{site_name}: nothing found_")
                continue
            has_content = True
            lines.append(f"  📦 *{site_name}*")
            for p in sorted(products, key=lambda x: 0 if x["status"] == "in_stock" else 1):
                lines.append(product_line(p))
            lines.append("")

        # Always send, even if all sites returned nothing
            text = "\n".join(lines)
            if len(text) > 3800:
                mid = len(lines) // 2
                send_telegram("\n".join(lines[:mid]))
                send_telegram("\n".join(lines[mid:]))
            else:
                send_telegram(text)


# ══════════════════════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════════════════════

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN — runs all site+set combinations in parallel
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n🔍 Pokémon Restock Checker — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Sets:  {', '.join(SETS)}")
    print(f"   Sites: {', '.join(s['name'] for s in SITES)}")
    print(f"   Running {len(SETS) * len(SITES)} checks in parallel...\n")

    state        = load_state()
    all_results  = {s: {site["name"]: [] for site in SITES} for s in SETS}
    new_in_stock = {}

    # Build all (site, set_name) tasks
    tasks = [(site, set_name) for set_name in SETS for site in SITES]

    # Run all in parallel — max 5 workers (one per site) to avoid hammering
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(check_site_for_set, site, set_name): (site, set_name)
            for site, set_name in tasks
        }
        for future in as_completed(futures):
            try:
                site_name, set_name, products = future.result()
                all_results[set_name][site_name] = products
            except Exception as e:
                site, set_name = futures[future]
                print(f"  ⚠ Error checking {site['name']} for {set_name}: {e}")

    # Diff against state and find newly in-stock items
    for set_name in SETS:
        for site in SITES:
            products = all_results[set_name][site["name"]]
            key           = f"{site['domain']}|{set_name}"
            prev_in_stock = set(state.get(key, {}).get("in_stock_urls", []))
            now_in_stock  = {p["url"] for p in products if p["status"] == "in_stock"}
            newly         = [p for p in products
                             if p["status"] == "in_stock"
                             and p["url"] not in prev_in_stock]
            if newly:
                new_in_stock.setdefault(set_name, {})[site["name"]] = newly

            state[key] = {
                "in_stock_urls": list(now_in_stock),
                "last_checked":  datetime.now(timezone.utc).isoformat(),
            }

    save_state(state)

    if new_in_stock:
        print("\n🚨 New stock found! Sending alert...")
        send_alert(new_in_stock)

    print("\n📊 Sending digest...")
    send_digest(all_results)

if __name__ == "__main__":
    main()