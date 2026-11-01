/**
 * The panel's look: Home Assistant's own theme variables, so light, dark and custom themes
 * all apply, with a fallback for every one. Tokens and components are Reolink Stamina's,
 * trimmed to what this panel uses. Mobile first: the wide layout is the media query.
 *
 * Every control is at least 44 px to the finger, even where it looks smaller: a chip draws
 * 34 px and keeps a 5 px transparent border around it as hit area.
 */

export const STYLES = /* css */ `
:host {
  --ss-bg: var(--primary-background-color, #f2f4f7);
  --ss-surface: var(--card-background-color, #fff);
  --ss-sunken: var(--secondary-background-color, #e9ecf1);
  --ss-text: var(--primary-text-color, #1c1e21);
  --ss-dim: var(--secondary-text-color, #6b7280);
  --ss-line: var(--divider-color, rgba(127, 127, 127, 0.22));
  --ss-accent: var(--primary-color, #03a9f4);
  --ss-on-accent: var(--text-primary-color, #fff);
  --ss-unusual: var(--warning-color, #ffa600);
  --ss-error: var(--error-color, #db4437);
  --ss-ok: var(--success-color, #43a047);
  --ss-neutral: #8b93a7;
  /* The usual-to-unusual scale: green, yellow, the theme's warning orange, its error red. */
  --ss-lvl-low: var(--ss-ok);
  --ss-lvl-mid: #d8b200;
  --ss-lvl-unusual: var(--ss-unusual);
  --ss-lvl-high: var(--ss-error);
  --ss-radius: var(--ha-card-border-radius, 12px);
  --ss-shadow: var(--ha-card-box-shadow, 0 1px 2px rgba(0, 0, 0, 0.06), 0 2px 8px rgba(0, 0, 0, 0.06));
  --ss-filters-h: 56px;

  display: flex;
  flex-direction: column;
  height: 100vh;
  height: 100dvh;
  background: var(--ss-bg);
  color: var(--ss-text);
  font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif);
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}
*, *::before, *::after { box-sizing: border-box; }
[hidden] { display: none !important; }
:focus-visible { outline: 2px solid var(--ss-accent); outline-offset: 2px; }
button, select { font: inherit; color: inherit; }
button { border: none; background: none; cursor: pointer; padding: 0; }
.dim { color: var(--ss-dim); }

/* ------------------------------------------------------------------ header */

.header {
  display: flex;
  align-items: center;
  gap: 2px;
  height: var(--header-height, 56px);
  padding: 0 4px;
  flex: 0 0 auto;
  background: var(--app-header-background-color, var(--ss-accent));
  color: var(--app-header-text-color, var(--ss-on-accent));
  border-bottom: var(--app-header-border-bottom, none);
  z-index: 3;
}
.header :focus-visible { outline-color: var(--app-header-text-color, var(--ss-on-accent)); outline-offset: -4px; }
.header__title { flex: 1 1 auto; min-width: 0; margin-left: 8px; line-height: 1.25; }
.header__main { font-size: 1.2rem; font-weight: 400; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.header__sub { font-size: 0.82rem; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

.tabs { display: flex; align-self: stretch; flex: 0 0 auto; margin-right: 4px; }
.tab {
  display: inline-flex; align-items: center; min-height: 44px; padding: 0 14px;
  font-size: 0.95rem; font-weight: 500; opacity: 0.75;
  border-bottom: 2px solid transparent;
}
.tab:hover { opacity: 1; }
.tab[aria-selected="true"] { opacity: 1; border-bottom-color: currentColor; }

.icon { --mdc-icon-size: 20px; width: 20px; height: 20px; flex: 0 0 auto; display: inline-flex; }
.icon--sm { --mdc-icon-size: 16px; width: 16px; height: 16px; }
.icon--lg { --mdc-icon-size: 36px; width: 36px; height: 36px; }
.icon-btn {
  display: inline-flex; align-items: center; justify-content: center;
  width: 44px; height: 44px; border-radius: 50%; flex: 0 0 auto;
}
.icon-btn .icon { --mdc-icon-size: 24px; width: 24px; height: 24px; }
.icon-btn:hover:not(:disabled) { background: color-mix(in srgb, currentColor 12%, transparent); }
.icon-btn:disabled { opacity: 0.4; cursor: default; }

/* ------------------------------------------------------------------ views */

.split { flex: 1 1 auto; min-height: 0; display: flex; }
.pane { min-height: 0; overflow: auto; overscroll-behavior: contain; position: relative; }
.pane--list { flex: 1 1 auto; container: list / inline-size; }
/* The list is the main column; the event beside it keeps a sane player width unless dragged.
   The zero-width track holds the drag handle; a width set by hand comes in as a variable. */
.split--wide { display: grid; grid-template-columns: minmax(0, 1fr) 0 var(--ss-detail-w, clamp(380px, 34%, 640px)); }
.resizer {
  position: relative; z-index: 4; width: 10px; margin-left: -3px;
  cursor: col-resize; touch-action: none;
}
.resizer::after {
  content: ""; position: absolute; top: 0; bottom: 0; left: 3px; width: 2px;
  background: transparent; transition: background 120ms;
}
.resizer:hover::after, .resizer:focus-visible::after, .resizer.is-active::after { background: var(--ss-accent); }
.resizer:focus-visible { outline: none; }
.split.is-resizing { cursor: col-resize; user-select: none; -webkit-user-select: none; }
/* Beside the event the list fills its column: no centred gutters on a wide screen. */
.split--wide .pane--list .page { max-width: none; }
.split--wide .pane--list { border-right: 1px solid var(--ss-line); }
.pane--detail { background: var(--ss-bg); }
.page { max-width: 1040px; margin: 0 auto; padding: 0 0 calc(24px + env(safe-area-inset-bottom, 0px)); }

.banner {
  display: flex; gap: 10px; align-items: center; width: calc(100% - 24px); text-align: left;
  margin: 12px 12px 0; padding: 10px 14px; min-height: 44px;
  border-radius: var(--ss-radius);
  background: color-mix(in srgb, var(--ss-unusual) 16%, var(--ss-surface));
  font-size: 0.9rem; color: var(--ss-text);
}
.banner__text { flex: 1 1 auto; }
.banner__action { font-weight: 600; color: color-mix(in srgb, var(--ss-accent) 75%, var(--ss-text)); white-space: nowrap; }
.banner--quiet { background: var(--ss-sunken); }

/* ------------------------------------------------------------------ chips */

.filters {
  display: flex; gap: 2px; align-items: center; height: var(--ss-filters-h);
  padding: 0 7px; overflow-x: auto; scrollbar-width: none;
  position: sticky; top: 0; z-index: 2; background: var(--ss-bg);
}
.filters::-webkit-scrollbar { display: none; }
.chip {
  display: inline-flex; align-items: center; gap: 6px; flex: 0 0 auto;
  height: 44px; padding: 0 14px; border-radius: 22px;
  border: 5px solid transparent; background: var(--ss-surface); background-clip: padding-box;
  box-shadow: inset 0 0 0 1px var(--ss-line);
  font-size: 0.88rem; font-weight: 500; white-space: nowrap; color: var(--ss-text);
}
.chip[aria-pressed="true"], .chip.is-set {
  box-shadow: none;
  background-color: color-mix(in srgb, var(--ss-accent) 20%, var(--ss-surface));
  color: var(--ss-text);
}
.chip--unusual[aria-pressed="true"] {
  background-color: color-mix(in srgb, var(--ss-unusual) 32%, var(--ss-surface));
  color: var(--ss-text);
  font-weight: 600;
}
select.chip {
  appearance: none; -webkit-appearance: none; cursor: pointer;
  padding-right: 30px; max-width: 60vw; text-overflow: ellipsis;
  background-image: linear-gradient(45deg, transparent 50%, currentColor 50%), linear-gradient(135deg, currentColor 50%, transparent 50%);
  background-position: calc(100% - 16px) 15px, calc(100% - 11px) 15px;
  background-size: 5px 5px; background-repeat: no-repeat;
}
.link-btn {
  display: inline-flex; align-items: center; min-height: 44px; padding: 0 10px;
  color: color-mix(in srgb, var(--ss-accent) 80%, var(--ss-text)); font-weight: 500; border-radius: 8px;
}
.link-btn:hover { background: color-mix(in srgb, var(--ss-accent) 10%, transparent); }
.link-btn--small { font-size: 0.85rem; }

/* ------------------------------------------------------------------ list */

.day {
  position: sticky; top: var(--ss-filters-h); z-index: 1;
  margin: 0; padding: 10px 16px 6px; background: var(--ss-bg);
  font-size: 0.8rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ss-dim);
}
.rows { display: flex; flex-direction: column; gap: 1px; background: var(--ss-line); border-block: 1px solid var(--ss-line); }
.row {
  display: flex; gap: 10px; align-items: center; width: 100%; text-align: left;
  padding: 10px 12px 10px 14px; background: var(--ss-surface); position: relative; min-height: 76px;
  color: inherit; text-decoration: none;
}
.row:focus-visible { outline-offset: -2px; }
.row:hover { background: color-mix(in srgb, var(--ss-accent) 5%, var(--ss-surface)); }
.row--unusual::before {
  content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 4px; background: var(--ss-unusual);
}
.row--dropped .thumb { opacity: 0.5; }
.thumb {
  position: relative; flex: 0 0 auto; width: 88px; aspect-ratio: 16 / 9;
  border-radius: 8px; overflow: hidden; background: var(--ss-sunken);
  display: flex; align-items: center; justify-content: center; color: var(--ss-dim);
}
.thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.thumb__len {
  position: absolute; right: 4px; bottom: 4px; padding: 0 5px; border-radius: 4px;
  font-size: 0.7rem; line-height: 1.5; background: rgba(0, 0, 0, 0.66); color: #fff;
  font-variant-numeric: tabular-nums;
}
.row__body { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.row__top { display: flex; flex-wrap: wrap; column-gap: 8px; align-items: baseline; min-width: 0; }
.row__time { font-weight: 600; font-variant-numeric: tabular-nums; white-space: nowrap; }
.row__camera { color: var(--ss-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; max-width: 100%; }
.row__what { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; font-size: 0.9rem; }
.row__why { font-size: 0.84rem; color: var(--ss-text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row__why--usual { color: var(--ss-dim); }

.badge {
  display: inline-flex; align-items: center; gap: 4px; flex: 0 0 auto;
  padding: 1px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; line-height: 1.6;
  background: color-mix(in srgb, var(--tone, var(--ss-neutral)) 22%, transparent);
  color: var(--ss-text);
}
.badge[data-tone="unusual"], .badge[data-tone="warn"] { --tone: var(--ss-unusual); }
.badge[data-tone="ok"] { --tone: var(--ss-ok); }
.badge[data-tone="error"] { --tone: var(--ss-error); }
.badge[data-tone="accent"] { --tone: var(--ss-accent); }
.live-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ss-error); animation: ss-pulse 1.4s infinite; }
@keyframes ss-pulse { 50% { opacity: 0.3; } }

.more { display: flex; justify-content: center; align-items: center; gap: 8px; padding: 16px; color: var(--ss-dim); font-size: 0.88rem; text-align: center; }

.pill {
  position: sticky; top: calc(var(--ss-filters-h) + 8px); z-index: 3;
  display: flex; width: max-content; margin: 0 auto -44px; align-items: center; gap: 6px;
  min-height: 44px; padding: 0 18px; border-radius: 22px;
  background: var(--ss-accent); color: var(--ss-on-accent); font-weight: 600; box-shadow: var(--ss-shadow);
}

/* ------------------------------------------------------------------ states */

.empty {
  display: flex; flex-direction: column; align-items: center; gap: 10px;
  padding: 56px 24px; text-align: center; color: var(--ss-dim);
}
.empty .icon { --mdc-icon-size: 48px; width: 48px; height: 48px; opacity: 0.55; }
.empty__title { font-size: 1.05rem; font-weight: 600; color: var(--ss-text); }
.empty__body { max-width: 42ch; line-height: 1.5; font-size: 0.9rem; }
.btn {
  display: inline-flex; align-items: center; gap: 8px; min-height: 44px; padding: 0 22px;
  border-radius: 22px; font-weight: 500; background: var(--ss-accent); color: var(--ss-on-accent);
}
.skeleton { background: color-mix(in srgb, var(--ss-text) 8%, transparent); border-radius: 6px; animation: ss-pulse 1.4s infinite; }
.spinner {
  width: 22px; height: 22px; border-radius: 50%;
  border: 2px solid color-mix(in srgb, currentColor 25%, transparent); border-top-color: currentColor;
  animation: ss-spin 720ms linear infinite;
}
@keyframes ss-spin { 100% { transform: rotate(360deg); } }

/* ------------------------------------------------------------------ event */

.event { display: grid; gap: 12px; padding: 0 0 12px; }
.event__media, .event__details { display: grid; gap: 8px; align-content: start; }
.event__details { gap: 12px; }
.player {
  position: relative; width: 100%; aspect-ratio: 16 / 9; background: #000; color: #fff;
  display: flex; align-items: center; justify-content: center; overflow: hidden;
}
.player__video { width: 100%; height: 100%; display: block; background: #000; }
.player__poster { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0.35; }
.player__message {
  position: relative; display: flex; flex-direction: column; align-items: center; gap: 6px;
  padding: 16px; text-align: center; max-width: 44ch;
}
.player__title { font-weight: 600; }
.player__body { font-size: 0.88rem; line-height: 1.45; }
.player__spinner { position: relative; }
.player__cover { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
.player__play {
  position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 72px; height: 72px; border-radius: 50%;
  background: rgba(0, 0, 0, 0.6); color: #fff; box-shadow: 0 2px 12px rgba(0, 0, 0, 0.35);
}
.player__play .icon { --mdc-icon-size: 44px; width: 44px; height: 44px; }
.player__play:hover { background: rgba(0, 0, 0, 0.75); }
.player__play:focus-visible { outline-color: #fff; }
.source-switch { display: flex; justify-content: flex-end; padding: 0 4px; margin-top: -4px; }

.card {
  background: var(--ss-surface); border-radius: var(--ss-radius); box-shadow: var(--ss-shadow);
  margin: 0 12px; padding: 14px 16px;
}
.card__title { margin: 16px 0 8px; font-size: 0.8rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ss-dim); }
.card__title:first-child { margin-top: 0; }
.verdict { display: flex; flex-direction: column; align-items: flex-start; gap: 6px; }
.detail__what { color: var(--ss-dim); }
.sentence { margin: 8px 0 0; line-height: 1.5; }
.note { margin: 10px 0 0; font-size: 0.88rem; color: var(--ss-dim); line-height: 1.45; display: flex; gap: 6px; align-items: flex-start; }

.reasons { display: grid; gap: 10px; margin-top: 14px; }
.reason { display: grid; grid-template-columns: 6.5em minmax(0, 1fr) auto; gap: 2px 10px; align-items: center; }
.reason__term { font-size: 0.82rem; color: var(--ss-dim); }
.reason__label { font-weight: 500; min-width: 0; overflow-wrap: anywhere; }
.reason__rest { margin: 0; font-size: 0.84rem; color: var(--ss-dim); }
/* Per signal: a word over a bar, no camera line (a signal has none). */
.meter--signal { width: 84px; }
.meter--signal .meter__track::after { display: none; }

details.card { padding: 0; }
details.card > summary {
  display: flex; align-items: center; gap: 8px; min-height: 48px; padding: 0 16px;
  cursor: pointer; font-weight: 500; list-style: none;
}
details.card > summary::-webkit-details-marker { display: none; }
details.card > summary::after { content: "▾"; margin-left: auto; color: var(--ss-dim); transition: transform 150ms; }
details.card[open] > summary::after { transform: rotate(180deg); }
.details__body { padding: 0 16px 14px; }

.items { display: grid; gap: 10px; }
.item { display: flex; gap: 10px; align-items: flex-start; }
.item__body { flex: 1 1 auto; min-width: 0; }
.item__head { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.item__name { font-weight: 500; }
.item__sub { font-size: 0.84rem; color: var(--ss-dim); margin-top: 2px; }
.path {
  display: block; margin-top: 8px; padding: 8px 10px; border-radius: 8px; background: var(--ss-sunken);
  font-family: var(--code-font-family, ui-monospace, monospace); font-size: 0.8rem;
  overflow-wrap: anywhere; user-select: all;
}
.event-id { margin: 12px 0 0; font-size: 0.78rem; color: var(--ss-dim); overflow-wrap: anywhere; }

/* ------------------------------------------------------------------ small marks */

.mini { width: 28px; height: 4px; border-radius: 2px; background: color-mix(in srgb, var(--ss-accent) 22%, transparent); overflow: hidden; }
.mini > span { display: block; height: 100%; background: var(--ss-accent); }
.dot { width: 10px; height: 10px; border-radius: 50%; flex: 0 0 auto; background: var(--tone, var(--ss-neutral)); }
.dot[data-tone="ok"] { --tone: var(--ss-ok); }
.dot[data-tone="warn"] { --tone: var(--ss-unusual); }
.dot[data-tone="error"] { --tone: var(--ss-error); }
.dot[data-tone="accent"] { --tone: var(--ss-accent); }
.dot[data-tone="none"] { --tone: transparent; }

/* ------------------------------------------------------------------ the Cameras tab */

.cams-page { flex: 1 1 auto; min-height: 0; overflow: auto; container: camspage / inline-size; }
.cams-page__inner { max-width: 1280px; margin: 0 auto; padding: 8px 12px calc(24px + env(safe-area-inset-bottom, 0px)); }
.cams-page__summary { margin: 8px 4px 2px; color: var(--ss-dim); font-size: 0.9rem; }
.cams-page__note { margin: 0 4px 12px; color: var(--ss-dim); font-size: 0.8rem; }
/* The week by hour: a strip of 24 hours per class, darker where busier. */
.cam-recap {
  flex: 1 0 100%; display: grid; grid-template-columns: 5.5em minmax(0, 1fr) 2.5em; gap: 3px 8px; align-items: center;
  margin-top: 6px; font-size: 0.75rem; color: var(--ss-dim);
}
.cam-recap--empty { display: block; }
.cam-recap__strip { display: grid; grid-template-columns: repeat(24, 1fr); gap: 1px; height: 12px; }
.cam-recap__hour { background: var(--ss-accent); border-radius: 1px; }
.cam-recap__hour[data-empty="true"] { background: color-mix(in srgb, var(--ss-text) 7%, transparent); }
.cam-recap__total { text-align: right; font-variant-numeric: tabular-nums; }
.cam-recap__axis { display: grid; grid-template-columns: repeat(4, 1fr); font-size: 0.68rem; }
.cam-table { background: var(--ss-surface); border-radius: var(--ss-radius); box-shadow: inset 0 0 0 1px var(--ss-line); overflow: hidden; }
.cam-area {
  margin: 0; padding: 14px 16px 6px; border-top: 1px solid var(--ss-line);
  font-size: 0.75rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ss-dim);
}
.cam-head + .cam-area { border-top: none; }
/* A phone: the name, then the facts wrapping under it, each with its own small label. */
.cam-head { display: none; }
.cam-row {
  display: flex; flex-wrap: wrap; gap: 4px 16px; align-items: center; width: 100%;
  padding: 10px 16px; min-height: 48px; text-align: left; border-top: 1px solid var(--ss-line);
}
.cam-area + .cam-row { border-top: none; }
.cam-row:hover { background: color-mix(in srgb, var(--ss-accent) 6%, transparent); }
.cam-row__name { flex: 1 0 100%; font-weight: 600; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cam-cell { display: inline-flex; align-items: center; gap: 6px; font-size: 0.86rem; font-variant-numeric: tabular-nums; min-width: 0; }
.cam-cell__label { color: var(--ss-dim); }
.cam-cell__label::after { content: ":"; }
.cam-cell__unusual { color: color-mix(in srgb, var(--ss-unusual) 60%, var(--ss-text)); font-weight: 600; }
/* Wide enough: one table, a header row, no labels in the cells. */
@container camspage (min-width: 760px) {
  .cam-head, .cam-row {
    display: grid; grid-template-columns: minmax(9em, 1.5fr) 0.8fr 1fr 1fr 1.1fr 1fr; gap: 12px; align-items: center;
  }
  .cam-head {
    padding: 10px 16px; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ss-dim);
    border-bottom: 1px solid var(--ss-line);
  }
  .cam-row__name { flex: none; }
  .cam-cell__label { display: none; }
  .cam-recap { grid-column: 2 / -1; max-width: 640px; }
}

/* ------------------------------------------------------------------ learning */

/* In place of the meter while a camera learns: neutral, and plainly not a verdict. */
.learning-tag {
  flex: 0 0 auto; align-self: center; padding: 2px 8px; border-radius: 6px; white-space: nowrap;
  font-size: 0.72rem; font-weight: 500; line-height: 1.4; color: var(--ss-dim); text-align: center;
  background: color-mix(in srgb, var(--ss-text) 7%, transparent);
}
.detail__title .learning-tag { align-self: baseline; }
@container list (min-width: 640px) {
  .row .learning-tag { width: 104px; order: -1; }
}

/* ------------------------------------------------------------------ how unusual */

/* A word over a short bar, both in the level's colour; the tick on the bar is the camera's
   own line (0.5), so the fill says how far past it, or short of it, the event landed. */
.meter { --tone: var(--ss-neutral); display: inline-flex; flex-direction: column; gap: 4px; flex: 0 0 auto; width: 76px; }
.meter[data-tone="low"] { --tone: var(--ss-lvl-low); }
.meter[data-tone="mid"] { --tone: var(--ss-lvl-mid); }
.meter[data-tone="unusual"] { --tone: var(--ss-lvl-unusual); }
.meter[data-tone="high"] { --tone: var(--ss-lvl-high); }
.meter__label {
  align-self: flex-start; padding: 1px 7px; border-radius: 6px; white-space: nowrap;
  font-size: 0.72rem; font-weight: 700; line-height: 1.5;
  background: color-mix(in srgb, var(--tone) 22%, transparent);
  color: color-mix(in srgb, var(--tone) 45%, var(--ss-text));
}
.meter[data-tone="none"] .meter__label { font-weight: 500; color: var(--ss-dim); background: none; padding-left: 0; }
/* Most events are usual: in the list that word stays plain, so only what is not stands out. */
.meter:not(.meter--large)[data-tone="low"] .meter__label { font-weight: 500; color: var(--ss-dim); background: none; padding-left: 0; }
.meter__track {
  position: relative; height: 6px; border-radius: 3px;
  background: color-mix(in srgb, var(--ss-text) 10%, transparent);
}
/* The camera's line: a tick that stands out of the track, not a cut through the fill. */
.meter__track::after {
  content: ""; position: absolute; left: 50%; top: -3px; bottom: -3px; width: 2px; margin-left: -1px;
  border-radius: 1px; background: var(--ss-dim);
}
.meter__fill { display: block; height: 100%; border-radius: 3px; background: var(--tone); }
.meter--large { width: min(100%, 280px); gap: 6px; }
.meter--large .meter__label { font-size: 0.95rem; padding: 2px 10px; border-radius: 8px; }
.meter--large .meter__track { height: 8px; border-radius: 4px; }
.meter__legend {
  display: flex; justify-content: space-between; margin-top: -2px;
  font-size: 0.68rem; letter-spacing: 0.04em; text-transform: uppercase; color: var(--ss-dim);
}
.verdict .meter--large { flex-direction: column; }

/* ------------------------------------------------------------------ selection */

.row.is-selected { background: color-mix(in srgb, var(--ss-accent) 14%, var(--ss-surface)); }
.detail { background: var(--ss-bg); }
.rows > .detail { padding: 8px 0 4px; }
/* Under its row the event needs no title: the row right above says the same. */
.rows > .detail .detail__title { display: none; }
.detail__title { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; padding: 0 12px; }
.detail__camera { font-weight: 600; font-size: 1.05rem; }
.avails { display: inline-flex; gap: 12px; margin-right: auto; padding: 0 8px; flex-wrap: wrap; }
.avail { display: inline-flex; align-items: center; gap: 4px; font-size: 0.82rem; color: var(--ss-dim); min-height: 32px; }
.avail--on { color: var(--ss-text); }
.avail--on .icon { color: var(--ss-ok); }
.source-switch { align-items: center; flex-wrap: wrap; }

/* ------------------------------------------------------------------ wide */

.pane--detail .event { padding: 16px; gap: 16px; }
.pane--detail .detail__title { padding: 0; }
.pane--detail .event__media .player { border-radius: var(--ss-radius); }
.pane--detail .event .card { margin: 0; }

/* By the list's own width, not the window's: a column dragged narrow gets the phone rows. */
@container list (min-width: 640px) {
  .thumb { width: 128px; }
  .row { gap: 14px; }
  /* The meter sits by the thumbnail: one column to scan, never a long way from the row's text. */
  .row .meter { width: 104px; order: -1; }
  .row .thumb { order: -2; }
  .row { padding-left: 18px; }
  .rows { border-radius: var(--ss-radius); border: 1px solid var(--ss-line); overflow: hidden; margin: 0 12px; }
}
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: 0.001ms !important; animation-iteration-count: 1 !important; transition-duration: 0.001ms !important; }
}
`;
