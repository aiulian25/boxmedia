"""Two films, one title: the source decides, or the card admits it does not know.

The audit's reproduction: Nosferatu (1922) then Nosferatu (2024) from Radarr, with the release
page naming 2024. The run picked 1922, with no warning and no call to the source.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.core import crypto
from app.core.audit import AuditLog
from app.core.config import Settings
from app.services.apps import AppsStore
from app.services.boxoffice import BoxOfficeEntry
from app.services.corrections import Correction, CorrectionStore
from app.services.ignore import IgnoreStore
from app.services.pipeline import Pipeline
from app.services.radarr import RadarrError, RadarrLookupResult, RadarrMovie
from app.services.release_ids import ReleaseIdCache
from app.services.reports import (
    MATCHED_AMBIGUOUS,
    MATCHED_BY_GUESS,
    SCOPE_OTHER,
    SCOPE_SAME,
    SCOPE_UNVERIFIED,
    MovieAction,
    MovieResult,
    MovieStatus,
    Report,
    ReportsStore,
    ReportTotals,
    RunStatus,
    RunTrigger,
)
from tests.conftest import AppHarness

WEEK = "2026W30"
TITLE = "Nosferatu"
NORMALIZED = "nosferatu"
RELEASE_2024 = "/release/rl2024/"
RELEASE_1922 = "/release/rl1922/"
IMDB_2024 = "tt5040012"
IMDB_1922 = "tt0013442"
TMDB_1922 = 653
TMDB_2024 = 426063
RADARR_URL = "http://radarr.local:7878"
RADARR_KEY = "0123456789abcdef0123456789abcdef"  # noqa: S105 — the suite's dummy key
LOOKUP_URL = f"{RADARR_URL}/api/v3/movie/lookup"


def _film(tmdb: int, year: int, imdb: str | None, title: str = TITLE) -> RadarrLookupResult:
    return RadarrLookupResult(
        tmdb_id=tmdb, title=title, year=year, overview="x", poster_url=f"http://poster/{tmdb}.jpg",
        genres=("Horror",), imdb_id=imdb, rating=float(year) / 1000,
    )


ORIGINAL = _film(TMDB_1922, 1922, IMDB_1922)
REMAKE = _film(TMDB_2024, 2024, IMDB_2024)


class _Radarr:
    def __init__(self, answers: dict[str, list[RadarrLookupResult]], library=()) -> None:
        self.answers = answers
        self.library = list(library)
        self.terms: list[str] = []
        self.fail_terms: set[str] = set()

    async def list_movies(self) -> list[RadarrMovie]:
        return self.library

    async def lookup(self, term: str) -> list[RadarrLookupResult]:
        self.terms.append(term)
        if term in self.fail_terms:
            raise RadarrError("metadata service is down")
        return self.answers.get(term, [])


class _Release:
    def __init__(self, answers: dict[str, str | None]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    async def __call__(self, path: str) -> str | None:
        self.asked.append(path)
        return self.answers.get(path)


def _chart(path: str | None = RELEASE_2024, title: str = TITLE) -> list[BoxOfficeEntry]:
    return [BoxOfficeEntry(
        rank=1, title=title, gross_amount=1_000, weeks_in_release=1, release_path=path
    )]


class _Env:
    def __init__(self, tmp_path: Path, radarr: _Radarr, release: _Release) -> None:
        key_file = tmp_path / "k.key"
        crypto._main(["genkey", str(key_file)])
        settings = Settings(
            _env_file=None, session_secret="s" * 40, encryption_key_file=key_file,
            data_dir=tmp_path / "data",
        )
        settings.ensure_data_dirs()
        audit = AuditLog(settings.logs_dir / "audit.jsonl")
        self.radarr, self.release = radarr, release
        self.reports = ReportsStore(settings.history_dir)
        self.corrections = CorrectionStore(settings.config_dir)
        self.chart = _chart()

        async def fetch(week: str | None) -> tuple[str, list[BoxOfficeEntry]]:
            return WEEK, self.chart

        self.pipeline = Pipeline(
            apps=AppsStore(settings.config_dir, key=crypto.generate_key(), audit=audit),
            reports_store=self.reports,
            settings=settings,
            audit=audit,
            ignore_store=IgnoreStore(settings.config_dir, audit=audit),
            fetch_chart=fetch,
            make_radarr=lambda _: radarr,
            select_app_id=lambda: "app-1",
            corrections=self.corrections,
            release_ids=ReleaseIdCache(settings.cache_dir),
            fetch_release_id=release,
        )

    async def row(self) -> MovieResult:
        report = await self.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK)
        return report.movies[0]


@pytest.fixture
def namesakes(tmp_path: Path) -> _Env:
    return _Env(
        tmp_path,
        _Radarr({TITLE: [ORIGINAL, REMAKE]}),
        _Release({RELEASE_2024: IMDB_2024, RELEASE_1922: IMDB_1922}),
    )


# --- resolving it ---


async def test_a_remake_resolves_to_the_film_the_source_names(namesakes: _Env) -> None:
    row = await namesakes.row()

    assert row.tmdb_id == TMDB_2024
    assert row.detail is None
    assert row.source_imdb_id == IMDB_2024
    assert row.release_path == RELEASE_2024


async def test_a_remake_nothing_can_settle_stays_uncertain_and_says_so(tmp_path: Path) -> None:
    env = _Env(tmp_path, _Radarr({TITLE: [ORIGINAL, REMAKE]}), _Release({}))

    row = await env.row()

    assert row.detail == MATCHED_AMBIGUOUS
    assert row.source_imdb_id is None


async def test_a_row_with_no_release_link_stays_uncertain(tmp_path: Path) -> None:
    env = _Env(tmp_path, _Radarr({TITLE: [ORIGINAL, REMAKE]}), _Release({}))
    env.chart = _chart(path=None)

    row = await env.row()

    assert row.detail == MATCHED_AMBIGUOUS
    assert env.release.asked == []


async def test_a_source_page_that_is_down_leaves_it_uncertain_not_failed(tmp_path: Path) -> None:
    env = _Env(tmp_path, _Radarr({TITLE: [ORIGINAL, REMAKE]}), _Release({RELEASE_2024: None}))

    report = await env.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK)

    assert report.status == RunStatus.OK
    assert report.movies[0].detail == MATCHED_AMBIGUOUS


async def test_one_film_with_that_title_costs_no_source_request(tmp_path: Path) -> None:
    env = _Env(tmp_path, _Radarr({TITLE: [REMAKE]}), _Release({RELEASE_2024: IMDB_2024}))

    row = await env.row()

    assert row.tmdb_id == TMDB_2024
    assert row.detail is None
    assert env.release.asked == []


async def test_an_empty_title_search_is_recovered_through_the_source_id(tmp_path: Path) -> None:
    """Radarr's search can miss a film Mojo spells differently; it used to end right there."""
    radarr = _Radarr({f"imdb:{IMDB_2024}": [REMAKE]})
    env = _Env(tmp_path, radarr, _Release({RELEASE_2024: IMDB_2024}))

    row = await env.row()

    assert row.tmdb_id == TMDB_2024
    assert row.action == MovieAction.NONE
    assert row.detail is None
    assert radarr.terms == [TITLE, f"imdb:{IMDB_2024}"]


