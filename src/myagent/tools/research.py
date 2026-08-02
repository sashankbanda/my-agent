"""Research: find sources, read them, return excerpts that keep their URLs.

One tool call replaces the "search, open, read, go back, open, read" sequence
a model would otherwise need six turns and six page loads to perform - each of
which resends the previous page's text and burns a free tier's tokens-per-
minute budget.

**Why there is no search-engine scraping here.** The obvious implementation -
drive a headless browser at DuckDuckGo or Bing - was built first and does not
work: measured on this machine, DuckDuckGo's HTML and Lite endpoints both
answer a headless browser with a CAPTCHA ("bots use DuckDuckGo too"), Mojeek
serves an Altcha challenge, and Bing returns results for the wrong query. A
realistic user agent and the usual automation-flag tricks changed nothing.
Scraping engines that actively block scraping is a treadmill, and one that
fails silently at the worst moment.

So discovery uses interfaces built to be called: DuckDuckGo's Instant Answer
API and Wikipedia's search API. Both are free, need no key, and do not block
automated traffic.

**What that costs.** This is strong for factual and reference questions and
weak for "what happened today" - neither API indexes the live news cycle. The
tool description says so, and the honest failure is "I could not find sources"
rather than an answer from memory dressed up as research. When the user names
a site, ``browser.open`` reads it directly and perfectly well.

Reading prefers Wikipedia's extract API over the browser for Wikipedia pages,
which means research still works when Chromium is not installed at all.

Everything fetched here is untrusted content and taints the turn, exactly as
``browser`` does.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from myagent.logging import get_logger
from myagent.security.tiers import Tier
from myagent.tools import browsing
from myagent.tools.registry import ToolContext, ToolError, tool

log = get_logger(__name__)

DDG_API = "https://api.duckduckgo.com/"
WIKI_API = "https://en.wikipedia.org/w/api.php"
# Wikipedia asks automated clients to identify themselves; doing so is the
# difference between being welcome and being rate-limited.
USER_AGENT = "MyAgent/0.1 (personal assistant; https://github.com/sashankbanda/my-agent)"
HTTP_TIMEOUT_S = 20.0

MAX_SOURCES = 5
DEFAULT_SOURCES = 3
EXCERPT_CHARS = 1_200  # per source; three of these fit the loop's 4k observation cap
MIN_PARAGRAPH_CHARS = 60
WIKI_EXTRACT_CHARS = 20_000

STOP_WORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "your",
        "you",
        "my",
        "me",
        "i",
        "do",
        "does",
    ]
)


def _keywords(question: str) -> set[str]:
    """Content words of the question, used to score paragraphs."""
    words = re.findall(r"[a-z0-9']+", question.lower())
    return {word for word in words if len(word) > 2 and word not in STOP_WORDS}


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    """One JSON GET with an honest user agent, or an empty result."""
    try:
        response = httpx.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=HTTP_TIMEOUT_S,
            follow_redirects=True,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.info("research_api_failed", url=url, error=str(exc)[:120])
        return {}


def _instant_answer(query: str) -> list[dict[str, str]]:
    """Sources from DuckDuckGo's Instant Answer API (no key, no blocking)."""
    data = _get(DDG_API, {"q": query, "format": "json", "no_html": 1, "skip_disambig": 1})
    found: list[dict[str, str]] = []
    abstract_url = data.get("AbstractURL")
    if abstract_url:
        found.append({"url": abstract_url, "title": data.get("Heading") or query})
    for entry in data.get("Results", []):
        if isinstance(entry, dict) and entry.get("FirstURL"):
            found.append({"url": entry["FirstURL"], "title": entry.get("Text") or query})
    # RelatedTopics are mostly duckduckgo.com category pages, which are not
    # sources; keep only ones that point somewhere real.
    for entry in data.get("RelatedTopics", []):
        if not isinstance(entry, dict):
            continue
        url = entry.get("FirstURL", "")
        if url and "duckduckgo.com" not in urlparse(url).netloc:
            found.append({"url": url, "title": entry.get("Text") or url})
    return found


