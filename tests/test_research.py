"""Research tests: sourced excerpts, prose selection, and honest failure.

No network: the two discovery APIs and the page reader are stubbed, so these
assert the logic that decides *which* text answers a question and that every
claim keeps the URL it came from.
"""

from __future__ import annotations

from typing import Any

import pytest

from myagent.config import Settings
from myagent.security.taint import TurnContext
from myagent.tools import research
from myagent.tools.registry import ToolContext, ToolError

DDG_RESPONSE = {
    "Heading": "Solar panel",
    "AbstractURL": "https://en.wikipedia.org/wiki/Solar_panel",
    "Results": [{"FirstURL": "https://solar.example/official", "Text": "Official site"}],
    "RelatedTopics": [
        {"FirstURL": "https://duckduckgo.com/c/Solar_energy", "Text": "category"},
        {"FirstURL": "https://panels.example/eff", "Text": "Panel efficiency"},
    ],
}

WIKI_SEARCH = {"query": {"search": [{"title": "Solar panel"}, {"title": "Photovoltaic system"}]}}

PAGES = {
    "https://en.wikipedia.org/wiki/Solar_panel": (
        "Solar panel",
        "Cookie notice. This website uses cookies to improve your browsing experience "
        "and analyse traffic.\n\n"
        "A solar panel converts sunlight into electricity using photovoltaic cells, "
        "and its efficiency describes how much of that light becomes power.\n\n"
        "Company history footer text from nineteen ninety onwards, unrelated entirely.",
    ),
    "https://solar.example/official": (
        "Official",
        "Modern silicon panels reach an efficiency of about twenty two percent under "
        "standard test conditions in laboratories.",
    ),
    "https://panels.example/eff": (
        "Efficiency",
        "Panels degrade slowly over several decades of ordinary rooftop use.",
    ),
}


@pytest.fixture
def context(settings: Settings) -> ToolContext:
    return ToolContext(
        turn=TurnContext(session_id="s"), db_path=settings.db_path(), settings=settings
    )


