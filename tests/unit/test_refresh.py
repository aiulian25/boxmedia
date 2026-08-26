"""TV step 15 unit test: the unattended re-reads.

These were `deps.refresh_calendar`'s tests until the morning job needed the same work and
had no request to hang it off. They test the same claims at the seam the code now has: a
stub connection store hands out stub clients, so nothing here goes near a socket.

The claims that matter are the ones about SILENCE. A connection that does not answer keeps
the rows it gave last time, is not asked again straight away, and never lets an empty
answer be written down as a fresh one — because "we could not look" and "there is nothing"
are different claims and only one of them is true.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.apps import KIND_RADARR, KIND_SONARR, ExternalApp, InvalidAppError
from app.services.backoff import RadarrBackoff
from app.services.calendar import KIND_MOVIE, KIND_SERIES, CalendarCache
from app.services.radarr import RELEASE_DIGITAL, RadarrError, RadarrRelease
from app.services.refresh import ServerRefresher
from app.services.series import SeriesLibraryCache
from app.services.sonarr import SonarrCalendarEpisode, SonarrError, SonarrSeries

TONIGHT = datetime.now(UTC).replace(hour=23, minute=0, second=0, microsecond=0)
SONARR_CONNECTION = "Attic Sonarr"
RADARR_CONNECTION = "Attic Radarr"


def _episode(
    *,
    when: datetime | None = TONIGHT,
    series_id: int = 14,
    episode_number: int = 7,
) -> SonarrCalendarEpisode:
    return SonarrCalendarEpisode(
        episode_id=901, series_id=series_id, series_title="The Hollow Coast",
        tvdb_id=121361, season_number=2, episode_number=episode_number,
        title="The Long Way Down", air_date_utc=when, has_file=False, monitored=True,
    )


def _release(*, when: datetime = TONIGHT, radarr_id: int = 88) -> RadarrRelease:
    return RadarrRelease(
        radarr_id=radarr_id, tmdb_id=550, title="Harbour Lights", year=2026,
        release_kind=RELEASE_DIGITAL, when=when, has_file=False, monitored=True,
    )


def _series(*, sonarr_id: int = 14, title: str = "The Hollow Coast") -> SonarrSeries:
    return SonarrSeries(
        sonarr_id=sonarr_id, tvdb_id=121361, title=title, year=2023,
        monitored=True, ended=False, episode_count=34, episode_file_count=26,
        poster_url=None, path="/tv/hollow-coast",
    )


class _StubClient:
    """A Radarr or Sonarr that answers, or does not."""

    def __init__(
        self,
        rows: list[object] | None = None,
        progress: dict[int, float] | None = None,
        error: Exception | None = None,
        library: list[SonarrSeries] | None = None,
    ) -> None:
        self._rows = rows or []
        self._progress = progress or {}
        self._error = error
        self._library = library or []
        self.calendar_calls = 0
        self.library_calls = 0

    async def calendar(self, start: datetime, end: datetime) -> list[object]:
        self.calendar_calls += 1
        if self._error is not None:
            raise self._error
        assert start < end
        return self._rows

    async def queue(self) -> dict[int, float]:
        return self._progress

    async def list_series(self) -> list[SonarrSeries]:
        self.library_calls += 1
        if self._error is not None:
            raise self._error
        return self._library


class _StubApps:
    """The connection store, minus the disk. Hands out whichever stub is registered for a
    connection, and refuses an unknown one exactly as `AppsStore` does."""

    def __init__(self, apps: list[ExternalApp], clients: dict[str, _StubClient]) -> None:
        self._apps = apps
        self.clients = clients
        self.tls_seen: list[dict] = []

    def list_apps(self, kind: str | None = KIND_RADARR) -> list[ExternalApp]:
        if kind is None:
            return list(self._apps)
        return [app for app in self._apps if app.kind == kind]

    def _build(self, app_id: str, **tls: object) -> _StubClient:
        self.tls_seen.append(tls)
        if app_id not in self.clients:
            raise InvalidAppError(f"unknown connection: {app_id}")
        return self.clients[app_id]

    build_client = _build
    build_sonarr_client = _build


def _app(app_id: str, name: str, kind: str) -> ExternalApp:
    return ExternalApp(
        id=app_id, name=name, url="https://box.invalid", api_key_encrypted="x", kind=kind
    )


def _refresher(
    tmp_path: Path,
    apps: list[ExternalApp],
    clients: dict[str, _StubClient],
    *,
    tls_verify: bool = True,
    ca_file: object = None,
) -> ServerRefresher:
    return ServerRefresher(
        apps=_StubApps(apps, clients),
        settings=SimpleNamespace(outbound_tls_verify=tls_verify, tls_ca_file=ca_file),
        calendar_cache=CalendarCache(tmp_path),
        series_cache=SeriesLibraryCache(tmp_path),
        backoff=RadarrBackoff(),
    )


class TestTheCalendar:
    def test_both_servers_merge_into_one_week(self, tmp_path: Path) -> None:
        refresher = _refresher(
            tmp_path,
            [
                _app("sonarr-1", SONARR_CONNECTION, KIND_SONARR),
                _app("radarr-1", RADARR_CONNECTION, KIND_RADARR),
            ],
            {
                "sonarr-1": _StubClient([_episode()], {14: 30.0}),
                "radarr-1": _StubClient([_release()]),
            },
        )

        assert asyncio.run(refresher.calendar()) is True

        entries = CalendarCache(tmp_path).load()
        assert {entry.kind for entry in entries} == {KIND_MOVIE, KIND_SERIES}
        assert {entry.connection for entry in entries} == {
            SONARR_CONNECTION, RADARR_CONNECTION
        }

    def test_refreshing_twice_does_not_double_the_week(self, tmp_path: Path) -> None:
        # The rows a connection gave last time are kept only while it is silent. Keeping
        # them alongside its fresh ones would show every episode twice.
        refresher = _refresher(
            tmp_path,
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _StubClient([_episode()])},
        )
        asyncio.run(refresher.calendar())
        asyncio.run(refresher.calendar())

        assert len(CalendarCache(tmp_path).load()) == 1

    def test_the_queue_reaches_the_row_it_belongs_to(self, tmp_path: Path) -> None:
        refresher = _refresher(
            tmp_path,
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _StubClient(
                [_episode(series_id=14), _episode(series_id=99, episode_number=8)],
                {14: 30.0},
            )},
        )
        asyncio.run(refresher.calendar())

        by_progress = {
            entry.sub: entry.progress for entry in CalendarCache(tmp_path).load()
        }
        assert by_progress == {
            "S02E07 · 23:00 · The Long Way Down": 30.0,
            "S02E08 · 23:00 · The Long Way Down": None,
        }

    def test_a_films_progress_is_keyed_by_the_id_radarr_uses(self, tmp_path: Path) -> None:
        # Radarr's queue is keyed by its own movie id, never by the TMDB id — reading it
        # with the wrong key silently loses every progress bar on the film side.
        refresher = _refresher(
            tmp_path,
            [_app("radarr-1", RADARR_CONNECTION, KIND_RADARR)],
            {"radarr-1": _StubClient([_release(radarr_id=88)], {88: 74.0})},
        )
        asyncio.run(refresher.calendar())

        assert CalendarCache(tmp_path).load()[0].progress == 74.0

    def test_an_episode_with_no_air_date_does_not_reach_the_cache(
        self, tmp_path: Path
    ) -> None:
        # Sonarr returns them for unscheduled episodes. One has no day to belong to, and
        # it must not cost the rest of the week.
        refresher = _refresher(
            tmp_path,
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _StubClient([_episode(when=None), _episode()])},
        )

        assert asyncio.run(refresher.calendar()) is True
        assert len(CalendarCache(tmp_path).load()) == 1

    def test_an_unreachable_connection_keeps_the_rows_it_gave_last_time(
        self, tmp_path: Path
    ) -> None:
        apps = [
            _app("sonarr-1", SONARR_CONNECTION, KIND_SONARR),
            _app("radarr-1", RADARR_CONNECTION, KIND_RADARR),
        ]
        clients = {
            "sonarr-1": _StubClient([_episode()]),
            "radarr-1": _StubClient([_release()]),
        }
        assert asyncio.run(_refresher(tmp_path, apps, clients).calendar()) is True

        clients["radarr-1"] = _StubClient(error=RadarrError("down"))
        refresher = _refresher(tmp_path, apps, clients)
        assert asyncio.run(refresher.calendar()) is False

        # The film is still on the week — "we could not look" is not "nothing is due".
        assert {entry.connection for entry in CalendarCache(tmp_path).load()} == {
            SONARR_CONNECTION, RADARR_CONNECTION
        }

    def test_a_connection_that_is_down_is_not_asked_again_straight_away(
        self, tmp_path: Path
    ) -> None:
        clients = {"sonarr-1": _StubClient(error=SonarrError("x"))}
        refresher = _refresher(
            tmp_path, [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)], clients
        )
        assert asyncio.run(refresher.calendar()) is False

        # Second pass: the backoff answers, so a dead box costs one timeout a minute
        # rather than one per page view.
        working = _StubClient([_episode()])
        clients["sonarr-1"] = working
        assert asyncio.run(refresher.calendar()) is False
        assert working.calendar_calls == 0
        assert CalendarCache(tmp_path).load() == []

    def test_a_failed_fetch_does_not_stamp_an_empty_week_as_fresh(
        self, tmp_path: Path
    ) -> None:
        # The whole point of the cache: an empty week written with a fresh timestamp
        # would say "nothing is due" in a voice the page has no reason to doubt.
        refresher = _refresher(
            tmp_path,
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _StubClient(error=SonarrError("x"))},
        )

        assert asyncio.run(refresher.calendar()) is False
        assert CalendarCache(tmp_path).is_stale() is True

    def test_a_removed_connections_rows_do_not_haunt_the_week(
        self, tmp_path: Path
    ) -> None:
        apps = [
            _app("sonarr-1", SONARR_CONNECTION, KIND_SONARR),
            _app("radarr-1", RADARR_CONNECTION, KIND_RADARR),
        ]
        clients = {
            "sonarr-1": _StubClient([_episode()]),
            "radarr-1": _StubClient([_release()]),
        }
        asyncio.run(_refresher(tmp_path, apps, clients).calendar())

        assert asyncio.run(_refresher(tmp_path, apps[:1], clients).calendar()) is True
        assert {entry.connection for entry in CalendarCache(tmp_path).load()} == {
            SONARR_CONNECTION
        }

    def test_no_connections_at_all_is_an_honest_empty_week(self, tmp_path: Path) -> None:
        assert asyncio.run(_refresher(tmp_path, [], {}).calendar()) is True
        assert CalendarCache(tmp_path).load() == []
        assert CalendarCache(tmp_path).is_stale() is False

    def test_a_server_that_answers_its_calendar_but_not_its_queue_still_fills_the_week(
        self, tmp_path: Path
    ) -> None:
        class _NoQueue(_StubClient):
            async def queue(self) -> dict[int, float]:
                raise SonarrError("no queue for you")

        refresher = _refresher(
            tmp_path,
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _NoQueue([_episode()])},
        )

        assert asyncio.run(refresher.calendar()) is True
        entries = CalendarCache(tmp_path).load()
        assert len(entries) == 1
        assert entries[0].progress is None

    def test_the_window_asked_for_covers_the_week_around_today(
        self, tmp_path: Path
    ) -> None:
        seen: dict[str, tuple[datetime, datetime]] = {}

        class _Recording(_StubClient):
            async def calendar(self, start: datetime, end: datetime) -> list[object]:
                seen["span"] = (start, end)
                return []

        refresher = _refresher(
            tmp_path,
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _Recording()},
        )
        asyncio.run(refresher.calendar())

        start, end = seen["span"]
        today = datetime.now(UTC).date()
        assert start.date() <= today - timedelta(days=7)
        assert end.date() >= today + timedelta(days=14)


class TestWhatEachSonarrHolds:
    def test_every_sonarr_is_read(self, tmp_path: Path) -> None:
        apps = [
            _app("sonarr-1", SONARR_CONNECTION, KIND_SONARR),
            _app("sonarr-2", "Loft Sonarr", KIND_SONARR),
        ]
        clients = {
            "sonarr-1": _StubClient(library=[_series()]),
            "sonarr-2": _StubClient(library=[_series(sonarr_id=9, title="Low Orbit")]),
        }

        assert asyncio.run(_refresher(tmp_path, apps, clients).series()) is True

        snapshot = SeriesLibraryCache(tmp_path).load_all()
        assert set(snapshot) == {"sonarr-1", "sonarr-2"}

    def test_the_radarrs_are_left_alone(self, tmp_path: Path) -> None:
        """A Radarr has no series to hold, and asking one would fail on the first call."""
        apps = [
            _app("sonarr-1", SONARR_CONNECTION, KIND_SONARR),
            _app("radarr-1", RADARR_CONNECTION, KIND_RADARR),
        ]
        radarr = _StubClient(error=RadarrError("no series here"))
        clients = {"sonarr-1": _StubClient(library=[_series()]), "radarr-1": radarr}

        assert asyncio.run(_refresher(tmp_path, apps, clients).series()) is True
        assert radarr.library_calls == 0

    def test_a_sonarr_that_is_off_leaves_its_snapshot_alone(self, tmp_path: Path) -> None:
        """"We could not look" is not "you own nothing" — the second would empty every
        "already in Sonarr" answer on the app until the box came back."""
        apps = [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)]
        clients = {"sonarr-1": _StubClient(library=[_series()])}
        asyncio.run(_refresher(tmp_path, apps, clients).series())

        clients["sonarr-1"] = _StubClient(error=SonarrError("off"))
        assert asyncio.run(_refresher(tmp_path, apps, clients).series()) is False

        assert len(SeriesLibraryCache(tmp_path).load_all()["sonarr-1"]) == 1

    def test_no_sonarr_at_all_is_not_a_failure(self, tmp_path: Path) -> None:
        assert asyncio.run(_refresher(tmp_path, [], {}).series()) is True

    def test_the_stale_pass_leaves_a_fresh_snapshot_alone(self, tmp_path: Path) -> None:
        """What a page open pays for: a connection already inside its TTL is not asked."""
        apps = [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)]
        clients = {"sonarr-1": _StubClient(library=[_series()])}
        refresher = _refresher(tmp_path, apps, clients)
        asyncio.run(refresher.series())

        clients["sonarr-1"] = second = _StubClient(library=[_series()])
        asyncio.run(refresher.stale_series_libraries())

        assert second.library_calls == 0

    def test_the_stale_pass_leaves_a_connection_that_just_failed_alone(
        self, tmp_path: Path
    ) -> None:
        """The backoff's whole reason on this path. A snapshot that failed to load is
        still stale, so without it every page view would pay the timeout again."""
        apps = [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)]
        clients = {"sonarr-1": _StubClient(error=SonarrError("off"))}
        refresher = _refresher(tmp_path, apps, clients)
        asyncio.run(refresher.series())

        clients["sonarr-1"] = second = _StubClient(library=[_series()])
        asyncio.run(refresher.stale_series_libraries())

        assert second.library_calls == 0

    def test_the_stale_pass_does_read_one_that_has_never_been_read(
        self, tmp_path: Path
    ) -> None:
        apps = [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)]
        client = _StubClient(library=[_series()])

        asyncio.run(_refresher(tmp_path, apps, {"sonarr-1": client}).stale_series_libraries())

        assert client.library_calls == 1

    def test_one_connection_can_be_read_on_its_own(self, tmp_path: Path) -> None:
        """What an Add asks for, so the card is right immediately rather than at the next
        TTL."""
        apps = [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)]
        clients = {"sonarr-1": _StubClient(library=[_series()])}

        assert asyncio.run(
            _refresher(tmp_path, apps, clients).series_library("sonarr-1")
        ) is True
        assert "sonarr-1" in SeriesLibraryCache(tmp_path).load_all()

    def test_an_unknown_connection_is_a_failure_not_a_crash(self, tmp_path: Path) -> None:
        """A connection deleted between the page render and the refresh — `build_*` raises
        InvalidAppError, and an unattended job must survive it."""
        assert asyncio.run(
            _refresher(tmp_path, [], {}).series_library("gone")
        ) is False


class TestTheOutboundTlsSettings:
    """The app's own settings reach every client, never anything a form can influence."""

    @pytest.mark.parametrize("verify", [True, False])
    def test_the_verify_flag_is_passed_through(self, tmp_path: Path, verify: bool) -> None:
        apps = _StubApps(
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _StubClient([_episode()])},
        )
        refresher = ServerRefresher(
            apps=apps,
            settings=SimpleNamespace(outbound_tls_verify=verify, tls_ca_file=None),
            calendar_cache=CalendarCache(tmp_path),
            series_cache=SeriesLibraryCache(tmp_path),
            backoff=RadarrBackoff(),
        )
        asyncio.run(refresher.calendar())

        assert apps.tls_seen[0]["tls_verify"] is verify

    def test_a_ca_file_reaches_the_client_as_a_string(self, tmp_path: Path) -> None:
        apps = _StubApps(
            [_app("sonarr-1", SONARR_CONNECTION, KIND_SONARR)],
            {"sonarr-1": _StubClient([_episode()])},
        )
        refresher = ServerRefresher(
            apps=apps,
            settings=SimpleNamespace(
                outbound_tls_verify=True, tls_ca_file=tmp_path / "ca.pem"
            ),
            calendar_cache=CalendarCache(tmp_path),
            series_cache=SeriesLibraryCache(tmp_path),
            backoff=RadarrBackoff(),
        )
        asyncio.run(refresher.calendar())

        assert apps.tls_seen[0]["ca_file"] == str(tmp_path / "ca.pem")
