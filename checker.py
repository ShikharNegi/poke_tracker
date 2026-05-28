import json
import os
import re
import time
import random
import requests
from bs4 import BeautifulSoup
from datetime import datetime

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

def is_bot_blocked(r):
    if not r or r.status_code in (403, 429, 503):
        return True
    return any(p in r.text.lower() for p in [
        "pardon our interruption", "access denied",
        "please verify you are a human", "too many requests",
        "ddos-guard", "are you a robot",
    ])

def polite_delay():
    time.sleep(random.uniform(1.5, 3.5))

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
# SMYTHS SESSION — visits homepage first to get cookies, then searches
# ══════════════════════════════════════════════════════════════════════════════

def make_smyths_session():
    """Create a requests session that looks like a real browser to Smyths."""
    session = requests.Session()
    session.headers.update(get_headers())
    try:
        print("      🍪 Getting Smyths cookies...")
        session.get("https://www.smythstoys.com/ie/en-ie/",
                    timeout=15, allow_redirects=True)
        polite_delay()
        session.headers["Referer"] = "https://www.smythstoys.com/ie/en-ie/"
    except Exception as e:
        print(f"      ⚠ Could not load Smyths homepage: {e}")
    return session

def smyths_search(session, set_name):
    """Search Smyths and return product cards matching the set name."""
    search_url = f"https://www.smythstoys.com/ie/en-ie/search?text={requests.utils.quote(set_name)}"
    try:
        r = session.get(search_url, timeout=20)
    except Exception as e:
        print(f"      ⚠ Smyths search error: {e}")
        return []

    if is_bot_blocked(r):
        print("      🚫 Smyths is blocking — will retry next run")
        return []

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup.select("nav,header,footer,script,style,noscript"):
        tag.decompose()

    products  = []
    seen_urls = set()

    # Smyths uses <li class="product ..."> cards
    for sel in CARD_SELECTORS:
        cards = soup.select(sel)
        if not cards:
            continue
        for card in cards:
            card_text = card.get_text(" ", strip=True)
            if not text_matches(card_text, set_name):
                continue
            link = card.find("a", href=True)
            url  = absolute_url(link["href"] if link else None, "smythstoys.com")
            if not url or url in seen_urls:
                continue
            title_tag = card.find(["h2","h3","h4"]) or card.find("a")
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

    # Fallback to matching <a> tags
    if not products:
        for a in soup.find_all("a", href=True):
            url = absolute_url(a["href"], "smythstoys.com")
            if not url or url in seen_urls:
                continue
            link_text = a.get_text(strip=True)
            if text_matches(link_text, set_name) and len(link_text) > 15:
                seen_urls.add(url)
                products.append({"title": link_text, "url": url, "price": ""})

    return products

def smyths_check_product(session, product):
    """Fetch individual Smyths product page and get stock status."""
    polite_delay()
    try:
        r = session.get(product["url"], timeout=20)
    except Exception as e:
        print(f"      ⚠ Error: {e}")
        product["status"] = "unknown"
        return product

    if is_bot_blocked(r):
        product["status"] = "blocked"
        return product

    soup = BeautifulSoup(r.text, "html.parser")

    # Update price from product page if we didn't get it from search
    if not product.get("price"):
        price_tag = soup.find("span", class_=re.compile(r"price", re.I))
        if price_tag:
            m = re.search(r"€[\d,]+\.?\d*", price_tag.get_text())
            product["price"] = m.group(0) if m else ""

    btn = soup.find("button", {"id": "addToCartBtn"})
    if btn:
        product["status"] = "out_of_stock" if btn.get("disabled") else "in_stock"
    elif soup.find(string=lambda t: t and "out of stock" in t.lower()):
        product["status"] = "out_of_stock"
    else:
        product["status"] = "unknown"

    return product

def check_smyths_for_set(set_name):
    session  = make_smyths_session()
    print(f"    🔎 Searching Smyths for '{set_name}'...")
    products = smyths_search(session, set_name)

    if not products:
        print("       No products found")
        return []

    print(f"       Found {len(products)} product(s), checking each...")
    results = []
    for p in products:
        p = smyths_check_product(session, p)
        icon = {"in_stock":"✅","out_of_stock":"❌","blocked":"🚫"}.get(p["status"],"❓")
        print(f"      {icon} {p['title'][:60]} — {p.get('price') or 'no price'}")
        results.append(p)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# GENERIC SEARCH-BASED SITES
# ══════════════════════════════════════════════════════════════════════════════

