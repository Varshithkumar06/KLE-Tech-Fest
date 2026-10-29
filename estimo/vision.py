import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Literal

from pydantic import BaseModel, field_validator, model_validator

# Set your key as an environment variable (recommended):
#   setx GEMINI_API_KEY "your-key"   (then open a new terminal)
# Never share this file with a real key pasted in it.
API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
IMAGE_PATH = r"C:\Users\HP\Downloads\Project Requirements Report Workflow.png"
# The provider-supported model can be changed without editing source code.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash").strip()
# Keep cache paths stable even when Flask is launched from another directory.
CACHE_DIR = Path(__file__).resolve().parent.parent / ".image_cache"
CACHE_DIR.mkdir(exist_ok=True)
MAX_ITEMS = 25
MAX_SIDE = 1600
THINKING = "medium"
SUGGEST_MODELS = True   # set False to skip the 2nd API call (faster)
WANT_TEAM = True
RETRIES = 1
RATE_WAIT = 20
RATE_RETRIES = 3
CALL_TIMEOUT = 90
VERBOSE = True

LAST_TIMING = {}
_client_cache = {}
_uploaded = {}


def _say(message):
    if VERBOSE:
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


_DISC = {"sw": "SW", "software": "SW", "firmware": "SW", "hw": "HW", "hardware": "HW",
         "electronics": "HW", "mech": "Mech", "mechanical": "Mech", "mechanics": "Mech"}

_KIND = {"buy": "buy", "purchase": "buy", "procure": "buy", "license": "buy",
         "licence": "buy", "service": "buy", "build": "build", "develop": "build",
         "custom": "build", "code": "build", "process": "process",
         "document": "process", "documentation": "process", "activity": "process",
         "deliverable": "process", "review": "process", "meeting": "process"}


def build_prompt():
    lines = [
        "You help estimate the cost of setting up a product.",
        "Read ALL the text on this page or image (a requirements table, a specification,",
        "a product photo, a diagram, or a process page).",
        "",
        "List every component, document or activity that is written or shown.",
        "Group lines that belong together into ONE item. Do not list every sentence.",
        f"At most {MAX_ITEMS} items. Each item has:",
        "- name: short",
        "- discipline: HW, SW or Mech",
        "- kind: buy (hardware, a licence, a product or service that is purchased or run),"
        " build (software or firmware the team must develop), or process (a document,"
        " form, review, log, meeting, traceability or other activity or deliverable;"
        " it costs people's time, nothing is bought)",
        "- spec: the recommended or minimum values exactly as written; empty if the page"
        " gives none; never repeat the name; under 100 characters",
        "- options: alternatives the page lists (several operating systems or databases);"
        " empty if there is only one",
    ]
    if WANT_TEAM:
        lines.append("Also list up to 10 engineering roles a team would need to set up, build"
                     " and run all of this: role plus a reason of at most 10 words. These are"
                     " your suggestion, not text from the page.")
    lines += [
        "",
        "Rules:",
        "- Items must come from the page. Do NOT invent items.",
        "- Keep the spec text as written. Do not convert or guess values.",
        "- Do NOT give costs, prices, hours, days or sizes.",
        "- Do NOT suggest product models.",
        "",
        "Return compact JSON only: one line, no indentation, no markdown, in this shape:",
    ]
    item = '{"name": "...", "discipline": "HW", "kind": "buy", "spec": "...", "options": []}'
    shape = '{"items": [' + item + "]"
    shape += ', "team": [{"role": "...", "reason": "..."}]' if WANT_TEAM else ""
    shape += ', "confidence": "low"}'
    lines += [shape,
              "confidence is low, medium or high: how sure you are that the item list is"
              " complete."]
    return "\n".join(lines)