def _wikipedia_search(query: str, limit: int) -> list[dict[str, str]]:
    """Top Wikipedia articles for a query."""
    data = _get(
        WIKI_API,
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": limit,
            "format": "json",
        },
    )
    hits = data.get("query", {}).get("search", [])
    return [
        {
            "url": f"https://en.wikipedia.org/wiki/{quote(hit['title'].replace(' ', '_'))}",
            "title": hit["title"],
        }
        for hit in hits
        if hit.get("title")
    ]


def discover(query: str, limit: int) -> list[dict[str, str]]:
    """Candidate sources for a query, best first, de-duplicated."""
    candidates = _instant_answer(query) + _wikipedia_search(query, limit)
    seen: set[str] = set()
    results: list[dict[str, str]] = []
    for candidate in candidates:
        url = candidate["url"]
        if url in seen or not url.startswith(("http://", "https://")):
            continue
        seen.add(url)
        results.append(candidate)
        if len(results) >= limit:
            break
    return results


def _wikipedia_title(url: str) -> str | None:
    """The article title in a Wikipedia URL, or None if it is not one."""
    parsed = urlparse(url)
    if not parsed.netloc.endswith("wikipedia.org") or "/wiki/" not in parsed.path:
        return None
    return parsed.path.split("/wiki/", 1)[1].replace("_", " ")


def read_source(url: str) -> tuple[str, str]:
    """Fetch one source as ``(title, plain text)``.

    Wikipedia has an extract API that returns clean prose, so it is used in
    preference to the browser: fewer moving parts, and research keeps working
    on a machine where Chromium was never installed.
    """
    title = _wikipedia_title(url)
    if title is not None:
        data = _get(
            WIKI_API,
            {
                "action": "query",
                "prop": "extracts",
                "explaintext": 1,
                "titles": title,
                "format": "json",
                "redirects": 1,
            },
        )
        pages = data.get("query", {}).get("pages", {})
        for page in pages.values():
            extract = page.get("extract")
            if extract:
                return page.get("title", title), extract[:WIKI_EXTRACT_CHARS]
        raise ConnectionError(f"Wikipedia had no extract for {title!r}")

    page = browsing.session().goto(url)
    return page.title, page.text


MIN_SPACE_SHARE = 0.12  # navigation runs words together; prose does not


def _is_prose(block: str) -> bool:
    """True when a block reads like sentences rather than a menu."""
    if block.count(". ") == 0 and not block.rstrip().endswith("."):
        return False
    return block.count(" ") / len(block) >= MIN_SPACE_SHARE


def _excerpt(text: str, keywords: set[str]) -> str:
    """The passages of a source most likely to answer the question.

    Paragraphs are scored by how many of the question's content words they
    contain, then re-joined in their original order so the excerpt still reads
    as prose rather than a bag of sentences. Crude, free, and wrong only in
    ways that cost a slightly worse excerpt - the source URL is always there.
    """
    blocks = [
        paragraph.strip()
        for paragraph in re.split(r"\n\s*\n|\n(?=[A-Z=])", text)
        if len(paragraph.strip()) >= MIN_PARAGRAPH_CHARS
    ]
    # Site menus survive HTML stripping as long runs without sentences
    # ("MissionsSearch All NASA MissionsA to Z List of..."). Prefer blocks that
    # read as prose, but fall back if a page has none rather than returning
    # nothing at all.
    prose = [block for block in blocks if _is_prose(block)]
    paragraphs = prose or blocks
    if not paragraphs:
        return text[:EXCERPT_CHARS].strip()

    scored: list[tuple[int, int, str]] = []
    for index, paragraph in enumerate(paragraphs):
        words = set(re.findall(r"[a-z0-9']+", paragraph.lower()))
        scored.append((len(words & keywords), index, paragraph))

    chosen: list[tuple[int, str]] = []
    used = 0
    for score, index, paragraph in sorted(scored, key=lambda item: (-item[0], item[1])):
        if score == 0 and chosen:
            break  # nothing left that relates to the question
        if used + len(paragraph) > EXCERPT_CHARS and chosen:
            continue
        chosen.append((index, paragraph))
        used += len(paragraph)
        if used >= EXCERPT_CHARS:
            break
    return "\n\n".join(paragraph for _index, paragraph in sorted(chosen))[:EXCERPT_CHARS]


