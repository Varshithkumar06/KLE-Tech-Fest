# Browser Search Flask App

Run from this folder:

```powershell
python -m pip install -r requirements.txt
python app.py
```

The server runs without Flask's development auto-reloader by default, so Chrome sessions are not interrupted by package-file changes. Set `FLASK_DEBUG=1` only while debugging the Flask app.

Open http://127.0.0.1:5000 and enter a product name such as `uno board`, or paste a URL. The app first connects to Chrome's existing remote-debugging endpoint at `127.0.0.1:9222` (the setting shown in Chrome DevTools). If unavailable, it starts an isolated temporary Chrome session. The rendered page text and source data are returned. A connected Chrome instance may include its existing account, cookies, and permissions, so only enable remote debugging for software you trust.

Product-name searches use the locally installed TinyFish CLI first and return structured web results. SerpAPI is disabled by default and is used only when `USE_SERPAPI_FALLBACK=1` is explicitly set. Direct URLs still use backend Chrome.

Search snippets provide the fast price path. Set `FETCH_PAGE_PRICES=1` only when you want the app to visit up to three seller pages for deeper price extraction; this is slower.

Set `CHROME_PATH` if your Chrome installation uses a different location. Do not expose this app publicly without adding authentication and URL restrictions.
