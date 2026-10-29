import sys
if hasattr(sys, "stdout") and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys, "stderr") and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import pypdf
"""
KLE Fest 2 - Unified Project Cost Estimator Pipeline
=====================================================
Flow:
  1. User uploads an image OR types a text description (or both)
  2. Gemini Agent thoroughly breaks down project into:
     - Real hardware products & components with suggested models
     - Software modules, AI models & firmware
     - Engineering team roles with specific responsibilities
  3. HW Agent + SW Agent run IN PARALLEL (TinyFish live prices + effort model)
  4. Gemini synthesises final cost prediction in INR
  5. Step-by-step SSE stream to frontend
  6. Chatbot for follow-up Q&A (cached context)
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import joblib
import numpy as np
from flask import Flask, Response, jsonify, render_template, request, stream_with_context

try:
    from .config import ROOT_DIR, ESTIMO_DIR, DATA_DIR, CACHE_DIR, get_nvidia_api_key, NEMOTRON_MODEL, PORT
    from .local_model import HOURS_PER_PERSON_MONTH, predict_ucp_effort
    from . import vision as _img
except (ImportError, ValueError):
    from estimo.config import ROOT_DIR, ESTIMO_DIR, DATA_DIR, CACHE_DIR, get_nvidia_api_key, NEMOTRON_MODEL, PORT
    from estimo.local_model import HOURS_PER_PERSON_MONTH, predict_ucp_effort
    from estimo import vision as _img
_GEMINI_CLIENT = None

def _gemini():
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        _GEMINI_CLIENT = _img._client()
    return _GEMINI_CLIENT

def _gemini_text(prompt: str) -> str:
    """Call Gemini text via standard generate_content API with interactions fallback."""
    client = _gemini()
    try:
        resp = client.models.generate_content(model=_img.MODEL, contents=prompt)
        if resp.text:
            return resp.text
    except Exception as e:
        logging.warning("generate_content failed (%s), falling back to interactions...", e)
    result = _img._create(client, model=_img.MODEL, input=prompt)
    return result.output_text

import json_repair

def robust_json_parse(raw_text: str):
    """Robustly parse JSON from LLM output, repairing unescaped quotes, trailing commas, or formatting glitches."""
    if not raw_text or not str(raw_text).strip():
        raise ValueError("Empty response received from LLM.")
    
    cleaned = re.sub(r"```(?:json)?", "", str(raw_text)).strip().replace("```", "")
    s = cleaned.find("{")
    e = cleaned.rfind("}")
    if s == -1 or e <= s:
        s = cleaned.find("[")
        e = cleaned.rfind("]")
        if s == -1 or e <= s:
            try:
                repaired = json_repair.repair_json(raw_text, return_objects=True)
                if isinstance(repaired, (dict, list)):
                    return repaired
            except Exception:
                pass
            raise ValueError(f"No JSON object found in response: {raw_text[:120]}")

    substr = cleaned[s:e + 1]
    try:
        return json.loads(substr)
    except Exception:
        pass

    try:
        repaired = json_repair.repair_json(substr, return_objects=True)
        if isinstance(repaired, (dict, list)):
            return repaired
    except Exception:
        pass

    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(cleaned[s:])
        if isinstance(obj, (dict, list)):
            return obj
    except Exception:
        pass

    try:
        repaired = json_repair.repair_json(raw_text, return_objects=True)
        if isinstance(repaired, (dict, list)):
            return repaired
    except Exception as exc:
        raise ValueError(f"Failed to parse LLM JSON: {exc}")
    
    raise ValueError("Could not extract valid JSON from LLM response.")

from openai import OpenAI

_NVIDIA_CLIENT = None
NEMOTRON_MODEL = "nvidia/nemotron-3-ultra-550b-a55b"

def _get_nvidia_client():
    global _NVIDIA_CLIENT
    if _NVIDIA_CLIENT is None:
        nvkey = get_nvidia_api_key()
        if nvkey:
            _NVIDIA_CLIENT = OpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=nvkey)
    return _NVIDIA_CLIENT

def _call_llm(prompt: str) -> str:
    """Calls Nemotron LLM as requested, with automatic fallback to Gemini."""
    client = _get_nvidia_client()
    if client:
        try:
            resp = client.chat.completions.create(
                model=NEMOTRON_MODEL,
                messages=[
                    {"role": "system", "content": "You are a precise estimation engine. Output ONLY raw RFC 8259 JSON matching the requested structure. Never include markdown fences, thinking reasoning, or explanatory text."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=2048,
            )
            out = resp.choices[0].message.content
            if out and len(out.strip()) > 5:
                try:
                    robust_json_parse(out)
                    return out
                except Exception:
                    logging.warning("Nemotron returned thinking/non-JSON, falling back to Gemini.")
        except Exception as e:
            logging.warning("Nemotron call failed (%s), falling back to Gemini.", e)
    return _gemini_text(prompt)


# --------------------------------------------------- Trained effort model ----
# The model is loaded lazily through local_model.py from Models/models/registry.json.
# This prevents the old, missing china_model.joblib placeholder from being used.
HOURS_PER_MONTH = HOURS_PER_PERSON_MONTH
ROLE_HOURS = json.loads((DATA_DIR / "role_hours.json").read_text(encoding="utf-8"))
WORK_ITEMS_FP = json.loads((DATA_DIR / "work_items_fp.json").read_text(encoding="utf-8"))

# ------------------------------------------------------------------ Cache ----
CACHE_DIR.mkdir(exist_ok=True)
_chat_cache: dict = {}

# ----------------------------------------------------------------- Flask ----
app = Flask(__name__, template_folder=str(ESTIMO_DIR / "templates"), static_folder=str(ESTIMO_DIR / "static"))
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024
logging.basicConfig(level=logging.INFO)

# ====================================================================
#  HELPER: TinyFish CLI
# ====================================================================
PRICE_PATTERN = re.compile(
    r"(?:[$\u20b9\u20ac\xa3]\s?\d[\d,]*(?:\.\d+)?|\b(?:USD|usd|EUR|eur|GBP|gbp|INR|inr)\s*\d[\d,]*(?:\.\d+)?)"
)

def _tinyfish_cmd():
    explicit = os.path.join(os.environ.get("APPDATA", ""), "npm", "tinyfish.cmd")
    return explicit if os.path.exists(explicit) else (
        shutil.which("tinyfish.cmd") or shutil.which("tinyfish")
    )

def _tavily_search(query: str, max_results: int = 6) -> list:
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return []
    try:
        import requests
        url = "https://api.tavily.com/search"
        payload = {"query": query, "api_key": api_key, "max_results": max_results}
        resp = requests.post(url, json=payload, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            return [
                {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
                for r in data.get("results", []) if isinstance(r, dict)
            ]
    except Exception as exc:
        logging.warning("Tavily search error for %r: %s", query, exc)
    return []

def _tinyfish_search(query: str, timeout: int = 90) -> list:
    tf = _tinyfish_cmd()
    if not tf:
        return _tavily_search(query)
    try:
        completed = subprocess.run(
            [tf, "search", "query", query],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout, check=False,
        )
        raw = (completed.stdout or completed.stderr or "").strip()
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            data = json.loads(lines[-1])
        return data.get("results", data if isinstance(data, list) else [])
    except Exception as exc:
        logging.warning("TinyFish error for %r: %s", query, exc)
        return []

def _extract_prices(text: str) -> list:
    return list(dict.fromkeys(
        m.group(0).rstrip(".,") for m in PRICE_PATTERN.finditer(text or "")
    ))

def _parse_price_value(raw: str):
    m = re.search(r"\d[\d,]*(?:\.\d+)?", raw or "")
    return float(m.group(0).replace(",", "")) if m else None

def _price_summary(results: list) -> dict:
    vals = []
    for r in results:
        text = f"{r.get('title', '')} {r.get('snippet', '')}"
        for p in _extract_prices(text):
            v = _parse_price_value(p)
            if v and 10 < v < 10_000_000:
                vals.append(v)
    if not vals:
        return {}
    vals.sort()
    mid = vals[len(vals) // 2]
    filtered = [v for v in vals if mid / 20 <= v <= mid * 20] or vals
    return {"min": round(min(filtered), 2), "max": round(max(filtered), 2),
            "median": round(mid, 2), "count": len(filtered)}

# ====================================================================
#  STEP 1 - Gemini Architecture & Product Breakdown
# ====================================================================
def _build_architect_prompt(text: str, has_image: bool = False) -> str:
    image_note = (
        "The user has also uploaded an image (requirements diagram / process chart / architecture). "
        "Carefully examine the image content and combine it with the project goal.\n"
        if has_image else ""
    )
    return f"""You are an expert Chief Technology Officer and systems architect.
