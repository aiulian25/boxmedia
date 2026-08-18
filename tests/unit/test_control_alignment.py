"""Design-system rules that only the source files can prove.

Both were real, measured defects rather than hypotheticals, and both are invisible to
every other test in the suite: the markup is correct in each case, only the geometry was
wrong. Asserted against the SOURCE stylesheet because that is the file a change would
touch — the compiled app.css is built inside the image.
"""

from __future__ import annotations

import re
from pathlib import Path

STYLESHEET = Path(__file__).resolve().parent.parent.parent / "styles" / "tailwind.css"
_COMMENTS = re.compile(r"/\*.*?\*/", re.DOTALL)
_RULES = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _applied_to(selector: str) -> str:
    """Every utility applied to `selector`, across all rules that name it.

    Comments are stripped first: they sit between rules and contain commas, so a naive
    split would fold half a sentence into the selector list.
    """
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))
    bodies = [
        rule.group(2)
        for rule in _RULES.finditer(source)
        if selector in [part.strip() for part in rule.group(1).split(",")]
    ]
    assert bodies, f"no rule in the stylesheet targets {selector!r}"
    return " ".join(bodies)


def test_a_link_in_the_run_actions_row_is_sized_like_a_button() -> None:
    """The weekly view's week nav renders a LINK when that week already has a report and
    a BUTTON when it has to be fetched. The sizing rule covered input and button but not
    a, so the two controls sat 5px apart at 38px and 40px tall — and which pair you got
    depended on which weeks happened to exist.
    """
    applied = _applied_to(".run-actions a")

    assert "h-10" in applied            # the same height as the button beside it
    assert "mt-0" in applied            # btn-* carry mt-2, which offset the link
    assert "min-w-[11rem]" in applied   # and the same minimum width
    assert "items-center" in applied    # else the label sits at the top of the 40px box


def test_the_poster_action_block_is_pinned_to_the_bottom_of_its_card() -> None:
    """The trend line is optional, so a title that never charted before had its whole
    action block 22px higher than its neighbours. The grid stretches every card to the
    same height, so the slack belongs at the bottom of the card.
    """
    applied = _applied_to(".poster-actions")

    assert "mt-auto" in applied
    assert "mt-1" not in applied  # a fixed top margin re-introduces the offset


def test_a_linked_card_clears_the_sticky_header() -> None:
    """The week chips link to #movie-<rank>. The topnav is `sticky top-0 h-16`, so
    without scroll margin the browser lands the card exactly underneath it and the one
    title the user asked for is the one hidden behind the bar.
    """
    applied = _applied_to(".poster-card")

    assert "scroll-mt-24" in applied  # 96px > the 64px bar


def test_the_arrival_flash_is_marked_and_survives_reduced_motion() -> None:
    """Arriving is only useful if you can see where you landed. The border is the part
    that must not depend on motion: a reader who prefers reduced motion still needs the
    card marked, so only the animation is dropped for them.
    """
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))

    target_rule = _applied_to(".poster-card:target .poster-frame")
    assert "border-primary" in target_rule  # the persistent mark, not an animation
    assert "bm-poster-flash" in source      # ...and the pulse on top of it

    # The media block runs to the next at-rule or the end of the file.
    reduced = source.split("prefers-reduced-motion")[1].split("@")[0]
    assert ".poster-card:target" in reduced
    assert "animation: none" in reduced


def test_the_connection_health_classes_are_written_out_in_full() -> None:
    """Tailwind scans templates for LITERAL class names, so a composed
    `app-health-{{ state }}` is invisible to it: all three modifiers were being purged
    from the built stylesheet, leaving every connection state the same grey with the
    coloured dot as the only cue.

    No rendered-page test can catch this. The class name IS in the HTML — it is the CSS
    rule that is missing, and the CSS is built inside the Docker image.
    """
    template = (
        Path(__file__).resolve().parent.parent.parent
        / "app" / "templates" / "settings.html"
    ).read_text(encoding="utf-8")

    for state in ("ok", "auth", "unreachable"):
        assert f"app-health-{state}" in template, f"no literal class for the {state} state"
        assert _applied_to(f".app-health-{state}"), f"stylesheet defines no .app-health-{state}"
    # The composed form is what broke it, and it must not come back.
    assert "app-health-{{" not in template


