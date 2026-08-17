"""Integration: the Weekly Check form persists to filters.yml.

Quality profile and root folder are NOT here — they live on each Radarr connection's
own card, because profile ids and folder paths mean nothing on another instance.
"""

from __future__ import annotations

import asyncio
from html import escape

import pytest
from pydantic import ValidationError

from app.services import boxoffice
from app.services.boxoffice import (
    MAX_CHART_SIZE,
    MIN_CHART_SIZE,
    REGIONS,
    ScrapeError,
)
from app.services.filters import (
    MAX_REPORT_KEEP,
    MIN_REPORT_KEEP,
    SCHEDULE_MODE_CADENCE,
    SCHEDULE_MODE_INTERVAL,
    FiltersConfig,
)
from app.services.reports import RunTrigger
from app.services.scheduler import BoxMediaScheduler
from app.web.settings import SettingsStatus, _filters_with
from tests.conftest import AppHarness


def test_weekly_check_form_persists(harness: AppHarness) -> None:
    """The section owns the box-office scrape only: when to check, and how deep.

    Quality and root folder moved onto each connection's own card — one Radarr's profile
    ids mean nothing on another, so a single shared pair could never be right for both.
    """
    harness.activate()
    response = harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24", "chart_size": "15"},
        follow_redirects=False,
    )
    assert SettingsStatus.FILTERS_SAVED in response.headers["location"]

    config = harness.client.app.state.filters.load()
    assert config.schedule_interval_hours == 24
    assert config.chart_size == 15

    page = harness.client.get("/settings").text
    assert "Weekly Check" in page
    assert "Radarr Defaults" not in page  # renamed: half of it was never about Radarr
    assert 'name="default_root_folder"' not in page  # now per connection


def test_backup_schedule_persists(harness: AppHarness) -> None:
    # The schedule lives with the Backups section, not under Radarr defaults.
    harness.activate()
    response = harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "7", "backup_keep": "3"},
        follow_redirects=False,
    )
    assert SettingsStatus.BACKUP_SCHEDULE_SAVED in response.headers["location"]

    config = harness.client.app.state.filters.load()
    assert config.backup_interval_days == 7
    assert config.backup_keep == 3
    page = harness.client.get("/settings").text
    assert "Automatic backups" in page
    assert "Create one every (days)" in page
    assert "Keep the last (backups)" in page
    assert "A snapshot is taken every 7 day(s)." in page


def test_saving_the_weekly_check_keeps_the_backup_schedule(harness: AppHarness) -> None:
    # Two separate forms over one config file: saving one must not reset the other.
    harness.activate()
    harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "5", "backup_keep": "4"},
        follow_redirects=False,
    )
    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24"},
        follow_redirects=False,
    )

    config = harness.client.app.state.filters.load()
    assert (config.backup_interval_days, config.backup_keep) == (5, 4)  # preserved
    assert config.schedule_interval_hours == 24  # and the edit applied


def test_saving_the_backup_schedule_keeps_the_weekly_check(harness: AppHarness) -> None:
    harness.activate()
    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "48"},
        follow_redirects=False,
    )
    harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "1", "backup_keep": "2"},
        follow_redirects=False,
    )

    config = harness.client.app.state.filters.load()
    assert config.schedule_interval_hours == 48
    assert (config.backup_interval_days, config.backup_keep) == (1, 2)


def test_invalid_backup_schedule_is_rejected(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "-1", "backup_keep": "0"},
        follow_redirects=False,
    )
    assert SettingsStatus.BACKUP_SCHEDULE_INVALID in response.headers["location"]
    assert harness.client.app.state.filters.load().backup_interval_days == 0  # unchanged


def test_backup_schedule_defaults_to_off(harness: AppHarness) -> None:
    harness.activate()
    config = harness.client.app.state.filters.load()
    assert config.backup_interval_days == 0  # unattended backups are opt-in
    assert config.backup_keep == 10


def test_scheduled_backup_creates_a_real_archive(harness: AppHarness) -> None:
    """End-to-end: the scheduler's job produces an encrypted archive that shows up in the
    Backups table and is audited as `scheduled`."""
    harness.activate()
    scheduler = BoxMediaScheduler(
        harness.client.app.state.pipeline,
        interval_hours=168,
        backups=harness.client.app.state.backups,
        backup_interval_days=1,
        backup_keep=2,
    )
    for _ in range(3):
        asyncio.run(scheduler._run_backup())

    backups = harness.client.app.state.backups.list_backups()
    assert len(backups) == 2  # keep=2 pruned the oldest
    assert all(info.name.startswith("boxmedia-") for info in backups)
    assert '"reason": "scheduled"' in "\n".join(harness.audit_lines())
    assert backups[0].name in harness.client.get("/settings").text