@pytest.fixture
def apis(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub both discovery APIs and the source reader."""

    def fake_get(url: str, params: dict[str, Any]) -> dict[str, Any]:
        if url == research.DDG_API:
            return DDG_RESPONSE
        if params.get("list") == "search":
            return WIKI_SEARCH
        return {}

    def fake_read(url: str) -> tuple[str, str]:
        if url not in PAGES:
            raise ConnectionError("404")
        return PAGES[url]

    monkeypatch.setattr(research, "_get", fake_get)
    monkeypatch.setattr(research, "read_source", fake_read)


class TestDiscovery:
    def test_both_apis_contribute_sources(self, apis: None) -> None:
        urls = [item["url"] for item in research.discover("solar panels", 10)]
        assert "https://en.wikipedia.org/wiki/Solar_panel" in urls  # instant answer
        assert "https://en.wikipedia.org/wiki/Photovoltaic_system" in urls  # wiki search

    def test_the_search_engine_itself_is_not_a_source(self, apis: None) -> None:
        """RelatedTopics are mostly duckduckgo category pages, not sources."""
        urls = [item["url"] for item in research.discover("solar panels", 10)]
        assert not any("duckduckgo.com" in url for url in urls)

    def test_duplicates_are_dropped(self, apis: None) -> None:
        urls = [item["url"] for item in research.discover("solar panels", 10)]
        assert len(urls) == len(set(urls))

    def test_the_abstract_source_comes_first(self, apis: None) -> None:
        """The instant answer is the most on-topic thing available."""
        assert research.discover("solar panels", 5)[0]["url"].endswith("Solar_panel")

    def test_a_dead_api_degrades_instead_of_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One API down still leaves the other; neither takes the turn with it."""
        monkeypatch.setattr(
            research,
            "_get",
            lambda url, params: WIKI_SEARCH if params.get("list") == "search" else {},
        )
        assert research.discover("solar panels", 5)


class TestSearchTool:
    def test_results_are_returned_with_urls(self, context: ToolContext, apis: None) -> None:
        found = research.search(context, query="solar panels")
        assert found["count"] > 0
        assert all(item["url"].startswith("https://") for item in found["results"])

    def test_searching_taints_the_turn(self, context: ToolContext, apis: None) -> None:
        research.search(context, query="solar panels")
        assert context.turn.tainted is True

    def test_no_sources_is_an_honest_error(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Say nothing was found rather than answer from memory."""
        monkeypatch.setattr(research, "discover", lambda query, limit: [])
        with pytest.raises(ToolError, match="no sources found"):
            research.search(context, query="what happened five minutes ago")

    def test_an_empty_query_is_refused(self, context: ToolContext, apis: None) -> None:
        with pytest.raises(ToolError, match="empty"):
            research.search(context, query="   ")


class TestAnswerTool:
    def test_every_excerpt_keeps_its_source(self, context: ToolContext, apis: None) -> None:
        result = research.answer(context, question="how efficient are solar panels", sources=2)

        assert len(result["sources"]) == 2
        for source in result["sources"]:
            assert source["url"].startswith("https://")
            assert source["excerpt"]

    def test_the_relevant_paragraph_beats_boilerplate(
        self, context: ToolContext, apis: None
    ) -> None:
        result = research.answer(context, question="solar panel efficiency", sources=1)
        excerpt = result["sources"][0]["excerpt"]

        assert "efficiency" in excerpt
        assert "cookie" not in excerpt.lower()

    def test_reading_taints_the_turn_with_each_url(self, context: ToolContext, apis: None) -> None:
        research.answer(context, question="solar panel efficiency", sources=1)
        assert context.turn.tainted is True
        assert any("wikipedia.org" in source for source in context.turn.taint_sources)

    def test_one_unreadable_source_does_not_sink_the_research(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch, apis: None
    ) -> None:
        broken = {url: page for url, page in PAGES.items() if "wikipedia" not in url}

        def partial(url: str) -> tuple[str, str]:
            if url not in broken:
                raise ConnectionError("timed out")
            return broken[url]

        monkeypatch.setattr(research, "read_source", partial)
        result = research.answer(context, question="solar panel efficiency", sources=2)

        assert result["sources"]
        assert any("Solar_panel" in item["url"] for item in result["unreadable"])

    def test_it_says_so_when_nothing_could_be_read(
        self, context: ToolContext, monkeypatch: pytest.MonkeyPatch, apis: None
    ) -> None:
        def always_fail(url: str) -> tuple[str, str]:
            raise ConnectionError("unreachable")

        monkeypatch.setattr(research, "read_source", always_fail)
        with pytest.raises(ToolError, match="could not read any"):
            research.answer(context, question="solar panel efficiency")

    def test_the_caller_is_told_to_cite(self, context: ToolContext, apis: None) -> None:
        result = research.answer(context, question="solar panel efficiency", sources=1)
        assert "cite" in result["note"].lower()

    def test_source_count_is_capped(self, context: ToolContext, apis: None) -> None:
        result = research.answer(context, question="solar", sources=99)
        assert len(result["sources"]) <= research.MAX_SOURCES


class TestWikipediaReading:
    def test_wikipedia_urls_are_recognised(self) -> None:
        assert (
            research._wikipedia_title("https://en.wikipedia.org/wiki/Solar_panel") == "Solar panel"
        )
        assert research._wikipedia_title("https://nasa.gov/webb") is None

    def test_wikipedia_is_read_by_api_not_browser(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """So research still works on a machine with no Chromium installed."""

        def no_browser() -> Any:
            raise AssertionError("the browser must not be used for Wikipedia")

        monkeypatch.setattr(
            research,
            "_get",
            lambda url, params: {
                "query": {"pages": {"1": {"title": "Solar panel", "extract": "Panels convert."}}}
            },
        )
        monkeypatch.setattr(research.browsing, "session", no_browser)

        title, text = research.read_source("https://en.wikipedia.org/wiki/Solar_panel")

        assert title == "Solar panel"
        assert text == "Panels convert."

    def test_a_missing_extract_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(research, "_get", lambda url, params: {"query": {"pages": {}}})
        with pytest.raises(ConnectionError, match="no extract"):
            research.read_source("https://en.wikipedia.org/wiki/Nonexistent")


class TestExcerptSelection:
    def test_navigation_soup_loses_to_prose(self) -> None:
        """Real failure: a NASA page's menu outscored its actual content."""
        text = (
            "MissionsSearch All NASA MissionsA to Z List of MissionsUpcoming "
            "LaunchesSpaceships and RocketsJames Webb Space Telescope\n\n"
            "The James Webb Space Telescope is the largest telescope ever placed in "
            "space, observing the universe in infrared light. It launched in 2021."
        )
        excerpt = research._excerpt(text, {"webb", "telescope", "space"})
        assert excerpt.startswith("The James Webb")

    def test_a_page_of_only_menus_still_returns_something(self) -> None:
        """Falling back is better than an empty excerpt."""
        text = "HomeAboutContactProductsServicesSupportLoginRegisterCartCheckoutBlogNewsroom"
        assert research._excerpt(text, {"home"})

    def test_excerpts_stay_within_the_observation_budget(self) -> None:
        """Three of these are resent on every later step of the turn."""
        text = "\n\n".join(
            f"Paragraph {index} about solar energy systems and how they work. " * 3
            for index in range(50)
        )
        assert len(research._excerpt(text, {"solar", "energy"})) <= research.EXCERPT_CHARS

    def test_paragraphs_keep_their_original_order(self) -> None:
        """A reordered excerpt reads as nonsense even when every line is relevant."""
        text = (
            "First, solar panels absorb light from the sun during the daytime hours.\n\n"
            "Then an inverter converts that direct current into alternating current.\n\n"
            "Finally the electricity flows into your home or back to the power grid."
        )
        excerpt = research._excerpt(text, {"solar", "electricity", "inverter"})
        assert excerpt.index("First") < excerpt.index("Then") < excerpt.index("Finally")

    def test_question_words_are_ignored_when_scoring(self) -> None:
        assert research._keywords("what is the efficiency of a panel") == {"efficiency", "panel"}