A client wants to build the following project:
"{text}"

{image_note}
Your job is to thoroughly break down this project into a complete, realistic Bill of Materials (BOM), software stack, and engineering team:
1. HARDWARE PRODUCTS & COMPONENTS (discipline: "HW", kind: "buy"):
   - List 6-12 essential physical components, boards, sensors, motors, batteries, chassis, or peripherals needed to build the physical device.
   - For EACH hardware item, include 1-3 suggested real commercial models on sale today with maker and model (e.g. maker: "Holybro", model: "Pixhawk 6C").
   - Give reasonable specifications for each part.
2. SOFTWARE MODULES & DELIVERABLES (discipline: "SW", kind: "build" or "buy"):
   - List 4-8 software components: firmware, AI models (e.g. CV detection), cloud backend, mobile app, or ground control station.
3. ENGINEERING TEAM (team):
   - Suggest 4-7 specialized engineering roles required to design, program, assemble, and test this project (e.g. Robotics Engineer, Computer Vision Engineer, Embedded Firmware Engineer, Cloud/Mobile Dev).
   - For each role, provide a concise 5-15 word reason for why they are needed.

Return compact JSON only, with NO markdown backticks:
{{
  "items": [
    {{
      "name": "Quadcopter Drone Frame",
      "discipline": "HW",
      "kind": "buy",
      "spec": "Carbon fiber 650mm quadcopter",
      "options": [],
      "suggested_models": [{{"maker": "Tarot", "model": "FY650", "why": "Rigid payload-capable frame"}}]
    }},
    {{
      "name": "Flight Controller",
      "discipline": "HW",
      "kind": "buy",
      "spec": "STM32 H7 with redundant IMUs",
      "options": [],
      "suggested_models": [{{"maker": "Holybro", "model": "Pixhawk 6C", "why": "Autonomous GPS waypoint navigation"}}]
    }},
    {{
      "name": "Crop Disease Detection AI Model",
      "discipline": "SW",
      "kind": "build",
      "spec": "PyTorch YOLOv8 model trained on plant pathology datasets",
      "options": []
    }},
    {{
      "name": "Farmer Dashboard & Ground Control",
      "discipline": "SW",
      "kind": "build",
      "spec": "React web app with live GPS map and disease heatmap",
      "options": []
    }}
  ],
  "team": [
    {{"role": "Drone Systems Engineer", "reason": "Airframe assembly, propulsion integration, and PID tuning"}},
    {{"role": "Computer Vision / AI Engineer", "reason": "Trains disease detection models and optimizes edge inference"}}
  ],
  "ucp_features": {{
    "Simple Actors": null, "Average Actors": null, "Complex Actors": null, "UAW": null,
    "Simple UC": null, "Average UC": null, "Complex UC": null, "UUCW": null,
    "TCF": "1.0", "ECF": "1.0", "Sector": "", "Language": "",
    "Methodology": "", "ApplicationType": "", "DataDonator": ""
  }},
  "confidence": "high"
}}"""


def _extract_ucp_features(sw_items: list, team: list) -> dict:
    """Ask Gemini for the complete UCP input row required by the local model.

    These are planning assumptions extracted from the project description, not
    values measured from completed work.
    """
    prompt = f"""Convert this software project summary into exactly one JSON object for a