def test_chart_depth_persists_and_renders_back(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24", "chart_size": "15"},
        follow_redirects=False,
    )
    assert SettingsStatus.FILTERS_SAVED in response.headers["location"]
    assert harness.client.app.state.filters.load().chart_size == 15

    page = harness.client.get("/settings").text
    assert 'name="chart_size"' in page
    assert 'value="15"' in page
    # Its own point, finally kept: the bounds come from boxoffice, not from the HTML —
    # and not from this assertion either, which named 5 and 25 until they moved.
    assert f'min="{MIN_CHART_SIZE}"' in page and f'max="{MAX_CHART_SIZE}"' in page


def test_chart_depth_beyond_the_ceiling_is_rejected(harness: AppHarness) -> None:
    """Derived from the ceiling rather than written out: this test named 30 when the
    ceiling was 25, and said the opposite the moment the ceiling moved."""
    harness.activate()
    response = harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24", "chart_size": str(MAX_CHART_SIZE + 1)},
        follow_redirects=False,
    )
    assert SettingsStatus.FILTERS_INVALID in response.headers["location"]
    assert harness.client.app.state.filters.load().chart_size == 10  # untouched


def test_chart_depth_below_the_floor_is_rejected(harness: AppHarness) -> None:
    harness.activate()
    response = harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24", "chart_size": str(MIN_CHART_SIZE - 1)},
        follow_redirects=False,
    )
    assert SettingsStatus.FILTERS_INVALID in response.headers["location"]
    assert harness.client.app.state.filters.load().chart_size == 10


def test_the_whole_supported_depth_range_saves(harness: AppHarness) -> None:
    """Both ends and a middle value. Only the chart-topper is a real way to use this, and
    so is the deepest chart Box Office Mojo publishes."""
    harness.activate()
    for depth in (MIN_CHART_SIZE, 10, MAX_CHART_SIZE):
        response = harness.client.post(
            "/settings/filters",
            data={"schedule_interval_hours": "24", "chart_size": str(depth)},
            follow_redirects=False,
        )
        assert SettingsStatus.FILTERS_SAVED in response.headers["location"], depth
        assert harness.client.app.state.filters.load().chart_size == depth


def test_the_field_offers_the_range_it_accepts(harness: AppHarness) -> None:
    """The input's own bounds come from the same constants the model validates against, so
    the browser cannot offer a value the save would refuse."""
    harness.activate()

    page = harness.client.get("/settings").text
    field = page.split('id="chart_size"')[1].split(">")[0]

    assert f'min="{MIN_CHART_SIZE}"' in field
    assert f'max="{MAX_CHART_SIZE}"' in field
    assert f"{MIN_CHART_SIZE} to {MAX_CHART_SIZE}" in page  # ...and the note agrees


def test_chart_depth_defaults_to_ten(harness: AppHarness) -> None:
    harness.activate()
    assert harness.client.app.state.filters.load().chart_size == 10


def test_saving_the_backup_schedule_keeps_the_chart_depth(harness: AppHarness) -> None:
    harness.activate()
    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24", "chart_size": "20"},
        follow_redirects=False,
    )
    harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "1", "backup_keep": "2"},
        follow_redirects=False,
    )
    assert harness.client.app.state.filters.load().chart_size == 20


async def test_saved_depth_reaches_the_scrape(harness: AppHarness, monkeypatch) -> None:
    """End-to-end: Settings -> filters.yml -> the app's own pipeline -> fetch_weekly_chart.

    Covers the wiring in main.py, which no unit test sees.
    """
    harness.activate()
    harness.client.app.state.apps.add(
        name="Radarr", url="http://127.0.0.1:1", api_key="0123456789abcdef0123456789abcdef"
    )
    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "24", "chart_size": "15"},
        follow_redirects=False,
    )

    seen: dict[str, int] = {}

    async def _capture(*, snapshot_dir, url, week, top_n, area):  # noqa: ANN001, ANN202
        seen["top_n"] = top_n
        seen["area"] = area
        raise ScrapeError("stop here — the depth is what's under test")

    monkeypatch.setattr(boxoffice, "fetch_weekly_chart", _capture)
    await harness.client.app.state.pipeline.run(trigger=RunTrigger.MANUAL)
    assert seen["top_n"] == 15


