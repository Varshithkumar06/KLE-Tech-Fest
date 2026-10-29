import os
import json
import re
import shutil
import socket
import statistics
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote_plus, urlparse

import requests
import websocket
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)


def load_local_env():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_local_env()
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

PROFILE_DIR = os.environ.get(
    "ANTIGRAVITY_PROFILE",
    r"C:\Users\HP\.gemini\antigravity-browser-profile",
)
CHROME_PATHS = [
    os.environ.get("CHROME_PATH", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]
DEBUG_URL = os.environ.get("CHROME_DEBUG_URL", "http://127.0.0.1:9222")

SPEC_REQUIREMENTS = {
    "control computer": "model or CPU, RAM, storage, and industrial/consumer requirement",
    "camera": "camera model, sensor/resolution, interface (USB/CSI/MIPI), and lens requirement",
    "framegrabber": "camera interface (USB3, GigE, Camera Link, CoaXPress), channel count, and model",
    "serial port": "adapter type (USB/RS232/RS485/CAN), connector, and port count",
    "interface board": "protocol/interface type and the target device or controller model",
    "control-side mote": "wireless technology (LoRa/Zigbee/Wi-Fi), frequency, and module/model",
    "vehicle-side mote": "wireless technology (LoRa/Zigbee/Wi-Fi), frequency, and module/model",
    "motor board": "motor type, voltage/current rating, and a driver/controller model",
    "steering motor": "motor type, voltage, torque, RPM, and model",
    "driving motor": "motor type, voltage, torque, RPM, and model",
}

# ---------------------------------------------------------------------------
# Hardware sources. Edit these lists freely - they drive every search.
# ---------------------------------------------------------------------------
# Dedicated electronics / industrial hardware sellers (Indian prices first).
TRUSTED_STORES = [
    "robu.in", "sunrom.com", "thinkrobotics.com", "electronicscomp.com",
    "evelta.com", "rarecomponents.com", "robokits.co.in", "quartzcomponents.com",
    "in.element14.com", "mouser.in", "in.rsdelivers.com", "digikey.in",
    "moglix.com", "industrybuying.com", "e-consystems.com",
]
MARKETPLACES = ["amazon.in", "flipkart.com", "indiamart.com", "alibaba.com", "ebay.", "amazon."]
PRICE_REFERENCE = ["buyhatke", "pricehistory", "pricebefore"]
NON_SELLER = ["youtube.", "facebook.", "instagram.", "quora.", "wikipedia.", "reddit."]
TENDER_SITES = ["eprocure.gov.in", "gem.gov.in", "etenders.gov.in"]
# The CPPP link you pasted contains a one-time session token. Only the home
# domain is used for searching; the session link itself will expire.
EPROCURE_HOME = "https://eprocure.gov.in/eprocure/app"


def make_search_url(value: str) -> tuple[str, str]:
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a product name or URL.")
    if re.match(r"^https?://", value, re.I):
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Use a complete http:// or https:// URL.")
        return value, "url"
    return f"https://www.google.com/search?q={quote_plus(value)}", "product"


def chrome_path():
    return next((p for p in CHROME_PATHS if p and os.path.exists(p)), None)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def rendered_page(url: str) -> dict:
    """Run an isolated, headless Chrome session and read rendered DOM text."""
    chrome = chrome_path()
    port = free_port()
    temp_profile = tempfile.mkdtemp(prefix="antigravity-guest-")
    process = None
    try:
        endpoint = DEBUG_URL.rstrip("/")
        try:
            requests.get(f"{endpoint}/json/version", timeout=0.5).raise_for_status()
        except requests.RequestException:
            if not chrome:
                raise RuntimeError("Chrome was not found and the remote debugging endpoint is unavailable. Set CHROME_PATH to chrome.exe.")
            process = subprocess.Popen(
                [chrome, "--headless=new", "--incognito", "--no-first-run", "--no-default-browser-check",
                 "--disable-gpu", "--disable-crash-reporter", "--disable-background-networking",
                 "--remote-allow-origins=*", f"--user-data-dir={temp_profile}",
                 "--remote-debugging-address=127.0.0.1", f"--remote-debugging-port={port}", "about:blank"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            endpoint = f"http://127.0.0.1:{port}"
        version_url = f"{endpoint}/json/version"
        for _ in range(100):
            try:
                requests.get(version_url, timeout=0.3).json()
                break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            raise RuntimeError("Chrome did not start its DevTools backend.")
        target = requests.put(f"{endpoint}/json/new", params={"url": url}, timeout=5).json()
        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)
        counter = 0

        def call(method, params=None):
            nonlocal counter
            counter += 1
            ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))
            while True:
                message = json.loads(ws.recv())
                if message.get("id") == counter:
                    return message

        call("Runtime.enable")
        call("Page.enable")
        try:
            call("Page.navigate", {"url": url})
        except websocket.WebSocketTimeoutException:
            pass
        time.sleep(2)
        result = call("Runtime.evaluate", {"expression": "(() => { const jsonld = [...document.querySelectorAll('script[type=\\\"application/ld+json\\\"]')].map(s => { try { return JSON.parse(s.textContent); } catch (_) { return null; } }).filter(Boolean); const meta = [...document.querySelectorAll('meta[name],meta[property]')].reduce((a,m) => { a[m.name || m.getAttribute('property')] = m.content || ''; return a; }, {}); return {title: document.title, text: document.body ? document.body.innerText : '', url: location.href, source: document.documentElement.outerHTML, jsonld, meta}; })()", "returnByValue": True})
        value = result.get("result", {}).get("result", {}).get("value", {})
        ws.close()
        text = "\n".join(line.strip() for line in value.get("text", "").splitlines() if line.strip())
        source = value.get("source", "")
        return {"url": value.get("url", url), "title": value.get("title", url), "status": 200,
                "text": text[:20000], "truncated": len(text) > 20000,
                "structured_data": value.get("jsonld", []), "metadata": value.get("meta", {}),
                "source_html": source[:50000], "source_truncated": len(source) > 50000,
                "browser_opened": True, "account": "Connected Chrome" if endpoint == DEBUG_URL.rstrip("/") else "Isolated"}
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(temp_profile, ignore_errors=True)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------
PRICE_PATTERN = re.compile(
    r"(?:[$€£₹]\s?\d[\d,]*(?:\.\d+)?(?:\s*(?:lakh|lac|crore))?|\b(?:USD|usd|EUR|eur|GBP|gbp|INR|inr)\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:lakh|lac|crore))?|\b(?:Rs|rs)\.?\s*\d[\d,]*(?:\.\d+)?(?:\s*(?:lakh|lac|crore))?)",
)


