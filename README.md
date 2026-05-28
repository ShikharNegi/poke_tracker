# 🎴 Pokémon Restock Tracker

Checks Irish retail websites for Pokémon product restocks every hour and emails you when something comes back in stock. Runs free on GitHub Actions — no PC or server needed.

---

## Setup (takes ~10 minutes)

### Step 1 — Create a GitHub account & repo

1. Go to [github.com](https://github.com) and sign up (free)
2. Click **New repository**, name it `pokemon-restock-tracker`
3. Set it to **Private** (so your product list isn't public)
4. Upload these files into the repo:
   - `checker.py`
   - `products.json`
   - `.github/workflows/restock_checker.yml`

### Step 2 — Set up a Gmail App Password

The script sends emails via Gmail. You need an "App Password" (not your normal password).

1. Go to your Google account → **Security**
2. Enable **2-Step Verification** if not already on
3. Search for **App Passwords** → create one → name it "Restock Tracker"
4. Copy the 16-character password shown — you'll need it below

> You can use any Gmail address for this. You can even send to yourself.

### Step 3 — Add your secrets to GitHub

In your GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret** and add these three:

| Secret name     | Value                                      |
|-----------------|--------------------------------------------|
| `NOTIFY_EMAIL`  | The email address to send alerts TO        |
| `SMTP_EMAIL`    | Your Gmail address (e.g. you@gmail.com)    |
| `SMTP_PASSWORD` | The 16-character App Password from Step 2  |

### Step 4 — Add your products

Edit `products.json` and replace the example entries with real product URLs. For example:

```json
{
  "products": [
    {
      "name": "Prismatic Evolutions ETB",
      "site": "smythstoys.com",
      "url": "https://www.smythstoys.com/ie/en-ie/the-actual-product-page-url"
    }
  ]
}
```

Supported sites with smart detection:
- `smythstoys.com`
- `gamestop.ie`
- `argos.ie`

Any other site uses generic detection (works for most Irish retailers).

### Step 5 — Enable Actions

Go to the **Actions** tab in your GitHub repo and click **Enable workflows** if prompted.

The checker will now run automatically every hour. You can also trigger it manually from the Actions tab anytime.

---

## Customising the schedule

Edit the `cron` line in `.github/workflows/restock_checker.yml`:

```yaml
- cron: "0 * * * *"    # every hour (default)
- cron: "0 8,12,18 * * *"  # three times a day (8am, noon, 6pm UTC)
- cron: "*/30 * * * *"  # every 30 minutes
```

Note: GitHub Actions free tier has a limit of ~2,000 minutes/month, but this script runs in under 30 seconds so hourly checks use well under that.

---

## Checking logs

Go to **Actions** tab → click any workflow run → you'll see output like:

```
🔍 Pokémon Restock Checker — 2025-01-15 14:00 UTC
   Checking 3 product(s)...

  Checking: Prismatic Evolutions ETB
    ❌ Out of stock
  Checking: Surging Sparks Booster Box
    ✅ IN STOCK
  ...
🎉 1 item(s) restocked! Sending email...
  ✉ Email sent to you@gmail.com
```

---

## Getting Android push notifications instead of email

If you'd prefer a phone notification:

1. Install **Pushover** on Android (one-time €5.49 purchase)
2. Sign up at pushover.net, get your **User Key** and create an **API Token**
3. Add `PUSHOVER_USER` and `PUSHOVER_TOKEN` as GitHub secrets
4. In `checker.py`, replace the `send_email()` call with:

```python
import requests
requests.post("https://api.pushover.net/1/messages.json", data={
    "token": os.environ["PUSHOVER_TOKEN"],
    "user":  os.environ["PUSHOVER_USER"],
    "title": "Pokémon Restock!",
    "message": "\n".join(p["name"] for p in restocked),
    "url": restocked[0]["url"],
})
```