# --- one carry-through builder for both forms (review step 11) ---

# Which form owns which field. Every FiltersConfig field must appear in exactly one of
# these, so adding a field to the model without deciding fails the coverage test below.
WEEKLY_CHECK_FIELDS = {
    "schedule_interval_hours", "chart_size", "report_keep", "schedule_mode",
}
BACKUP_SCHEDULE_FIELDS = {"backup_interval_days", "backup_keep"}
REGION_FIELDS = {"boxoffice_region"}
# Owned by neither form: the legacy global pair, read-only and carried by both.
CARRIED_ONLY_FIELDS = {"quality_profile_id", "default_root_folder"}
# Distinctive non-default values, so "carried through" cannot pass by coincidence.
DISTINCTIVE = {
    "quality_profile_id": 7,
    "default_root_folder": "/carried/through",
    "schedule_interval_hours": 36,
    "chart_size": 22,
    "backup_interval_days": 9,
    "backup_keep": 4,
    "report_keep": 33,
    "boxoffice_region": "GB",
    "schedule_mode": SCHEDULE_MODE_INTERVAL,  # not the default, so a reset shows
}


def test_every_filters_field_is_owned_by_exactly_one_form() -> None:
    """The guard that makes the two tests below grow on their own.

    A field added to FiltersConfig without being classified here fails immediately,
    instead of silently escaping the carry-through checks.
    """
    classified = (
        WEEKLY_CHECK_FIELDS | BACKUP_SCHEDULE_FIELDS | REGION_FIELDS | CARRIED_ONLY_FIELDS
    )
    assert set(FiltersConfig.model_fields) == classified
    owners = (WEEKLY_CHECK_FIELDS, BACKUP_SCHEDULE_FIELDS, REGION_FIELDS)
    for index, group in enumerate(owners):
        for other in owners[index + 1:]:
            assert not (group & other), "a field is claimed by two forms"
    assert set(DISTINCTIVE) == classified  # every field has a test value


def _seed_every_field(harness: AppHarness) -> None:
    harness.client.app.state.filters.save(FiltersConfig(**DISTINCTIVE))


def test_saving_the_weekly_check_carries_every_field_it_does_not_own(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed_every_field(harness)

    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "48", "chart_size": "11"},
        follow_redirects=False,
    )

    config = harness.client.app.state.filters.load()
    assert (config.schedule_interval_hours, config.chart_size) == (48, 11)  # applied
    for field in BACKUP_SCHEDULE_FIELDS | REGION_FIELDS | CARRIED_ONLY_FIELDS:
        assert getattr(config, field) == DISTINCTIVE[field], f"{field} was reset"


def test_saving_the_backup_schedule_carries_every_field_it_does_not_own(
    harness: AppHarness,
) -> None:
    harness.activate()
    _seed_every_field(harness)

    harness.client.post(
        "/settings/backups/schedule",
        data={"backup_interval_days": "2", "backup_keep": "6"},
        follow_redirects=False,
    )

    config = harness.client.app.state.filters.load()
    assert (config.backup_interval_days, config.backup_keep) == (2, 6)  # applied
    for field in WEEKLY_CHECK_FIELDS | REGION_FIELDS | CARRIED_ONLY_FIELDS:
        assert getattr(config, field) == DISTINCTIVE[field], f"{field} was reset"


def test_saving_the_region_carries_every_field_it_does_not_own(
    harness: AppHarness,
) -> None:
    """The third form, held to the same rule as the other two: it owns one field and must
    carry every other one through untouched."""
    harness.activate()
    _seed_every_field(harness)

    harness.client.post(
        "/settings/region", data={"boxoffice_region": "DE"}, follow_redirects=False
    )

    config = harness.client.app.state.filters.load()
    assert config.boxoffice_region == "DE"  # applied
    for field in WEEKLY_CHECK_FIELDS | BACKUP_SCHEDULE_FIELDS | CARRIED_ONLY_FIELDS:
        assert getattr(config, field) == DISTINCTIVE[field], f"{field} was reset"


def test_a_mistyped_field_name_is_refused_not_silently_ignored() -> None:
    """Pydantic ignores unknown keys, so without this guard `_filters_with(stored,
    chart_sze=20)` would report a successful save having changed nothing."""
    with pytest.raises(TypeError, match="not FiltersConfig fields"):
        _filters_with(FiltersConfig(), chart_sze=20)