def extract_prices(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0).rstrip(".,") for m in PRICE_PATTERN.finditer(text or "")))


def extract_price(text: str):
    prices = extract_prices(text)
    return prices[0] if prices else None


def parse_price(price: str):
    if not price:
        return None
    value_match = re.search(r"\d[\d,]*(?:\.\d+)?", price)
    if not value_match:
        return None
    lowered = price.lower()
    if "₹" in price or lowered.startswith(("rs", "inr")):
        currency, symbol = "INR", "₹"
    elif "$" in price or lowered.startswith("usd"):
        currency, symbol = "USD", "$"
    elif "€" in price or lowered.startswith("eur"):
        currency, symbol = "EUR", "€"
    elif "£" in price or lowered.startswith("gbp"):
        currency, symbol = "GBP", "£"
    else:
        return None
    value = float(value_match.group(0).replace(",", ""))
    if re.search(r"\b(?:lakh|lac)\b", lowered):
        value *= 100_000
    elif re.search(r"\bcrore\b", lowered):
        value *= 10_000_000
    return {"currency": currency, "symbol": symbol, "value": value}


def format_amount(symbol: str, value: float) -> str:
    return f"{symbol}{value:,.2f}".rstrip("0").rstrip(".")


def comparable_prices(values: list[float]) -> list[float]:
    """Largest cluster of prices within a 5x spread (tighter than before, so
    bulk quotes / MRP typos do not stretch the range)."""
    values = sorted(set(values))
    if len(values) < 2:
        return values
    best = []
    for start, low in enumerate(values):
        cluster = [v for v in values[start:] if v / max(low, 0.01) <= 5]
        if len(cluster) > len(best):
            best = cluster
    return best or [values[0]]


def host_of(url: str) -> str:
    return urlparse(url or "").netloc.lower()


