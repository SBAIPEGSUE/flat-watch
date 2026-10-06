#!/usr/bin/env python3
"""
flat_watch.py - watches Rightmove rental searches and pushes new matches to your phone.

No extra packages needed: plain Python 3.8 or newer.

    python3 flat_watch.py --test    one pass: shows what it can read and sends a test notification
    python3 flat_watch.py           runs continuously (leave it running)
    python3 flat_watch.py --once    a single check, then exit (for cron / Task Scheduler)
    python3 flat_watch.py --reset   forget every listing seen so far

All settings live in config.json next to this file.
"""

import argparse
import csv
import datetime as dt
import html as htmllib
import json
import os
import random
import re
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "seen.json")
MATCHES_CSV = os.path.join(HERE, "matches.csv")
LOG_PATH = os.path.join(HERE, "flat_watch.log")

IN_GITHUB = os.environ.get("GITHUB_ACTIONS") == "true"

BASE = "https://www.rightmove.co.uk"
SQFT_PER_SQM = 10.7639
MAX_DETAIL_FETCHES_PER_CYCLE = 15

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

DEFAULT_CONFIG = {
    "areas": {},
    "min_price": 0,
    "max_price": 100000,
    "min_beds": 0,
    "max_beds": 10,
    "ideal_min_sqm": 60,
    "skip_if_stated_below_sqm": 55,
    "radius_miles": 0.0,
    "check_every_minutes": 8,
    "pause_overnight": {"from_hour": 0, "to_hour": 6},
    "daily_summary_hour": 9,
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
}

HIGHLIGHTS = [
    ("Garden", r"\bgarden\b"),
    ("Balcony", r"\bbalcon(?:y|ies)\b"),
    ("Terrace", r"\b(?:roof )?terrace\b"),
    ("Period property", r"\b(?:victorian|edwardian|georgian|period)\b"),
    ("New build", r"\b(?:brand new|new build|new-build|build to rent|built for renters)\b"),
    ("Short let", r"\bshort[- ]?let\b"),
    ("Pets considered", r"\bpets? (?:allowed|friendly|considered|welcome)\b|\bpet[- ]friendly\b"),
    ("Bills included", r"\bbills? included\b|\binclusive of (?:all )?bills\b"),
    ("Lift", r"\blift\b"),
    ("Parking", r"\bparking\b"),
    ("Zero-deposit option", r"\bzero deposit\b|\bno deposit option\b"),
]


# ---------------------------------------------------------------- utilities

try:  # always use UK time, even on a server set to UTC
    from zoneinfo import ZoneInfo
    UK_TZ = ZoneInfo("Europe/London")
except Exception:
    UK_TZ = None


def now():
    return dt.datetime.now(UK_TZ) if UK_TZ else dt.datetime.now()


def log(msg):
    line = f"[{now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError as e:
        sys.exit(f"Could not read {os.path.basename(path)}: {e}\n"
                 "Check the file for a missing comma or quote.")


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit("config.json is missing - it should sit next to flat_watch.py.")
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    if os.environ.get("NTFY_TOPIC", "").strip():  # e.g. a GitHub secret, kept out of the public repo
        cfg["ntfy_topic"] = os.environ["NTFY_TOPIC"].strip()
    if not cfg["areas"]:
        sys.exit("config.json has no areas to watch.")
    return cfg