def test_the_builder_still_enforces_the_model_bounds() -> None:
    """`model_copy(update=...)` would be the tempting one-liner here and skips validation
    entirely — the bounds are what turn a bad submission into FILTERS_INVALID."""
    with pytest.raises(ValidationError):
        _filters_with(FiltersConfig(), chart_size=MAX_CHART_SIZE + 1)
    with pytest.raises(ValidationError):
        _filters_with(FiltersConfig(), backup_keep=0)


def test_saving_the_retention_persists_and_reaches_the_running_pipeline(
    harness: AppHarness,
) -> None:
    """Settings -> filters.yml -> the callable the pipeline reads on its next run."""
    harness.activate()

    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "168", "chart_size": "10", "report_keep": "20"},
        follow_redirects=False,
    )

    assert harness.client.app.state.filters.load().report_keep == 20
    assert "report_keep: 20" in (harness.settings.config_dir / "filters.yml").read_text(
        encoding="utf-8"
    )


def test_a_retention_outside_the_bounds_is_refused(harness: AppHarness) -> None:
    """Bounded at both ends, and a bad value leaves the stored config untouched rather
    than half-applying the rest of the form."""
    harness.activate()
    harness.client.app.state.filters.save(FiltersConfig(report_keep=40))

    for bad in (str(MIN_REPORT_KEEP - 1), str(MAX_REPORT_KEEP + 1), "not-a-number"):
        response = harness.client.post(
            "/settings/filters",
            data={"schedule_interval_hours": "168", "chart_size": "10", "report_keep": bad},
            follow_redirects=False,
        )
        assert SettingsStatus.FILTERS_INVALID in response.headers["location"], bad
        assert harness.client.app.state.filters.load().report_keep == 40, bad


def test_the_field_shows_its_bounds(harness: AppHarness) -> None:
    harness.activate()

    section = harness.client.get("/settings").text.split(">Weekly Check<")[1]

    assert 'name="report_keep"' in section
    assert f'min="{MIN_REPORT_KEEP}"' in section
    assert f'max="{MAX_REPORT_KEEP}"' in section


# --- the region form (M1) ---


def test_the_region_form_offers_every_supported_chart(harness: AppHarness) -> None:
    harness.activate()

    page = harness.client.get("/settings").text

    assert "Box Office Region" in page
    assert 'name="boxoffice_region"' in page
    for code, label in REGIONS.items():
        assert f'value="{code}"' in page
        # Escaped, not raw: "Domestic (US & Canada)" reaches the page as "US &amp;
        # Canada", which is Jinja autoescaping doing its job on a label that happens to
        # contain an ampersand.
        assert escape(label) in page
    # Domestic is what an install without a choice is on, and the select must say so.
    assert 'value="" selected' in page


def test_choosing_a_region_saves_and_says_so(harness: AppHarness) -> None:
    harness.activate()

    response = harness.client.post(
        "/settings/region", data={"boxoffice_region": "GB"}, follow_redirects=False
    )

    assert SettingsStatus.REGION_SAVED in response.headers["location"]
    assert harness.client.app.state.filters.load().boxoffice_region == "GB"


def test_a_region_this_build_does_not_ship_is_refused(harness: AppHarness) -> None:
    """Write-strict, unlike the model's own validator. A hand-edited file loads as
    Domestic so the app still starts; a form is a deliberate act and gets an answer."""
    harness.activate()
    harness.client.post(
        "/settings/region", data={"boxoffice_region": "GB"}, follow_redirects=False
    )

    for crafted in ("EVIL", "../weekly", "gb", "GB&area=US"):
        response = harness.client.post(
            "/settings/region", data={"boxoffice_region": crafted}, follow_redirects=False
        )
        assert SettingsStatus.REGION_INVALID in response.headers["location"], crafted
        # ...and the stored choice is untouched by the attempt.
        assert harness.client.app.state.filters.load().boxoffice_region == "GB"


def test_the_region_note_says_what_a_regional_figure_actually_is(
    harness: AppHarness,
) -> None:
    """Mojo publishes a weekly chart for Domestic only, so a region's numbers are its
    weekend takings. Leaving that unsaid would let a UK figure be read as a full week's."""
    harness.activate()

    page = harness.client.get("/settings").text

    assert "weekend" in page
    assert "Re-run" in page  # ...and how to bring an old week over


