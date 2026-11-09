"""Deals collector: reads DealNews's public RSS feeds once a day, keeps only real discounts
(no credit-card cash back, points or sign-up bonuses), sorts them into Best and Average deals
per category and writes data/deals.json for the website.

Standard library only. Run: python deals.py
"""
import html, json, os, re, sys, time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "deals.json")
KEEP_DAYS = 10            # deals older than this drop off the site
BEST_PCT = 50             # 50 % off or more = Best deal
MIN_PCT = 25              # below 25 % off is not worth showing
PAUSE = 3                 # seconds between feed requests (DealNews asks for 2)
UA = "Mozilla/5.0 (compatible; DealsDigest/1.0; +https://github.com/)"

# (feed URL, category it belongs to, or None = decide from the item)
DN = "https://www.dealnews.com"
FEEDS = [
    (DN + "/?rss=1&sort=hotness", None),
    (DN + "/f1682/Staff-Pick/?rss=1", None),
    (DN + "/?rss=1&sort=time", None),
    (DN + "/c213/Home-Garden/Food-Drink/?rss=1", "Food & Grocery"),
    (DN + "/c214/Home-Garden/Food-Drink/Groceries/?rss=1", "Food & Grocery"),
    (DN + "/c377/Home-Garden/Food-Drink/Restaurants/?rss=1", None),     # fast food: sorted by chain name below
    (DN + "/c202/Clothing-Accessories/?rss=1", "Fashion"),
    (DN + "/c280/Clothing-Accessories/Shoes/?rss=1", "Fashion"),
    (DN + "/c39/Computers/?rss=1", "Tech"),
    (DN + "/c142/Electronics/?rss=1", "Tech"),
    (DN + "/c196/Home-Garden/?rss=1", "Home & Household"),
    (DN + "/c201/Home-Garden/Kitchen/?rss=1", "Home & Household"),
    (DN + "/c637/Home-Garden/Cleaning-Supplies/?rss=1", "Home & Household"),
    (DN + "/c756/Health-Beauty/?rss=1", "Beauty & Health"),
    (DN + "/c211/Sports-Fitness/?rss=1", "Sports & Outdoors"),
    (DN + "/c186/Gaming-Toys/?rss=1", "Toys & Gaming"),
]
CATEGORIES = ["Fast Food", "Food & Grocery", "Fashion", "Tech", "Home & Household", "Beauty & Health",
              "Sports & Outdoors", "Toys & Gaming", "Other"]

# DealNews's own category tag (or words in the title) -> our category. First match wins.
CAT_RULES = [
    ("Food & Grocery", r"grocer|food|drink|coffee|snack|restaurant|pizza|beverage|wine|beer|candy|meal|chocolate|tea\b|kitchen staples"),
    ("Fashion", r"cloth|apparel|shoe|sneaker|boot|jacket|coat|dress|jeans|shirt|hoodie|watch|jewel|handbag|bag\b|backpack|sunglass|underwear|sock|fashion|outerwear"),
    ("Tech", r"computer|laptop|notebook|tablet|ipad|macbook|monitor|tv\b|television|phone|iphone|android|headphone|earbud|airpods|speaker|camera|electronic|ssd|hard drive|router|doorbell|smart home|echo\b|alexa|kindle|fire tv|roku|gpu|graphics card|processor|keyboard|mouse|smart ?watch|charger|power bank|printer|software|vpn|streaming device"),
    ("Home & Household", r"home|garden|kitchen|cookware|appliance|vacuum|furniture|mattress|bedding|towel|cleaning|laundry|detergent|paper towel|toilet paper|storage bin|lighting|lamp|tool|patio|grill|air fryer|blender|coffee maker|household|decor|rug|curtain|bath"),
    ("Beauty & Health", r"beauty|health|medicine|medical|vitamin|supplement|skin|hair|makeup|cosmetic|fragrance|perfume|shav|razor|tooth|oral|personal care|fitness tracker"),
    ("Sports & Outdoors", r"sport|fitness|outdoor|camping|hiking|bike|bicycle|golf|fishing|exercise|gym|yoga|running|kayak"),
    ("Toys & Gaming", r"toy|game|gaming|lego|nintendo|playstation|xbox|console|puzzle|hobby"),
]