Use-Case-Point effort model. Estimate counts only where reasonable and use null
when genuinely unknown. Do not include any text outside JSON.

Software items: {json.dumps(sw_items, ensure_ascii=False)[:6000]}
Team: {json.dumps(team, ensure_ascii=False)[:2000]}

Required JSON keys:
{{"Simple Actors":null,"Average Actors":null,"Complex Actors":null,"UAW":null,
"Simple UC":null,"Average UC":null,"Complex UC":null,"UUCW":null,
"TCF":"","ECF":"","Sector":"","Language":"","Methodology":"",
"ApplicationType":"","DataDonator":""}}"""
    try:
        value = robust_json_parse(_gemini_text(prompt))
        return value if isinstance(value, dict) else {}
    except Exception as exc:
        logging.warning("UCP extraction failed: %s", exc)
        return {}


def _usable_ucp_features(value: dict) -> bool:
    """Reject Gemini's all-zero template before it reaches the trained model."""
    numeric = ("Simple Actors", "Average Actors", "Complex Actors", "UAW",
               "Simple UC", "Average UC", "Complex UC", "UUCW")
    if not isinstance(value, dict) or any(value.get(key) in (None, "") for key in numeric):
        return False
    try:
        return any(float(value[key]) > 0 for key in numeric)
    except (TypeError, ValueError):
        return False


def _effort_inputs(data: dict, sw_items: list, team: list) -> dict:
    extracted = data.get("ucp_features") or {}
    return extracted if _usable_ucp_features(extracted) else _extract_ucp_features(sw_items, team)