@pytest.mark.parametrize(
    "answers, failing",
    [({}, None), ({f"imdb:{IMDB_2024}": [ORIGINAL]}, None), ({}, f"imdb:{IMDB_2024}")],
    ids=["id-finds-nothing", "id-finds-another-film", "id-lookup-down"],
)
async def test_an_empty_search_with_no_recovery_is_still_no_match(
    tmp_path: Path, answers: dict, failing: str | None
) -> None:
    radarr = _Radarr(answers)
    if failing:
        radarr.fail_terms.add(failing)
    env = _Env(tmp_path, radarr, _Release({RELEASE_2024: IMDB_2024}))

    row = await env.row()

    assert row.action == MovieAction.NO_MATCH
    assert row.tmdb_id is None
    assert row.release_path == RELEASE_2024, "the source is still worth recording"


async def test_a_single_match_the_source_contradicts_is_not_confident(tmp_path: Path) -> None:
    """Only spends what it already knows: a cached id, no new request."""
    radarr = _Radarr({TITLE: [ORIGINAL]})
    env = _Env(tmp_path, radarr, _Release({}))
    env.pipeline._release_ids.put(RELEASE_2024, IMDB_2024)

    row = await env.row()

    assert row.detail == MATCHED_BY_GUESS
    assert env.release.asked == []


