import json
import os
import re
import requests
from bs4 import BeautifulSoup
from datetime import datetime

# ── Load config ────────────────────────────────────────────────────────────────
with open("products.json") as f:
    config = json.load(f)

SETS               = config["sets"]
SITES              = config["sites"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID   = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE         = "last_state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
}

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def fetch(url, timeout=15):
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        return r
    except Exception as e:
        print(f"      ⚠ Fetch error: {e}")
        return None

def keywords_from_set(set_name):
    stopwords = {"the", "a", "an", "and", "or", "for", "of", "in", "to", "&", "pokemon", "trading", "card", "game", "tcg"}
    return [w.lower() for w in re.split(r"\W+", set_name) if w and w.lower() not in stopwords]

def text_matches(text, set_name):
    return all(w in text.lower() for w in keywords_from_set(set_name))

def absolute_url(href, domain):
    if not href:
        return None
    if href.startswith("http"):
        return href
    return f"https://www.{domain}{href}" if href.startswith("/") else None


# ══════════════════════════════════════════════════════════════════════════════
# STEP 1 — SCRAPE SEARCH PAGE → collect product URLs
# ══════════════════════════════════════════════════════════════════════════════

# Smyths-specific: product cards are <li> with data-product or class product-card
CARD_SELECTORS = [
    ".product-tile", ".product-card", ".product-item",
    "[class*='product-tile']", "[class*='product-card']", "[class*='product-item']",
    "article[class*='product']",
    "li[class*='item']", "li[class*='product']",
    ".grid-product", ".collection-product",
]

def scrape_search_results(site, set_name):
    """
    Hit the site's search page and return a list of product dicts:
    [{title, url, price}]  — only products matching the set name.
    """
    template = site.get("search_url")
    if not template:
        return []

    search_url = template.format(query=requests.utils.quote(set_name))
    r = fetch(search_url)
    if not r or not r.ok:
        print(f"      ⚠ Search page failed ({r.status_code if r else 'no response'})")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    domain = site["domain"]
    products = []
    seen_urls = set()

    # Try structured card selectors first
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if not cards:
            continue
        for card in cards:
            card_text = card.get_text(" ", strip=True)
            if not text_matches(card_text, set_name):
                continue
            # Title
            title_tag = (card.find(["h2","h3","h4"]) or
                         card.find(class_=re.compile(r"title|name", re.I)) or
                         card.find("a"))
            title = title_tag.get_text(strip=True) if title_tag else card_text[:80]
            # URL
            link = card.find("a", href=True)
            url = absolute_url(link["href"] if link else None, domain)
            if not url or url in seen_urls:
                continue
            # Price
            price_tag = card.find(class_=re.compile(r"price", re.I))
            price = price_tag.get_text(strip=True) if price_tag else ""
            seen_urls.add(url)
            products.append({"title": title, "url": url, "price": price})
        if products:
            break  # stop at first selector that yields results

    # Fallback: grab all <a> tags whose text matches the set name
    if not products:
        for a in soup.find_all("a", href=True):
            href = absolute_url(a["href"], domain)
            if not href or href in seen_urls:
                continue
            link_text = a.get_text(strip=True)
            if text_matches(link_text, set_name) and len(link_text) > 15:
                seen_urls.add(href)
                products.append({"title": link_text, "url": href, "price": ""})

    return products


# ══════════════════════════════════════════════════════════════════════════════
# STEP 2 — CHECK INDIVIDUAL PRODUCT PAGE → get stock status + price
# ══════════════════════════════════════════════════════════════════════════════

OUT_SIGNALS = ["out of stock", "sold out", "unavailable",
               "notify me when available", "currently unavailable",
               "temporarily out of stock"]
IN_SIGNALS  = ["add to cart", "add to basket", "add to trolley",
               "add to bag", "buy now", "in stock", "add to wishlist"]

