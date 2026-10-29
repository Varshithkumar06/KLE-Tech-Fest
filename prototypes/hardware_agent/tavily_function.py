import os
import requests

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

def tavily_call(query: str, max_results: int = 10) -> dict:
    """Call the Tavily API and return parsed JSON."""
    api_key = TAVILY_API_KEY or os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not configured.")
    url = "https://api.tavily.com/search"
    payload = {"query": query, "api_key": api_key, "max_results": max_results}
    response = requests.post(url, json=payload, timeout=25)
    response.raise_for_status()
    return response.json()