async def test_a_single_match_contradicted_finds_the_right_film_by_id(tmp_path: Path) -> None:
    radarr = _Radarr({TITLE: [ORIGINAL], f"imdb:{IMDB_2024}": [REMAKE]})
    env = _Env(tmp_path, radarr, _Release({}))
    env.pipeline._release_ids.put(RELEASE_2024, IMDB_2024)

    row = await env.row()

    assert row.tmdb_id == TMDB_2024
    assert row.detail is None


async def test_the_lookup_budget_still_bounds_the_source_requests(tmp_path: Path) -> None:
    from app.services.boxoffice import MAX_RELEASE_LOOKUPS_PER_RUN

    titles = [f"Film {index}" for index in range(MAX_RELEASE_LOOKUPS_PER_RUN + 3)]
    env = _Env(tmp_path, _Radarr({}), _Release({}))
    env.chart = [
        BoxOfficeEntry(
            rank=index + 1, title=title, gross_amount=1, weeks_in_release=1,
            release_path=f"/release/rl{index}/",
        )
        for index, title in enumerate(titles)
    ]
    env.pipeline._chart_size = lambda: len(titles)

    await env.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK)

    assert len(env.release.asked) == MAX_RELEASE_LOOKUPS_PER_RUN


# --- corrections and namesakes in a run ---


async def test_a_release_correction_is_honoured_without_asking_the_source(
    namesakes: _Env,
) -> None:
    namesakes.corrections.save(
        NORMALIZED,
        Correction(tmdb_id=TMDB_1922, title=TITLE, release_path=RELEASE_2024, imdb_id=IMDB_1922),
    )

    row = await namesakes.row()

    assert row.tmdb_id == TMDB_1922, "a human looked at both posters"
    assert namesakes.release.asked == []


async def test_a_title_only_correction_does_not_capture_a_remake(namesakes: _Env) -> None:
    """Made for the 1922 film. When 2024 charts, the source tells them apart."""
    namesakes.corrections.save(
        NORMALIZED,
        Correction(
            tmdb_id=TMDB_1922, title=TITLE, imdb_url=f"https://www.imdb.com/title/{IMDB_1922}/"
        ),
    )

    row = await namesakes.row()

    assert row.tmdb_id == TMDB_2024
    assert row.source_imdb_id == IMDB_2024


async def test_a_title_only_correction_survives_a_source_outage(tmp_path: Path) -> None:
    """Nothing contradicts it, so the decision somebody made stands."""
    env = _Env(tmp_path, _Radarr({TITLE: [ORIGINAL, REMAKE]}), _Release({}))
    env.corrections.save(
        NORMALIZED,
        Correction(
            tmdb_id=TMDB_1922, title=TITLE, imdb_url=f"https://www.imdb.com/title/{IMDB_1922}/"
        ),
    )

    assert (await env.row()).tmdb_id == TMDB_1922


async def test_provenance_does_not_rewrite_an_unchanged_week(namesakes: _Env) -> None:
    """Routing, not chart data, so a re-fetch of the same figures stays a no-op."""
    first = await namesakes.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK)
    second = await namesakes.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK)

    assert second.run_at == first.run_at