def classify_price_source(url: str) -> str:
    host = host_of(url)
    if any(h in host for h in TENDER_SITES):
        return "govt tender"
    if any(h in host for h in TRUSTED_STORES):
        return "trusted store"
    if any(h in host for h in PRICE_REFERENCE):
        return "price reference"
    if any(h in host for h in MARKETPLACES):
        return "marketplace"
    if any(h in host for h in NON_SELLER):
        return "non-seller source"
    return "direct seller"


SOURCE_RANK = {
    "trusted store": 0,
    "direct seller": 1,
    "marketplace": 2,
    "govt tender": 3,
    "price reference": 4,
    "non-seller source": 5,
}
RETAIL_SOURCES = {"trusted store", "direct seller", "marketplace"}

EXCLUDED = re.compile(
    r"\b(card|cards|game|games|toy|playing|deck|market size|market report|forecast|research report|"
    r"buy electronics components|components online|best online stores)\b", re.I)


def annotate(item: dict, provider: str) -> dict:
    """Give every provider's result the same shape."""
    url = item.get("url") or item.get("link") or item.get("product_link") or ""
    text = f"{item.get('title', '')} {item.get('snippet', item.get('description', ''))} {item.get('price', '') or ''}"
    prices = item.get("prices") or extract_prices(text)
    # Structured price from SerpAPI shopping beats text scraping.
    if item.get("extracted_price") and not prices:
        prices = [f"₹{item['extracted_price']}"]
    item["url"] = url
    item["provider"] = provider
    item["prices"] = prices
    item["price"] = prices[0] if prices else None
    item["price_source"] = classify_price_source(url) if provider != "serpapi-shopping" else (
        classify_price_source(url) if classify_price_source(url) != "direct seller" else "marketplace")
    parsed = parse_price(item["price"])
    item["price_value"] = parsed["value"] if parsed else None
    item["price_currency"] = parsed["currency"] if parsed else None
    return item


def build_price_summary(results: list[dict], sources=RETAIL_SOURCES) -> list[dict]:
    buckets = {}
    for item in results:
        if item.get("price_source") not in sources:
            continue
        for raw_price in item.get("prices", []):
            parsed = parse_price(raw_price)
            if parsed:
                buckets.setdefault(parsed["currency"], {"symbol": parsed["symbol"], "values": []})["values"].append(parsed["value"])
    summaries = []
    for currency, bucket in buckets.items():
        values = comparable_prices(bucket["values"])
        if not values:
            continue
        label = format_amount(bucket["symbol"], values[0])
        if values[-1] != values[0]:
            label += f"–{format_amount(bucket['symbol'], values[-1])}"
        summaries.append({
            "currency": currency, "low": values[0], "high": values[-1],
            "median": statistics.median(values), "count": len(values), "label": label,
        })
    return sorted(summaries, key=lambda s: s["low"])


def pick_best_offer(results: list[dict], summary: list[dict]):
    """Cheapest in-range retail offer, preferring trusted stores on ties."""
    if not summary:
        return None
    main = summary[0]  # lowest-priced currency cluster (INR first in practice)
    pool = [r for r in results
            if r.get("price_source") in RETAIL_SOURCES and r.get("price_currency") == main["currency"]
            and r.get("price_value") is not None and main["low"] <= r["price_value"] <= main["high"]]
    if not pool:
        return None
    best = min(pool, key=lambda r: (r["price_value"], SOURCE_RANK.get(r["price_source"], 9)))
    return {"title": best.get("title"), "price": best.get("price"), "value": best["price_value"],
            "currency": best["price_currency"], "url": best["url"], "source": best["price_source"],
            "provider": best["provider"]}


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------
def find_tinyfish():
    explicit = os.path.join(os.environ.get("APPDATA", ""), "npm", "tinyfish.cmd")
    path = explicit if os.path.exists(explicit) else (shutil.which("tinyfish.cmd") or shutil.which("tinyfish"))
    if not path:
        raise RuntimeError("TinyFish CLI not found on PATH. Restart the terminal/server.")
    return path