def scrape_search_results(site, set_name):
    template = site.get("search_url","")
    if not template:
        return []

    search_url = template.format(query=requests.utils.quote(set_name))
    polite_delay()
    try:
        r = requests.get(search_url, headers=get_headers(), timeout=20)
    except Exception as e:
        print(f"      ⚠ Fetch error: {e}")
        return []

    if is_bot_blocked(r) or not r.ok:
        print(f"      ⚠ Search blocked or failed ({r.status_code if r else 'no response'})")
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
                         card.find(class_=re.compile(r"title|name",re.I)) or
                         card.find("a"))
            title = title_tag.get_text(strip=True) if title_tag else card_text[:80]
            price_tag = card.find(class_=re.compile(r"price",re.I))
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
    polite_delay()
    try:
        r = requests.get(url, headers=get_headers(), timeout=20)
    except Exception as e:
        return "unknown", ""

    if not r or r.status_code in (404, 410):
        return "out_of_stock", ""
    if is_bot_blocked(r) or not r.ok:
        return "unknown", ""

    soup   = BeautifulSoup(r.text, "html.parser")
    domain = site["domain"]
    text   = soup.get_text().lower()

    price = ""
    price_tag = soup.find(class_=re.compile(r"(^|\b)price(\b|$)",re.I))
    if price_tag:
        m = re.search(r"€[\d,]+\.?\d*", price_tag.get_text())
        price = m.group(0) if m else ""

    # Shopify sites (Eire Hobbies, Discarded, Toyful etc.)
    if any(d in domain for d in ["eirehobbies","discarded","toyful"]):
        sold = (soup.find(class_=re.compile(r"sold.?out",re.I)) or
                soup.find("button", string=re.compile(r"sold out",re.I)) or
                soup.find(string=lambda t: t and "sold out" in t.lower()))
        atc  = soup.find("button", string=re.compile(r"add to cart",re.I))
        if sold:
            return "out_of_stock", price
        if atc and not atc.get("disabled"):
            return "in_stock", price
        return "unknown", price

    # Generic
    for sig in OUT_SIGNALS:
        if sig in text:
            return "out_of_stock", price
    for sig in IN_SIGNALS:
        if sig in text:
            return "in_stock", price
    return "unknown", price

def check_search_site_for_set(site, set_name):
    print(f"    🔎 Searching {site['name']} for '{set_name}'...")
    found = scrape_search_results(site, set_name)
    if not found:
        print("       No products found")
        return []

    print(f"       Found {len(found)} product(s), checking each...")
    products = []
    for p in found:
        status, price = get_page_stock_status(site, p["url"])
        if not p["price"] and price:
            p["price"] = price
        p["status"] = status
        icon = {"in_stock":"✅","out_of_stock":"❌"}.get(status,"❓")
        print(f"      {icon} {p['title'][:60]} — {p.get('price') or 'no price'}")
        products.append(p)
    return products


# ══════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ══════════════════════════════════════════════════════════════════════════════

def check_site_for_set(site, set_name):
    if "smythstoys" in site["domain"]:
        return check_smyths_for_set(set_name)
    return check_search_site_for_set(site, set_name)


# ══════════════════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════════════════

STATUS_EMOJI = {"in_stock":"✅","out_of_stock":"❌","blocked":"🚫","unknown":"❓"}

def product_line(p):
    emoji = STATUS_EMOJI.get(p["status"],"❓")
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
    lines.append(f"_Checked {datetime.utcnow().strftime('%H:%M UTC')}_")
    send_telegram("\n".join(lines))

def send_digest(all_results):
    ts = datetime.utcnow().strftime("%H:%M UTC")
    for set_name, sites in all_results.items():
        lines = [f"📊 *{set_name}* — {ts}\n"]
        has_content = False
        for site_name, products in sites.items():
            if not products:
                lines.append(f"  _{site_name}: nothing found_")
                continue
            has_content = True
            lines.append(f"  📦 *{site_name}*")
            for p in sorted(products,
                            key=lambda x: 0 if x["status"]=="in_stock" else 1):
                lines.append(product_line(p))
            lines.append("")

        if has_content:
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
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    print(f"\n🔍 Pokémon Restock Checker — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Sets:  {', '.join(SETS)}")
    print(f"   Sites: {', '.join(s['name'] for s in SITES)}\n")

    state        = load_state()
    all_results  = {}
    new_in_stock = {}

    for set_name in SETS:
        print(f"══ {set_name} ══")
        all_results[set_name] = {}

        for site in SITES:
            print(f"  {site['name']}...")
            products = check_site_for_set(site, set_name)
            all_results[set_name][site["name"]] = products

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
                "last_checked":  datetime.utcnow().isoformat(),
            }
        print()

    save_state(state)

    if new_in_stock:
        print("🚨 New stock found! Sending alert...")
        send_alert(new_in_stock)

    print("📊 Sending digest...")
    send_digest(all_results)

if __name__ == "__main__":
    main()