# --- correcting stored history ---


def _stored_row(
    rank: int = 1,
    release_path: str | None = None,
    imdb: str | None = None,
    tmdb: int = 999,
) -> MovieResult:
    return MovieResult(
        rank=rank, title=TITLE, normalized_title=NORMALIZED, gross_amount=1, gross_display="$1",
        weeks_in_release=1, status=MovieStatus.IN_LIBRARY, action=MovieAction.NONE, tmdb_id=tmdb,
        rating=9.9, genres=["Stale"], detail=MATCHED_BY_GUESS,
        release_path=release_path, source_imdb_id=imdb,
    )


def _save_week(store: ReportsStore, week: str, row: MovieResult) -> str:
    report_id = f"report-{week}-0000"
    store.save(Report(
        id=report_id, run_at=f"2026-01-01T00:00:{week[-2:]}+00:00", trigger=RunTrigger.SCHEDULED,
        status=RunStatus.OK, week=week, totals=ReportTotals(movies=1, matched=0), movies=[row],
    ))
    return report_id


CHOSEN = Correction(
    tmdb_id=TMDB_2024, title="Nosferatu (2024)", release_path=RELEASE_2024, imdb_id=IMDB_2024
)


@pytest.fixture
def history(tmp_path: Path) -> ReportsStore:
    store = ReportsStore(tmp_path)
    _save_week(store, "2026W01", _stored_row(release_path=RELEASE_2024))
    _save_week(store, "2026W02", _stored_row(release_path=RELEASE_2024))
    _save_week(store, "2026W03", _stored_row(release_path=RELEASE_1922))
    _save_week(store, "2026W04", _stored_row())
    _save_week(store, "2026W05", _stored_row(release_path="/release/rlre/", imdb=IMDB_2024))
    return store


def test_the_scope_sorts_every_appearance(history: ReportsStore) -> None:
    scope = history.correction_scope(NORMALIZED, RELEASE_2024, IMDB_2024)

    assert sorted(scope[SCOPE_SAME]) == ["2026W01", "2026W02", "2026W05"]
    assert scope[SCOPE_UNVERIFIED] == ["2026W04"]
    assert scope[SCOPE_OTHER] == ["2026W03"]


def _tmdb_by_week(store: ReportsStore) -> dict[str, int]:
    return {report.week: report.movies[0].tmdb_id for report in store.list_reports()}


def test_a_correction_reaches_this_film_and_leaves_its_namesake(history: ReportsStore) -> None:
    """Same release, a re-release by id, and an old row with no record. Not the 1922 film."""
    weeks = history.apply_correction(NORMALIZED, CHOSEN)

    assert weeks == 4
    assert _tmdb_by_week(history) == {
        "2026W01": TMDB_2024, "2026W02": TMDB_2024, "2026W03": 999,
        "2026W04": TMDB_2024, "2026W05": TMDB_2024,
    }


def test_rows_with_no_source_can_be_left_out(history: ReportsStore) -> None:
    history.apply_correction(NORMALIZED, CHOSEN, include_unverified=False)

    assert _tmdb_by_week(history)["2026W04"] == 999
    assert _tmdb_by_week(history)["2026W01"] == TMDB_2024


def test_the_row_being_corrected_always_changes(history: ReportsStore) -> None:
    history.apply_correction(
        NORMALIZED, CHOSEN, include_unverified=False, always=("report-2026W04-0000", 1)
    )

    assert _tmdb_by_week(history)["2026W04"] == TMDB_2024


def test_a_film_that_changed_takes_its_metadata_with_it(history: ReportsStore) -> None:
    """A rating and genres stay about the film they were read for."""
    history.apply_correction(NORMALIZED, CHOSEN, rating=7.5, genres=["Horror"])

    row = history.latest_for_week("2026W01").movies[0]
    assert row.rating == 7.5
    assert row.genres == ["Horror"]
    assert row.status == MovieStatus.MISSING, "the stored status described the other film"