def test_the_guess_hint_is_a_literal_class_carrying_a_themed_colour() -> None:
    """Same purge lesson, and one more: the hint is the app's only amber, and amber is
    exactly where a raw palette step fails — amber-400 reads 10.3:1 on the dark card and
    1.4:1 on the light one. So the class must be literal in the template AND its colour
    must come from a token, which the completeness and AA tests then police per theme.
    """
    template = (
        Path(__file__).resolve().parent.parent.parent
        / "app" / "templates" / "report_detail.html"
    ).read_text(encoding="utf-8")

    assert 'class="guess-hint"' in template
    assert "text-warn" in _applied_to(".guess-hint")
    assert "--bm-warn" in _tokens_of(":root")


# --- theme tokens: the colour system is one block, not a value per rule ---

CONFIG = Path(__file__).resolve().parent.parent.parent / "tailwind.config.js"
_TOKEN_BLOCK = re.compile(r"^(:root|html\.light)\s*\{(.*?)^\}", re.S | re.M)
_TOKEN_DECL = re.compile(r"(--bm-[a-z-]+)\s*:")


def _token_block(selector: str) -> str:
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))
    for match in _TOKEN_BLOCK.finditer(source):
        if match.group(1) == selector:
            return match.group(2)
    raise AssertionError(f"no {selector} token block in the stylesheet")


def _tokens_of(selector: str) -> set[str]:
    return set(_TOKEN_DECL.findall(_token_block(selector)))


def test_every_config_colour_is_a_variable() -> None:
    """A theme is one block of tokens, so no rule may carry a literal colour.

    A hex left in the config would look right in dark and silently stay dark in light —
    the failure mode is invisible until someone switches theme and finds one component
    still painted for the other one.
    """
    colours = re.search(r"colors: \{(.*?)\n      \},", CONFIG.read_text(encoding="utf-8"), re.S)
    assert colours, "no colors block in tailwind.config.js"
    body = colours.group(1)

    assert "#" not in body, "a raw hex remains in the config's colours"
    referenced = set(re.findall(r"var\((--bm-[a-z-]+)\)", body))
    assert referenced, "the colours block references no tokens"
    missing = referenced - _tokens_of(":root")
    assert not missing, f"config references tokens the stylesheet never defines: {sorted(missing)}"


def test_no_opacity_utility_is_applied_to_a_token_colour() -> None:
    """Tailwind emits `color: var(--bm-x)` for a variable colour — with no rgb()/alpha
    wrapper for `bg-opacity-*` or `bg-x/50` to modify, so such a utility is silently
    inert. `.rank-chip` lost its translucency to exactly this, and a pixel diff against
    the previous build was the only thing that noticed.
    """
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))
    token_names = {name.removeprefix("--bm-") for name in _tokens_of(":root")}

    legacy = re.findall(r"\b(?:bg|text|border|ring|divide|placeholder)-opacity-\d+", source)
    assert not legacy, f"opacity utilities cannot modify a var() colour: {legacy}"

    for utility, name, _ in re.findall(
        r"\b((?:bg|text|border|ring|from|to|via)-([a-z-]+)/(\d{1,3}))\b", source
    ):
        assert name not in token_names, (
            f"{utility} applies an opacity modifier to the token colour '{name}'; "
            "carry the translucent value in its own token instead"
        )


def _hex_of(selector: str, token: str) -> str:
    """One token's value from a theme block, as #rrggbb."""
    match = re.search(rf"{re.escape(token)}\s*:\s*(#[0-9a-fA-F]{{6}})", _token_block(selector))
    assert match, f"{token} is not a 6-digit hex in {selector}"
    return match.group(1)


