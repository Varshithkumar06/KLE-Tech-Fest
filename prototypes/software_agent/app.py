import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

PRICE_PATTERN = re.compile(r"(?:[$€£₹]\s?\d[\d,]*(?:\.\d+)?|\b(?:USD|usd|EUR|eur|GBP|gbp|INR|inr)\s*\d[\d,]*(?:\.\d+)?)")
TEAM_RANGE_PATTERN = re.compile(r"\b(\d[\d,]*)\s*(?:-|–|to)\s*(\d[\d,]*)\s*(?:employees|people|team members|staff)\b", re.I)
TEAM_SINGLE_PATTERN = re.compile(r"\b(?:over|about|approximately|around|more than)?\s*(\d[\d,]*)\+?\s*(?:employees|people|team members|staff)\b", re.I)


def tinyfish_command():
    explicit = os.path.join(os.environ.get("APPDATA", ""), "npm", "tinyfish.cmd")
    return explicit if os.path.exists(explicit) else (shutil.which("tinyfish.cmd") or shutil.which("tinyfish"))


def split_names(value: str) -> list[str]:
    names = [part.strip() for part in re.split(r"[,;\n]+", value) if part.strip()]
    return list(dict.fromkeys(names))[:12]


def extract_prices(text: str) -> list[str]:
    return list(dict.fromkeys(match.group(0).rstrip(".,") for match in PRICE_PATTERN.finditer(text or "")))


def parse_price(price: str):
    amount = re.search(r"\d[\d,]*(?:\.\d+)?", price or "")
    if not amount:
        return None
    lower = price.lower()
    if "₹" in price or lower.startswith("inr"):
        currency, symbol = "INR", "₹"
    elif "$" in price or lower.startswith("usd"):
        currency, symbol = "USD", "$"
    elif "€" in price or lower.startswith("eur"):
        currency, symbol = "EUR", "€"
    elif "£" in price or lower.startswith("gbp"):
        currency, symbol = "GBP", "£"
    else:
        return None
    return currency, symbol, float(amount.group(0).replace(",", ""))


def format_amount(symbol: str, value: float) -> str:
    return f"{symbol}{value:,.2f}".rstrip("0").rstrip(".")


def extract_team_ranges(text: str) -> list[dict]:
    found = []
    for match in TEAM_RANGE_PATTERN.finditer(text or ""):
        low, high = (int(value.replace(",", "")) for value in match.groups())
        if high >= low:
            found.append({"low": low, "high": high, "label": f"{low:,}–{high:,}"})
    if not found:
        for match in TEAM_SINGLE_PATTERN.finditer(text or ""):
            value = int(match.group(1).replace(",", ""))
            found.append({"low": value, "high": value, "label": f"{value:,}"})
    return found


def classify_source(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    if any(domain in host for domain in ("linkedin.", "crunchbase.", "tracxn.", "zoominfo.")):
        return "company profile"
    if any(domain in host for domain in ("g2.", "capterra.", "getapp.")):
        return "review directory"
    return "vendor or source page"


def price_summary(results: list[dict]) -> list[dict]:
    buckets = {}
    for result in results:
        for raw in result.get("prices", []):
            parsed = parse_price(raw)
            if parsed:
                currency, symbol, value = parsed
                buckets.setdefault(currency, {"symbol": symbol, "values": []})["values"].append(value)
    output = []
    for currency, bucket in buckets.items():
        values = sorted(set(bucket["values"]))
        if len(values) >= 3:
            middle = values[len(values) // 2]
            values = [value for value in values if middle / 15 <= value <= middle * 15] or values
        low, high = values[0], values[-1]
        label = format_amount(bucket["symbol"], low)
        if high != low:
            label += f"–{format_amount(bucket['symbol'], high)}"
        output.append({"currency": currency, "low": low, "high": high, "count": len(values), "label": label})
    return sorted(output, key=lambda item: item["low"])


def team_summary(results: list[dict]):
    ranges = [team for result in results for team in result.get("team_ranges", [])]
    if not ranges:
        return None
    lows, highs = sorted(item["low"] for item in ranges), sorted(item["high"] for item in ranges)
    low, high = lows[len(lows) // 2], highs[len(highs) // 2]
    return {"low": low, "high": high, "label": f"{low:,}" if low == high else f"{low:,}–{high:,}", "sources": len(ranges)}


def software_search(name: str) -> dict:
    tinyfish = tinyfish_command()
    if not tinyfish:
        raise RuntimeError("TinyFish CLI is not available. Restart the terminal after installing it.")
    pricing_query = f"{name} software pricing plans per user per month"
    team_query = f"{name} company employees team size LinkedIn"

    def search(query):
        completed = subprocess.run([tinyfish, "search", "query", query], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=True)
        raw = (completed.stdout or completed.stderr or "").strip()
        if not raw:
            raise RuntimeError("TinyFish returned no search data.")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = json.loads([line.strip() for line in raw.splitlines() if line.strip()][-1])
        return payload.get("results", payload if isinstance(payload, list) else [])

    pricing_results = search(pricing_query)
    team_results = search(team_query)
    results, seen = [], set()
    for item in pricing_results + team_results:
        if not isinstance(item, dict):
            continue
        key = item.get("url", item.get("link", item.get("title", "")))
        if key not in seen:
            results.append(item)
            seen.add(key)
    excluded = re.compile(r"\b(job|jobs|hiring|careers|salary|course|tutorial|crack|download free)\b", re.I)
    results = [item for item in results if isinstance(item, dict) and not excluded.search(f"{item.get('title', '')} {item.get('snippet', '')}")]
    for item in results:
        text = f"{item.get('title', '')} {item.get('snippet', item.get('description', ''))}"
        item["prices"] = extract_prices(text)
        item["team_ranges"] = extract_team_ranges(text)
        item["source_type"] = classify_source(item.get("url", item.get("link", "")))
    return {"mode": "software", "title": f"Software estimate for {name}", "query": name, "search_query": {"pricing": pricing_query, "team_size": team_query}, "status": 200, "pricing_summary": price_summary(results), "team_summary": team_summary(results), "results": results, "account": "TinyFish", "note": "Pricing data was collected from publicly available online sources. The reported price ranges represent observed listings and may vary by provider, product version, licensing model, and collection date. Prices have not all been independently verified and should be treated as indicative reference values rather than confirmed market prices."}


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/estimate")
def estimate():
    try:
        value = request.json.get("software", "") if request.is_json else request.form.get("software", "")
        names = split_names(value)
        if not names:
            return jsonify({"error": "Enter at least one software name."}), 400
        if len(names) == 1:
            return jsonify(software_search(names[0]))
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {pool.submit(software_search, name): name for name in names}
            items = []
            for future in as_completed(futures):
                name = futures[future]
                try:
                    items.append({"software": name, "data": future.result()})
                except Exception as exc:
                    items.append({"software": name, "error": str(exc)})
        items.sort(key=lambda item: names.index(item["software"]))
        return jsonify({"mode": "batch", "title": "Software cost and team estimates", "items": items})
    except Exception as exc:
        app.logger.exception("Software estimate failed")
        return jsonify({"error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
