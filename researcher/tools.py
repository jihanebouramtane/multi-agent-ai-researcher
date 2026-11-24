"""Outils des agents : compacts (peu de tokens), avec timeouts, cache et protection SSRF."""
import functools
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from researcher.security import get_secret, is_safe_url

TIMEOUT = 15
HEADERS = {"User-Agent": "MultiAgentResearcher/1.0 (portfolio project)"}
CACHE_TTL = 3600
MAX_ARTICLE_BYTES = 2_000_000


def ttl_cache(func):
    """Cache d'une heure : une meme recherche n'appelle pas deux fois l'API externe."""
    store: dict = {}

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        key = (args, tuple(sorted(kwargs.items())))
        hit = store.get(key)
        if hit and time.time() - hit[0] < CACHE_TTL:
            return hit[1]
        result = func(*args, **kwargs)
        if not str(result).startswith(("Web search unavailable", "Refused")):  # on ne garde pas les echecs
            store[key] = (time.time(), result)
        return result

    return wrapper


def _get(url: str, **kwargs) -> httpx.Response:
    r = httpx.get(url, headers=HEADERS, timeout=TIMEOUT, **kwargs)
    r.raise_for_status()
    return r


def _clamp(value, low, high) -> int:
    return max(low, min(int(value), high))


def _hn_link(item_id) -> str:
    return f"https://news.ycombinator.com/item?id={item_id}"


# ---------- Hacker News ----------

@ttl_cache
def search_hackernews(query: str, num_results: int = 8, last_days: int = 0) -> str:
    """Search Hacker News stories by keyword across the whole archive. Use this to find stories about a topic.

    Args:
        query: Keywords to search for, e.g. "AI agents".
        num_results: Number of stories to return (1 to 20).
        last_days: Only return stories from the last N days. 0 means no date limit.

    Returns:
        A JSON list of stories with title, url, points, comments, date and hn_link.
    """
    params = {"query": query, "tags": "story", "hitsPerPage": _clamp(num_results, 1, 20)}
    if int(last_days) > 0:
        params["numericFilters"] = f"created_at_i>{int(time.time()) - int(last_days) * 86400}"
    hits = _get("https://hn.algolia.com/api/v1/search", params=params).json().get("hits", [])
    return json.dumps([{
        "title": h.get("title"),
        "url": h.get("url") or _hn_link(h.get("objectID")),
        "points": h.get("points"),
        "comments": h.get("num_comments"),
        "date": (h.get("created_at") or "")[:10],
        "hn_link": _hn_link(h.get("objectID")),
    } for h in hits], ensure_ascii=False)


@ttl_cache
def get_top_hackernews_stories(num_stories: int = 10) -> str:
    """Get the current top stories on the Hacker News front page (what is trending right now).

    Args:
        num_stories: Number of stories to return (1 to 30).

    Returns:
        A JSON list of stories with title, url, points, comments and hn_link.
    """
    base = "https://hacker-news.firebaseio.com/v0"
    ids = _get(f"{base}/topstories.json").json()[: _clamp(num_stories, 1, 30)]

    def fetch(item_id):
        item = _get(f"{base}/item/{item_id}.json").json() or {}
        return {"title": item.get("title"), "url": item.get("url") or _hn_link(item_id),
                "points": item.get("score"), "comments": item.get("descendants"), "hn_link": _hn_link(item_id)}

    with ThreadPoolExecutor(max_workers=10) as pool:
        return json.dumps(list(pool.map(fetch, ids)), ensure_ascii=False)


# ---------- GitHub et arXiv (nouveaux outils) ----------

@ttl_cache
def search_github_repositories(query: str, num_results: int = 5, created_last_days: int = 0) -> str:
    """Search GitHub repositories about a topic, sorted by stars. Use it to find popular open-source projects and tools.

    Args:
        query: Topic keywords, e.g. "ai agents framework".
        num_results: Number of repositories to return (1 to 10).
        created_last_days: Only repositories created in the last N days (to spot new rising projects). 0 means no limit.

    Returns:
        A JSON list of repositories with name, description, stars, language, url and last update.
    """
    q = query
    if int(created_last_days) > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=int(created_last_days))).strftime("%Y-%m-%d")
        q += f" created:>{since}"
    headers = dict(HEADERS, Accept="application/vnd.github+json")
    token = get_secret("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = httpx.get("https://api.github.com/search/repositories", headers=headers, timeout=TIMEOUT,
                  params={"q": q, "sort": "stars", "order": "desc", "per_page": _clamp(num_results, 1, 10)})
    r.raise_for_status()
    return json.dumps([{
        "name": repo.get("full_name"),
        "description": (repo.get("description") or "")[:200],
        "stars": repo.get("stargazers_count"),
        "language": repo.get("language"),
        "url": repo.get("html_url"),
        "updated": (repo.get("pushed_at") or "")[:10],
    } for repo in r.json().get("items", [])], ensure_ascii=False)