def run_gemini_agent(image_path: str = None, text: str = None, pdf_path: str = None) -> dict:
    """Accepts ONLY ONE input: text, image_path, OR pdf_path. Returns hw_items, sw_items, team."""
    _DISC = {"sw": "SW", "software": "SW", "firmware": "SW", "hw": "HW", "hardware": "HW",
             "electronics": "HW", "mech": "Mech", "mechanical": "Mech"}
    _KIND = {"buy": "buy", "purchase": "buy", "license": "buy", "licence": "buy",
             "service": "buy", "build": "build", "develop": "build", "code": "build",
             "process": "process", "document": "process", "review": "process"}

    def _norm(it):
        disc = _DISC.get(str(it.get("discipline", "")).strip().lower(), "SW")
        kind = _KIND.get(str(it.get("kind", "")).strip().lower(), "build")
        sugs = []
        for s in it.get("suggested_models", []):
            if isinstance(s, dict) and s.get("model"):
                sugs.append({
                    "maker": str(s.get("maker", ""))[:40],
                    "model": str(s.get("model", ""))[:80],
                    "why": str(s.get("why", ""))[:160],
                })
        return {
            "name": str(it.get("name", "Unnamed Item")),
            "discipline": disc,
            "kind": kind,
            "options": it.get("options", []),
            "spec": str(it.get("spec", ""))[:100],
            "suggested_models": sugs,
        }

    # MODE 1: PDF Document
    if pdf_path and os.path.isfile(pdf_path):
        pdf_text = ""
        try:
            reader = pypdf.PdfReader(pdf_path)
            parts = [page.extract_text() for page in reader.pages[:25] if page.extract_text()]
            pdf_text = "\n".join(parts).strip()
        except Exception as e:
            logging.warning("pypdf extraction: %s", e)

        if len(pdf_text) > 40:
            prompt = _build_architect_prompt(f"Project requirements extracted from uploaded PDF document:\n{pdf_text[:12000]}")
            raw = _gemini_text(prompt)
        else:
            prompt = """You help estimate the cost of setting up a project described in this PDF document.
Thoroughly examine all pages, diagrams, tables, and text in this PDF.
Break down this project into a complete Bill of Materials (BOM), software modules, and required engineering team:
1. HARDWARE (HW, buy): 6-12 physical parts, sensors, microcontrollers, modules with suggested real commercial models (maker + model).
2. SOFTWARE (SW, build/buy): 4-8 software components (AI models, firmware, backend, mobile/web app).
3. TEAM (team): 4-7 specialized engineering roles with clear reasons.
Return compact JSON only:
{"items":[{"name":"...","discipline":"HW","kind":"buy","spec":"...","options":[],"suggested_models":[{"maker":"...","model":"...","why":"..."}]}],"team":[{"role":"...","reason":"..."}],"confidence":"high"}"""
            client = _gemini()
            uploaded = client.files.upload(file=pdf_path)
            interaction = _img._create(
                client,
                model=_img.MODEL,
                input=[
                    {"type": "text", "text": prompt},
                    {"type": "image", "uri": uploaded.uri, "mime_type": uploaded.mime_type},
                ],
            )
            raw = interaction.output_text

        try:
            data = robust_json_parse(raw)
        except Exception as e:
            logging.warning("PDF LLM parse failed (%s), retrying with direct Gemini call...", e)
            raw = _gemini_text(prompt)
            data = robust_json_parse(raw)
        if isinstance(data, list):
            data = {"items": data, "team": []}
        norm_items = [_norm(it) for it in data.get("items", [])]
        hw_items = [it for it in norm_items if it["discipline"] in ("HW", "Mech") and it["kind"] == "buy"]
        sw_items = [it for it in norm_items if it["discipline"] == "SW" or it["kind"] in ("build", "process")]
        for it in norm_items:
            if it["discipline"] == "SW" and it["kind"] == "buy" and it not in sw_items:
                sw_items.append(it)
        return {
            "confidence": data.get("confidence", "high"),
            "hw_items": hw_items,
            "sw_items": sw_items,
            "team": data.get("team", []),
            "effort_inputs": _effort_inputs(data, sw_items, data.get("team", [])),
            "all_items": hw_items + sw_items,
            "input_type": "pdf",
        }

    # MODE 2: Image File
    elif image_path and os.path.isfile(image_path):
        result = _img.split_image(image_path, use_cache=True)
        hw_items = [
            {"name": it.name, "spec": it.spec, "kind": it.kind,
             "discipline": it.discipline, "options": it.options,
             "suggested_models": [s.model_dump() for s in it.suggested_models]}
            for it in result.buy() if it.discipline in ("HW", "Mech")
        ]
        sw_items = [
            {"name": it.name, "spec": it.spec, "kind": it.kind,
             "discipline": it.discipline, "options": it.options}
            for it in result.build() + result.process()
        ]
        for it in result.buy():
            if it.discipline == "SW":
                sw_items.append({"name": it.name, "spec": it.spec, "kind": "buy",
                                  "discipline": it.discipline, "options": it.options})
        team = [{"role": r.role, "reason": r.reason} for r in result.team]
        return {
            "confidence": result.confidence,
            "hw_items": hw_items,
            "sw_items": sw_items,
            "team": team,
            "effort_inputs": _extract_ucp_features(sw_items, team),
            "all_items": hw_items + sw_items,
            "input_type": "image",
        }

    # MODE 3: Text Description
    elif text and text.strip():
        prompt = _build_architect_prompt(text.strip(), has_image=False)
        raw = _gemini_text(prompt)
        try:
            data = robust_json_parse(raw)
        except Exception as e:
            logging.warning("First LLM parse failed (%s), retrying with direct Gemini call...", e)
            raw = _gemini_text(prompt)
            data = robust_json_parse(raw)
        if isinstance(data, list):
            data = {"items": data, "team": []}
        norm_items = [_norm(it) for it in data.get("items", [])]
        hw_items = [it for it in norm_items if it["discipline"] in ("HW", "Mech") and it["kind"] == "buy"]
        sw_items = [it for it in norm_items if it["discipline"] == "SW" or it["kind"] in ("build", "process")]
        for it in norm_items:
            if it["discipline"] == "SW" and it["kind"] == "buy" and it not in sw_items:
                sw_items.append(it)
        return {
            "confidence": data.get("confidence", "high"),
            "hw_items": hw_items,
            "sw_items": sw_items,
            "team": data.get("team", []),
            "effort_inputs": _effort_inputs(data, sw_items, data.get("team", [])),
            "all_items": hw_items + sw_items,
            "input_type": "text",
        }

    else:
        raise ValueError("Please provide exactly ONE input: either text, an image, or a PDF.")