def get_stock_status(site, product_url):
    """
    Fetch a product page and return:
      status: "in_stock" | "out_of_stock" | "unknown"
      price:  string or ""
    """
    r = fetch(product_url)
    if not r:
        return "unknown", ""
    if r.status_code in (404, 410):
        return "out_of_stock", ""
    if not r.ok:
        return "unknown", ""

    soup = BeautifulSoup(r.text, "html.parser")
    domain = site["domain"]

    # ── Site-specific logic ──────────────────────────────────────────────────
    if "smythstoys" in domain:
        # Smyths: "addToCartBtn" button is disabled when OOS
        btn = soup.find("button", {"id": "addToCartBtn"})
        if btn:
            status = "out_of_stock" if btn.get("disabled") else "in_stock"
        elif soup.find(string=lambda t: t and "out of stock" in t.lower()):
            status = "out_of_stock"
        else:
            status = "unknown"

    elif "argos" in domain:
        if soup.find("button", string=lambda t: t and "add to trolley" in t.lower()):
            status = "in_stock"
        elif soup.find(string=lambda t: t and ("out of stock" in t.lower() or "unavailable" in t.lower())):
            status = "out_of_stock"
        else:
            status = "unknown"

    elif "discarded" in domain:
        # Discarded sometimes shows OOS
        sold_out = soup.find(class_=re.compile(r"sold.?out", re.I)) or \
                   soup.find(string=lambda t: t and "sold out" in t.lower())
        atc = soup.find("button", string=lambda t: t and "add to cart" in t.lower())
        if sold_out:
            status = "out_of_stock"
        elif atc and not atc.get("disabled"):
            status = "in_stock"
        else:
            status = "unknown"

    else:
        # Generic fallback
        text = soup.get_text().lower()
        status = "unknown"
        for sig in OUT_SIGNALS:
            if sig in text:
                status = "out_of_stock"
                break
        if status == "unknown":
            for sig in IN_SIGNALS:
                if sig in text:
                    status = "in_stock"
                    break

    # ── Extract price ────────────────────────────────────────────────────────
    price = ""
    price_tag = (soup.find(class_=re.compile(r"(^|\s)price(\s|$)", re.I)) or
                 soup.find("span", string=re.compile(r"€\d")))
    if price_tag:
        price = price_tag.get_text(strip=True)
        # Clean up: keep only the first price-like token
        match = re.search(r"€[\d,]+\.?\d*", price)
        price = match.group(0) if match else price[:20]

    return status, price


# ══════════════════════════════════════════════════════════════════════════════
# STEP 3 — COMBINE: search → per-product status check
# ══════════════════════════════════════════════════════════════════════════════

def check_site_for_set(site, set_name):
    """
    Returns list of all products found for this set on this site,
    each with their individual stock status:
    [{title, url, price, status}]
    """
    method = site.get("method", "search")
    products = []

    # --- Smyths: use pre-configured URLs (user already found them)
    if method == "page" and site.get("urls"):
        for url in site["urls"]:
            # We already know these are Chaos Rising etc — skip keyword filter
            status, price = get_stock_status(site, url)
            # Get title from page
            r = fetch(url)
            title = url
            if r and r.ok:
                s = BeautifulSoup(r.text, "html.parser")
                h = s.find("h1")
                if h:
                    title = h.get_text(strip=True)
            products.append({"title": title, "url": url,
                             "price": price, "status": status})
        return products

    # --- Search-based sites: discover products first, then check each page
    print(f"    🔎 Searching {site['name']} for '{set_name}'...")
    found = scrape_search_results(site, set_name)
    if not found:
        print(f"       No products found in search results")
        return []

    print(f"       Found {len(found)} product(s), checking each...")
    for p in found:
        status, price = get_stock_status(site, p["url"])
        # Use price from product page if search didn't find one
        if not p["price"] and price:
            p["price"] = price
        p["status"] = status
        icon = "✅" if status == "in_stock" else "❌" if status == "out_of_stock" else "❓"
        print(f"       {icon} {p['title'][:60]} — {p['price'] or 'no price'}")
        products.append(p)

    return products


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