@ttl_cache
def search_arxiv_papers(query: str, max_results: int = 5) -> str:
    """Search the most recent research papers on arXiv about a topic.

    Args:
        query: Topic keywords, e.g. "LLM agents".
        max_results: Number of papers to return (1 to 10).

    Returns:
        A JSON list of papers with title, authors, date, a short summary and url.
    """
    params = {"search_query": f"all:{query}", "sortBy": "submittedDate", "sortOrder": "descending",
              "max_results": _clamp(max_results, 1, 10)}
    root = ET.fromstring(_get("https://export.arxiv.org/api/query", params=params).text)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("a:entry", ns):
        def text(tag):
            return " ".join((entry.findtext(tag, default="", namespaces=ns) or "").split())
        papers.append({
            "title": text("a:title"),
            "authors": [a.findtext("a:name", default="", namespaces=ns) for a in entry.findall("a:author", ns)][:3],
            "date": text("a:published")[:10],
            "summary": text("a:summary")[:400],
            "url": text("a:id"),
        })
    return json.dumps(papers, ensure_ascii=False)


# ---------- Web ----------

@ttl_cache
def search_web(query: str, max_results: int = 5) -> str:
    """Search the web (DuckDuckGo) for recent information on a topic.

    Args:
        query: What to search for.
        max_results: Number of results (1 to 8).

    Returns:
        A JSON list of results with title, url and snippet, or an error message if search is unavailable.
    """
    try:
        from ddgs import DDGS

        results = DDGS().text(query, max_results=_clamp(max_results, 1, 8))
        return json.dumps([{"title": r.get("title"), "url": r.get("href"), "snippet": (r.get("body") or "")[:250]}
                           for r in results], ensure_ascii=False)
    except Exception as e:
        return f"Web search unavailable right now ({type(e).__name__}). Use search_wikipedia instead."


@ttl_cache
def search_wikipedia(query: str, max_results: int = 3) -> str:
    """Search Wikipedia for background information and definitions on a topic.

    Args:
        query: What to search for.
        max_results: Number of articles (1 to 5).

    Returns:
        A JSON list of articles with title, snippet and url.
    """
    params = {"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": _clamp(max_results, 1, 5)}
    results = _get("https://en.wikipedia.org/w/api.php", params=params).json().get("query", {}).get("search", [])
    return json.dumps([{
        "title": r["title"],
        "snippet": re.sub(r"<[^>]+>", "", r.get("snippet", "")),
        "url": "https://en.wikipedia.org/wiki/" + r["title"].replace(" ", "_"),
    } for r in results], ensure_ascii=False)


@ttl_cache
def read_article(url: str, max_chars: int = 4000) -> str:
    """Read the main text of a public web article.

    Args:
        url: The article URL (http or https).
        max_chars: Maximum number of characters to return (500 to 6000).

    Returns:
        The article title and text, shortened to max_chars.
    """
    # Les redirections sont suivies a la main pour verifier chaque adresse (protection SSRF)
    current = url
    for _ in range(4):
        if not is_safe_url(current):
            return f"Refused: {current} is not a public http(s) address."
        with httpx.stream("GET", current, headers=HEADERS, timeout=TIMEOUT, follow_redirects=False) as r:
            if r.is_redirect:
                current = str(r.url.join(r.headers.get("location", "")))
                continue
            r.raise_for_status()
            body = b""
            for chunk in r.iter_bytes():
                body += chunk
                if len(body) > MAX_ARTICLE_BYTES:
                    break
            html = body.decode(r.encoding or "utf-8", errors="replace")
        break
    else:
        return "Refused: too many redirects."

    from newspaper import Article

    article = Article(current)
    article.download(input_html=html)
    article.parse()
    text = " ".join(article.text.split())
    limit = _clamp(max_chars, 500, 6000)
    return f"Title: {article.title}\nURL: {current}\n\n{text[:limit]}" + ("..." if len(text) > limit else "")
