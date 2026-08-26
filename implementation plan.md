# BoxMedia — TV Discovery Implementation Plan (B · Unified Spine)

Status: awaiting approval before any code is written.
Produced: 2026-08-26. Sources: `tv-discovery.md` (the nzb360 teardown), the `tv/` mockup set (direction **B** artboards `SpineLibrary`, `SpineDiscover`, `SpineCalendar`, plus the shared `DeckShow` and `DeckSettings`), and the current codebase, read against `backups/DEVELOPMENT_PLAN.md`'s format.

Working area: **`/home/iulian/projects/BoxMedia-tv`, branch `tv-unified-spine`** — a full copy of the project. Main (`/home/iulian/projects/BoxMedia`) is not touched by any step below. If the result convinces, main merges or adopts the copy; that decision is the user's, and it is re-confirmed before any build-and-push.

---

## The direction, in one paragraph

The nav stops listing services and starts listing activities: **Library · Box Office · Discover · Calendar · Settings**. Media type becomes a filter, not a place — every page that can show both carries the same **All / Movies / TV** chip row, in the app's existing chip grammar. "Box Office" keeps naming the weekly Mojo chart section it always was; "Library" is today's dashboard grid with series merged in; Discover and Calendar are new. The payoff is the calendar: a Radarr digital release and a Sonarr episode land in the same day column. The honest part is Discover: there is no television box office, so films rank by what they took and series rank by how many people are watching them on Trakt right now — and the page says so instead of faking parity.

## Where TV data comes from (from `tv-discovery.md`)

| Source | Provides | Auth |
|---|---|---|
| **Trakt** | Ranking only: trending ("watching now") + anticipated. Payload carries **all four ids** (trakt/tmdb/tvdb/imdb) — no bridge needed | user's Trakt client ID, header `trakt-api-key` |
| **TMDB** | Descriptions, seasons, cast, **every poster**, and `external_ids` (the TMDB→TVDB bridge) | user's TMDB v3 key, query param (TMDB's own contract) |
| **Sonarr** | The user's library, calendar, lookup, add | per-connection API key, like Radarr |
| **Plex/Jellyfin** | "already on your media server" for shows | the one existing connection |

The single most important mechanic: **Radarr keys movies on TMDB id; Sonarr keys series on TVDB id.** A Trakt row adds directly (ids included); a TMDB-sourced row must resolve `tv/{id}/external_ids` first, and a show TMDB has no TVDB id for is honestly unaddable ("Find on Sonarr" fallback), never a silent failure. Unlike nzb360's six parallel detail calls, we are server-side: **one** TMDB request with `append_to_response=external_ids,content_ratings,credits,videos` fetches the whole detail page.

## Rulings & assumptions (to confirm or correct)

1. **Keys are the user's own.** TMDB key and Trakt client ID are entered in Settings, encrypted at rest (AES-256-GCM, same key as every other secret), masked once saved, editable and deletable at any time. They ship with the app **nowhere** — not in code, not in `.env.example`, not in the image. The dev instance gets the two values from `tv-discovery.md` §0 typed into its Settings UI after bring-up; they land only in the copy's encrypted data dir.
2. **No Trakt account, ever** (as in the teardown): no OAuth, no device pairing, no watchlist. Trending is global. Read-only.
3. **Zero new Python dependencies.** httpx + BeautifulSoup + the existing stack cover everything TMDB/Trakt/Sonarr need. `pyproject.toml` is untouched.
4. **The Movies flow is a regression contract.** The Movies chip on Library renders byte-for-byte today's dashboard; weekly reports, fix-match, ignore, upgrade, add-to-Radarr are untouched. All ~1223 existing tests keep passing at every step.
5. **Merged Library ordering:** the All view interleaves alphabetically by title (the one key both kinds share honestly); the Movies chip preserves today's exact ordering. *Assumption — flag if you want recency instead.*
6. **Old URLs live on:** `/dashboard` permanently redirects to `/library`; `/reports` keeps its URL under the new "Box Office" nav label.
7. **Calendar window:** current week ± the 14-day look-ahead list, refreshed each morning (06:00 UTC job) and on page open when stale. Dates render day-first everywhere.
8. **Discover freshness:** rows cache to disk with a TTL (6 h); the page always renders from cache and never blocks on Trakt/TMDB; Refresh re-fetches. Missing keys → an honest empty state pointing at Settings, not an error.
9. **en only**, matching the app today. Templates keep the truncation/wrap discipline so longer strings shorten lines instead of widening cards.
10. **No new share surfaces.** Every new route sits behind the session gate + CSRF like the rest; the standing signed-link requirement stays satisfied vacuously.

