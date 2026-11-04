"""Private expense categorisation by a local Llama model inside the same protected boundary.

The model listens only on the group's loopback. Row text is never logged or sent elsewhere.
"""
import json
import time
from urllib.request import ProxyHandler, Request, build_opener

MODEL_URL = "http://127.0.0.1:8080/completion"
CATEGORIES = ("food", "transport", "education", "health", "other")
MAX_ROWS = 25
_GRAMMAR = "root ::= " + " | ".join(f'"{c}"' for c in CATEGORIES)


_SYSTEM = "You classify one expense merchant into a category. food: restaurants, cafes, groceries, snacks. transport: taxis, rides, trains, metro, flights. education: courses, tuition, schools, learning apps. health: pharmacies, doctors, hospitals, medical tests. other: anything else. Answer with the category only."
_EXAMPLES = (("Local Bakery", "food"), ("City Bus Pass", "transport"), ("Online Math Class", "education"), ("Family Clinic Visit", "health"), ("Phone Repair Shop", "other"))


def _turn(role, text):
    return f"<|start_header_id|>{role}<|end_header_id|>\n\n{text}<|eot_id|>"


def _ask(merchant):
    prompt = _turn("system", _SYSTEM) + "".join(_turn("user", f"Merchant: {m}") + _turn("assistant", c) for m, c in _EXAMPLES) + _turn("user", f"Merchant: {merchant}") + "<|start_header_id|>assistant<|end_header_id|>\n\n"
    request = Request(MODEL_URL, json.dumps({"prompt": prompt, "grammar": _GRAMMAR, "n_predict": 6, "temperature": 0}).encode(), {"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=120) as response:
        return json.load(response)["content"].strip()


def process(plaintext):
    rows = json.loads(plaintext)["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_ROWS or any(not isinstance(r, dict) or not isinstance(r.get("merchant"), str) or len(r["merchant"]) > 200 for r in rows):
        raise ValueError("Invalid task input")
    started = time.monotonic()
    categories = [_ask(r["merchant"]) for r in rows]
    return json.dumps({"categories": categories, "model": "llama-3.2-3b-q4", "ai_inference": True, "elapsed_ms": round((time.monotonic() - started) * 1000)}).encode()