def suggest_prompt(items, context):
    parts = [{"name": i.name, "spec": i.spec} for i in items]
    return "\n".join([
        "A project needs the hardware parts listed below.",
        "For each part give up to 3 real models that are on sale today and fit the project"
        " and the spec. Each has maker, model and a reason of at most 10 words.",
        "Only name models you are sure exist. If you are not sure, give an empty list.",
        "Do NOT give prices.",
        "",
        "Everything in the project: " + "; ".join(context),
        "Hardware parts: " + json.dumps(parts),
        "",
        "Return compact JSON only, one line, no markdown, in this shape:",
        '{"suggestions": {"<part name exactly as given>": '
        '[{"maker": "...", "model": "...", "why": "..."}]}}',
    ])


def _as_text(v, limit):
    return str(v if v is not None else "").strip()[:limit]


class Suggestion(BaseModel):
    maker: str = ""
    model: str
    why: str = ""

    @field_validator("model")
    @classmethod
    def _model(cls, v):
        v = str(v).strip()
        if not v or v.lower() in {"unknown", "n/a", "none", "tbd"}:
            raise ValueError("empty model")
        return v[:80]

    @field_validator("maker", mode="before")
    @classmethod
    def _maker(cls, v):
        return _as_text(v, 40)

    @field_validator("why", mode="before")
    @classmethod
    def _why(cls, v):
        return _as_text(v, 160)


class Item(BaseModel):
    name: str
    discipline: Literal["SW", "HW", "Mech", "Unknown"] = "Unknown"
    kind: Literal["buy", "build", "process"] = "build"
    spec: str = ""
    options: List[str] = []
    suggested_models: List[Suggestion] = []

    @model_validator(mode="before")
    @classmethod
    def _infer_kind(cls, data):
        if not isinstance(data, dict):
            return data
        data = dict(data)
        kind = _KIND.get(str(data.get("kind") or "").strip().lower())
        if kind is None:
            disc = _DISC.get(str(data.get("discipline") or "").strip().lower(), "Unknown")
            kind = "buy" if disc in {"HW", "Mech"} or data.get("options") else "build"
        data["kind"] = kind
        return data

    @model_validator(mode="after")
    def _tidy(self):
        if self.spec.strip().lower() == self.name.strip().lower():
            self.spec = ""
        if self.kind != "buy" or self.discipline != "HW":
            self.suggested_models = []
        return self

    @property
    def spec_needed(self):
        return self.kind == "buy" and self.discipline in {"HW", "Mech"} and not self.spec

    @field_validator("suggested_models", mode="before")
    @classmethod
    def _suggested(cls, v):
        if not isinstance(v, list):
            return []
        good = []
        for s in v:
            try:
                s = Suggestion.model_validate(s)
            except Exception:
                continue
            key = (s.maker.lower(), s.model.lower())
            if key not in [(g.maker.lower(), g.model.lower()) for g in good]:
                good.append(s)
        return good[:3]

    @field_validator("name")
    @classmethod
    def _name(cls, v):
        v = str(v).strip()
        if not v:
            raise ValueError("empty name")
        return v[:80]

    @field_validator("discipline", mode="before")
    @classmethod
    def _disc(cls, v):
        return _DISC.get(str(v).strip().lower(), "Unknown")

    @field_validator("spec", mode="before")
    @classmethod
    def _spec(cls, v):
        return _as_text(v, 200)

    @field_validator("options", mode="before")
    @classmethod
    def _options(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = re.split(r"[;\n]", v)
        out = []
        for o in v:
            o = _as_text(o, 80)
            if o and o not in out:
                out.append(o)
        return out[:12]


class Role(BaseModel):
    role: str
    reason: str = ""

    @field_validator("role")
    @classmethod
    def _role(cls, v):
        v = str(v).strip()
        if not v:
            raise ValueError("empty role")
        return v[:60]

    @field_validator("reason", mode="before")
    @classmethod
    def _reason(cls, v):
        return _as_text(v, 160)


class ImageSplit(BaseModel):
    items: List[Item]
    team: List[Role] = []
    confidence: Literal["low", "medium", "high"] = "low"

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v):
        v = str(v).strip().lower()
        return v if v in {"low", "medium", "high"} else "low"

    @field_validator("items")
    @classmethod
    def _items(cls, v):
        if not v:
            raise ValueError("no items found")
        return v[:MAX_ITEMS]

    @field_validator("team", mode="before")
    @classmethod
    def _team(cls, v):
        if not isinstance(v, list):
            return []
        good = []
        for r in v:
            try:
                good.append(Role.model_validate(r))
            except Exception:
                continue
        return good[:10]

    def counts(self):
        out = {}
        for it in self.items:
            out[it.kind] = out.get(it.kind, 0) + 1
        return out

    def lane(self, kind):
        return [i for i in self.items if i.kind == kind]

    def buy(self):
        return self.lane("buy")

    def build(self):
        return self.lane("build")

    def process(self):
        return self.lane("process")

    def price_queries(self):
        queries = []
        for it in self.buy():
            if it.discipline == "HW" and it.suggested_models:
                for s in it.suggested_models:
                    label = f"{s.maker} {s.model}".strip()
                    queries.append({"item": it.name, "option": label, "discipline": "HW",
                                    "source": "suggested", "query": f"{label} price"})
                continue
            names = it.options or [it.name]
            for n in names:
                parts = [n]
                if it.discipline in {"HW", "Mech"} and it.spec:
                    parts.append(re.sub(r"\(.*?\)", "", it.spec).strip())
                parts.append("price")
                queries.append({"item": it.name, "option": n if it.options else "",
                                "discipline": it.discipline, "source": "page",
                                "query": " ".join(parts)})
        return queries

    def to_lists(self):
        def dump(items):
            return [{**i.model_dump(), "spec_needed": i.spec_needed} for i in items]
        return {
            "buy": dump(self.buy()),
            "build": dump(self.build()),
            "process": dump(self.process()),
            "team": [r.model_dump() for r in self.team],
            "price_queries": self.price_queries(),
            "confidence": self.confidence,
        }