# ====================================================================
#  STEP 2a - Hardware Agent (live TinyFish price search)
# ====================================================================
def run_hardware_agent(hw_items: list) -> dict:
    if not hw_items:
        return {"items": [], "total": {}, "note": "No hardware items found."}

    def search_one(item: dict) -> dict:
        sugs = item.get("suggested_models", [])
        top_model = f"{sugs[0].get('maker', '')} {sugs[0].get('model', '')}".strip() if sugs else ""
        if top_model:
            q = f"{item['name']} {top_model} price India"
        elif item.get("spec"):
            q = f"{item['name']} {item['spec']} price India"
        else:
            q = f"{item['name']} price India"
        results = _tinyfish_search(q, timeout=35)
        if not results and top_model:
            results = _tinyfish_search(f"{item['name']} price India", timeout=25)
        summary = _price_summary(results)
        return {
            "name": item["name"], "spec": item.get("spec", ""),
            "suggested_models": sugs,
            "price_summary": summary, "source_count": len(results),
            "sources": [{"title": r.get("title",""), "url": r.get("url", r.get("link","")),
                         "snippet": r.get("snippet","")[:200]}
                        for r in results[:5] if isinstance(r, dict)],
        }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {pool.submit(search_one, it): it for it in hw_items}
        results = []
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as exc:
                results.append({"name": futures[fut]["name"], "error": str(exc), "price_summary": {}})

    total_min = sum(r.get("price_summary",{}).get("min",0) for r in results)
    total_max = sum(r.get("price_summary",{}).get("max",0) for r in results)
    all_sources = []
    seen_urls = set()
    for it in results:
        for s in it.get("sources", []):
            u = s.get("url") or s.get("link")
            if u and u not in seen_urls:
                seen_urls.add(u)
                all_sources.append({**s, "item": it["name"]})
    return {"items": results, "total": {"min": round(total_min,2), "max": round(total_max,2)}, "all_sources": all_sources, "source_count": len(all_sources)}

# ====================================================================
#  STEP 2b - Software Agent (effort model + TinyFish licence pricing)
# ====================================================================
def _effort_hours(item_name: str, kind: str) -> dict:
    key = "default"
    low = item_name.lower()
    if "firmware" in low:
        key = "firmware"
    elif any(w in low for w in ("web","app","portal","dashboard","ui","api","mobile","model","detection","ai")):
        key = "web app"
    fp = WORK_ITEMS_FP.get(key, WORK_ITEMS_FP.get("default", {"low":5,"likely":12,"high":25}))

    return {
        "hours_low": fp["low"] * 8, "hours_likely": fp["likely"] * 8,
        "hours_high": fp["high"] * 8,
        "months_likely": round(fp["likely"] * 8 / HOURS_PER_MONTH, 1),
        "source": "planning weight for allocating the project-level model prediction",
    }