STATUS_EMOJI = {"in_stock": "✅", "out_of_stock": "❌", "unknown": "❓"}

def send_telegram(messages):
    """messages: list of text blocks to send (split if too long)."""
    for text in messages:
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
            print(f"  ✈ Telegram message sent")
        else:
            print(f"  ⚠ Telegram error: {r.text}")

def build_telegram_messages(all_results, new_in_stock):
    """
    all_results: {set_name: {site_name: [products]}}
    new_in_stock: same structure but only newly in-stock items
    Returns list of message strings (Telegram has 4096 char limit).
    """
    messages = []

    # --- Alert message for newly in-stock items ---
    if new_in_stock:
        lines = ["🚨 *New Pokémon Stock Alert!*\n"]
        for set_name, sites in new_in_stock.items():
            lines.append(f"*{set_name}*")
            for site_name, products in sites.items():
                lines.append(f"  📦 {site_name}")
                for p in products:
                    price = f" · {p['price']}" if p.get("price") else ""
                    title = p["title"][:55] + ("…" if len(p["title"]) > 55 else "")
                    lines.append(f"    ✅ [{title}]({p['url']}){price}")
            lines.append("")
        lines.append(f"_Checked at {datetime.utcnow().strftime('%H:%M UTC')}_")
        messages.append("\n".join(lines))

    # --- Full status digest (sent every run so you always know the state) ---
    lines = [f"📊 *Full Stock Digest* — {datetime.utcnow().strftime('%H:%M UTC')}\n"]
    for set_name, sites in all_results.items():
        lines.append(f"*{set_name}*")
        for site_name, products in sites.items():
            if not products:
                lines.append(f"  {site_name}: nothing found")
                continue
            lines.append(f"  📦 {site_name}")
            for p in products:
                emoji = STATUS_EMOJI.get(p["status"], "❓")
                price = f" · {p['price']}" if p.get("price") else ""
                title = p["title"][:50] + ("…" if len(p["title"]) > 50 else "")
                lines.append(f"    {emoji} [{title}]({p['url']}){price}")
        lines.append("")

    # Split into chunks under 4000 chars
    chunk, chunk_len = [], 0
    for line in lines:
        if chunk_len + len(line) > 3800:
            messages.append("\n".join(chunk))
            chunk, chunk_len = [], 0
        chunk.append(line)
        chunk_len += len(line) + 1
    if chunk:
        messages.append("\n".join(chunk))

    return messages


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

def state_key(site, set_name):
    return f"{site['domain']}|{set_name}"


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n🔍 Pokémon Restock Checker — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Sets:  {', '.join(SETS)}")
    print(f"   Sites: {', '.join(s['name'] for s in SITES)}\n")

    state       = load_state()
    all_results = {}   # {set_name: {site_name: [products]}}
    new_in_stock = {}  # same, but only newly in-stock products

    for set_name in SETS:
        print(f"══ {set_name} ══")
        all_results[set_name] = {}
        for site in SITES:
            products = check_site_for_set(site, set_name)
            all_results[set_name][site["name"]] = products

            # Diff against previous state
            key = state_key(site, set_name)
            prev_in_stock_urls = set(state.get(key, {}).get("in_stock_urls", []))
            now_in_stock_urls  = {p["url"] for p in products if p["status"] == "in_stock"}

            newly = [p for p in products
                     if p["status"] == "in_stock" and p["url"] not in prev_in_stock_urls]

            if newly:
                new_in_stock.setdefault(set_name, {})[site["name"]] = newly

            # Save state
            state[key] = {
                "in_stock_urls":  list(now_in_stock_urls),
                "all_urls":       [p["url"] for p in products],
                "last_checked":   datetime.utcnow().isoformat(),
            }
        print()

    save_state(state)

    messages = build_telegram_messages(all_results, new_in_stock)
    if messages:
        print("📨 Sending Telegram update...")
        send_telegram(messages)

if __name__ == "__main__":
    main()