## Security impact (summary — details per step)

- **New outbound hosts:** `api.trakt.tv`, `api.themoviedb.org`, `image.tmdb.org` — public APIs, TLS always verified (the `BM_OUTBOUND_TLS_VERIFY`/CA escape hatch stays what it is: for the user's own servers, not these). Documented in the README.
- **The one rule deviation, stated:** the TMDB v3 key travels as a `api_key` query parameter because that is TMDB's API contract (the "header, never URL" rule was written for our own choices). Mitigation: request URLs are never logged, and every exception message or failure snapshot that could carry a URL is **redacted** (`api_key=…` stripped) before persisting — with a test asserting it.
- **Duplicate-add detection is never string-matching an error body** (the teardown's own finding §11): we check our Sonarr snapshot by TVDB id before POSTing, and surface Sonarr's error verbatim if one still occurs.
- **CSP unchanged** (`img-src 'self'` intact): all TMDB artwork goes through the existing local `PosterCache` and serves from `/posters/…`. No inline styles/scripts anywhere new; Tailwind class names stay literal (the purge trap), and anything shipping `hidden` with its own `display` gets the `[hidden]` guard.
- **Audit rows** for every new mutating action: discovery keys saved/removed, series added, calendar/discover refresh.
- Container posture (distroless, read-only, non-root, cap-drop) untouched.

---

## Repo structure — new/changed files, annotated with step numbers

```
BoxMedia-tv/
├── docker-compose.dev.yml           # 1  (copy's own stack identity)
├── app/
│   ├── main.py                      # 4,5,8,11,12,14,16 (wiring only)
│   ├── services/
│   │   ├── apps.py                  # 2  (kind: radarr|sonarr, primary per kind)
│   │   ├── sonarr.py                # 3  NEW (Sonarr v3 client)
│   │   ├── sonarr_options.py        # 4  NEW (profiles/folders cache, mirrors radarr_options)
│   │   ├── discovery.py             # 5,11 NEW (keys store + discover row cache)
│   │   ├── tmdb.py                  # 6  NEW (TMDB client, the TVDB bridge)
│   │   ├── trakt.py                 # 7  NEW (trending/anticipated)
│   │   ├── series.py                # 8  NEW (Sonarr library snapshot cache)
│   │   ├── mediaserver.py           # 9  (show libraries for Plex/Jellyfin)
│   │   ├── posters.py               # 10 (reused as-is; size-cap test added)
│   │   ├── ignore.py                # 12 (additive kind marker)
│   │   ├── calendar.py              # 13 NEW (merged Radarr+Sonarr calendar cache)
│   │   └── scheduler.py             # 15 (morning calendar job, series refresh)
│   ├── web/
│   │   ├── settings.py              # 4,5 (Sonarr card, Which-app radio, Discovery card)
│   │   ├── discover.py              # 11 NEW
│   │   ├── shows.py                 # 12 NEW (detail + add-series)
│   │   ├── calendar.py              # 14 NEW
│   │   └── library.py               # 16 (dashboard.py renamed + merged grid)
│   ├── templates/
│   │   ├── settings.html            # 4,5
│   │   ├── discover.html            # 11 NEW
│   │   ├── show_detail.html / _show_detail.html   # 12 NEW
│   │   ├── calendar.html            # 14 NEW
│   │   ├── library.html             # 16 (dashboard.html renamed, chips + TV cards)
│   │   └── base.html                # 16 (nav: Library · Box Office · Discover · Calendar · Settings)
│   └── static/js/app.js             # 4,12,16 (kind radio, show modal, unchanged progress poll)
├── tests/  (unit + integration per step, named below)
└── README.md                        # 18
```

Runtime data (all inside the existing `/data` layout, all covered by the existing backup scope):
`/data/config/discovery.yml` (5) · `/data/config/sonarr-options.json` (4) · `/data/cache/sonarr-library.json` (8) · `/data/cache/discover.json` (11) · `/data/cache/calendar.json` (13) · TV posters join `/data/cache/posters/` (10).

---

## Numbered implementation plan

Each step: **what**, **why at that point**, **security**, and the **test that gates moving on**. One commit per step (secret-leak gate before each), in the copy.

1. ✅ **DONE** — **The copy's own dev stack identity** — `docker-compose.dev.yml`: compose project `name: boxmedia-tv`, `container_name: boxmedia-tv` (+ `boxmedia-tv-init`), image `boxmedia:tv-dev`, host port **58547**, and the data/secrets mounts single-sourced through `BM_DATA_PATH`/`BM_SECRETS_PATH` anchors so both services can never name different directories. The copy's `.env` took a **fresh session secret** as well as the port and paths — it signs session cookies and every CSRF token, so sharing main's would make one stack's tokens structurally valid on the other. Fresh AES-256-GCM key minted to `./secrets/boxmedia.key` (mode 600) with `python -m app.core.crypto genkey`. The dev file also gained the **`init` service** the published compose already has: `./data` and `./secrets` were deliberately not copied from main, so without it the first `up` would die on `PermissionError: /data/config` — the exact failure `app/core/provision.py` exists to prevent.
   *Why first:* the real mechanism turned out to be sharper than "derived project name" — a `container_name` is **global to the Docker daemon, not scoped to its compose project**, so main's explicit `container_name: boxmedia` was the collision, and the second `up` would adopt and recreate main's container regardless of project name.
   *Security:* fresh encryption key and fresh session secret; main's data, key and container never referenced (verified: main's key still mode 640 dated 14/08, its container dated 18/08, both untouched). `/secrets` stays writable on init and read-only on the app, so a compromised app cannot rewrite the key that decrypts its own backups.
   *Test:* `docker compose config` resolves to project `boxmedia-tv`, containers `boxmedia-tv`/`boxmedia-tv-init`, image `boxmedia:tv-dev`, `0.0.0.0:58547→8686`, all volumes under the copy — while main's resolves unchanged to `boxmedia`/`boxmedia:0.1.0`/`58546`. Three new invariants in `tests/unit/test_provision.py` pin it in code: the dev stack shares no container name, image tag or host port with the published one, and its two services cannot be pointed at different directories. **Mutation-tested** — restoring each of the five collisions (main's container name, main's port, the published image, a diverged app data mount, a bare `./data` mount, a writable app `/secrets`) fails its guard; ruff clean, 16/16 in the file.

2. ✅ **DONE** — **Apps store learns kinds** — `app/services/apps.py`: a `kind` field (`radarr` | `sonarr`) with `_validated_kind` **read-tolerant** (missing or unknown → `radarr`, so every existing `apps.yml` loads unchanged — additive field, no schema bump) and `add()` **write-strict** (refuses an unknown kind), exactly the `users._validated_theme`/`set_theme` split. `primary_id(kind)`/`set_primary` are per-kind: `set_primary` clears only within the chosen connection's own kind, and `remove` promotes a successor of that same kind, so removing the last Sonarr never hands series adds to a Radarr. `set_defaults` gained `series_type`/`season_folders`/`search_on_add`, written only when not None — the same blank-keeps-the-stored-one contract the API key field uses, so `False` is honoured but a two-argument caller cannot erase them. `ExternalApp.public()` carries `kind`/`kind_name` for step 4's card tag.
   *The one judgement call, and it drove the design:* `list_apps()` has 20 callers across `deps.py`, `reports.py`, `movies.py`, `dashboard.py`, `auth.py` and `settings.py`, and every one means *the Radarr connections* — Add menus, the target caret, where-does-this-film-live. Left unfiltered, step 4's first Sonarr would silently appear as somewhere to send a **film**. So `list_apps(kind=KIND_RADARR)` and `primary_id(kind=KIND_RADARR)` default to Radarr, `kind=None` lists every kind, and `get()` still finds any id. Cost: a default that is not "everything". Gain: the Movies regression contract holds **structurally** rather than depending on auditing 20 sites correctly — zero call-site edits, and a Sonarr cannot reach a list that did not ask for it.
   *Security:* the credential path is untouched — AES-GCM at rest, masked in `public()`, blank-keeps on update. One hardening added: `build_client` now refuses a non-Radarr connection. Sonarr answers `/api/v3/system/status` too, so a Radarr client pointed at one would have gone green on the health dot and failed later at the first `movie` call, far from the cause. `series_type` is validated against Sonarr's own three.
   *Test:* `tests/unit/test_apps.py` — 16 new, 20 existing **unmodified**, 36 green. A hand-written pre-kinds `apps.yml` loads as radarr and stays primary; an unknown stored kind reads as radarr; `add(kind="lidarr")` is refused; a Sonarr never appears in a bare `list_apps()`; per-kind primaries are independent in both directions; promotion on remove stays within a kind; Sonarr defaults round-trip and survive a two-argument caller; `False` survives; a Radarr's file carries no series keys. **Mutation-tested** — all nine reverts (unfiltered listing, kind-blind primary, cross-kind clear, cross-kind promotion, strict read, lax write, unconditional default write, dropped client guard, dropped series-type check) fail their guard. Ruff clean; full suite **1239 passed, 1 skipped**.

3. **Sonarr client** — `app/services/sonarr.py`: `SonarrClient` mirroring `RadarrClient`'s shape (same `_request`/`_json` discipline, `build_verify` **imported** from radarr.py, not duplicated): `system_status`, `list_series` → `SonarrSeries` (sonarr id, **tvdb_id**, imdb_id, title, year, monitored, ended, episode/file counts, per-season stats, path), `queue()` (episode-level → per-series progress map), `lookup(term)` (`tvdb:{id}` preferred, text fallback), `add_series(...)` (tvdb id, quality profile, root folder, monitor: all/future/firstSeason/none, season folders, series type, search-on-add), `quality_profiles()`, `root_folders()`, `calendar(start, end)`. Typed errors `SonarrError/SonarrAuthError/SonarrConnectionError`.
   *Why:* the whole TV feature's reason to exist; mocking it now (respx) fixes the contract early — the exact argument Radarr's client made in the original plan.
   *Security:* API key as `X-Api-Key` header; TLS on by default with the same CA-file translation; **no error-body string-matching for duplicates** — the snapshot check in step 12 is the guard, and a Sonarr 400 surfaces its own message verbatim.
   *Test:* `tests/unit/test_sonarr.py` — respx per endpoint incl. 401→auth error and connect-failure; queue maps episode records to series progress; add posts the exact body Sonarr v3 expects.

4. **Sonarr in Settings** — `app/web/settings.py`, `settings.html`, `app.js`, plus `app/services/sonarr_options.py` (a `SonarrOptionsCache` mirroring `radarr_options.py`, fetching profiles + root folders on save/test). Add New App grows a **"Which app" radio (Radarr — films / Sonarr — series)** — the identical fieldset + `data-*` swap mechanics the media-server card already uses for Plex/Jellyfin, so a second service never means a second pattern; the address placeholder swaps 7878/8989 with it. Each Sonarr card shows: Adds series as / into / Series type / season folders / search-on-add. Cards are tagged `Radarr`/`Sonarr` (+ per-kind `Primary`). Pre-save Test Connection reuses the existing test-credentials route, kind-aware (`querySelectorAll`, pinned — the lesson is already in `test_progressive_enhancement.py`).
   *Why:* nothing TV-facing can be exercised until a Sonarr can be configured; config errors must surface at config time.
   *Security:* same encrypted-at-rest, masked, blank-keeps key handling; health dot probes with the same 4 s wall.
   *Test:* `tests/integration/test_settings.py` additions — add a Sonarr, kind persisted, options fetched (respx), key never echoed into HTML; a Radarr card renders exactly as before.

5. **Discovery keys store + Settings card** — `app/services/discovery.py` (store half): `/data/config/discovery.yml` holding `tmdb_key_encrypted` and `trakt_client_id_encrypted`; masked display, blank-keeps-stored, an explicit **Delete** per key; audit `discovery_key_saved` / `discovery_key_removed`. Settings gains a **Discovery** card: two fields with field-notes on where each comes from (TMDB: account → API → v3 key; Trakt: create an API app → client ID), per-key Test buttons (TMDB `GET /3/configuration`; Trakt `GET /shows/trending?limit=1`).
   *Why:* every client below needs a key source, and the user ruling is that keys are theirs to add, change, and remove at any time.
   *Security:* AES-GCM with the app's one key; the **query-param deviation and its redaction rule** (see summary) are implemented here as a helper every TMDB call site must use; the Trakt client ID is technically a public identifier but rides the same encrypted path for one uniform pattern. Dev instance: the two `tv-discovery.md` §0 values are entered through this UI only.
   *Test:* `tests/unit/test_discovery_keys.py` + integration — stored only as `gcm:` tokens; mask/keep/delete cycle; Test buttons map success/401/unreachable to the existing banner fragments; grep-style assertion that no fixture or template carries either real key.

6. **TMDB client** — `app/services/tmdb.py`: `configuration()` (key check), `discover_tv(...)` (the few filters Discover exposes: genre, first-air-date window, sort, language), `tv_detail(id)` with `append_to_response=external_ids,content_ratings,credits,videos` (one round trip — the bridge **and** the detail page), `search_tv(q)`; poster paths fed to the existing `posters.sized`/`PosterCache`. Honors `429 Retry-After` once (bounded `asyncio.sleep`), then gives up loudly.
   *Why:* TMDB is both the artwork source and the TVDB bridge; Trakt rows and the detail page both lean on it.
   *Security:* always-verified TLS; the step-5 redaction helper on every error path; descriptive BoxMedia User-Agent.
   *Test:* `tests/unit/test_tmdb.py` — respx per endpoint; external_ids extraction; 429-then-success; **an error raised from a failing call contains no `api_key`**.

7. **Trakt client** — `app/services/trakt.py`: `trending_shows(limit)` (watchers count = the rank), `anticipated_shows(limit)`; headers `trakt-api-key`, `trakt-api-version: 2`, `User-Agent: BoxMedia/<version>` — our own, not another app's. Returns title, year, and all four ids per row.
   *Why:* the ranking half of Discover; deliberately tiny (two endpoints is genuinely all the teardown found in the wild, and all we need).
   *Security:* read-only, no account, no token storage — nothing to leak beyond the client ID.
   *Test:* `tests/unit/test_trakt.py` — respx; ids parsed from `show.ids`; 401 → typed auth error the Settings Test button can name.

8. **Sonarr library snapshot** — `app/services/series.py`: `SeriesLibraryCache` (`/data/cache/sonarr-library.json`), mirroring the `MediaServerLibraryCache` pattern: per-connection series lists (tvdb id keyed, title+year fallback), refreshed on settings save/test and by the scheduler, read with the existing backoff so an offline Sonarr never stalls a page.
   *Why:* "already in Sonarr", "missing 8 episodes", and the pre-add duplicate check all read this; nothing user-facing should ever wait on a live Sonarr round trip.
   *Security:* cache contains titles/ids only — no secrets; lives inside the backup scope like every cache.
   *Test:* `tests/unit/test_series.py` — snapshot round-trip; tvdb hit; title+year fallback labelled a guess; stale-tolerant read.

9. **Media server show libraries** — `app/services/mediaserver.py`: Plex `list_shows` (sections `type=show`, Guids `tvdb://`/`tmdb://`), Jellyfin series items (`IncludeItemTypes=Series`, ProviderIds read by **key equality** — the `TmdbCollection` trap is already fenced for movies and stays fenced), `MediaServerSeries`, snapshot gains an additive `series` list. Show `server_state` reuses the two registers verbatim: id match → "Already in Plex" (confident, green); title+year → "Probably in Plex — verify" (amber guess).
   *Why:* the mockups' most valuable card state — "Plex has S1–S2, Sonarr does not" — needs the show library; the movie half already taught us every trap.
   *Security:* read-only against the media server, as ever; token handling untouched.
   *Test:* `tests/unit/test_mediaserver.py` additions — Plex + Jellyfin show fixtures; TmdbCollection ignored; legacy snapshot without `series` loads fine.

10. **TV posters through the existing cache** — no new file: Discover/detail call sites feed TMDB poster URLs (w342 grid / w500 detail) to `PosterCache.ensure`, served from the existing `/posters/{sha1}` route.
    *Why its own step:* CSP `img-src 'self'` makes this load-bearing, and the cache's download size-cap and failure-cooldown deserve an explicit test against TMDB-shaped URLs before pages depend on them.
    *Security:* TLS to `image.tmdb.org`; bytes stored and served from our origin; CSP untouched.
    *Test:* unit — TMDB URL cached under sha1, size cap enforced, recent-failure cooldown respected; existing poster tests green.

11. **Discover: cache + page** — `app/services/discovery.py` (cache half): `DiscoverCache` (`/data/cache/discover.json`, 6 h TTL, `fetched_at` stamp) holding the two Trakt rows resolved against steps 8/9 (each entry: title, year, ids, watchers, poster name, state: in-sonarr / on-server / wanted / no-tvdb-id). `app/web/discover.py` + `templates/discover.html`: film row **from the latest stored report** (zero new fetches), Trakt trending, Trakt anticipated; the All/Movies/TV chip row (`?type=`, server-rendered links); Refresh POST; per-row source captions ("Mojo · week …", "Trakt · artwork from TMDB") and the ranking-authority footnote from the mockup. Missing keys → the configure-in-Settings empty state. Nav wiring waits for step 16; until then the page is reachable by URL.
    *Why here:* first user-visible TV value, and it composes 5–10 without touching anything movie-shaped.
    *Security:* page renders from cache only — external calls happen in the Refresh action (session + CSRF gated, audited) with a bounded wall, so no page load can be held hostage by a third party.
    *Test:* `tests/integration/test_discover.py` — seeded cache renders three rows; chips filter; no-keys empty state; Refresh (respx) repopulates and re-resolves states; a report-less install still renders the TV rows.

12. **Series detail + Add to Sonarr** — `app/web/shows.py`: `/shows/{tmdb_id}` as page + fragment (the `movies.py` modal pattern; `app.js` gains the `data-show` hook beside `data-movie`), `templates/show_detail.html`/`_show_detail.html` on the film detail's exact anatomy; season table from the step-6 single detail call. The add block per the mockup: **Monitor is the only per-show control** (all/future/first season/none) — quality, folder, series type, season folders stay per-connection, set once in Settings, and the block says so in words. `POST /add-series`: resolve TVDB id (Trakt rows carry it; TMDB rows via external_ids), no id → honest refusal with the "Find on Sonarr" lookup fallback; snapshot says it exists → "Open in Sonarr" instead of a duplicate POST; success audits `series_added` and refreshes the snapshot. `ignore.py` gains an additive `kind` marker so Ignore on a discover card works and never collides with movie entries.
    *Why:* completes the decision loop Discover opens; everything it needs now exists.
    *Security:* CSRF + session like `/add-movie`; target validated against the store (an unknown connection id is refused); audit row per add.
    *Test:* `tests/integration/test_shows.py` — detail renders from one respx call; add posts the configured defaults + chosen monitor; tvdb-less show refused with the fallback offered; existing-series short-circuits; ignore round-trip.

13. **Calendar cache** — `app/services/calendar.py`: `CalendarCache` (`/data/cache/calendar.json`) merging every Sonarr connection's `calendar(start,end)` (episodes: series title, SxxExx, episode title, air time UTC, hasFile, monitored) and every Radarr connection's `calendar` (digital/physical/cinema release dates) into normalized entries `{kind, title, ids, when, sub, state, connection}`. State ladder: has file → downloaded; in queue → downloading (per-series progress from 3); aired + monitored + no file → missing; airs today; else monitored. Window: current week ± 14 days. Refresh on page open when stale (bounded wall) — the morning job lands in 15.
    *Why:* the direction's payoff; it is pure composition of clients that now exist.
    *Security:* reads only the user's own servers; cache is titles/dates.
    *Test:* `tests/unit/test_calendar.py` — frozen-clock fixtures drive every state; a film and an episode merge into the same day; an unreachable connection degrades to its last cached entries, marked stale.

14. **Calendar page** — `app/web/calendar.py` + `templates/calendar.html`: the 7-column week (Mon-first, **day-first dates**), today's column in the primary border, prev/this/next week nav (`?week=`), the All/Movies/TV chips, film entries on the heavier ground per the mockup, the legend, and the 14-day list underneath (where long episode titles actually fit). Header line states last fetch / next automatic fetch — the same track-record-beside-the-promise pattern the reports page uses.
    *Why:* the visible half of 13.
    *Security:* session-gated; the on-open refresh is GET-safe because it only rebuilds a local cache (no external mutation) — still time-bounded.
    *Test:* `tests/integration/test_calendar_page.py` — seeded cache renders the grid; chips narrow; week nav clamps sanely; empty Saturday renders the empty state, not a broken column.

15. **Scheduler jobs** — `app/services/scheduler.py`: a daily `calendar-refresh` job (06:00 UTC, jittered like everything else) and a daily Sonarr snapshot refresh riding the same rhythm as the media-server refresh. Discover deliberately gets **no job** — its TTL + Refresh button is enough, and unattended third-party traffic should stay minimal.
    *Why here:* jobs wrap services that are now stable; the scheduler's jitter/catch-up conventions are already established.
    *Security:* no new credentials; jobs use stored connections.
    *Test:* `tests/unit/test_scheduler.py` additions — jobs registered with the expected triggers; reschedule-on-save honored; run bodies call the right services (stubbed).

16. **The Unified Spine: nav + merged Library** — `base.html` nav becomes **Library (/library) · Box Office (/reports) · Discover · Calendar · Settings**; `dashboard.py`/`dashboard.html` rename to `library.py`/`library.html`; `/dashboard` 308-redirects to `/library`; reports pages take `active_nav="boxoffice"`. The Library grid: movies exactly as today, series cards from the snapshot (poster via 10, type mark in the rank-chip slot, `SONARR` service chip with the season-completeness band, "Missing N episodes"/"Complete — X/Y" line, media-server hint), the chip row with counts (`All 241 · Movies 207 · TV 34`), search across both. Ordering per ruling 5.
    *Why last-but-the-gate:* the nav rewrite touches every template and every nav test — done once, when all five destinations exist.
    *Security:* none new; redirect preserves the session gate.
    *Test:* full-suite nav/active-state assertions updated; `/dashboard` redirect covered; Movies chip output diffed against a pre-change golden render of today's dashboard for one seeded state (ruling 4 made checkable); progress-poll selectors still find both movie and series chips.

17. **Security & hardening pass** — sweep the new surface as one step: every mutating route CSRF'd and audited; redaction test greps captured logs/audit/snapshots for both key shapes; CSP smoke on the three new pages (zero console violations, no inline styles, `[hidden]` guard where an element sets its own display); Tailwind rebuild confirmed to carry every literal class the new templates use (the purge trap, checked by rendering each template and asserting the built CSS contains its novel classes); `pip-audit` unchanged by construction (ruling 3).
    *Why:* the same argument as the original plan's step 21 — applied when the full surface exists so nothing is missed.
    *Test:* `tests/integration/test_security.py` additions + the template/CSS invariant test extended to the new templates.

18. **README + docs** — TV discovery section (what it does, what it never does: no Trakt account, read-only media server, nothing sent anywhere until Add), the new outbound endpoints listed in the security section, keys how-to, Settings screenshots refreshed later. The "Requirements & resource use" re-measurement waits until the feature settles — before any merge, on measured numbers as last time.
    *Test:* README claims spot-checked against behavior; no fabricated numbers.

19. **Full gate + dev bring-up (stop point)** — `./scripts/check.sh` green (ruff, full pytest, pip-audit, Trivy); `docker compose -f docker-compose.dev.yml up -d --build` **in the copy** (its own name/port from step 1); walkthrough on `:58547`: enter the TMDB key and Trakt client ID via Settings → add the Sonarr connection → Discover populates → open a show → add it → Calendar shows its next episode beside a Radarr release → Library merges. Then **stop**: no push, no touching main. The user reviews; merging or replacing main — and any build-and-push, which will be preceded by the standing reminder that this all lives in the copy — is their call.

---

## Deferred / explicitly out of scope for v1

| Item | Reason |
|---|---|
| Custom TMDB discovery cards (the 25-filter surface) | The teardown documents it, but v1 ships the three fixed rows; the filter bar grows later without structural change |
| TMDB "popular" third row | Trakt trending + anticipated cover the mockup's decision value; add later if the two rows feel thin |
| Episode-level operations (interactive search, wanted/cutoff, season pass) | Sonarr's own UI does this well; BoxMedia adds shows, it doesn't manage them — same philosophy as the Radarr side |
| Newznab direct search | Legacy path in the teardown; no place in this app |
| Trakt OAuth / personal lists | Ruling 2 |
| TV in weekly Mojo reports | There is no TV box office; the Box Office section stays film-only and Discover says so |