def _relative_luminance(colour: str) -> float:
    channels = [int(colour.lstrip("#")[index : index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)), reverse=True
    )
    return (lighter + 0.05) / (darker + 0.05)


# Text-on-ground pairs the app actually renders, as (foreground, background).
_CONTRAST_PAIRS = (
    ("--bm-on-background", "--bm-background"),
    ("--bm-on-surface", "--bm-surface-container-low"),
    ("--bm-primary", "--bm-background"),
    ("--bm-primary", "--bm-surface-container-low"),
    ("--bm-primary", "--bm-slate-panel"),          # the gross overlay on a poster card
    ("--bm-outline", "--bm-background"),           # every .subtle line and label
    ("--bm-outline", "--bm-surface-container-low"),
    ("--bm-on-surface-variant", "--bm-surface-container-low"),
    ("--bm-error", "--bm-surface-container-low"),  # .btn-danger, .form-error
    ("--bm-ok", "--bm-surface-container-low"),     # connection health, banners
    ("--bm-ok-banner", "--bm-surface-container-low"),
    ("--bm-danger", "--bm-surface-container-low"),
    ("--bm-danger-banner", "--bm-surface-container-low"),
    ("--bm-background", "--bm-primary"),           # .btn-primary, label on the fill
    ("--bm-warn", "--bm-surface-container-low"),   # .guess-hint on a weekly card
)
_AA_NORMAL_TEXT = 4.5


def test_light_defines_every_dark_token() -> None:
    """A token added to one block and forgotten in the other inherits the wrong theme's
    value — one component painted for the opposite ground, which nothing else catches."""
    dark, light = _tokens_of(":root"), _tokens_of("html.light")

    assert dark, "no tokens found in :root"
    assert light == dark, (
        f"missing from light: {sorted(dark - light)}; "
        f"only in light: {sorted(light - dark)}"
    )


def test_both_themes_meet_wcag_aa_on_every_text_pair() -> None:
    """The palette cannot regress below AA without this failing.

    Light is where it matters: the state colours are the trap — green-400 reads 10.7:1 on
    the dark background and 1.6:1 on the light one, so the same value cannot serve both.
    """
    for theme in (":root", "html.light"):
        for foreground, background in _CONTRAST_PAIRS:
            ratio = _contrast(_hex_of(theme, foreground), _hex_of(theme, background))
            assert ratio >= _AA_NORMAL_TEXT, (
                f"{theme}: {foreground} on {background} is {ratio:.2f}:1, below AA"
            )


def test_each_theme_declares_a_colour_scheme() -> None:
    """What makes the browser's own widgets follow along — the weekly view's date picker
    draws itself and never sees this stylesheet."""
    assert "color-scheme: dark" in _token_block(":root")
    assert "color-scheme: light" in _token_block("html.light")


def test_both_theme_classes_are_written_out_in_full() -> None:
    """base.html branches on the theme rather than interpolating it.

    Two reasons, neither visible at runtime while everything else is correct — which is
    why this is pinned on the source rather than left to a rendering test. Tailwind scans
    templates for literal class names, so an interpolated one is invisible to it (the
    mistake that purged the connection-health colours). And branching means no value read
    back from user.yml is ever placed into markup, so the template stays safe even if the
    read-side validation in `users._validated_theme` regresses.
    """
    base = (
        Path(__file__).resolve().parent.parent.parent / "app" / "templates" / "base.html"
    ).read_text(encoding="utf-8")

    assert 'class="light"' in base
    assert 'class="dark"' in base
    assert 'class="{{ theme }}"' not in base


