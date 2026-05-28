import json
import os
import smtplib
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup
from datetime import datetime

# ── Load config ────────────────────────────────────────────────────────────────
with open("products.json") as f:
    config = json.load(f)

PRODUCTS = config["products"]
NOTIFY_EMAIL = os.environ["NOTIFY_EMAIL"]        # your email address
SMTP_EMAIL   = os.environ["SMTP_EMAIL"]          # Gmail address to send FROM
SMTP_PASSWORD = os.environ["SMTP_PASSWORD"]      # Gmail app password

STATE_FILE = "last_state.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 10; Pixel 4) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Mobile Safari/537.36"
    ),
    "Accept-Language": "en-IE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── Site-specific stock detection ──────────────────────────────────────────────
def check_smyths(soup):
    btn = soup.find("button", {"id": "addToCartBtn"}) or soup.find("button", string=lambda t: t and "add to cart" in t.lower())
    if btn and not btn.get("disabled"):
        return True
    oos = soup.find(string=lambda t: t and "out of stock" in t.lower())
    return oos is None

def check_gamestop(soup):
    btn = soup.find("button", class_=lambda c: c and "add-to-cart" in c)
    if btn and btn.get("disabled"):
        return False
    oos = soup.find(class_=lambda c: c and "out-of-stock" in str(c).lower())
    return oos is None

def check_argos(soup):
    oos = soup.find(string=lambda t: t and ("out of stock" in t.lower() or "unavailable" in t.lower()))
    atc = soup.find("button", string=lambda t: t and "add to trolley" in t.lower())
    if atc:
        return True
    return oos is None

def check_generic(soup):
    """Fallback: look for common out-of-stock signals."""
    out_signals = ["out of stock", "sold out", "unavailable", "notify me when available", "currently unavailable"]
    in_signals  = ["add to cart", "add to basket", "add to trolley", "buy now", "in stock"]
    text = soup.get_text().lower()
    for sig in out_signals:
        if sig in text:
            return False
    for sig in in_signals:
        if sig in text:
            return True
    return None  # unknown

SITE_CHECKERS = {
    "smythstoys.com": check_smyths,
    "gamestop.ie":    check_gamestop,
    "argos.ie":       check_argos,
}

# ── Core check logic ───────────────────────────────────────────────────────────
def check_product(product):
    url  = product["url"]
    site = product.get("site", "")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        checker = SITE_CHECKERS.get(site, check_generic)
        return checker(soup)
    except Exception as e:
        print(f"  ⚠ Error checking {product['name']}: {e}")
        return None

# ── Email notification ─────────────────────────────────────────────────────────
def send_email(restocked):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎉 Pokémon Restock Alert — {len(restocked)} item(s) back in stock!"
    msg["From"]    = SMTP_EMAIL
    msg["To"]      = NOTIFY_EMAIL

    lines = "\n".join(
        f"  • {p['name']} ({p.get('site','')}) — {p['url']}"
        for p in restocked
    )
    html_lines = "".join(
        f"""<tr>
          <td style="padding:10px 0; border-bottom:1px solid #eee;">
            <strong>{p['name']}</strong><br>
            <span style="color:#666;font-size:13px;">{p.get('site','')}</span><br>
            <a href="{p['url']}" style="color:#1D9E75;">{p['url']}</a>
          </td>
        </tr>"""
        for p in restocked
    )

    plain = f"Pokémon Restock Alert!\n\nThe following items are back in stock:\n{lines}\n\nCheck them out now!"
    html  = f"""
    <html><body style="font-family:sans-serif;max-width:500px;margin:auto;padding:20px;">
      <h2 style="color:#1D9E75;">🎉 Pokémon Restock Alert!</h2>
      <p>The following items are back in stock:</p>
      <table style="width:100%;border-collapse:collapse;">{html_lines}</table>
      <p style="color:#888;font-size:12px;margin-top:20px;">
        Checked at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}
      </p>
    </body></html>"""

    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, NOTIFY_EMAIL, msg.as_string())
    print(f"  ✉ Email sent to {NOTIFY_EMAIL}")

# ── State management ───────────────────────────────────────────────────────────
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    print(f"\n🔍 Pokémon Restock Checker — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"   Checking {len(PRODUCTS)} product(s)...\n")

    state = load_state()
    restocked = []

    for product in PRODUCTS:
        name = product["name"]
        print(f"  Checking: {name}")
        in_stock = check_product(product)

        prev = state.get(name)
        state[name] = in_stock

        if in_stock is True:
            print(f"    ✅ IN STOCK")
            if prev is not True:  # was not in stock before → alert!
                restocked.append(product)
        elif in_stock is False:
            print(f"    ❌ Out of stock")
        else:
            print(f"    ❓ Unknown status")

    save_state(state)

    if restocked:
        print(f"\n🎉 {len(restocked)} item(s) restocked! Sending email...")
        send_email(restocked)
    else:
        print("\n😴 No new restocks. No email sent.")

if __name__ == "__main__":
    main()