def test_a_film_that_changed_with_nothing_looked_up_has_no_metadata(
    history: ReportsStore,
) -> None:
    history.apply_correction(NORMALIZED, CHOSEN)

    row = history.latest_for_week("2026W01").movies[0]
    assert row.rating is None
    assert row.genres == []


def test_re_confirming_the_same_film_keeps_what_it_had(tmp_path: Path) -> None:
    store = ReportsStore(tmp_path)
    _save_week(store, "2026W01", _stored_row(release_path=RELEASE_2024, tmdb=TMDB_2024))

    store.apply_correction(NORMALIZED, CHOSEN)

    row = store.latest_for_week("2026W01").movies[0]
    assert row.rating == 9.9
    assert row.status == MovieStatus.IN_LIBRARY


def test_a_correction_with_no_source_treats_every_row_as_unverified(tmp_path: Path) -> None:
    """What a correction from an old row can honestly say. Still reaches them by default."""
    store = ReportsStore(tmp_path)
    _save_week(store, "2026W01", _stored_row(release_path=RELEASE_2024))
    _save_week(store, "2026W02", _stored_row())

    scope = store.correction_scope(NORMALIZED, None, None)

    assert sorted(scope[SCOPE_UNVERIFIED]) == ["2026W01", "2026W02"]
    assert store.apply_correction(NORMALIZED, Correction(tmdb_id=1, title="T")) == 2


# --- over HTTP ---


def _candidates() -> list[dict]:
    return [
        {"tmdbId": TMDB_2024, "title": "Nosferatu", "year": 2024, "imdbId": IMDB_2024,
         "remotePoster": "http://img/ok.jpg", "genres": ["Horror"],
         "ratings": {"tmdb": {"value": 7.1}}},
    ]


def _with_history(harness: AppHarness) -> ReportsStore:
    harness.activate()
    state = harness.client.app.state
    state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)
    store = state.reports
    _save_week(store, "2026W01", _stored_row(release_path=RELEASE_2024))
    _save_week(store, "2026W02", _stored_row(release_path=RELEASE_1922))
    _save_week(store, "2026W03", _stored_row())
    return store


def _mock_radarr() -> None:
    respx.get(LOOKUP_URL).mock(return_value=httpx.Response(200, json=_candidates()))
    respx.get("http://img/ok.jpg").mock(return_value=httpx.Response(200, content=b"\xff\xd8\xff"))


def _fix(harness: AppHarness, **extra: str) -> httpx.Response:
    return harness.client.post(
        "/fix-match",
        data={
            "report_id": "report-2026W01-0000", "rank": "1", "term": TITLE,
            "tmdb_id": str(TMDB_2024), **extra,
        },
        follow_redirects=False,
    )


@respx.mock
def test_the_page_says_which_weeks_will_change_before_anything_is_pressed(
    harness: AppHarness,
) -> None:
    _with_history(harness)
    _mock_radarr()

    page = harness.client.get(
        "/reports/report-2026W01-0000/fix-match", params={"rank": 1, "term": TITLE}
    ).text

    assert "This correction will update" in page
    assert "W01" in page
    assert "recorded no source" in page
    assert "W03" in page
    assert "Not changed" in page
    assert "W02" in page
    assert 'name="leave_unverified"' in page


@respx.mock
def test_the_page_offers_no_choice_when_there_is_none_to_make(harness: AppHarness) -> None:
    harness.activate()
    state = harness.client.app.state
    state.apps.add(name="Radarr", url=RADARR_URL, api_key=RADARR_KEY)
    _save_week(state.reports, "2026W01", _stored_row(release_path=RELEASE_2024))
    _mock_radarr()

    page = harness.client.get(
        "/reports/report-2026W01-0000/fix-match", params={"rank": 1, "term": TITLE}
    ).text

    assert 'name="leave_unverified"' not in page