def test_the_add_control_exists_in_exactly_one_template() -> None:
    """It had drifted into two copies already — the weekly card and the search result —
    and the movie modal would have made three. Three forms posting to one route is three
    places to forget a hidden field, and the CSRF token is one of them.

    Asserted on the split-button markup rather than the form tag: that is the part with
    the per-connection menu, and the part a fourth copy would be tempted to paste.
    """
    templates = Path(__file__).resolve().parent.parent.parent / "app" / "templates"
    carriers = sorted(
        path.name for path in templates.glob("*.html") if "split-add" in path.read_text("utf-8")
    )

    assert carriers == ["_add_control.html"], f"the Add control is duplicated in {carriers}"
    # Every page that offers an add reaches it through the include, not a copy.
    including = sorted(
        path.name
        for path in templates.glob("*.html")
        if '{% include "_add_control.html" %}' in path.read_text("utf-8")
    )
    assert including == ["_movie_detail.html", "_search_results.html", "report_detail.html"]


def test_where_a_film_lives_is_said_in_exactly_one_template() -> None:
    """The second copy of this line already existed before it grew a link — the search
    results and the movie modal each carried it. A third would be one more place for the
    two to disagree about whether a queued film is In Library or Wanted."""
    templates = Path(__file__).resolve().parent.parent.parent / "app" / "templates"
    carriers = sorted(
        path.name for path in templates.glob("*.html") if 'class="held"' in path.read_text("utf-8")
    )

    assert carriers == ["_held.html"], f"the held line is duplicated in {carriers}"
    including = sorted(
        path.name
        for path in templates.glob("*.html")
        if '{% include "_held.html" %}' in path.read_text("utf-8")
    )
    assert including == ["_movie_detail.html", "_search_results.html"]


def test_the_upgrade_control_exists_in_exactly_one_template() -> None:
    """The third form posting to a mutating route, and the third to be shared rather than
    pasted. Two copies of this one — the primary's copy and a copy held elsewhere — would
    be two places for the connection id to drift out of step with the profile list beside
    it, which is precisely the per-database bug the form exists to avoid."""
    templates = Path(__file__).resolve().parent.parent.parent / "app" / "templates"
    carriers = sorted(
        path.name
        for path in templates.glob("*.html")
        if "/upgrade-movie" in path.read_text("utf-8")
    )

    assert carriers == ["_upgrade_control.html"], f"the upgrade form is duplicated in {carriers}"
    including = sorted(
        path.name
        for path in templates.glob("*.html")
        if '{% include "_upgrade_control.html" %}' in path.read_text("utf-8")
    )
    assert including == ["report_detail.html"]


def test_the_progress_fill_classes_live_outside_the_layers() -> None:
    """The app-health purge lesson, in its sharpest form yet.

    `where-chip-p{{ step }}` is composed in the template, so Tailwind's scan cannot see any
    of the eleven names. Inside `@layer` they would all be tree-shaken and every download
    fill would silently vanish from the built CSS — with the class still in the HTML, so no
    rendered-page test would notice. Top-level rules are emitted verbatim, which is why the
    token blocks and the keyframes live out there too.
    """
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))
    layers_start = source.index("@layer")

    for step in range(0, 101, 10):
        rule = f".where-chip-p{step} {{"
        assert rule in source, f"no fill rule for {step}%"
        assert source.index(rule) < layers_start, (
            f".where-chip-p{step} is inside @layer — Tailwind will purge it"
        )

    template = (
        Path(__file__).resolve().parent.parent.parent / "app" / "templates" / "dashboard.html"
    ).read_text(encoding="utf-8")
    # The composed form is the whole reason the rules sit out there.
    assert "where-chip-p{{" in template


def test_the_scroll_to_top_button_is_a_thumb_sized_target_clear_of_the_notch() -> None:
    """It exists for a phone held one-handed, which is the case the app's ordinary 38px
    buttons are not sized for: 44px is the smallest target WCAG 2.5.5 accepts and what a
    thumb reliably hits. The offsets carry env(safe-area-inset-*) so a notched phone's home
    indicator does not sit on top of it — env() is 0 elsewhere, so a desktop sees the same
    1rem inset.
    """
    applied = _applied_to(".to-top")

    assert "w-11" in applied and "h-11" in applied  # 2.75rem = 44px
    assert "fixed" in applied
    # Its own edge is the only thing saying a control is there — it floats over the page
    # rather than sitting in it. outline-variant measured 1.98:1 dark / 1.56:1 light
    # against the background, under the 3:1 WCAG 1.4.11 wants for a non-text indicator.
    assert "border-outline" in applied and "border-outline-variant" not in applied
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))
    assert "env(safe-area-inset-bottom)" in source
    assert "env(safe-area-inset-right)" in source