@tool(
    name="research.search",
    tier=Tier.READ,
    description=(
        "Find web sources for a topic and return their titles and URLs, "
        "without reading them. Best for factual and reference topics. Use "
        "research.answer when you want the pages actually read, or "
        "browser.open when the user named a specific site."
    ),
    params={
        "query": {"type": "string", "description": "What to look for"},
        "limit": {"type": "integer", "description": "How many sources (default 5, max 10)"},
    },
    required=["query"],
    summarize=lambda args: f"look up {args.get('query')!r} on the web",
)
def search(context: ToolContext, query: str, limit: int = 5) -> dict[str, Any]:
    """Return candidate sources without fetching any of them."""
    cleaned = query.strip()
    if not cleaned:
        raise ToolError("query is empty")
    results = discover(cleaned, max(1, min(limit, 10)))
    context.turn.taint("web search results")
    log.info("research_search", query=cleaned, results=len(results))
    if not results:
        raise ToolError(
            f"no sources found for {cleaned!r}. This looks up reference material, "
            "not today's news - if you know the site, open it directly."
        )
    return {"query": cleaned, "count": len(results), "results": results}


@tool(
    name="research.answer",
    tier=Tier.REVERSIBLE,
    description=(
        "Research a question: finds sources, reads them, and returns an "
        "excerpt from each with its URL. Write your answer from these "
        "excerpts and CITE each source by URL. If they do not answer the "
        "question, say so - do not fill the gap from memory. Strong on "
        "factual and reference questions, weak on breaking news."
    ),
    params={
        "question": {"type": "string", "description": "The question to research"},
        "sources": {
            "type": "integer",
            "description": "How many sources to read (default 3, max 5)",
        },
    },
    required=["question"],
    summarize=lambda args: f"research {args.get('question')!r} on the web",
)
def answer(context: ToolContext, question: str, sources: int = DEFAULT_SOURCES) -> dict[str, Any]:
    """Find sources, read them, and return sourced excerpts."""
    cleaned = question.strip()
    if not cleaned:
        raise ToolError("question is empty")
    wanted = max(1, min(sources, MAX_SOURCES))

    candidates = discover(cleaned, wanted + 2)
    context.turn.taint("web search results")
    if not candidates:
        raise ToolError(
            f"no sources found for {cleaned!r}. This looks up reference material, "
            "not today's news - if you know the site, open it directly."
        )

    keywords = _keywords(cleaned)
    gathered: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for candidate in candidates:
        if len(gathered) >= wanted:
            break
        try:
            title, text = read_source(candidate["url"])
        except Exception as exc:  # one bad source must not sink the research
            log.info("research_source_failed", url=candidate["url"], error=str(exc)[:120])
            failures.append({"url": candidate["url"], "error": str(exc)[:120]})
            continue
        context.turn.taint(f"web page {candidate['url']}")
        excerpt = _excerpt(text, keywords)
        if not excerpt:
            failures.append({"url": candidate["url"], "error": "no readable text"})
            continue
        gathered.append(
            {"url": candidate["url"], "title": title or candidate["title"], "excerpt": excerpt}
        )

    if not gathered:
        raise ToolError(
            f"found {len(candidates)} sources for {cleaned!r} but could not read any of them"
        )
    log.info("research_answer", question=cleaned, sources=len(gathered), failed=len(failures))
    return {
        "question": cleaned,
        "sources": gathered,
        "unreadable": failures,
        "note": "Answer from these excerpts and cite each claim with its source URL.",
    }