def _client():
    key = API_KEY or os.environ.get('GEMINI_API_KEY', '').strip()
    if not key:
        raise ValueError("Gemini API key missing. Set GEMINI_API_KEY, then restart the server.")
    if "c" not in _client_cache:
        from google import genai
        from google.genai import types
        _client_cache["c"] = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(
                timeout=int(CALL_TIMEOUT * 1000),
                retry_options=types.HttpRetryOptions(attempts=1)))
    return _client_cache["c"]


def _tick(key, started):
    LAST_TIMING[key] = LAST_TIMING.get(key, 0.0) + (time.time() - started)


def _create(client, **kw):
    if THINKING:
        try:
            return client.interactions.create(
                generation_config={"thinking_level": THINKING}, **kw)
        except Exception as e:
            if "thinking" not in str(e).lower():
                raise
    return client.interactions.create(**kw)


def shrink(image_path):
    if not MAX_SIDE:
        return image_path
    from PIL import Image
    img = Image.open(image_path)
    w, h = img.size
    if max(w, h) <= MAX_SIDE:
        return image_path
    k = MAX_SIDE / max(w, h)
    img = img.convert("RGB").resize((round(w * k), round(h * k)), Image.Resampling.LANCZOS)
    CACHE_DIR.mkdir(exist_ok=True)
    key = hashlib.sha256(Path(image_path).read_bytes()).hexdigest()[:16]
    out = CACHE_DIR / f"small_{key}.jpg"
    img.save(out, quality=90)
    return str(out)