# Not real product discounts: card rewards, cash back, points, financial offers, sponsored posts.
EXCLUDE = re.compile(
    r"cash ?back|credit card|debit card|card ?member|cardholder|\bpoints?\b|\bmiles\b|sign[- ]?up bonus|"
    r"\bbonus\b.*\b(account|checking|savings)|checking account|savings account|\bapr\b|"
    r"\brebate\b|\bsweepstakes\b|\bgiveaway\b|financial services|\bplugs?\b|\binsurance\b|% back\b",
    re.I)
EXCLUDE_CATS = re.compile(r"financial|credit|gift card|plugs|travel", re.I)


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:                         # network hiccup or 5xx: wait and retry
            print(f"  {url}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
    return b""


def text_of(fragment):
    """HTML description -> plain text."""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment or "", flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>|</p>|</li>", " . ", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", html.unescape(t)).strip()


def money(s):
    try:
        return float(s.replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_discount(title, body):
    """Return (price, was, percent_off, lowest_price_flag) from the deal's own wording."""
    t = f"{title} . {body}"
    price = was = None
    pct = 0
    tp = re.search(r"\bfor \$([\d,]+(?:\.\d\d)?)", title, re.I) or re.search(r"\$([\d,]+(?:\.\d\d)?)", title)
    title_price = money(tp.group(1)) if tp else None
    pairs = [r"\$([\d,]+(?:\.\d\d)?)\s*\((?:instead of|reg\.?|was|list|orig\.?|regularly)\s*\$([\d,]+(?:\.\d\d)?)\)",
             r"\$([\d,]+(?:\.\d\d)?)[^$]{0,60}?(?:instead of|reg\.?|was|list price of|originally|down from|normally)\s*\$([\d,]+(?:\.\d\d)?)"]
    for where in (title, body):                       # the title first; the description only if it agrees
        for rx in pairs:
            m = re.search(rx, where, re.I)
            if m and (where is title or title_price is None or money(m.group(1)) == title_price):
                price, was = money(m.group(1)), money(m.group(2))
                break
        if price is not None:
            break
    if price is None:
        price = title_price
    if price and not was:
        m = re.search(r"(?:\$([\d,]+(?:\.\d\d)?) (?:off|under|below)(?: list| retail)?|best price we could find by \$([\d,]+(?:\.\d\d)?))", t, re.I)
        if m:
            off = money(m.group(1) or m.group(2))
            if off and m.group(1):                         # "$80 off" -> was = price + 80
                was = price + off
            elif off and not m.group(1):                   # beats the next best price by $X: a discount vs. market
                pct = max(pct, round(100 * off / (price + off)))
    if price and was and was > price:
        pct = max(pct, round(100 * (was - price) / was))
    for m in re.finditer(r"(?:up to\s*)?(\d{1,2})\s*%\s*off", t, re.I):   # "60% off", "up to 85% off"
        v = int(m.group(1))
        if "up to" in m.group(0).lower():
            v = int(v * 0.8)                                # "up to" is the best item only
        pct = max(pct, v)
    if re.search(r"\b(bogo|b1g1|buy one,? get one (free|for \$0))\b", t, re.I):
        pct = max(pct, 50)
    elif re.search(r"\bbuy (two|2),? get (one|1) free\b|\bb2g1\b", t, re.I):
        pct = max(pct, 33)
    elif re.search(r"\bbuy one,? get one (50|half)\s*%? off\b", t, re.I):
        pct = max(pct, 25)
    if (re.match(r"free\b(?! shipping)", title, re.I) and not re.search(r"%|\$\s*[1-9]|\bsale\b|\bclearance\b", title, re.I)) or re.search(r"\bfor free\b(?! shipping)|\bfree after (code|coupon)\b", title, re.I):
        pct = 100                                          # a real freebie
    low = bool(re.search(r"lowest price|all-time low|best price we('ve| have) seen|record low|best price we could find", t, re.I))
    return price, was, pct, low


FAST_FOOD = re.compile(r"\b(" + "|".join([
    "McDonald'?s", "Burger King", "Wendy'?s", "Taco Bell", "KFC", "Popeyes", "Chick-fil-A", "Subway", "Domino'?s", "Pizza Hut",
    "Papa John'?s", "Little Caesars", "Sonic", "Arby'?s", "Jack in the Box", "Carl'?s Jr", "Hardee'?s", "Dairy Queen", "DQ",
    "Chipotle", "Panera", "Five Guys", "Shake Shack", "Whataburger", "In-N-Out", "Wingstop", "Buffalo Wild Wings", "Dunkin'?",
    "Starbucks", "Krispy Kreme", "Jersey Mike'?s", "Jimmy John'?s", "Firehouse Subs", "Panda Express", "Raising Cane'?s",
    "Zaxby'?s", "Bojangles", "Culver'?s", "Qdoba", "Del Taco", "El Pollo Loco", "White Castle", "Checkers", "Rally'?s",
    "Church'?s", "Moe'?s", "Noodles & Company", "Sbarro", "Auntie Anne'?s", "Cinnabon", "Tim Hortons", "7-Eleven", "Wawa",
    "Sheetz", "Taco John'?s", "Captain D'?s", "Long John Silver'?s", "Pollo Tropical", "Smoothie King", "Tropical Smoothie",
    "Jamba", "Crumbl", "Insomnia Cookies", "Marco'?s Pizza", "Papa Murphy'?s", "Hungry Howie'?s", "Steak ?n'? Shake",
    "Freddy'?s", "Portillo'?s", "Blaze Pizza", "MOD Pizza", "Sweetgreen", "Cava", "Baskin-Robbins", "Cold Stone",
    "Dutch Bros", "Caribou Coffee", "Peet'?s", "Wienerschnitzel", "Potbelly", "McAlister'?s", "Schlotzsky'?s"]) + r")\b", re.I)


def how_to_get(title, body):
    """Short labels for how to claim the offer, and the promo code if one is given."""
    t = f"{title} . {body}"
    how = []
    if re.search(r"\bapp\b|app[- ]exclusive|mobile order", t, re.I):
        how.append("In the app")
    if re.search(r"rewards|loyalty|members?\b|sign up|account", t, re.I):
        how.append("Free rewards account")
    if re.search(r"in[- ]store|in[- ]restaurant|drive[- ]?thru|participating locations", t, re.I):
        how.append("In store")
    if re.search(r"\bonline\b|order ahead|website", t, re.I):
        how.append("Online")
    if re.search(r"doordash|uber ?eats|grubhub|delivery", t, re.I):
        how.append("Delivery")
    m = re.search(r"(?:promo |coupon |offer )?code[:\s]+[\"“']?([A-Z0-9]{4,15})\b", t)
    return how[:3], (m.group(1) if m else "")


def category_for(tag, title, feed_cat):
    if FAST_FOOD.search(title) and re.search(r"restaurant|fast food|food|coffee|pizza|drink|snack|groceries|^$", tag or "", re.I) \
            and not re.search(r"gift card|grocery|k-cup|\bpods?\b|ground coffee|whole bean|\boz\.?\b|\bounce", title, re.I):
        return "Fast Food"
    if feed_cat:
        return feed_cat
    for cat, rx in CAT_RULES:
        if re.search(rx, tag or "", re.I):
            return cat
    for cat, rx in CAT_RULES:
        if re.search(rx, title, re.I):
            return cat
    return "Other"


def store_of(title, body):
    m = re.search(r"\bat ((?:[A-Z][\w&'-]*)(?: [A-Z][\w&'-]*){0,2})", f"{body} {title}")
    if m and m.group(1).lower() not in {"the", "a", "checkout", "this", "least"}:
        return m.group(1).rstrip(".")
    m = re.match(r"([A-Z][\w&'.-]+(?: [A-Z][\w&'.-]+)?)\b.{0,40}?\b(?i:sale|clearance|coupon|promo|open-box|deals?|graphic)\b", title)
    if m and m.group(1).split()[-1].lower() in {"sale", "clearance", "open-box", "graphic"}:
        return m.group(1).split()[0]
    return m.group(1) if m else ""


def parse_feed(xml, feed_cat, source):
    out = []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        print(f"  could not read feed: {e}", file=sys.stderr)
        return out
    for it in root.iter("item"):
        title = html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        guid = (it.findtext("guid") or link).strip()
        desc = it.findtext("description") or ""
        for child in it:                                   # <content:encoded> if present
            if child.tag.endswith("encoded") and child.text:
                desc = child.text
        tag = " ".join((c.text or "") for c in it.findall("category"))
        body = text_of(desc)
        img = re.search(r"<img[^>]+src=[\"']([^\"']+)", desc or "")
        when = it.findtext("pubDate")
        try:
            dt = parsedate_to_datetime(when).astimezone(timezone.utc)
        except (TypeError, ValueError):
            dt = datetime.now(timezone.utc)
        if not title or not link:
            continue
        if EXCLUDE.search(title) or EXCLUDE.search(body[:400]) or EXCLUDE_CATS.search(tag):
            continue
        if re.search(r"\bexpired\b|sold out", title, re.I):
            continue
        price, was, pct, low = parse_discount(title, body)
        m = re.search(r"/(\d{6,})\.html", guid) or re.search(r"(\d{6,})", guid)
        out.append({
            "id": m.group(1) if m else guid,
            "t": title,
            "url": link,
            "img": html.unescape(img.group(1)) if img else "",
            "cat": category_for(tag, title, feed_cat),
            "tag": tag.strip(),
            "store": store_of(title, body),
            "price": price, "was": was, "pct": pct, "low": low,
            "hot": source in ("hotness", "staff"),
            "staff": source == "staff",
            "date": dt.strftime("%Y-%m-%d"),
            "ts": int(dt.timestamp()),
            "note": body[:220],
            **dict(zip(("how", "code"), how_to_get(title, body))),
        })
    return out


def rank(d):
    """Deal score: mostly how much you save, plus small bonuses for 'lowest price' and editors' picks."""
    s = d["pct"]
    if d["low"]:
        s += 10
    if d["staff"]:
        s += 8
    if d["hot"]:
        s += 5
    if d["was"] and d["price"] is not None and d["was"] - d["price"] >= 100:
        s += 5                                             # big dollar saving
    return s


def tier(d):
    if d["cat"] == "Fast Food":                            # fast food rarely states a % off
        t = d["t"]
        if d["pct"] >= 50:                                 # free item, buy one get one, half price
            return "best"
        free = re.search(r"\bfree\b(?! shipping| delivery)", t, re.I)
        big = re.search(r"(?:with|w/)(?: a)? \$([\d.]+)\+? (?:purchase|order|minimum)", t, re.I)
        if free and (not big or float(big.group(1)) <= 5):
            d["pct"] = 100                                 # shown as FREE
            return "best"                                  # free food with no or a tiny purchase
        if d["pct"] >= 20 or re.search(r"\bfree\b(?! shipping| delivery)|\$0?\.\d\d\b|\$[1-6](\.\d\d)?\b|\b\d+ for \$\d+|\bvalue\b|\bmeal deal\b|\bbundle\b|\bbox\b|\$\d+ off", t, re.I):
            return "average"
        return None
    if d["pct"] >= BEST_PCT or (d["pct"] >= 40 and (d["low"] or d["staff"])):
        return "best"
    if d["pct"] >= MIN_PCT or (d["staff"] and d["pct"] >= 15):
        return "average"
    return None


def main():
    old = []
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = (json.load(f) or {}).get("deals", [])
    by_id = {d["id"]: d for d in old}
    seen = 0
    for url, cat in FEEDS:
        source = "hotness" if "hotness" in url else "staff" if "Staff-Pick" in url else "feed"
        items = parse_feed(get(url), cat, source)
        print(f"{len(items):3d} deals  {url}")
        seen += len(items)
        for d in items:
            prev = by_id.get(d["id"])
            if prev:                                       # keep the earliest date; merge flags
                d["date"], d["ts"] = prev["date"], prev["ts"]
                d["hot"] = d["hot"] or prev.get("hot", False)
                d["staff"] = d["staff"] or prev.get("staff", False)
                if prev["cat"] != "Other" and d["cat"] == "Other":
                    d["cat"] = prev["cat"]
            by_id[d["id"]] = d
        time.sleep(PAUSE)
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).timestamp())
    keep = []
    for d in by_id.values():
        if d["ts"] < cutoff:
            continue
        d["tier"] = tier(d)
        if not d["tier"]:
            continue
        d["score"] = rank(d)
        keep.append(d)
    seen_titles, deals = set(), []
    for d in sorted(keep, key=lambda d: (-d["score"], -d["ts"])):
        k = re.sub(r"[^a-z0-9]+", " ", d["t"].lower()).strip()[:80]
        if k in seen_titles:                               # same deal posted twice
            continue
        seen_titles.add(k)
        deals.append(d)
    counts = {c: {"best": 0, "average": 0} for c in CATEGORIES}
    for d in deals:
        counts.setdefault(d["cat"], {"best": 0, "average": 0})[d["tier"]] += 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                   "source": "DealNews", "counts": counts, "deals": deals}, f, ensure_ascii=False, indent=0)
    best = sum(1 for d in deals if d["tier"] == "best")
    print(f"Read {seen} feed items; kept {len(deals)} deals ({best} best, {len(deals) - best} average).")
    if seen == 0:
        sys.exit("No feed could be read today; the site keeps yesterday's deals.")


if __name__ == "__main__":
    main()