def tinyfish_raw(search_query: str) -> list[dict]:
    completed = subprocess.run(
        [find_tinyfish(), "search", "query", search_query],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=True,
    )
    raw = (completed.stdout or completed.stderr or "").strip()
    if not raw:
        raise RuntimeError("TinyFish returned no search data.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = json.loads([l.strip() for l in raw.splitlines() if l.strip()][-1])
    return data.get("results", data if isinstance(data, list) else [])


def serpapi_call(params: dict) -> dict:
    if not SERPAPI_KEY:
        raise RuntimeError("SERPAPI_KEY is not configured.")
    response = requests.get("https://serpapi.com/search.json",
                            params={**params, "api_key": SERPAPI_KEY}, timeout=25)
    response.raise_for_status()
    data = response.json()
    if data.get("error"):
        raise RuntimeError(data["error"])
    return data


def tavily_call(query: str, max_results: int = 10) -> dict:
    """Call the Tavily API and return parsed JSON."""
    api_key = TAVILY_API_KEY
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured.")
    url = "https://api.tavily.com/search"
    payload = {"query": query, "api_key": api_key, "max_results": max_results}
    response = requests.post(url, json=payload, timeout=25)
    response.raise_for_status()
    return response.json()

def site_clause(domains: list[str]) -> str:
    return "(" + " OR ".join(f"site:{d}" for d in domains) + ")"


def provider_tavily_general(item: str, category: bool):
    q = f"{item} electronics hardware price India" if category else f"{item} price India"
    data = tavily_call(q)
    return [annotate(r, "tavily") for r in data.get("results", []) if isinstance(r, dict)]


def provider_tinyfish_stores(item: str, category: bool):
    # Two store batches so the query is not too long for the search engine.
    out = []
    for batch in (TRUSTED_STORES[:7], TRUSTED_STORES[7:]):
        q = f"{item} price {site_clause(batch)}"
        out += [annotate(r, "tinyfish-stores") for r in tinyfish_raw(q) if isinstance(r, dict)]
    return out


def provider_serp_shopping(item: str, category: bool):
    data = serpapi_call({"engine": "google_shopping", "q": item, "gl": "in", "hl": "en", "num": 20})
    out = []
    for r in data.get("shopping_results", []):
        out.append(annotate({
            "title": r.get("title", ""),
            "snippet": f"{r.get('source', '')} {r.get('delivery', '') or ''}".strip(),
            "url": r.get("product_link") or r.get("link") or "",
            "price": r.get("price"),
            "extracted_price": r.get("extracted_price"),
        }, "serpapi-shopping"))
    return out


def provider_serp_stores(item: str, category: bool):
    out = []
    for batch in (TRUSTED_STORES[:7], TRUSTED_STORES[7:]):
        data = serpapi_call({"engine": "google", "q": f"{item} price {site_clause(batch)}",
                             "gl": "in", "hl": "en", "num": 10})
        out += [annotate(r, "serpapi-stores") for r in data.get("organic_results", [])]
    return out


def provider_tenders(item: str, category: bool):
    """Government award/tender data (CPPP eprocure, GeM). These are bulk/award
    prices, so they are kept separate from the retail price range."""
    q = f'{item} (AOC OR "award of contract" OR tender OR "rate contract") {site_clause(TENDER_SITES)}'
    out = []
    if SERPAPI_KEY:
        data = serpapi_call({"engine": "google", "q": q, "gl": "in", "num": 10})
        out += [annotate(r, "serpapi-tender") for r in data.get("organic_results", [])]
    elif TAVILY_API_KEY:
        data = tavily_call(q)
        out += [annotate(r, "tavily-tender") for r in data.get("results", []) if isinstance(r, dict)]
    else:
        out += [annotate(r, "tinyfish-tender") for r in tinyfish_raw(q) if isinstance(r, dict)]
    for r in out:
        r["price_source"] = "govt tender"
    return out


def provider_tavily_tender(item: str, category: bool):
    """Tavily-based tender search fallback."""
    q = f'{item} (AOC OR "award of contract" OR tender OR "rate contract") {site_clause(TENDER_SITES)}'
    data = tavily_call(q)
    return [annotate(r, "tavily-tender") for r in data.get("results", []) if isinstance(r, dict)]


PROVIDERS = {
    "tavily": provider_tavily_general,
    "serpapi_shopping": provider_serp_shopping,
    "serpapi_stores": provider_serp_stores,
    "tenders": provider_tenders,
    "tavily_tender": provider_tavily_tender,
}


def search_item(query: str) -> dict:
    """Run every provider IN PARALLEL, merge, de-duplicate, rank, summarise."""
    normalized = query.strip().lower()
    category = normalized in SPEC_REQUIREMENTS
    raw, status = [], {}
    with ThreadPoolExecutor(max_workers=len(PROVIDERS)) as pool:
        futures = {pool.submit(provider, normalized, category): name for name, provider in PROVIDERS.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
                if result:
                    raw.append(result)
            except Exception as e:
                logger.error(f"Provider {name} failed: {e}")
        jobs = {pool.submit(fn, query, category): name for name, fn in PROVIDERS.items()}
        for job in as_completed(jobs):
            name = jobs[job]
            try:
                found = job.result()
                raw += found
                status[name] = {"ok": True, "count": len(found)}
            except Exception as exc:
                status[name] = {"ok": False, "error": str(exc)[:300]}

    # De-duplicate by URL (keep the entry that has a price), drop junk.
    merged = {}
    for r in raw:
        if EXCLUDED.search(f"{r.get('title', '')} {r.get('snippet', '')}"):
            continue
        key = (r["url"].split("#")[0].rstrip("/")) or f"{r.get('title')}-{r.get('provider')}"
        if key not in merged or (r["price"] and not merged[key]["price"]):
            merged[key] = r
    results = list(merged.values())
    results.sort(key=lambda r: (r["price"] is None, SOURCE_RANK.get(r["price_source"], 9),
                                r.get("price_value") or 1e12))

    summary = build_price_summary(results)
    tender_summary = build_price_summary(results, sources={"govt tender"})
    best = pick_best_offer(results, summary)
    if not any(s["ok"] for s in status.values()):
        raise RuntimeError("All providers failed: " + "; ".join(f"{k}: {v.get('error')}" for k, v in status.items()))
    return {
        "query": query, "mode": "multi", "title": f"Search results for {query}", "status": 200, "url": "",
        "results": results, "price_summary": summary, "best_offer": best,
        "tender_summary": tender_summary, "providers": status,
        "shopping_results": [], "knowledge_graph": {},
        "text": "\n".join(f"{r.get('title', '')}\nPrice: {r.get('price') or 'Not found'} ({r['price_source']}, {r['provider']})\n{r.get('snippet', '')}\n{r['url']}" for r in results),
        "browser_opened": False, "account": "SerpAPI + TinyFish",
        "estimate_note": (f"Category estimate only. For an exact quote add: {SPEC_REQUIREMENTS[normalized]}."
                          if category else ""),
    }


def split_product_list(value: str) -> list[str]:
    parts = re.split(r"[,;\n]+", value)
    cleaned = []
    for part in parts:
        item = re.sub(r"\s+price\s*$", "", part.strip(), flags=re.I).rstrip(": ")
        if item and len(item) >= 2 and item.lower() not in {"and", "or"}:
            cleaned.append(item)
    return list(dict.fromkeys(cleaned))[:12]


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/search")
def search():
    try:
        value = request.json.get("url", "") if request.is_json else request.form.get("url", "")
        if not re.match(r"^https?://", value.strip(), re.I):
            items = split_product_list(value)
            if len(items) > 1:
                grouped = []
                with ThreadPoolExecutor(max_workers=3) as pool:
                    jobs = {pool.submit(search_item, item): item for item in items}
                    for job in as_completed(jobs):
                        item = jobs[job]
                        try:
                            grouped.append({"item": item, "data": job.result()})
                        except Exception as exc:
                            grouped.append({"item": item, "error": str(exc)})
                grouped.sort(key=lambda e: items.index(e["item"]))
                summary = [{"item": e["item"],
                            "ranges": e.get("data", {}).get("price_summary", []),
                            "best_offer": e.get("data", {}).get("best_offer"),
                            "tender_ranges": e.get("data", {}).get("tender_summary", [])}
                           for e in grouped]
                return jsonify({"mode": "batch", "title": "Hardware price results", "status": 200,
                                "items": grouped, "price_summary": summary})
        url, mode = make_search_url(value)
        if mode == "product":
            return jsonify(search_item(value))
        result = rendered_page(url)  # also works for a pasted eprocure/seller URL
        result["query"] = value
        result["mode"] = mode
        return jsonify(result)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except requests.RequestException as exc:
        return jsonify({"error": f"Could not fetch the URL: {exc}"}), 502
    except Exception as exc:
        app.logger.exception("Search failed")
        return jsonify({"error": f"Search failed: {exc}"}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")),
            debug=os.environ.get("FLASK_DEBUG", "0") == "1", use_reloader=False)