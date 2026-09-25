"""Optional live evidence source: Wikipedia (no API key needed).

Results are treated as medium-reliability evidence (0.75). Any failure
(network blocked, timeout) degrades gracefully to "no extra evidence".
"""
from __future__ import annotations

import httpx

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "VeriMind/1.0 (hackathon multi-agent verification prototype)"}


async def wikipedia_search(query: str, limit: int = 2, timeout: float = 6.0) -> list[dict]:
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=HEADERS) as client:
            r = await client.get(WIKI_API, params={
                "action": "query", "list": "search", "srsearch": query, "srlimit": limit, "format": "json",
            })
            r.raise_for_status()
            titles = [h["title"] for h in r.json().get("query", {}).get("search", [])]
            if not titles:
                return []
            r2 = await client.get(WIKI_API, params={
                "action": "query", "prop": "extracts|info", "exintro": 1, "explaintext": 1, "inprop": "url",
                "titles": "|".join(titles), "format": "json", "redirects": 1,
            })
            r2.raise_for_status()
            pages = r2.json().get("query", {}).get("pages", {})
    except Exception:
        return []
    docs = []
    for p in pages.values():
        text = (p.get("extract") or "").strip()
        if not text:
            continue
        docs.append({
            "id": f"wiki-{p.get('pageid')}",
            "title": f"Wikipedia: {p.get('title')}",
            "source": "Wikipedia",
            "source_type": "web",
            "reliability": 0.75,
            "date": (p.get("touched") or "")[:10] or None,
            "entity": p.get("title"),
            "text": text[:3000],
            "origin": "web",
            "url": p.get("fullurl"),
        })
    return docs