@respx.mock
def test_a_correction_over_http_reaches_the_film_and_not_its_namesake(
    harness: AppHarness,
) -> None:
    store = _with_history(harness)
    _mock_radarr()

    assert "match_fixed" in _fix(harness).headers["location"]

    tmdb = _tmdb_by_week(store)
    assert tmdb["2026W01"] == TMDB_2024
    assert tmdb["2026W02"] == 999
    assert tmdb["2026W03"] == TMDB_2024
    fixed = store.latest_for_week("2026W01").movies[0]
    assert fixed.rating == 7.1
    assert fixed.genres == ["Horror"]
    book = harness.client.app.state.corrections.book()
    assert book.by_release[RELEASE_2024].imdb_id == IMDB_2024
    assert book.by_title == {}, "scoped, so it does not speak for the title"


@respx.mock
def test_leaving_the_unverified_weeks_out_over_http(harness: AppHarness) -> None:
    store = _with_history(harness)
    _mock_radarr()

    _fix(harness, leave_unverified="on")

    assert _tmdb_by_week(store)["2026W03"] == 999


@respx.mock
def test_declining_on_a_row_with_no_source_saves_nothing_title_wide(harness: AppHarness) -> None:
    """Saving would speak for the whole title, which is what was just declined."""
    store = _with_history(harness)
    _mock_radarr()

    harness.client.post(
        "/fix-match",
        data={
            "report_id": "report-2026W03-0000", "rank": "1", "term": TITLE,
            "tmdb_id": str(TMDB_2024), "leave_unverified": "on",
        },
        follow_redirects=False,
    )

    assert _tmdb_by_week(store)["2026W03"] == TMDB_2024
    assert harness.client.app.state.corrections.all() == {}


def test_an_ambiguous_card_warns_and_offers_the_fix_even_when_owned(
    harness: AppHarness,
) -> None:
    """The library holds a Nosferatu. Saying "in library" alone could be about its namesake."""
    harness.activate()
    state = harness.client.app.state
    row = _stored_row(release_path=RELEASE_2024).model_copy(
        update={"detail": MATCHED_AMBIGUOUS}
    )
    report_id = _save_week(state.reports, WEEK, row)

    page = harness.client.get(f"/reports/{report_id}").text

    assert "Best guess — verify" in page
    assert "<details open>" in page
    assert "Ignore" not in page.split("<details open>")[1].split("</details>")[0]


async def test_a_scoped_correction_made_during_a_run_survives_its_commit(tmp_path: Path) -> None:
    """The in-flight case for a correction filed under a release, not a title."""
    import asyncio

    env = _Env(tmp_path, _Radarr({}), _Release({RELEASE_2024: IMDB_2024}))
    in_lookup, release = asyncio.Event(), asyncio.Event()
    real_lookup = env.radarr.lookup

    async def held(term: str) -> list[RadarrLookupResult]:
        in_lookup.set()
        await release.wait()
        return await real_lookup(term)

    env.radarr.lookup = held
    run = asyncio.create_task(env.pipeline.run(trigger=RunTrigger.MANUAL, week=WEEK))
    await in_lookup.wait()

    env.corrections.save(NORMALIZED, CHOSEN)
    release.set()
    report = await run

    assert report.movies[0].tmdb_id == TMDB_2024
    assert report.movies[0].title == CHOSEN.title


@respx.mock
async def test_the_real_client_sends_an_exact_id_as_radarr_expects() -> None:
    """The contract the empty-search recovery rests on, as far as a mock can pin it."""
    from app.services.radarr import RadarrClient

    route = respx.get(LOOKUP_URL).mock(return_value=httpx.Response(200, json=_candidates()))

    found = await RadarrClient(RADARR_URL, RADARR_KEY).lookup(f"imdb:{IMDB_2024}")

    assert route.calls.last.request.url.params["term"] == f"imdb:{IMDB_2024}"
    assert found[0].imdb_id == IMDB_2024
