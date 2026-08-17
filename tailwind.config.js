/** BoxMedia — "Obsidian Archive" design tokens (from the page mockups). */
module.exports = {
  content: ["./app/templates/**/*.html"],
  darkMode: "class",
  theme: {
    extend: {
      // Every colour resolves to a CSS variable defined in styles/tailwind.css, so the
      // theme lives in one block there rather than in a `dark:`/`light:` variant on each
      // rule. Tailwind emits `color: var(--bm-x)` for these (verified against the built
      // stylesheet) — no rgb()/opacity wrapper, which is why no `token/50` opacity
      // modifier may be used with them. The one slash-opacity in the stylesheet uses a
      // literal (`backdrop:bg-black/70`) and is unaffected.
      colors: {
        background: "var(--bm-background)",
        surface: "var(--bm-surface)",
        "surface-container-lowest": "var(--bm-surface-container-lowest)",
        "surface-container-low": "var(--bm-surface-container-low)",
        "surface-container": "var(--bm-surface-container)",
        "surface-container-high": "var(--bm-surface-container-high)",
        "surface-container-highest": "var(--bm-surface-container-highest)",
        "on-background": "var(--bm-on-background)",
        "on-surface": "var(--bm-on-surface)",
        "on-surface-variant": "var(--bm-on-surface-variant)",
        primary: "var(--bm-primary)",
        outline: "var(--bm-outline)",
        "outline-variant": "var(--bm-outline-variant)",
        error: "var(--bm-error)",
        "slate-panel": "var(--bm-slate-panel)",
        "slate-border": "var(--bm-slate-border)",
        "slate-muted": "var(--bm-slate-muted)",
        // Translucent by design — see the token's note in styles/tailwind.css.
        "rank-chip": "var(--bm-rank-chip)",
        // Connection health and banners. Named for what they mean, not for a palette
        // step, because the two themes need different steps to stay legible.
        ok: "var(--bm-ok)",
        "ok-strong": "var(--bm-ok-strong)",
        "ok-banner": "var(--bm-ok-banner)",
        "ok-border": "var(--bm-ok-border)",
        danger: "var(--bm-danger)",
        "danger-strong": "var(--bm-danger-strong)",
        "danger-banner": "var(--bm-danger-banner)",
        "danger-border": "var(--bm-danger-border)",
        warn: "var(--bm-warn)",
      },
      borderRadius: { DEFAULT: "0px", none: "0px" },
      spacing: {
        gutter: "16px",
        unit: "4px",
        margin: "24px",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "-apple-system", "Segoe UI", "Roboto", "sans-serif"],
      },
      fontSize: {
        "headline-lg": ["32px", { lineHeight: "1.2", letterSpacing: "-0.02em", fontWeight: "600" }],
        "headline-md": ["24px", { lineHeight: "1.2", letterSpacing: "-0.01em", fontWeight: "600" }],
        "headline-sm": ["18px", { lineHeight: "1.4", fontWeight: "600" }],
        "body-lg": ["16px", { lineHeight: "1.6", fontWeight: "400" }],
        "body-md": ["14px", { lineHeight: "1.6", fontWeight: "400" }],
        "label-md": ["12px", { lineHeight: "1", letterSpacing: "0.05em", fontWeight: "500" }],
        "label-sm": ["10px", { lineHeight: "1", letterSpacing: "0.02em", fontWeight: "600" }],
      },
    },
  },
  plugins: [],
};