def clean_text(s):
    if not s:
        return ""
    s = re.sub(r"<\s*br\s*/?>|</p>", "\n", str(s), flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def snippet(text, limit=230):
    text = " ".join(clean_text(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("! "))
    if stop > limit * 0.5:
        return cut[: stop + 1]
    return cut.rsplit(" ", 1)[0] + "..."


def parse_money(s):
    m = re.search(r"£\s*([\d,]+(?:\.\d+)?)", s or "")
    return float(m.group(1).replace(",", "")) if m else None


# ---------------------------------------------------------------- fetching

class Blocked(Exception):
    pass


def make_ssl_context():
    """Certificate checking stays on. Python from python.org on a Mac ships without
    a certificate list, so also trust certifi (if installed) and the Mac's own list."""
    ctx = ssl.create_default_context()
    extra = []
    try:
        import certifi  # noqa: F401
        extra.append(certifi.where())
    except Exception:
        pass
    extra += ["/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem",
              "/opt/homebrew/etc/openssl@3/cert.pem", "/usr/local/etc/openssl@3/cert.pem",
              "/etc/ssl/certs/ca-certificates.crt"]
    for path in extra:
        if os.path.isfile(path):
            try:
                ctx.load_verify_locations(cafile=path)
            except (ssl.SSLError, OSError):
                pass
    return ctx


SSL_CTX = make_ssl_context()


def fetch(url, timeout=30):
    """Return (status, text). Raises URLError on network failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            charset = r.headers.get_content_charset() or "utf-8"
            return r.status, r.read().decode(charset, "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""


def search_url(cfg, location_id):
    params = {
        "locationIdentifier": location_id,
        "minPrice": cfg["min_price"],
        "maxPrice": cfg["max_price"],
        "minBedrooms": cfg["min_beds"],
        "maxBedrooms": cfg["max_beds"],
        "radius": cfg.get("radius_miles", 0.0),
        "sortType": 6,  # newest listed first
        "includeLetAgreed": "false",
        "dontShow": "houseShare,retirement,student",
        "channel": "RENT",
        "index": 0,
    }
    return BASE + "/property-to-rent/find.html?" + urllib.parse.urlencode(params)


def looks_blocked(html):
    low = (html or "").lower()
    return any(k in low for k in ("captcha", "access denied", "are you a robot",
                                  "unusual traffic", "request blocked"))


# ---------------------------------------------------------------- parsing

_DEC = json.JSONDecoder()


def unflatten(arr):
    """Decode Rightmove's compact page format: a list where index 0 is the root and
    every value inside an object/list is an index into the same list."""
    cache = {}

    def h(i):
        if not isinstance(i, int) or isinstance(i, bool) or i < 0 or i >= len(arr):
            return None
        if i in cache:
            return cache[i]
        v = arr[i]
        if isinstance(v, list):
            out = []
            cache[i] = out
            out.extend(h(x) for x in v)
            return out
        if isinstance(v, dict):
            out = {}
            cache[i] = out
            for k, idx in v.items():
                out[k] = h(idx)
            return out
        return v

    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(old, 10000))
    try:
        return h(0)
    finally:
        sys.setrecursionlimit(old)


def _expand(obj):
    """If obj wraps compact page data ({"data": "[...]", ...}), decode it."""
    if isinstance(obj, dict) and isinstance(obj.get("data"), str):
        try:
            inner = json.loads(obj["data"])
        except ValueError:
            return obj
        if isinstance(inner, list) and inner and isinstance(inner[0], dict) and \
                all(isinstance(v, int) for v in inner[0].values()):
            return unflatten(inner)
        return inner
    return obj


def json_roots(html):
    """Pull every embedded JSON structure out of a page. Returns (roots, flight_text)."""
    roots = []
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
        attrs, body = m.group(1), m.group(2)
        if "application/json" in attrs or "application/ld+json" in attrs or "__NEXT_DATA__" in attrs:
            try:
                roots.append(json.loads(body))
            except ValueError:
                pass
    for m in re.finditer(r"window\.(?:jsonModel|__PAGE_MODEL|PAGE_MODEL|__PRELOADED_STATE__|__INITIAL_STATE__)\s*=\s*", html):
        try:
            obj, _ = _DEC.raw_decode(html, m.end())
            roots.append(_expand(obj))
        except ValueError:
            pass
    # Next.js app-router "flight" data: self.__next_f.push([1,"..."])
    chunks = []
    for m in re.finditer(r'self\.__next_f\.push\(\[\s*1\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)', html, re.S):
        try:
            chunks.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            pass
    flight = "".join(chunks)
    for line in flight.split("\n"):
        mm = re.match(r"^[0-9a-zA-Z]+:", line)
        if not mm:
            continue
        body = line[mm.end():]
        if body[:1] in ("[", "{"):
            try:
                roots.append(json.loads(body))
            except ValueError:
                pass
    return roots, flight


def anchor_scan(text, anchor=r'\{"id":', cap=800):
    out = []
    for i, m in enumerate(re.finditer(anchor, text or "")):
        if i >= cap:
            break
        try:
            obj, _ = _DEC.raw_decode(text, m.start())
            out.append(obj)
        except ValueError:
            pass
    return out


def walk_dicts(obj, limit=300000):
    stack, n = [obj], 0
    while stack and n < limit:
        cur = stack.pop()
        n += 1
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(reversed(cur))


def find_first(roots, key, want=None):
    for r in roots:
        for d in walk_dicts(r):
            if key in d and d[key] not in (None, "", [], {}):
                v = d[key]
                if want is None or want(v):
                    return v
    return None


def looks_like_property(d):
    return (
        "id" in d and "bedrooms" in d and "price" in d
        and isinstance(d.get("price"), (dict, int, float))
        and any(k in d for k in ("propertyUrl", "displayAddress", "summary"))
    )


def monthly_price(price):
    if isinstance(price, (int, float)):
        return round(price) if price > 0 else None
    if not isinstance(price, dict):
        return None
    amt = price.get("amount")
    freq = str(price.get("frequency") or "").lower()
    if isinstance(amt, (int, float)) and amt > 0:
        if "week" in freq:
            return round(amt * 52 / 12)
        if "year" in freq or "annual" in freq:
            return round(amt / 12)
        if "day" in freq:
            return round(amt * 365 / 12)
        return round(amt)
    for dp in price.get("displayPrices") or []:
        s = str((dp or {}).get("displayPrice", ""))
        v = parse_money(s)
        if v:
            return round(v * 52 / 12) if "pw" in s.lower() else round(v)
    return None


def first_image(d):
    pi = d.get("propertyImages") or {}
    if isinstance(pi, dict):
        if pi.get("mainImageSrc"):
            return pi["mainImageSrc"]
        imgs = pi.get("images") or []
        if imgs and isinstance(imgs[0], dict):
            return imgs[0].get("srcUrl") or imgs[0].get("url")
    imgs = d.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        return imgs[0].get("srcUrl") or imgs[0].get("url")
    return None


def normalise_listing(d):
    pid = str(d.get("id"))
    url = d.get("propertyUrl") or f"/properties/{pid}"
    url = urllib.parse.urljoin(BASE, str(url).split("#")[0])
    customer = d.get("customer") or {}
    update = d.get("listingUpdate") or {}
    return {
        "id": pid,
        "url": url,
        "pcm": monthly_price(d.get("price")),
        "beds": d.get("bedrooms") if isinstance(d.get("bedrooms"), int) else None,
        "address": clean_text(d.get("displayAddress")),
        "summary": clean_text(d.get("summary")),
        "size_text": str(d.get("displaySize") or ""),
        "subtype": str(d.get("propertySubType") or ""),
        "status": str(d.get("displayStatus") or ""),
        "agent": clean_text(customer.get("branchDisplayName") or customer.get("brandTradingName")),
        "added": str(d.get("addedOrReduced") or ""),
        "update_reason": str(update.get("listingUpdateReason") or ""),
        "image": first_image(d),
        "available": str(d.get("letAvailableDate") or ""),
    }


def parse_search(html):
    """Return a list of listing dicts from a search results page."""
    roots, flight = json_roots(html)
    found = {}

    def collect(objs):
        for r in objs:
            for d in walk_dicts(r):
                if looks_like_property(d):
                    L = normalise_listing(d)
                    prev = found.get(L["id"])
                    if prev is None:
                        found[L["id"]] = L
                    else:  # keep whichever copy has more filled-in fields
                        for k, v in L.items():
                            if v and not prev.get(k):
                                prev[k] = v

    collect(roots)
    if not found:
        collect(anchor_scan(html))
        collect(anchor_scan(flight))
    if not found:
        # Last resort: just the property IDs; details come from each listing page.
        for pid in dict.fromkeys(re.findall(r"/properties/(\d{6,})", html)):
            found[pid] = {"id": pid, "url": f"{BASE}/properties/{pid}", "pcm": None,
                          "beds": None, "address": "", "summary": "", "size_text": "",
                          "subtype": "", "status": "", "agent": "", "added": "",
                          "update_reason": "", "image": None, "_ids_only": True}
    return list(found.values())


def parse_details(html):
    """Pull size, description, availability etc. from a single listing page."""
    roots, flight = json_roots(html)
    if not roots and flight:
        roots = anchor_scan(flight, r'\{"')
    pdata = None
    for r in roots:
        for d in walk_dicts(r):
            if isinstance(d.get("propertyData"), dict):
                pdata = d["propertyData"]
                break
        if pdata:
            break
    if pdata is None:
        for r in roots:
            for d in walk_dicts(r):
                if isinstance(d.get("sizings"), list) or ("keyFeatures" in d and "text" in d):
                    pdata = d
                    break
            if pdata:
                break
    src = pdata or {}
    search = [src] if src else roots

    text = src.get("text") if isinstance(src.get("text"), dict) else {}
    desc = text.get("description") or find_first(
        search, "description", lambda v: isinstance(v, str) and len(v) > 80)
    lettings = src.get("lettings") or find_first(search, "lettings", lambda v: isinstance(v, dict)) or {}
    floorplans = src.get("floorplans") or find_first(search, "floorplans", lambda v: isinstance(v, list)) or []
    sizings = src.get("sizings") or find_first(search, "sizings", lambda v: isinstance(v, list)) or []
    features = src.get("keyFeatures") or find_first(search, "keyFeatures", lambda v: isinstance(v, list)) or []
    prices = src.get("prices") if isinstance(src.get("prices"), dict) else {}
    address = src.get("address") if isinstance(src.get("address"), dict) else {}
    images = src.get("images") if isinstance(src.get("images"), list) else []
    status = src.get("status") if isinstance(src.get("status"), dict) else {}

    if not desc:
        m = re.search(r'<meta[^>]+(?:property|name)="(?:og:)?description"[^>]+content="([^"]*)"', html, re.I)
        desc = htmllib.unescape(m.group(1)) if m else ""

    det = {
        "description": clean_text(desc),
        "key_features": [clean_text(f) for f in features if isinstance(f, str)],
        "sizings": [s for s in sizings if isinstance(s, dict)],
        "available": str(lettings.get("letAvailableDate") or "") if isinstance(lettings, dict) else "",
        "furnish": str(lettings.get("furnishType") or "") if isinstance(lettings, dict) else "",
        "let_type": str(lettings.get("letType") or "") if isinstance(lettings, dict) else "",
        "deposit": lettings.get("deposit") if isinstance(lettings, dict) else None,
        "floorplans": [f.get("url") for f in floorplans if isinstance(f, dict) and f.get("url")],
        "bedrooms": src.get("bedrooms") if isinstance(src.get("bedrooms"), int) else None,
        "price_text": str(prices.get("primaryPrice") or ""),
        "address": clean_text(address.get("displayAddress")),
        "image": (images[0].get("url") if images and isinstance(images[0], dict) else None),
        "let_agreed": bool(status.get("archived")) or "let agreed" in json.dumps(status).lower(),
    }
    return det


# ---------------------------------------------------------------- sizes and filters

_SQFT_RE = re.compile(
    r"(\d{1,2},\d{3}|\d{3,4})(?:\.\d+)?\s*(?:sq\.?\s*f(?:ee)?t\.?|sqft|ft²|ft2\b|square\s*f(?:ee|oo)t)", re.I)
_SQM_RE = re.compile(
    r"(\d{2,3}(?:\.\d+)?)\s*(?:sq\.?\s*m(?:etres?|eters?|trs?)?\b\.?|sqm\b|m²|m2\b|square\s*met(?:re|er)s?)", re.I)


def sizes_in_text(text):
    vals = []
    for m in _SQFT_RE.finditer(text or ""):
        v = float(m.group(1).replace(",", ""))
        if 270 <= v <= 2700:
            vals.append(v / SQFT_PER_SQM)
    for m in _SQM_RE.finditer(text or ""):
        v = float(m.group(1))
        if 25 <= v <= 250:
            vals.append(v)
    return vals


def size_sqm(listing, det):
    """Return (sqm or None, source) where source is 'listing' or 'description'."""
    sqm_val = sqft_val = None
    for s in det.get("sizings") or []:
        unit = str(s.get("unit") or s.get("displayUnit") or "").lower().replace(".", "").replace(" ", "")
        size = s.get("minimumSize") or s.get("maximumSize")
        try:
            size = float(size)
        except (TypeError, ValueError):
            continue
        if size <= 0:
            continue
        if unit in ("sqm", "m2", "m²", "sqmetres", "sqmeters"):
            sqm_val = sqm_val or size
        elif unit in ("sqft", "ft2", "ft²", "sqfeet"):
            sqft_val = sqft_val or size
    if sqm_val:
        return sqm_val, "listing"
    if sqft_val:
        return sqft_val / SQFT_PER_SQM, "listing"
    vals = sizes_in_text(listing.get("size_text"))
    if vals:
        return max(vals), "listing"
    text = " ".join([det.get("description", ""), " ".join(det.get("key_features") or []),
                     listing.get("summary", "")])
    vals = sizes_in_text(text)
    if vals:
        return max(vals), "description"
    return None, None


def classify_size(cfg, sqm, source):
    if sqm is None:
        return "unknown"
    if sqm >= cfg["ideal_min_sqm"]:
        return "good"
    if sqm < cfg["skip_if_stated_below_sqm"] and source == "listing":
        return "small"  # only skip on a size the agent actually entered as the floor area
    return "close"


def basic_reject_reason(cfg, L):
    """Reason to drop a listing using what we already know, or None."""
    if re.search(r"share|room|studio", L.get("subtype", ""), re.I) and L.get("beds") in (None, 0):
        return f"type {L['subtype']}"
    if re.search(r"house share|room only", L.get("subtype", ""), re.I):
        return f"type {L['subtype']}"
    if "let agreed" in L.get("status", "").lower():
        return "let agreed"
    if L.get("pcm") is not None and not (cfg["min_price"] <= L["pcm"] <= cfg["max_price"]):
        return f"price £{L['pcm']}"
    if L.get("beds") is not None and not (cfg["min_beds"] <= L["beds"] <= cfg["max_beds"]):
        return f"{L['beds']} beds"
    return None


def highlights(text):
    return [label for label, pat in HIGHLIGHTS if re.search(pat, text or "", re.I)]


# ---------------------------------------------------------------- notifications

def notify(cfg, title, message, url=None, image=None, priority=3, tags=None):
    topic = cfg.get("ntfy_topic")
    if not topic:
        log("(no ntfy_topic set - notification printed only)")
        print(f"\n=== {title} ===\n{message}\n{url or ''}\n")
        return
    payload = {"topic": topic, "title": title, "message": message, "priority": priority}
    if tags:
        payload["tags"] = tags
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Open listing", "url": url}]
    if image:
        payload["attach"] = image
    req = urllib.request.Request(
        cfg.get("ntfy_server", "https://ntfy.sh").rstrip("/"),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20, context=SSL_CTX):
            pass
    except Exception as e:  # never let a notification failure stop the watcher
        log(f"Notification failed: {e}")


def build_message(cfg, area, L, det, sqm, source, verdict):
    beds = L.get("beds")
    beds_s = "Studio" if beds == 0 else (f"{beds} bed" if beds is not None else "? bed")
    price_s = f"£{L['pcm']:,} pcm" if L.get("pcm") else (det.get("price_text") or "price n/a")
    title = f"{price_s} · {beds_s} · {area}"

    ideal = cfg["ideal_min_sqm"]
    lines = []
    addr = L.get("address") or det.get("address")
    if addr:
        lines.append(addr)
    if sqm:
        tag = "✅" if verdict == "good" else f"⚠️ under {ideal}"
        note = " (from description - check floorplan)" if source == "description" else ""
        lines.append(f"📐 {sqm:.0f} m² / {sqm * SQFT_PER_SQM:.0f} sq ft {tag}{note}")
    else:
        lines.append("📐 Size not stated" + (" - floorplan on listing" if det.get("floorplans") else ""))
    bits = []
    avail = det.get("available") or L.get("available") or ""
    if avail and "ask" not in avail.lower():
        bits.append(f"Available {avail}")
    if det.get("furnish"):
        bits.append(det["furnish"])
    if bits:
        lines.append(" · ".join(bits))
    hl = highlights(" ".join([det.get("description", ""), " ".join(det.get("key_features") or []),
                              L.get("summary", "")]))
    if hl:
        lines.append(" · ".join(hl))
    blurb = snippet(det.get("description") or L.get("summary") or "")
    if blurb:
        lines.append("")
        lines.append(blurb)
    if L.get("agent"):
        lines.append(f"- {L['agent']}")
    return title, "\n".join(lines)


def record_match(row):
    new_file = not os.path.exists(MATCHES_CSV)
    with open(MATCHES_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new_file:
            w.writeheader()
        w.writerow(row)


# ---------------------------------------------------------------- the check itself

def polite_pause(cfg, lo, hi):
    if not cfg.get("_no_sleep"):
        time.sleep(random.uniform(lo, hi))


def save_debug(name, html):
    path = os.path.join(HERE, f"debug_{name}.html")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    except OSError:
        pass
    return path


def run_cycle(cfg, state, fetcher=fetch, notifier=notify):
    seen = state.setdefault("seen", {})
    seeded_areas = state.setdefault("seeded_areas", [])
    stats = {"pages_ok": 0, "pages_failed": 0, "new": 0, "sent": 0, "skipped": 0}
    candidates = []
    blocked = None
    newly_seeded = 0

    areas = list(cfg["areas"].items())
    random.shuffle(areas)
    for i, (area, loc) in enumerate(areas):
        if i:
            polite_pause(cfg, 3, 9)
        try:
            status, html = fetcher(search_url(cfg, loc))
        except Exception as e:
            log(f"{area}: network error ({e})")
            stats["pages_failed"] += 1
            continue
        if status in (403, 429, 503) or (status == 200 and looks_blocked(html) and "/properties/" not in html):
            blocked = f"{area}: HTTP {status}"
            break  # stop asking, but still process what was already read
        if status != 200:
            log(f"{area}: HTTP {status}")
            stats["pages_failed"] += 1
            continue
        listings = parse_search(html)
        if not listings:
            path = save_debug("search", html)
            log(f"{area}: page loaded but no listings could be read (saved {os.path.basename(path)})")
            stats["pages_failed"] += 1
            continue
        stats["pages_ok"] += 1
        # The first time an area is read, record what's already listed without notifying.
        first_time = area not in seeded_areas
        for L in listings:
            if L["id"] in seen:
                continue
            seen[L["id"]] = {"first_seen": now().isoformat(timespec="minutes"), "area": area, "pcm": L.get("pcm")}
            if first_time:
                newly_seeded += 1
            else:
                stats["new"] += 1
                candidates.append((area, L))
        if first_time:
            seeded_areas.append(area)

    if newly_seeded:
        stats["seeded"] = newly_seeded

    fetched = 0
    for area, L in candidates:
        reason = basic_reject_reason(cfg, L)
        if reason:
            log(f"skip {L['id']} ({area}): {reason}")
            stats["skipped"] += 1
            continue
        det = {}
        if fetched < MAX_DETAIL_FETCHES_PER_CYCLE:
            polite_pause(cfg, 2, 5)
            fetched += 1
            try:
                status, html = fetcher(L["url"])
                if status == 200:
                    det = parse_details(html)
                elif status in (403, 429):
                    log(f"listing page refused (HTTP {status}); sending what we have")
            except Exception as e:
                log(f"listing {L['id']}: {e}")
        # fill gaps when the search page only gave us IDs
        if L.get("beds") is None and det.get("bedrooms") is not None:
            L["beds"] = det["bedrooms"]
        if L.get("pcm") is None and det.get("price_text"):
            v = parse_money(det["price_text"])
            if v:
                L["pcm"] = round(v * 52 / 12) if "pw" in det["price_text"].lower() else round(v)
        if not L.get("address") and det.get("address"):
            L["address"] = det["address"]
        if not L.get("image") and det.get("image"):
            L["image"] = det["image"]
        if det.get("let_agreed"):
            log(f"skip {L['id']} ({area}): let agreed")
            stats["skipped"] += 1
            continue
        reason = basic_reject_reason(cfg, L)
        if reason:
            log(f"skip {L['id']} ({area}): {reason}")
            stats["skipped"] += 1
            continue

        sqm, source = size_sqm(L, det)
        verdict = classify_size(cfg, sqm, source)
        if verdict == "small":
            log(f"skip {L['id']} ({area}): {sqm:.0f} m² stated")
            stats["skipped"] += 1
            continue

        title, message = build_message(cfg, area, L, det, sqm, source, verdict)
        notifier(cfg, title, message, url=L["url"], image=L.get("image"),
                 priority=4 if verdict == "good" else 3, tags=["house"])
        stats["sent"] += 1
        log(f"SENT {title} -> {L['url']}")
        record_match({
            "found_at": now().isoformat(timespec="minutes"),
            "area": area,
            "price_pcm": L.get("pcm"),
            "beds": L.get("beds"),
            "sqm": round(sqm) if sqm else "",
            "size_source": source or "",
            "address": L.get("address", ""),
            "available": det.get("available", ""),
            "furnish": det.get("furnish", ""),
            "agent": L.get("agent", ""),
            "url": L["url"],
        })
    if blocked:
        raise Blocked(blocked)
    return stats


# ---------------------------------------------------------------- modes

def ensure_topic(cfg):
    if cfg.get("ntfy_topic"):
        return False
    if IN_GITHUB:
        sys.exit("No notification channel set. In the repository go to Settings > Secrets and variables > "
                 "Actions and add a secret called NTFY_TOPIC containing your ntfy channel name.")
    topic = "flatwatch-" + "".join(random.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(14))
    raw = load_json(CONFIG_PATH, {})
    raw["ntfy_topic"] = topic
    save_json(CONFIG_PATH, raw)
    cfg["ntfy_topic"] = topic
    print("\n" + "=" * 64)
    print(" New notification channel created.")
    print(" In the ntfy app on your phone, tap + and subscribe to:\n")
    print(f"     {topic}\n")
    print(" (Your partner can subscribe to the same name to get alerts too.)")
    print("=" * 64 + "\n")
    return True


def test_mode(cfg):
    if ensure_topic(cfg) and sys.stdin.isatty():
        input("Press Enter once you've subscribed in the ntfy app... ")
    print("Checking each area once (nothing is marked as seen)...\n")
    sample = None
    cert_errors = 0
    for area, loc in cfg["areas"].items():
        try:
            status, html = fetch(search_url(cfg, loc))
        except Exception as e:
            print(f"  {area:<16} network error: {e}")
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                cert_errors += 1
            continue
        if status != 200:
            print(f"  {area:<16} HTTP {status}" + ("  <- Rightmove is refusing requests" if status in (403, 429) else ""))
            continue
        listings = parse_search(html)
        ids_only = listings and listings[0].get("_ids_only")
        print(f"  {area:<16} {len(listings)} listings read" + ("  (IDs only - search data format not recognised)" if ids_only else ""))
        if not listings:
            print(f"                   saved page to {os.path.basename(save_debug('search_' + area.replace(' ', '_'), html))}")
        for L in listings[:3]:
            price = f"£{L['pcm']:,}" if L.get("pcm") else "£?"
            print(f"      {price:>7}  {L.get('beds', '?')} bed  {L.get('address', '')[:48]}  {L.get('size_text', '')}")
        if listings and sample is None:
            sample = (area, listings[0])
        time.sleep(random.uniform(2, 5))

    if not sample:
        if cert_errors:
            print("\nPython can't check secure connections on this Mac. Fix it by running this once:\n")
            print('    open "/Applications/Python %d.%d/Install Certificates.command"\n' % sys.version_info[:2])
            print("then run the test again.")
        elif any(f.startswith("debug_") for f in os.listdir(HERE)):
            print("\nCould not read any listings. Send the debug_*.html files to Claude so the parser can be fixed.")
        else:
            print("\nCould not read any listings. Copy the output above and send it to Claude.")
        return
    area, L = sample
    print(f"\nOpening one listing to test the detail reader: {L['url']}")
    try:
        status, html = fetch(L["url"])
    except Exception as e:
        status, html = 0, ""
        print(f"  network error: {e}")
    det = parse_details(html) if status == 200 else {}
    if status != 200:
        print(f"  HTTP {status}")
    elif not det.get("description"):
        print(f"  Could not read details (saved {os.path.basename(save_debug('listing', html))})")
    sqm, source = size_sqm(L, det)
    print(f"  size: {f'{sqm:.0f} m² ({source})' if sqm else 'not stated'}")
    print(f"  available: {det.get('available') or '-'}   furnishing: {det.get('furnish') or '-'}")
    print(f"  floorplans: {len(det.get('floorplans') or [])}   key features: {len(det.get('key_features') or [])}")
    title, message = build_message(cfg, area, L, det, sqm, source, classify_size(cfg, sqm, source))
    notify(cfg, "TEST · " + title, message, url=L["url"], image=L.get("image"), tags=["test_tube"])
    if IN_GITHUB:
        print("\nSent a test notification for that listing. If it arrived, the scheduled checks are ready.")
    else:
        print("\nSent a test notification for that listing. If it arrived, run:  python3 flat_watch.py")


def in_quiet_hours(cfg):
    q = cfg.get("pause_overnight") or {}
    a, b = q.get("from_hour"), q.get("to_hour")
    if a is None or b is None or a == b:
        return False
    h = now().hour
    return a <= h < b if a < b else (h >= a or h < b)


def maybe_daily_summary(cfg, state, sent_today):
    hour = cfg.get("daily_summary_hour")
    if hour is None:
        return
    today = now().date().isoformat()
    if "last_summary" not in state:  # first ever run: start counting from now
        state["last_summary"] = today
        return
    if state.get("last_summary") == today or now().hour < hour:
        return
    day = state.setdefault("day", {})
    msg = (f"Still watching {len(cfg['areas'])} areas. Since the last summary: "
           f"{day.get('checks', 0)} checks, {day.get('sent', 0)} matches sent, "
           f"{day.get('skipped', 0)} filtered out. {len(state.get('seen', {}))} listings seen in total.")
    notify(cfg, "Flat watch: daily check-in", msg, priority=2, tags=["white_check_mark"])
    state["last_summary"] = today
    state["day"] = {}


def check_once(cfg, state):
    """One check plus bookkeeping. Everything that must survive between runs lives in
    `state` (saved to seen.json), so this works both as a long-running loop and as a
    scheduled single run on GitHub. Returns the suggested wait in seconds."""
    base_wait = cfg["check_every_minutes"] * 60
    if in_quiet_hours(cfg):
        log("Overnight pause - no check.")
        return 15 * 60
    until = state.get("blocked_until")
    if until and time.time() < until:
        mins = int((until - time.time()) // 60) + 1
        log(f"Still backing off after Rightmove refused a request - next try in about {mins} min.")
        return until - time.time()

    try:
        stats = run_cycle(cfg, state)
    except Blocked as e:
        level = state.get("blocked_level", 0) + 1
        state["blocked_level"] = level
        wait = min(base_wait * (2 ** level), 90 * 60)
        state["blocked_until"] = time.time() + wait
        log(f"Blocked ({e}). Backing off for {wait // 60} min.")
        if level == 1:
            notify(cfg, "Flat watch paused",
                   f"Rightmove refused a request. Backing off and retrying in {wait // 60} min.",
                   priority=3, tags=["warning"])
        elif level == 5:
            notify(cfg, "Flat watch still blocked",
                   "Rightmove has refused every request for a few hours."
                   + (" It may be blocking GitHub's servers - running it on your Mac would avoid that."
                      if IN_GITHUB else " Try raising check_every_minutes in config.json."),
                   priority=4, tags=["warning"])
        return wait
    except Exception:
        log("Unexpected error:\n" + traceback.format_exc())
        return base_wait

    if state.get("blocked_level"):
        notify(cfg, "Flat watch resumed", "Rightmove is answering again.", priority=2)
    state.pop("blocked_level", None)
    state.pop("blocked_until", None)

    day = state.setdefault("day", {})
    day["checks"] = day.get("checks", 0) + 1
    day["sent"] = day.get("sent", 0) + stats["sent"]
    day["skipped"] = day.get("skipped", 0) + stats["skipped"]
    if "seeded" in stats:
        log(f"Recorded {stats['seeded']} listings already on the market in newly watched areas. "
            "From now on you'll only hear about new ones.")
        notify(cfg, "Flat watch is running",
               f"Recorded {stats['seeded']} listings already on the market. "
               "You'll get a notification for each new one that fits.", tags=["eyes"])
    log(f"Check done: {stats['pages_ok']} areas read, {stats['new']} new, "
        f"{stats['sent']} sent, {stats['skipped']} filtered.")

    if stats["pages_ok"] == 0:
        state["failed_checks"] = state.get("failed_checks", 0) + 1
        if state["failed_checks"] == 3:
            where = ("open the latest run in the repository's Actions tab, download the debug files "
                     "and send them to Claude." if IN_GITHUB else
                     "send the debug_search.html file to Claude.")
            notify(cfg, "Flat watch can't read Rightmove",
                   "Three checks in a row failed. The site layout may have changed - " + where,
                   priority=4, tags=["warning"])
    else:
        state.pop("failed_checks", None)

    maybe_daily_summary(cfg, state, 0)
    return base_wait


def watch(cfg, once=False):
    state = load_json(STATE_PATH, {})
    if ensure_topic(cfg):
        notify(cfg, "Flat watch connected", "Notifications are working. Matches will arrive here.",
               tags=["white_check_mark"])
    log(f"Watching {', '.join(cfg['areas'])} | £{cfg['min_price']}-{cfg['max_price']} pcm | "
        f"{cfg['min_beds']}-{cfg['max_beds']} beds | "
        + ("single check" if once else f"every ~{cfg['check_every_minutes']} min"))
    while True:
        wait = check_once(cfg, state)
        save_json(STATE_PATH, state)
        if once:
            return
        time.sleep(max(60, int(wait * random.uniform(0.8, 1.25))))


def main():
    ap = argparse.ArgumentParser(description="Watch Rightmove for new rentals.")
    ap.add_argument("--test", action="store_true", help="one pass that shows what is read, plus a test notification")
    ap.add_argument("--once", action="store_true", help="run a single check then exit")
    ap.add_argument("--reset", action="store_true", help="forget all listings seen so far")
    args = ap.parse_args()
    cfg = load_config()
    if args.reset:
        if os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
        print("Forgot all seen listings. The next run will re-record what's on the market.")
        return
    if args.test:
        test_mode(cfg)
        return
    try:
        watch(cfg, once=args.once)
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