def test_a_stored_region_this_build_dropped_loads_as_domestic() -> None:
    """The read-tolerant half, through the MODEL rather than the helper it calls: a
    filters.yml naming a region that no longer ships must let the app start."""
    assert FiltersConfig(boxoffice_region="GB").boxoffice_region == "GB"
    assert FiltersConfig(boxoffice_region="XX").boxoffice_region == ""
    assert FiltersConfig(boxoffice_region=7).boxoffice_region == ""
    assert FiltersConfig(boxoffice_region=None).boxoffice_region == ""
    assert FiltersConfig().boxoffice_region == ""


def test_a_dropped_region_survives_a_round_trip_through_disk(harness: AppHarness) -> None:
    """The path that actually matters: the file on disk is what a downgrade leaves
    behind, and reading it must not raise."""
    settings = harness.client.app.state.settings
    (settings.config_dir / "filters.yml").write_text(
        "schema_version: 1\nboxoffice_region: ZZ\nchart_size: 12\n", encoding="utf-8"
    )

    config = harness.client.app.state.filters.load()

    assert config.boxoffice_region == ""
    assert config.chart_size == 12  # ...and the rest of the file still loads


def test_choosing_the_cadence_saves_and_shows_as_chosen(harness: AppHarness) -> None:
    """M4: the mode is a stored setting like any other, and the form comes back showing
    what is in force rather than what was submitted."""
    harness.activate()
    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "48", "schedule_mode": SCHEDULE_MODE_INTERVAL},
        follow_redirects=False,
    )
    assert harness.client.app.state.filters.load().schedule_mode == SCHEDULE_MODE_INTERVAL

    interval_radio = '<input type="radio" name="schedule_mode" value="interval" checked>'
    assert interval_radio in harness.client.get("/settings").text.replace(" >", ">")

    harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "48", "schedule_mode": SCHEDULE_MODE_CADENCE},
        follow_redirects=False,
    )
    assert harness.client.app.state.filters.load().schedule_mode == SCHEDULE_MODE_CADENCE
    # And the other side of it: a radio that is always checked describes nothing.
    assert interval_radio not in harness.client.get("/settings").text.replace(" >", ">")


def test_a_schedule_mode_this_build_does_not_ship_is_refused(harness: AppHarness) -> None:
    """Write-strict, unlike the read-tolerant model validator: a form is a deliberate act,
    so silently saving something other than what was asked for would be the wrong mercy."""
    harness.activate()
    harness.client.app.state.filters.save(
        FiltersConfig(schedule_mode=SCHEDULE_MODE_INTERVAL)
    )

    response = harness.client.post(
        "/settings/filters",
        data={"schedule_interval_hours": "48", "schedule_mode": "hourly"},
        follow_redirects=False,
    )

    assert SettingsStatus.FILTERS_INVALID in response.headers["location"]
    config = harness.client.app.state.filters.load()
    assert config.schedule_mode == SCHEDULE_MODE_INTERVAL  # untouched
    assert config.schedule_interval_hours != 48, "a rejected form saved part of itself"


async def test_saving_the_weekly_check_pushes_the_mode_onto_the_live_schedule(
    harness: AppHarness,
) -> None:
    """A stored mode nothing acts on until the next restart is a setting that lies."""
    harness.activate()
    scheduler = BoxMediaScheduler(
        harness.client.app.state.pipeline, interval_hours=168,
        schedule_mode=SCHEDULE_MODE_INTERVAL,
    )
    scheduler.start()
    harness.client.app.state.scheduler = scheduler
    try:
        harness.client.post(
            "/settings/filters",
            data={"schedule_interval_hours": "168", "schedule_mode": SCHEDULE_MODE_CADENCE},
            follow_redirects=False,
        )
        assert scheduler.schedule_mode == SCHEDULE_MODE_CADENCE
        assert scheduler.job_interval_hours() is None  # the cron trigger really is live
    finally:
        scheduler.shutdown()
        harness.client.app.state.scheduler = None


def test_the_region_form_says_whose_dollars_they_are(harness: AppHarness) -> None:
    """Verified against the live site (GB, DE, JP — Aug 2026): Mojo prints every regional
    chart in US dollars, and its currency=local parameter is inert. Without this sentence,
    "I changed the region and it still shows $" reads as a hardcoded-currency bug — it was
    reported as exactly that — when the $ is the page's own figure, honestly repeated."""
    harness.activate()

    page = harness.client.get("/settings").text

    assert "Box Office Mojo reports every region in US dollars" in page
    assert "not a conversion" in page