def call_gemini(image_path, prompt):
    client = _client()
    t = time.time()
    small = shrink(image_path)
    _tick("resize", t)

    # Send bytes directly. This avoids the Windows file-upload path that produced
    # [Errno 22] and keeps one API contract for image analysis.
    from google.genai import types
    ext = Path(small).suffix.lower()
    mime = {".png": "image/png", ".webp": "image/webp"}.get(ext, "image/jpeg")
    with open(small, "rb") as handle:
        img_bytes = handle.read()

    _say(f"calling {MODEL} with inline image bytes...")
    t_gen = time.time()
    try:
        response = client.models.generate_content(
            model=MODEL,
            contents=types.Content(parts=[
                types.Part.from_bytes(data=img_bytes, mime_type=mime),
                types.Part.from_text(text=prompt),
            ]),
        )
    except Exception as exc:
        raise RuntimeError(f"Gemini image analysis failed using {MODEL}: {type(exc).__name__}: {exc}") from exc
    _tick("generate", t_gen)
    _say(f"Gemini answered in {time.time() - t_gen:.1f}s")
    if not response.text:
        raise RuntimeError(f"Gemini returned no text for the image using {MODEL}.")
    return response.text


def call_gemini_text(prompt):
    client = _client()
    _say("asking Gemini for hardware model suggestions ...")
    t = time.time()
    try:
        resp = client.models.generate_content(model=MODEL, contents=prompt)
        _tick("suggest", t)
        if resp.text:
            return resp.text
    except Exception as e:
        _say(f"generate_content failed ({e}), falling back to interactions...")
    interaction = _create(client, model=MODEL, input=prompt)
    _tick("suggest", t)
    return interaction.output_text


def is_rate_limit(e):
    if isinstance(e, ValueError):
        return False
    text = f"{type(e).__name__} {e}".lower()
    return any(k in text for k in ("429", "too many requests", "resource_exhausted",
                                   "quota", "temporaryerror", "ratelimit"))


RATE_MSG = ("The API says 429 Too Many Requests: you hit the rate limit or the quota of this "
            "key (free keys have small per-minute and per-day limits). Wait a minute and "
            "run again, or check your usage on the Google AI Studio site. "
            "Nothing is wrong with the code.")


def extract_json(text):
    text = re.sub(r"```(?:json)?", "", text or "").strip().replace("```", "")
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        substr = text[start:end + 1]
        try:
            return json.loads(substr)
        except Exception:
            pass
    try:
        import json_repair
        obj = json_repair.repair_json(text, return_objects=True)
        if isinstance(obj, (dict, list)):
            return obj
    except Exception:
        pass
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])
    raise ValueError("no JSON object in reply")


def _cache_path(image_path, prompt):
    h = hashlib.sha256()
    h.update(Path(image_path).read_bytes())
    h.update(MODEL.encode())
    h.update(prompt.encode())
    h.update(str(SUGGEST_MODELS).encode())
    return CACHE_DIR / f"{h.hexdigest()[:24]}.json"


def add_suggestions(split, text_call=None, cache_file=None):
    if not SUGGEST_MODELS:
        return split
    todo = [i for i in split.buy() if i.discipline == "HW" and not i.suggested_models]
    if not todo:
        return split
    got, last = None, None
    if cache_file and cache_file.exists():
        got = json.loads(cache_file.read_text(encoding="utf-8"))
    else:
        ask = text_call or call_gemini_text
        try:
            data = extract_json(ask(suggest_prompt(todo, [i.name for i in split.items])))
            got = {str(k).strip().lower(): v for k, v in data["suggestions"].items()}
        except Exception as e:
            _say(f"Gemini attempt failed ({type(e).__name__}): {e}")
            last = e
        if got is not None and cache_file:
            CACHE_DIR.mkdir(exist_ok=True)
            cache_file.write_text(json.dumps(got), encoding="utf-8")
    if got is None:
        LAST_TIMING["suggest_error"] = ("rate limit (429), model suggestions skipped; "
                                        "run again later" if is_rate_limit(last)
                                        else str(last)[:120])
        return split
    for it in todo:
        raw = got.get(it.name.lower())
        if raw:
            it.suggested_models = Item.model_validate(
                {**it.model_dump(), "suggested_models": raw}).suggested_models
    return split