def run_software_agent(sw_items: list, team: list, effort_inputs: dict | None = None) -> dict:
    if not sw_items:
        return {"items": [], "effort": {}, "licence_cost": {}}

    def process_build(item):
        effort = _effort_hours(item["name"], item["kind"])
        tech_results = _tinyfish_search(f"{item['name']} open source architecture github documentation", timeout=45)
        sources = []
        seen = set()
        for r in tech_results:
            if isinstance(r, dict):
                u = r.get("url") or r.get("link", "")
                if u and u not in seen:
                    seen.add(u)
                    sources.append({"title": r.get("title", item["name"]), "url": u, "snippet": r.get("snippet", r.get("description", ""))[:220]})
        return {"name": item["name"], "kind": item["kind"], "discipline": item["discipline"], "effort": effort, "sources": sources[:4], "source_count": len(sources)}

    def process_buy(item):
        results = _tinyfish_search(f"{item['name']} software pricing plans per user per month")
        results += _tinyfish_search(f"{item['name']} company employees team size")
        summary = _price_summary(results)
        TEAM_RE = re.compile(r"\b(\d[\d,]*)\s*(?:-|to)\s*(\d[\d,]*)\s*(?:employees|people|staff)", re.I)
        ranges = []
        for r in results:
            for m in TEAM_RE.finditer(f"{r.get('title','')} {r.get('snippet','')}"):
                lo, hi = int(m.group(1).replace(",","")), int(m.group(2).replace(",",""))
                ranges.append({"low": lo, "high": hi})
        return {"name": item["name"], "kind": "buy (licence/SaaS)",
                "discipline": item["discipline"], "price_summary": summary,
                "team_ranges": ranges[:3], "source_count": len(results),
                "sources": [{"title": r.get("title",""), "url": r.get("url",r.get("link","")),
                             "snippet": r.get("snippet","")[:200]}
                            for r in results[:4] if isinstance(r, dict)]}

    with ThreadPoolExecutor(max_workers=5) as pool:
        futures = {}
        for item in sw_items:
            fn = process_buy if item["kind"] == "buy" else process_build
            futures[pool.submit(fn, item)] = item
        item_results = []
        for fut in as_completed(futures):
            try:
                item_results.append(fut.result())
            except Exception as exc:
                item_results.append({"name": futures[fut]["name"], "error": str(exc)})

    baseline_low = sum(r.get("effort",{}).get("hours_low",0) for r in item_results)
    baseline_likely = sum(r.get("effort",{}).get("hours_likely",0) for r in item_results)
    baseline_high = sum(r.get("effort",{}).get("hours_high",0) for r in item_results)
    model_prediction = predict_ucp_effort(effort_inputs or {}, team_size=len(team))

    if model_prediction.get("available"):
        # The saved model predicts the whole project. The existing per-item
        # planning weights only distribute that one model prediction for display.
        model_total = model_prediction["hours_likely"]
        divisor = max(1, baseline_likely)
        for item in item_results:
            effort = item.get("effort")
            if not effort:
                continue
            share = effort["hours_likely"] / divisor
            effort.update({
                "hours_low": round(model_prediction["hours_low"] * share),
                "hours_likely": round(model_total * share),
                "hours_high": round(model_prediction["hours_high"] * share),
                "months_likely": round((model_total * share) / (HOURS_PER_MONTH * max(1, len(team))), 1),
                "source": "Predicted value by locally trained model (allocated project share)",
            })
        hrs_low = model_prediction["hours_low"]
        hrs_likely = model_total
        hrs_high = model_prediction["hours_high"]
    else:
        hrs_low, hrs_likely, hrs_high = baseline_low, baseline_likely, baseline_high
    lic_min    = sum(r.get("price_summary",{}).get("min",0) for r in item_results)
    lic_max    = sum(r.get("price_summary",{}).get("max",0) for r in item_results)

    all_sources = []
    seen_urls = set()
    for it in item_results:
        for s in it.get("sources", []):
            u = s.get("url") or s.get("link")
            if u and u not in seen_urls:
                seen_urls.add(u)
                all_sources.append({**s, "item": it["name"]})
    return {
        "items": item_results,
        "effort": {"total_hours_low": round(hrs_low), "total_hours_likely": round(hrs_likely),
                   "total_hours_high": round(hrs_high),
                   "months_likely": (model_prediction.get("delivery_months_assumed")
                                      if model_prediction.get("available")
                                      else round(hrs_likely / (HOURS_PER_MONTH * max(1, len(team))), 1))},
        "model_prediction": model_prediction,
        "licence_cost": {"min": round(lic_min,2), "max": round(lic_max,2)},
        "all_sources": all_sources,
        "source_count": len(all_sources),
    }