def test_the_scroll_to_top_button_yields_the_corner_to_a_toast() -> None:
    """They share the bottom-right, and below `sm` the toast region spans the full width
    (`left-6 right-6`). When both are on screen the confirmation of something just done
    matters more than a scroll shortcut, and a toast is gone in 3.4s — so the button sits
    under it, not over it.
    """
    assert "z-40" in _applied_to(".to-top")
    assert "z-50" in _applied_to(".toast-region")


def test_the_scroll_to_top_button_stays_hidden_without_the_script() -> None:
    """Preflight's `[hidden] { display: none }` lives in @layer base, which @layer
    components beats — so `.to-top`'s own `grid` would override the attribute and put a
    button in the corner of every page, on a short one and with the script blocked alike.
    """
    assert "grid" in _applied_to(".to-top")  # ...which is exactly what needs overriding
    assert "hidden" in _applied_to(".to-top[hidden]")


# --- an element that ships `hidden` has to actually be hidden ---

TEMPLATES = Path(__file__).resolve().parent.parent.parent / "app" / "templates"
# Utilities that set `display`. Any of these on an element that also ships the `hidden`
# attribute beats preflight's `[hidden] { display: none }`, because this stylesheet's
# layer wins over base — so the element paints regardless of the attribute.
_DISPLAY_UTILITIES = {
    "flex", "grid", "block", "inline-block", "inline-flex", "inline", "table",
    "flow-root", "contents", "list-item", "inline-grid",
}
_HIDDEN_TAG = re.compile(r"<[a-z]+[^>]*\bhidden\b[^>]*>")
_CLASS_ATTR = re.compile(r'class="([^"]*)"')


def _classes_shipped_hidden() -> set[str]:
    """Every class on an element that renders with the `hidden` attribute.

    Read from the templates rather than listed here, so a component added later is
    covered without anyone remembering to add it.
    """
    found: set[str] = set()
    for template in TEMPLATES.glob("*.html"):
        for tag in _HIDDEN_TAG.findall(template.read_text(encoding="utf-8")):
            if "aria-hidden" in tag and not re.search(r"(?<!aria-)\bhidden\b(?!=)", tag):
                continue  # aria-hidden hides it from a screen reader, not from layout
            attribute = _CLASS_ATTR.search(tag)
            if attribute:
                found.update(attribute.group(1).split())
    return found


def test_anything_that_ships_hidden_stays_hidden() -> None:
    """The save bar shipped `hidden` AND `display: flex`, so it painted on every page
    load — telling people they had unsaved changes before they touched anything, and
    refusing to leave when they pressed Discard. `.to-top` had carried the fix and the
    explanation for months; the second component to need it did not get one.

    Stated over the templates so the third one cannot repeat it.
    """
    source = _COMMENTS.sub("", STYLESHEET.read_text(encoding="utf-8"))
    for css_class in sorted(_classes_shipped_hidden()):
        selector = f".{css_class}"
        rules = [
            rule.group(2)
            for rule in _RULES.finditer(source)
            if selector in [part.strip() for part in rule.group(1).split(",")]
        ]
        sets_display = any(
            utility in _DISPLAY_UTILITIES
            for body in rules
            for utility in re.sub(r"@apply|;", " ", body).split()
        )
        if not sets_display:
            continue  # preflight's rule is unopposed, so `hidden` works on its own
        guard = f"{selector}[hidden]"
        assert any(
            guard in [part.strip() for part in rule.group(1).split(",")]
            for rule in _RULES.finditer(source)
        ), (
            f"{selector} sets its own display and ships hidden, so it needs "
            f"`{guard} {{ @apply hidden; }}` — without it the element paints anyway"
        )