def _load_cached(cache, text_call):
    LAST_TIMING["cache"] = True
    result = ImageSplit.model_validate_json(cache.read_text(encoding="utf-8"))
    add_suggestions(result, text_call, cache.with_suffix(".sug.json"))
    return result


def split_image(image_path=None, call=None, text_call=None, retries=None, use_cache=True):
    image_path = image_path or IMAGE_PATH
    if not os.path.isfile(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}\n"
                                f"Edit IMAGE_PATH at the top of image.py.")
    retries = RETRIES if retries is None else retries
    LAST_TIMING.clear()
    started = time.time()
    base = build_prompt()
    cache = _cache_path(image_path, base)
    if use_cache and cache.exists():
        result = _load_cached(cache, text_call)
        LAST_TIMING["total"] = time.time() - started
        return result

    prompt, bad, waits = base, 0, 0
    while True:
        LAST_TIMING["attempts"] = LAST_TIMING.get("attempts", 0) + 1
        try:
            ask = call or call_gemini
            reply = ask(image_path, prompt)
            result = ImageSplit.model_validate(extract_json(reply))
            break
        except Exception as e:
            bad_reply = isinstance(e, ValueError)
            if bad_reply and "API key missing" in str(e):
                raise
            if is_rate_limit(e):
                if waits >= RATE_RETRIES:
                    raise RuntimeError(RATE_MSG) from e
                waits += 1
                print(f"Rate limit (429). Waiting {RATE_WAIT}s, then retry "
                      f"{waits}/{RATE_RETRIES} ...", flush=True)
                time.sleep(RATE_WAIT)
                LAST_TIMING["rate_wait"] = LAST_TIMING.get("rate_wait", 0.0) + RATE_WAIT
                continue
            # a wrong model name or similar will never succeed on retry: show it now
            if not bad_reply and ("404" in str(e) or "not found" in str(e).lower()):
                raise
            bad += 1
            if bad > retries:
                raise ValueError(f"Could not read a valid split from the model: {e}")
            if bad_reply:
                prompt = base + "\n\nYour last reply was not valid (" + str(e)[:150] + \
                         "). Return ONLY the JSON object, nothing else."

    CACHE_DIR.mkdir(exist_ok=True)
    cache.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    add_suggestions(result, text_call, cache.with_suffix(".sug.json"))
    LAST_TIMING["total"] = time.time() - started
    return result


def _show(title, items, with_models=False):
    print(f"\n{title}")
    if not items:
        print("  (none)")
    for it in items:
        spec = f": {it.spec}" if it.spec else ""
        opts = f"  options: {', '.join(it.options)}" if it.options else ""
        flag = "   [spec needed]" if it.spec_needed else ""
        print(f"  {it.name}{spec}{opts}{flag}")
        if with_models:
            for s in it.suggested_models:
                print(f"      suggested: {s.maker} {s.model} - {s.why}")


def _show_timing():
    secs = {k: v for k, v in LAST_TIMING.items() if isinstance(v, float)}
    line = ", ".join(f"{k} {v:.1f}s" for k, v in secs.items())
    extra = [f"{k}={LAST_TIMING[k]}" for k in ("attempts", "cache", "usage", "suggest_error")
             if k in LAST_TIMING]
    print("time:", line, "|", " ".join(extra))


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    path = args[0] if args else IMAGE_PATH
    print("model:", MODEL)
    res = split_image(path)
    print("confidence:", res.confidence, "| items:", res.counts())
    _show("BUY (hardware, licences -> price search)", res.buy(), with_models=True)
    _show("BUILD (software to develop -> effort model)", res.build())
    _show("PROCESS (documents, reviews -> hours per role)", res.process())
    print("\nTEAM (suggested)")
    for r in res.team:
        print(f"  {r.role} - {r.reason}")
    print(f"\nprice searches to run: {len(res.price_queries())}")
    _show_timing()