# ====================================================================
#  STEP 3 - Deterministic model-led cost prediction
# ====================================================================
def run_final_prediction(gemini_out: dict, hw_out: dict, sw_out: dict) -> dict:
    hw_min = hw_out.get("total",{}).get("min",0)
    hw_max = hw_out.get("total",{}).get("max",0)
    lic_min = sw_out.get("licence_cost",{}).get("min",0)
    lic_max = sw_out.get("licence_cost",{}).get("max",0)
    effort = sw_out.get("effort",{})
    model = sw_out.get("model_prediction", {})
    dev_min = effort.get("total_hours_low",0) * 800
    dev_max = effort.get("total_hours_high",0) * 800
    optimal = round((hw_min + hw_max) / 2 + (lic_min + lic_max) / 2 + effort.get("total_hours_likely", 0) * 800)
    if model.get("available"):
        summary = (f"Optimal estimated cost: ₹{optimal:,.0f}. It uses {model['hours_likely']:,} "
                   f"person-hours predicted by the locally trained {model['model']} model and "
                   f"recommends {model['recommended_engineers']} engineers for a "
                   f"{model['delivery_months_assumed']}-month delivery target.")
    else:
        summary = "A trained-model cost estimate is unavailable until the project brief provides complete scope inputs."
    return {
        "total_cost_inr": {
            "optimistic": round(hw_min + lic_min + dev_min),
            "likely": optimal,
            "pessimistic": round(hw_max + lic_max + dev_max),
        },
        "hardware_cost_inr": {"min": hw_min, "max": hw_max},
        "software_dev_cost_inr": {"min": dev_min, "max": dev_max},
        "licence_cost_inr": {"min": lic_min, "max": lic_max},
        "effort_months": {"low": effort.get("total_hours_low",0)//HOURS_PER_MONTH,
                          "likely": effort.get("months_likely",0),
                          "high": effort.get("total_hours_high",0)//HOURS_PER_MONTH},
        "model_estimate": model,
        "duplicates_flagged": [], "missing_specs": [], "confidence": "model-backed" if model.get("available") else "needs-scope",
        "summary": summary,
    }

# ====================================================================
#  SSE streaming pipeline
# ====================================================================
def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

def pipeline_generator(input_type: str, text: str, file_path: str, session_id: str):
    # STAGE 1: Gemini
    inp_label = "PDF document" if input_type == "pdf" else ("image" if input_type == "image" else "text description")
    yield _sse("stage", {"stage":"gemini","status":"running",
                          "message":f"Gemini is analysing your {inp_label} and architecting products, modules & team..."})
    try:
        gemini_out = run_gemini_agent(
            text=text if input_type == "text" else None,
            image_path=file_path if input_type == "image" else None,
            pdf_path=file_path if input_type == "pdf" else None,
        )
        yield _sse("stage", {"stage":"gemini","status":"done",
                              "message": f"Identified {len(gemini_out['hw_items'])} hardware products, {len(gemini_out['sw_items'])} software components, and {len(gemini_out['team'])} team roles.",
                              "data": gemini_out})
    except Exception as exc:
        import traceback
        logging.error("Gemini stage error: %s\n%s", exc, traceback.format_exc())
        yield _sse("stage", {"stage":"gemini","status":"error","message": str(exc)})
        yield _sse("done", {"error": str(exc)})
        return

    # STAGES 2a + 2b: Parallel
    yield _sse("stage", {"stage":"hardware","status":"running","message":"Hardware agent searching live prices in parallel..."})
    yield _sse("stage", {"stage":"software","status":"running","message":"Software agent estimating effort & licence costs in parallel..."})

    hw_out, sw_out = {}, {}
    label_map = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        hw_fut = pool.submit(run_hardware_agent, gemini_out["hw_items"])
        sw_fut = pool.submit(
            run_software_agent,
            gemini_out["sw_items"],
            gemini_out["team"],
            gemini_out.get("effort_inputs", {}),
        )
        label_map[hw_fut] = "hardware"
        label_map[sw_fut] = "software"
        pending = set(label_map.keys())
        while pending:
            done = {f for f in pending if f.done()}
            if not done:
                time.sleep(2)
                yield ": keepalive\n\n"
                continue
            for fut in done:
                pending.remove(fut)
                label = label_map[fut]
                try:
                    r = fut.result()
                    if label == "hardware":
                        hw_out = r
                        yield _sse("stage", {"stage":"hardware","status":"done",
                                              "message": f"{len(r.get('items',[]))} items priced. Total: ₹{r.get('total',{}).get('min',0):,.0f} – ₹{r.get('total',{}).get('max',0):,.0f}",
                                              "data": r})
                    else:
                        sw_out = r
                        m = r.get("effort",{}).get("months_likely","?")
                        model_label = ("local trained model" if r.get("model_prediction", {}).get("available")
                                       else "planning fallback")
                        yield _sse("stage", {"stage":"software","status":"done",
                                              "message": f"{len(r.get('items',[]))} items. Dev effort: ~{m} months likely ({model_label}).",
                                              "data": r})
                except Exception as exc:
                    if label == "hardware":
                        hw_out = {"items":[],"total":{}}
                        yield _sse("stage",{"stage":"hardware","status":"error","message":str(exc)})
                    else:
                        sw_out = {"items":[],"effort":{},"licence_cost":{}}
                        yield _sse("stage",{"stage":"software","status":"error","message":str(exc)})

    # STAGE 3: Final prediction
    yield _sse("stage", {"stage":"prediction","status":"running","message":"LLM synthesising final cost prediction in INR..."})
    try:
        prediction = run_final_prediction(gemini_out, hw_out, sw_out)
        yield _sse("stage", {"stage":"prediction","status":"done",
                              "message": f"Likely total: ₹{prediction.get('total_cost_inr',{}).get('likely',0):,.0f}",
                              "data": prediction})
    except Exception as exc:
        prediction = {"error": str(exc)}
        yield _sse("stage", {"stage":"prediction","status":"error","message":str(exc)})

    _chat_cache[session_id] = {"gemini": gemini_out, "hardware": hw_out,
                                "software": sw_out, "prediction": prediction}
    yield _sse("done", {"session_id": session_id, "gemini": gemini_out,
                         "hardware": hw_out, "software": sw_out, "prediction": prediction})

# ====================================================================
#  ROUTES
# ====================================================================
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/api/estimate")
def estimate():
    mode = (request.form.get("mode", "") or "").strip().lower()  # "text", "image", or "pdf"
    image_path = None
    pdf_path = None
    text_input = None
    file_to_clean = None

    # Handle exactly ONE input mode:
    if mode == "pdf" or ("pdf" in request.files and request.files["pdf"].filename):
        f = request.files.get("pdf") or request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "No PDF file provided."}), 400
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf", dir=str(CACHE_DIR))
        f.save(tmp.name)
        tmp.close()
        pdf_path = tmp.name
        file_to_clean = pdf_path
        input_type = "pdf"
        session_seed = pdf_path

    elif mode == "image" or ("image" in request.files and request.files["image"].filename):
        f = request.files.get("image") or request.files.get("file")
        if not f or not f.filename:
            return jsonify({"error": "No image file provided."}), 400
        suffix = Path(f.filename).suffix.lower() or ".jpg"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=str(CACHE_DIR))
        f.save(tmp.name)
        tmp.close()
        image_path = tmp.name
        file_to_clean = image_path
        input_type = "image"
        session_seed = image_path

    elif mode == "text" or ("text" in request.form and request.form.get("text", "").strip()):
        text_input = request.form.get("text", "").strip()
        if not text_input:
            return jsonify({"error": "No text description provided."}), 400
        input_type = "text"
        session_seed = text_input[:60]

    else:
        return jsonify({"error": "Please provide exactly ONE input: either text, an image, or a PDF."}), 400

    session_id = hashlib.sha256(
        (session_seed + str(time.time())).encode()
    ).hexdigest()[:16]

    def gen():
        try:
            yield from pipeline_generator(input_type, text_input, file_to_clean, session_id)
        finally:
            if file_to_clean:
                try:
                    os.unlink(file_to_clean)
                except OSError:
                    pass

    return Response(stream_with_context(gen()), mimetype="text/event-stream",
                    headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})

@app.post("/api/chat")
def chat():
    body = request.get_json(force=True) or {}
    session_id = body.get("session_id","")
    user_msg = body.get("message","").strip()
    if not user_msg:
        return jsonify({"error":"Empty message."}), 400

    context = _chat_cache.get(session_id, {})
    ctx_str = json.dumps(context, indent=2)[:4000] if context else "(no estimate run yet)"

    prompt = f"""You are a helpful project cost estimation assistant.
A user has an AI-generated project cost estimate. Answer their question clearly and helpfully.
Do NOT reveal API keys, internal system details, or raw JSON dumps.
Keep your answer concise (under 200 words).

Project estimate data:
{ctx_str}

User question: {user_msg}

Answer:"""

    try:
        answer = _call_llm(prompt)
    except Exception as exc:
        logging.error("Chat error: %s", exc)
        answer = f"Sorry, I could not generate a response: {exc}"

    return jsonify({"reply": answer})

if __name__ == "__main__":
    port = int(os.environ.get("PORT","5002"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True, use_reloader=False)
