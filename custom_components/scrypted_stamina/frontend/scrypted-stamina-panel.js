/**
 * Scrypted Stamina: the review panel.
 *
 * One page, no navigation between views:
 *
 *   Wide (over WIDE_PX): master-detail. The event list fills the main column; the selected
 *   event sits on the right at a capped width: its thumbnail under a play button, how unusual
 *   it was and why, signal by signal, and the details folded beneath. On open the latest
 *   unusual event is selected (else the latest); a row click or the arrow keys change only the
 *   right side. The event column can be dragged wider or narrower (a double click on the
 *   handle resets it), remembered per browser.
 *   Narrow (a phone): the list only. Tapping a row opens that event inline under it; tapping
 *   it again, or another row, closes it.
 *
 * Video plays only on a press, from the cloud copy first unless `playback_order` says
 * "recording_first" (the recording loads the camera's recorder, and may be overwritten);
 * the other source takes over when one fails, and a link switches between them.
 *
 * Cameras are a filter like the kinds: one dropdown, an area selecting all of it. How each
 * camera is doing, and when its events happen, is the Cameras tab. Every row carries a
 * usual-to-unusual meter, beside its thumbnail once the list is 640 px wide (at the row's end
 * on a phone), and the one-line why when the event was anything but usual.
 * The URL holds only the filters (`?unusual=1&camera=11`) and the selected event
 * (`/event/<id>`), replaced rather than pushed, so Back leaves the panel. Registered through `panel_custom`, so it runs inside Home Assistant's frontend with
 * the real `hass`. Plain ES modules, no build. `tools/panel-preview/` runs it on sample data.
 */

import { adoptStyles, el, icon, replace } from "./dom.js";
import { STYLES } from "./styles.js";
import {
  DEFAULT_CLASSES,
  NOTES,
  className,
  classIcon,
  classList,
  dayKey,
  decision,
  formatAfter,
  formatAgo,
  formatBefore,
  formatDay,
  formatDuration,
  formatTime,
  joinWords,
  notKept,
  evidenceText,
  hourOf,
  sensorsText,
  signalLevel,
  signalText,
  sourceName,
  usualness,
  verdictText,
} from "./format.js";
import { mountPlayer, playKey } from "./player.js";
import { eventMarkers, mountControls } from "./controls.js";

const DOMAIN = "scrypted_stamina";
const PAGE = 50;
// Live updates: at most one refresh per THROTTLE_MS, none while the tab is hidden.
const THROTTLE_MS = 4000;
const RETRY_MS = 15000;
const SUBSCRIBE_RETRY_MS = 30000;
// Thumbnail URLs are signed for 6 h; one older than this is replaced by a fresh one.
const THUMB_TTL_MS = 5 * 3600 * 1000;
const MAX_ITEMS = 1000;
const NO_FILTERS = { unusual: false, cls: "", camera: "", area: "", dropped: false };
// From this width of the panel, the selected event shows beside the list.
const WIDE_PX = 900;
// The desktop columns can be dragged wider or narrower; the widths stay in this browser.
// Each is clamped against the panel's width in CSS too, so a smaller window never breaks it.
const COLUMNS_KEY = "scrypted-stamina.columns";
const COLUMNS = {
  detail: { min: 320, max: 0.6, edge: "right", label: "Resize the event column" },
};
// The list between them never gets narrower than this by a drag.
const LIST_MIN_PX = 400;
const KEY_STEP_PX = 16;
// The Cameras tab reads the last RECAP_DAYS of events (dropped ones too) from at most
// RECAP_PAGES pages of the list, and again at most every RECAP_REFRESH_MS while it is open.
const RECAP_DAYS = 7;
const RECAP_PAGES = 25;
const RECAP_LIMIT = 200;
const RECAP_REFRESH_MS = 60000;
// The rows of the recap, in this order; any other class counts as "Other".
const RECAP_CLASSES = ["person", "vehicle", "animal", "motion"];

function errorText(err) {
  if (err?.code === "not_loaded") {
    return "Scrypted Stamina is not running. Check it under Settings → Devices & services.";
  }
  if (err?.code === "unauthorized") return "Only administrators can review camera events.";
  return err?.message || String(err || "Something went wrong.");
}

function empty(iconName, title, body, ...actions) {
  return el(
    "div",
    { class: "empty" },
    icon(iconName),
    el("div", { class: "empty__title", text: title }),
    body ? el("div", { class: "empty__body", text: body }) : null,
    actions,
  );
}

function badge(text, tone, ...extra) {
  return el("span", { class: "badge", dataset: { tone } }, extra, text);
}

function filtersFromSearch(search) {
  const params = new URLSearchParams(search || "");
  return {
    unusual: params.get("unusual") === "1",
    cls: params.get("cls") || "",
    camera: params.get("camera") || "",
    area: params.get("area") || "",
    // One choice in the Events select: an old link with both flags reads as Unusual.
    dropped: params.get("dropped") === "1" && params.get("unusual") !== "1",
  };
}

function searchFromFilters(filters) {
  const params = new URLSearchParams();
  if (filters.unusual) params.set("unusual", "1");
  if (filters.cls) params.set("cls", filters.cls);
  if (filters.camera) params.set("camera", filters.camera);
  else if (filters.area) params.set("area", filters.area);
  if (filters.dropped) params.set("dropped", "1");
  const text = params.toString();
  return text ? `?${text}` : "";
}

const sameFilters = (a, b) => searchFromFilters(a) === searchFromFilters(b);

const NO_AREA = "No area";
const byName = (a, b) => a.localeCompare(b, undefined, { sensitivity: "base" });

/** Cameras grouped by their Home Assistant area: areas by name, "No area" last. */
function areaGroups(cams) {
  const groups = new Map();
  for (const cam of cams) {
    const area = cam.area || null;
    if (!groups.has(area)) groups.set(area, []);
    groups.get(area).push(cam);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a === null) - (b === null) || byName(a || "", b || ""))
    .map(([area, members]) => ({ area, label: area || NO_AREA, cams: members.sort((x, y) => byName(x.name, y.name)) }));
}

/** The usual-to-unusual meter: a word over a bar whose tick is the camera's own line. */
function meter(score, unusual, large = false) {
  const level = usualness(score, unusual);
  const pct = level.fill === null ? null : Math.round(level.fill * 100);
  const title =
    pct === null ? level.label : `${level.label}: ${pct} of 100, where 50 is this camera's line for unusual`;
  return el(
    "span",
    { class: `meter${large ? " meter--large" : ""}`, dataset: { tone: level.tone }, title, role: "img", "aria-label": title },
    el("span", { class: "meter__label", text: level.label }),
    pct === null
      ? null
      : el(
          "span",
          { class: "meter__track", "aria-hidden": "true" },
          el("span", { class: "meter__fill", style: { width: `${pct}%` } }),
        ),
    large && pct !== null
      ? el("span", { class: "meter__legend", "aria-hidden": "true" }, el("span", { text: "Usual" }), el("span", { text: "Unusual" }))
      : null,
  );
}

/** While a camera learns, its events carry this instead of a meter: no judgement yet. */
function learningTag(learning) {
  return el("span", {
    class: "learning-tag",
    title: `${learning.long}. Not judged until the camera has learned what is normal.`,
    text: learning.short,
  });
}

/** How unusual one signal was: the same meter, without the camera's line (it has none). */
function signalMeter(weight) {
  const level = signalLevel(weight);
  const pct = Math.round(level.fill * 100);
  const title = `${level.label}: ${weight.toFixed(2)} rarer than chance`;
  return el(
    "span",
    { class: "meter meter--signal", dataset: { tone: level.tone }, title, role: "img", "aria-label": level.label },
    el("span", { class: "meter__label", text: level.label }),
    el("span", { class: "meter__track", "aria-hidden": "true" }, el("span", { class: "meter__fill", style: { width: `${pct}%` } })),
  );
}

/**
 * What was seen, one line per thing: the same class with the same verdict, reported by more
 * than one source, reads once (with every source, and the longest it stayed).
 */
function mergeSubjects(subjects) {
  const merged = new Map();
  for (const subject of subjects) {
    const check = verdictText(subject);
    const key = `${subject.label || subject.class}|${check.text}`;
    const known = merged.get(key);
    if (!known) {
      merged.set(key, { subject: { ...subject, seen_by: [...(subject.seen_by || [])] }, check });
      continue;
    }
    for (const source of subject.seen_by || []) {
      if (!known.subject.seen_by.includes(source)) known.subject.seen_by.push(source);
    }
    known.subject.dwell = Math.max(known.subject.dwell || 0, subject.dwell || 0) || null;
  }
  return [...merged.values()];
}

/** "Area · Camera" where the area is known and says something the name does not. */
function cameraLabel(area, name) {
  return area && area !== name ? `${area} · ${name}` : name;
}

class ScryptedStaminaPanel extends HTMLElement {
  constructor() {
    super();
    this._root = this.attachShadow({ mode: "open" });
    adoptStyles(this._root, STYLES);
    this._header = el("header", { class: "header" });
    this._listPane = el("div", { class: "pane pane--list" });
    this._detailPane = el("aside", { class: "pane pane--detail", "aria-label": "Event" });
    this._detail = el("div", { class: "detail", role: "region", "aria-label": "Selected event" });
    this._columns = this._loadColumns();
    this._detailHandle = this._resizer("detail");
    this._main = el("main", { class: "split" }, this._listPane, this._detailHandle, this._detailPane);
    this._applyColumns();
    this._camsPage = el("main", { class: "cams-page", hidden: true, "aria-label": "Cameras" });
    this._root.append(this._header, this._main, this._camsPage);
    // "events" or "cameras": the two tabs, from the route.
    this._view = "events";
    this._today = null;

    this._filters = { ...NO_FILTERS };
    this._list = this._freshList();
    this._cams = { items: [], loaded: false, error: null, needs: null };
    this._event = null;
    this._selected = null;
    this._wide = false;
    // The route a filter change is writing (`_setRoute`), so `_routeChanged` knows it.
    this._filterRoute = null;
    this._link = true;
    this._thumbs = new Map();
    this._rows = new Map();
    this._lastRefresh = 0;
    this._onVisible = () => {
      if (!document.hidden && this._dirty) this._nudge();
    };

    this._buildList();
    this._onKey = (ev) => this._key(ev);
    this._root.addEventListener("keydown", this._onKey);
    this._resize = new ResizeObserver(() => this._layout());
  }

  _freshList() {
    return {
      items: [], cursor: null, loading: false, loaded: false, error: null, token: (this._list?.token || 0) + 1,
      updatedAt: 0, stale: false, hidden: 0, hiddenMore: false, pending: null,
    };
  }

  // ---------------------------------------------------------------- Home Assistant

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._start();
  }

  get hass() {
    return this._hass;
  }

  set narrow(value) {
    this._narrow = value;
    if (this._started) this._renderHeader();
  }

  set panel(value) {
    this._panel = value;
  }

  set route(value) {
    this._route = value;
    this._routeChanged();
  }

  connectedCallback() {
    document.addEventListener("visibilitychange", this._onVisible);
    this._resize.observe(this);
    this._start();
  }

  disconnectedCallback() {
    document.removeEventListener("visibilitychange", this._onVisible);
    this._resize.disconnect();
    this._started = false;
    this._routeKey = null;
    clearTimeout(this._nudgeTimer);
    clearTimeout(this._retryTimer);
    clearTimeout(this._subscribeTimer);
    this._nudgeTimer = this._retryTimer = this._subscribeTimer = null;
    const unsub = this._unsub;
    this._unsub = null;
    unsub?.then((stop) => stop?.()).catch(() => {});
    this._stopVideo();
  }

  _start() {
    if (this._started || !this._hass || !this.isConnected) return;
    this._started = true;
    const width = this.getBoundingClientRect().width;
    this._wide = width >= WIDE_PX;
    this._subscribe();
    this._loadCameras();
    if (this._list.loaded) this._refreshList();
    this._renderHeader();
    this._routeChanged();
  }

  /** Beside the list, or under its row: decided by the panel's own width. */
  _layout() {
    const width = this.getBoundingClientRect().width;
    const wide = width >= WIDE_PX;
    if (wide === this._wide) return;
    this._wide = wide;
    this._placeDetail();
    this._renderList();
    if (wide && !this._selected) this._preselect();
  }


  _placeDetail() {
    this._main.classList.toggle("split--wide", this._wide);
    this._detailPane.hidden = !this._wide;
    this._detailHandle.hidden = !this._wide;
    if (!this._wide) return;
    this._idle ??= empty("mdi:gesture-tap", "Pick an event", "Its video and why it was flagged show here.");
    const target = this._selected ? this._detail : this._idle;
    // Moving a node that holds a playing video pauses it: only place it when it is elsewhere.
    if (this._detailPane.firstChild !== target || this._detailPane.childNodes.length !== 1) {
      replace(this._detailPane, target);
    }
  }

  // ---------------------------------------------------------------- column widths

  _loadColumns() {
    try {
      const saved = JSON.parse(localStorage.getItem(COLUMNS_KEY) || "{}");
      const columns = {};
      for (const name of Object.keys(COLUMNS)) {
        if (Number.isFinite(saved?.[name])) columns[name] = saved[name];
      }
      return columns;
    } catch {
      return {};
    }
  }

  _saveColumns() {
    try {
      if (Object.keys(this._columns).length) localStorage.setItem(COLUMNS_KEY, JSON.stringify(this._columns));
      else localStorage.removeItem(COLUMNS_KEY);
    } catch {
      // Private mode or storage switched off: the widths last until the page closes.
    }
  }

  /** A width set by hand overrides the default; CSS still clamps it against the panel. */
  _applyColumns() {
    for (const [name, spec] of Object.entries(COLUMNS)) {
      const width = this._columns[name];
      const handle = this._detailHandle;
      if (width === undefined) {
        this._main.style.removeProperty(`--ss-${name}-w`);
      } else {
        this._main.style.setProperty(`--ss-${name}-w`, `clamp(${spec.min}px, ${Math.round(width)}px, ${spec.max * 100}%)`);
      }
      const shown = this._columnWidth(name);
      if (shown) handle.setAttribute("aria-valuenow", String(Math.round(shown)));
    }
  }

  _columnWidth(name) {
    const pane = this._detailPane;
    return pane.hidden ? 0 : pane.getBoundingClientRect().width;
  }

  _setColumn(name, width) {
    const spec = COLUMNS[name];
    const total = this._main.getBoundingClientRect().width;
    const max = Math.min(total * spec.max, total - LIST_MIN_PX);
    this._columns[name] = Math.max(spec.min, Math.min(width, max));
    this._applyColumns();
  }

  /**
   * The handle on a column's inner edge: drag it, or focus it and use the arrow keys;
   * a double click puts the default back.
   */
  _resizer(name) {
    const spec = COLUMNS[name];
    const handle = el("div", {
      class: "resizer",
      role: "separator",
      tabindex: "0",
      title: "Drag to resize, double-click to reset",
      "aria-label": spec.label,
      "aria-orientation": "vertical",
      "aria-valuemin": String(spec.min),
      hidden: true,
    });
    const widthAt = (x) => {
      const box = this._main.getBoundingClientRect();
      return spec.edge === "left" ? x - box.left : box.right - x;
    };
    handle.addEventListener("pointerdown", (ev) => {
      if (ev.button !== 0) return;
      ev.preventDefault();
      handle.setPointerCapture(ev.pointerId);
      handle.classList.add("is-active");
      this._main.classList.add("is-resizing");
    });
    handle.addEventListener("pointermove", (ev) => {
      if (handle.hasPointerCapture(ev.pointerId)) this._setColumn(name, widthAt(ev.clientX));
    });
    const end = (ev) => {
      if (!handle.hasPointerCapture(ev.pointerId)) return;
      handle.releasePointerCapture(ev.pointerId);
      this._saveColumns();
    };
    handle.addEventListener("pointerup", end);
    handle.addEventListener("pointercancel", end);
    handle.addEventListener("lostpointercapture", () => {
      handle.classList.remove("is-active");
      this._main.classList.remove("is-resizing");
    });
    handle.addEventListener("dblclick", () => {
      delete this._columns[name];
      this._applyColumns();
      this._saveColumns();
    });
    handle.addEventListener("keydown", (ev) => {
      if (ev.key !== "ArrowLeft" && ev.key !== "ArrowRight") return;
      ev.preventDefault();
      // Right widens the column on the left, and narrows the one on the right.
      const step = (ev.shiftKey ? 4 : 1) * KEY_STEP_PX * (ev.key === "ArrowRight" ? 1 : -1);
      this._setColumn(name, this._columnWidth(name) + (spec.edge === "left" ? step : -step));
      this._saveColumns();
    });
    return handle;
  }

  /** Videos in the open event that are playing now, to resume after the list is redrawn. */
  _playing() {
    const videos = [...this._detail.querySelectorAll("video")];
    for (const player of this._detail.querySelectorAll("ha-hls-player")) {
      const inner = player.shadowRoot?.querySelector("video");
      if (inner) videos.push(inner);
    }
    return videos.filter((video) => !video.paused);
  }

  _subscribe() {
    if (!this._started) return;
    this._unsub = this._hass.connection
      .subscribeMessage(() => this._nudge(), { type: `${DOMAIN}/subscribe` })
      .catch(() => {
        // Not loaded yet, or a hiccup: try again in a while, as long as the panel is open.
        this._unsub = null;
        this._subscribeTimer = setTimeout(() => this._subscribe(), SUBSCRIBE_RETRY_MS);
        return null;
      });
  }

  _ws(message) {
    return this._hass.callWS(message);
  }

  /** Something changed: refresh what is on screen, at most every THROTTLE_MS, never hidden. */
  _nudge() {
    this._dirty = true;
    if (document.hidden || this._nudgeTimer || !this._started) return;
    const wait = Math.max(0, this._lastRefresh + THROTTLE_MS - Date.now());
    this._nudgeTimer = setTimeout(() => {
      this._nudgeTimer = null;
      if (document.hidden) return;
      this._dirty = false;
      this._lastRefresh = Date.now();
      if (this._list.loaded) this._refreshList();
      if (this._cams.loaded) this._loadCameras();
      if (this._event?.data) this._loadEvent(this._event.id);
      if (this._view === "cameras") this._loadWeek();
    }, wait);
  }

  // ---------------------------------------------------------------- routing

  _search() {
    // Home Assistant's route has no query; the preview passes one in `route.search`.
    return this._route?.search ?? window.location.search;
  }

  _routeChanged() {
    if (!this._hass || !this._started) return;
    const path = this._route?.path || "";
    const search = this._search();
    // Home Assistant may hand over the same route again; only a new one changes anything.
    if (`${path}${search}` === this._routeKey) return;
    this._routeKey = `${path}${search}`;
    // Whether this route is one a filter change wrote, whoever passes it in first.
    const filtering = this._routeKey === this._filterRoute;
    this._filterRoute = null;

    const filters = filtersFromSearch(search);
    const view = path.startsWith("/cameras") ? "cameras" : "events";
    if (view !== this._view) this._showView(view);
    const match = path.match(/^\/event\/([A-Za-z0-9_.-]+)/);
    const id = match ? match[1] : null;
    if (!sameFilters(filters, this._filters) || (!this._list.loaded && !this._list.loading)) {
      this._filters = filters;
      // A selection from the URL (a link) stays even when it is not on the first page; one
      // that a filter change left off the list gives way to the latest that matches.
      this._routeHadEvent = Boolean(id) && !filtering;
      this._listPane.scrollTop = 0;
      this._loadList();
    }
    // The Cameras page leaves the open event as it is, for when Events comes back.
    if (view === "events" && id !== this._selected) this._select(id, { route: false });
    this._placeDetail();
  }

  /** One tab or the other; the event list keeps its place while Cameras is up. */
  _showView(view) {
    this._view = view;
    this._main.hidden = view !== "events";
    this._camsPage.hidden = view !== "cameras";
    this._renderHeader();
    if (view === "cameras") {
      for (const video of this._playing()) video.pause();
      this._renderCamsPage();
      this._loadWeek();
    }
  }

  _url(path, filters = this._filters) {
    const prefix = this._route?.prefix ?? "/scrypted-stamina";
    return `${prefix}${path}${searchFromFilters(filters)}`;
  }

  /**
   * Write the filters and the selection to the URL, replacing: Back leaves the panel.
   * `filtering` says a filter changed it, so an open event that no longer matches gives way.
   */
  _setRoute({ filters = this._filters, id = this._selected, filtering = false, view = this._view } = {}) {
    const path = view === "cameras" ? "/cameras" : id ? `/event/${id}` : "";
    this._filterRoute = filtering ? `${path}${searchFromFilters(filters)}` : null;
    history.replaceState(history.state, "", this._url(path, filters));
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: true } }));
    // Home Assistant does not always pass a new route when only the query changed.
    if (this._route && this._route.search === undefined) {
      this._route = { ...this._route, path };
    } else if (this._route) {
      this._route = { ...this._route, path, search: searchFromFilters(filters) };
    }
    this._routeChanged();
    this._filterRoute = null;
  }

  /**
   * Show one event: beside the list when wide, under its row on a phone. Selecting the open
   * row again on a phone closes it; `null` closes whatever is open.
   */
  _select(id, { route = true, focus = false } = {}) {
    if (id && id === this._selected && !this._wide && route) id = null;
    if (id !== this._selected) {
      this._stopVideo();
      this._selected = id;
      this._event = null;
      if (id) this._openEvent(id);
    }
    if (route) {
      this._setRoute({ id });
    }
    this._placeDetail();
    this._renderList();
    if (focus && id) this._rows.get(id)?.node.focus({ preventScroll: false });
  }

  /** Wide only: the latest unusual event, else the latest, so it plays without a click. */
  async _preselect() {
    if (!this._wide || this._selected) return;
    const token = this._list.token;
    let pick = null;
    try {
      const result = await this._ws({ ...this._query(), unusual: true, limit: 1 });
      pick = result.events[0] || null;
    } catch {
      pick = null;
    }
    if (token !== this._list.token || this._selected || !this._wide) return;
    pick = pick || this._list.items[0];
    if (pick) this._select(pick.id, { route: true });
  }

  /** Up and down move the selection when the list and the event sit side by side. */
  _key(ev) {
    if (!this._wide || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
    const tag = ev.composedPath()[0]?.tagName;
    if (["SELECT", "INPUT", "TEXTAREA", "VIDEO", "HA-HLS-PLAYER"].includes(tag)) return;
    const items = this._list.items;
    if (!items.length) return;
    const at = items.findIndex((item) => item.id === this._selected);
    const next = ev.key === "ArrowDown" ? Math.min(items.length - 1, at + 1) : Math.max(0, at - 1);
    if (next === at) return;
    ev.preventDefault();
    this._select(items[next].id, { focus: true });
  }

  // ---------------------------------------------------------------- header

  _renderHeader() {
    replace(
      this._header,
      this._narrow
        ? el(
            "button",
            {
              class: "icon-btn",
              title: "Menu",
              "aria-label": "Menu",
              onclick: () => this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true })),
            },
            icon("mdi:menu"),
          )
        : null,
      el(
        "div",
        { class: "header__title" },
        el("h1", { class: "header__main", style: { margin: "0" }, text: this._panel?.title || "Events" }),
      ),
      el(
        "nav",
        { class: "tabs", role: "tablist", "aria-label": "View" },
        [
          ["events", "Events"],
          ["cameras", "Cameras"],
        ].map(([view, label]) =>
          el(
            "button",
            {
              class: "tab",
              role: "tab",
              "aria-selected": String(this._view === view),
              onclick: () => {
                if (this._view !== view) this._setRoute({ view });
              },
            },
            label,
          ),
        ),
      ),
    );
  }

  /** At most one banner: the link, then a camera problem, then stale data. */
  _banner() {
    if (!this._link) {
      return el(
        "div",
        { class: "banner", role: "status" },
        icon("mdi:lan-disconnect"),
        el("span", { class: "banner__text", text: "Not connected to Scrypted. New events appear once the link is back." }),
      );
    }
    if (this._list.stale && this._list.updatedAt) {
      return el(
        "div",
        { class: "banner banner--quiet", role: "status" },
        icon("mdi:cloud-refresh-outline"),
        el("span", { class: "banner__text", text: `Last updated ${formatAgo(this._list.updatedAt / 1000)} · retrying` }),
      );
    }
    return null;
  }

  // ---------------------------------------------------------------- events list

  _buildList() {
    this._bannerSlot = el("div");
    // Which events: everything not dropped (default), only the unusual ones, or all, the dropped ones too.
    this._showSelect = el(
      "select",
      {
        class: "chip chip--show",
        "aria-label": "Events",
        onchange: (ev) => this._setFilter({ unusual: ev.target.value === "unusual", dropped: ev.target.value === "all" }),
      },
      el("option", { value: "", text: "Kept" }),
      el("option", { value: "unusual", text: "Unusual" }),
      el("option", { value: "all", text: "All" }),
    );
    this._classSelect = el("select", {
      class: "chip",
      "aria-label": "Kind",
      onchange: (ev) => this._setFilter({ cls: ev.target.value }),
    });
    this._whereSelect = el("select", {
      class: "chip chip--where",
      "aria-label": "Camera or area",
      onchange: (ev) => {
        const [kind, ...rest] = ev.target.value.split(":");
        const value = rest.join(":");
        this._setFilter({ camera: kind === "camera" ? value : "", area: kind === "area" ? value : "" });
      },
    });
    this._clearButton = el("button", { class: "link-btn", onclick: () => this._setFilter(NO_FILTERS) }, "Clear");
    this._filterBar = el(
      "div",
      { class: "filters", role: "toolbar", "aria-label": "Filters" },
      this._showSelect,
      this._classSelect,
      this._whereSelect,
      this._clearButton,
    );
    this._pillSlot = el("div");
    this._listBody = el("div");
    this._sentinel = el("div", { class: "more" });
    this._listPane.append(
      el("div", { class: "page" }, this._bannerSlot, this._filterBar, this._pillSlot, this._listBody),
    );

    this._observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) this._loadMore();
      },
      { root: this._listPane, rootMargin: "400px" },
    );
    this._observer.observe(this._sentinel);
  }

  _filtersSet() {
    const f = this._filters;
    return f.unusual || f.cls !== "" || f.camera !== "" || f.area !== "" || f.dropped;
  }

  _setFilter(change) {
    this._setRoute({ filters: { ...this._filters, ...change }, filtering: true });
  }

  _query() {
    const f = this._filters;
    const query = { type: `${DOMAIN}/events`, unusual: f.unusual, include_dropped: f.dropped, limit: PAGE };
    if (f.cls) query.cls = f.cls;
    if (f.camera) query.camera = f.camera;
    else if (f.area) query.area = f.area;
    return query;
  }

  /** One page with at least one event in it, unless the history ends first. */
  async _fetchPage(cursor, token, query = this._query()) {
    let events = [];
    let next = cursor;
    for (;;) {
      const message = next ? { ...query, cursor: next } : query;
      const result = await this._ws(message);
      if (token !== this._list.token) break;
      this._link = result.link_healthy;
      events = events.concat(result.events);
      next = result.cursor;
      if (events.length || !next) break;
    }
    return { events, cursor: next };
  }

  async _loadList() {
    this._list = this._freshList();
    const list = this._list;
    const token = list.token;
    list.loading = true;
    this._renderList();
    try {
      const result = await this._fetchPage(null, token);
      if (token !== list.token) return;
      Object.assign(list, { items: result.events, cursor: result.cursor, loaded: true, updatedAt: Date.now() });
      if (!result.events.length && !this._filtersSet()) {
        // Nothing kept: say whether anything was seen at all.
        const seen = await this._ws({ ...this._query(), include_dropped: true, limit: 100 });
        if (token !== list.token) return;
        list.hidden = seen.events.length;
        list.hiddenMore = Boolean(seen.cursor);
      }
    } catch (err) {
      if (token !== list.token) return;
      list.error = err;
    }
    list.loading = false;
    this._renderList();
    // A filter change can leave the selection off the list: pick again, beside it.
    if (this._wide && (!this._selected || !list.items.some((item) => item.id === this._selected))) {
      if (this._selected && !this._routeHadEvent) this._select(null, { route: true });
      this._preselect();
    }
    this._routeHadEvent = false;
  }

  async _loadMore() {
    const list = this._list;
    if (!list.loaded || list.loading || list.error || !list.cursor) return;
    if (list.items.length >= MAX_ITEMS) return;
    const token = list.token;
    list.loading = true;
    this._renderList();
    try {
      const result = await this._fetchPage(list.cursor, token);
      if (token !== list.token) return;
      const seen = new Set(list.items.map((item) => item.id));
      list.items = list.items.concat(result.events.filter((item) => !seen.has(item.id)));
      list.cursor = result.cursor;
    } catch (err) {
      if (token !== list.token) return;
      list.error = err;
    }
    list.loading = false;
    this._renderList();
  }

  /** Fold the newest page into what is loaded, keeping everything below it. */
  async _refreshList() {
    const list = this._list;
    if (!list.loaded || list.loading) return;
    const token = list.token;
    let result;
    try {
      result = await this._fetchPage(null, token);
    } catch {
      if (token !== list.token) return;
      list.stale = true;
      clearTimeout(this._retryTimer);
      this._retryTimer = setTimeout(() => this._nudge(), RETRY_MS);
      this._renderList();
      return;
    }
    if (token !== list.token || list.loading) return;
    list.stale = false;
    list.updatedAt = Date.now();
    const fresh = result.events;
    if (!fresh.length && !result.cursor) {
      list.items = [];
      list.cursor = null;
    } else {
      const known = new Set(list.items.map((item) => item.id));
      const top = list.items[0];
      const arrived = top
        ? fresh.filter((item) => !known.has(item.id) && (item.started > top.started || (item.started === top.started && item.id > top.id)))
        : fresh;
      const scrolled = this._listPane.scrollTop > 80;
      if (scrolled && arrived.length && list.items.length) {
        // Reading further down: update in place, and offer the new ones rather than pushing
        // the rows away under the finger.
        const byId = new Map(fresh.map((item) => [item.id, item]));
        list.items = list.items.map((item) => byId.get(item.id) || item);
        list.pending = result;
      } else {
        this._merge(result);
      }
    }
    this._renderList();
  }

  _merge(result) {
    const list = this._list;
    const fresh = result.events;
    list.pending = null;
    if (!result.cursor) {
      list.items = fresh;
      list.cursor = null;
      return;
    }
    const last = fresh.at(-1);
    const older = list.items.filter(
      (item) => item.started < last.started || (item.started === last.started && item.id < last.id),
    );
    list.items = fresh.concat(older);
    if (!older.length) list.cursor = result.cursor;
  }

  _showPending() {
    if (this._list.pending) this._merge(this._list.pending);
    this._listPane.scrollTop = 0;
    this._renderList();
  }

  _syncFilters() {
    const f = this._filters;
    const show = f.unusual ? "unusual" : f.dropped ? "all" : "";
    this._showSelect.value = show;
    this._showSelect.classList.toggle("is-set", show !== "");
    this._showSelect.classList.toggle("is-unusual", show === "unusual");

    const classes = new Set(DEFAULT_CLASSES);
    for (const item of this._list.items) for (const cls of item.classes) classes.add(cls);
    if (f.cls) classes.add(f.cls);
    const sorted = [...classes].sort();
    if (this._classKey !== sorted.join(",")) {
      this._classKey = sorted.join(",");
      replace(
        this._classSelect,
        el("option", { value: "", text: "All kinds" }),
        sorted.map((cls) => el("option", { value: cls, text: className(cls) })),
      );
    }
    this._classSelect.value = f.cls;
    this._classSelect.classList.toggle("is-set", f.cls !== "");

    const cams = this._cams.items;
    const groups = areaGroups([...cams]);
    const whereKey = JSON.stringify(groups.map((group) => [group.area, group.cams.map((cam) => [cam.id, cam.name])]));
    if (this._whereKey !== whereKey) {
      this._whereKey = whereKey;
      // Each area (choosing it chooses all of it), its cameras indented under it. Cameras
      // without an area come last, under a heading that is not itself a choice.
      const indent = "\u2003";
      replace(
        this._whereSelect,
        el("option", { value: "", text: "All cameras" }),
        groups.map((group) => [
          group.area
            ? el("option", { value: `area:${group.area}`, text: group.label })
            : el("option", { disabled: true, text: group.label }),
          group.cams.map((cam) => el("option", { value: `camera:${cam.id}`, text: `${indent}${cam.name}` })),
        ]),
      );
    }
    const where = f.camera ? `camera:${f.camera}` : f.area ? `area:${f.area}` : "";
    // A camera the list has not heard of yet (opened from a link) still shows as chosen.
    if (where && ![...this._whereSelect.options].some((option) => option.value === where)) {
      this._whereSelect.append(el("option", { value: where, text: f.camera || f.area }));
    }
    this._whereSelect.value = where;
    this._whereSelect.classList.toggle("is-set", where !== "");
    this._whereSelect.hidden = cams.length === 0 && !where;
    this._clearButton.hidden = !this._filtersSet();
  }

  _renderList() {
    const list = this._list;
    replace(this._bannerSlot, this._banner());
    this._syncFilters();
    const arrived = list.pending
      ? list.pending.events.filter((item) => !list.items.some((known) => known.id === item.id)).length
      : 0;
    replace(
      this._pillSlot,
      arrived
        ? el("button", { class: "pill", onclick: () => this._showPending() }, icon("mdi:arrow-up", "icon--sm"), `${arrived} new event${arrived === 1 ? "" : "s"}`)
        : null,
    );

    if (!list.items.length) {
      if (list.loading) {
        replace(this._listBody, this._skeletonRows());
      } else if (list.error) {
        replace(
          this._listBody,
          empty(
            "mdi:alert-circle-outline",
            "Events could not be loaded",
            errorText(list.error),
            el("button", { class: "btn", onclick: () => this._loadList() }, "Try again"),
          ),
        );
      } else {
        replace(this._listBody, this._emptyList());
      }
      return;
    }

    const groups = [];
    for (const item of list.items) {
      const key = dayKey(this._hass, item.started);
      if (groups.at(-1)?.key !== key) groups.push({ key, started: item.started, items: [] });
      groups.at(-1).items.push(item);
    }
    const keep = new Set(list.items.map((item) => item.id));
    for (const id of this._rows.keys()) if (!keep.has(id)) this._rows.delete(id);
    for (const id of this._thumbs.keys()) if (!keep.has(id) && id !== this._event?.id) this._thumbs.delete(id);

    const row = (item) => {
      const node = this._row(item);
      const open = item.id === this._selected;
      node.classList.toggle("is-selected", open);
      if (open) node.setAttribute("aria-current", "true");
      else node.removeAttribute("aria-current");
      if (!this._wide) node.setAttribute("aria-expanded", String(open));
      else node.removeAttribute("aria-expanded");
      // On a phone the event opens right under its row.
      return open && !this._wide ? [node, this._detail] : node;
    };
    // On a phone the open event is redrawn with its row: a playing video carries on.
    const playing = this._wide ? [] : this._playing();
    replace(
      this._listBody,
      groups.map((group) => [
        el("h2", { class: "day", text: formatDay(this._hass, group.started) }),
        el("div", { class: "rows" }, group.items.map(row)),
      ]),
      this._listFooter(),
    );
    for (const video of playing) video.play().catch(() => {});
    // The observer only speaks when visibility changes: a short page leaves the footer in
    // view, so look again once this render is on screen.
    requestAnimationFrame(() => {
      const box = this._listPane.getBoundingClientRect();
      const at = this._sentinel.getBoundingClientRect();
      if (this._sentinel.isConnected && at.top < box.bottom + 400) this._loadMore();
    });
  }

  _listFooter() {
    const list = this._list;
    if (list.loading) return replace(this._sentinel, el("div", { class: "spinner", role: "status", "aria-label": "Loading" }));
    if (list.error) {
      return replace(
        this._sentinel,
        el("button", { class: "link-btn", onclick: () => ((list.error = null), this._loadMore()) }, "Could not load more. Try again"),
      );
    }
    if (list.cursor && list.items.length >= MAX_ITEMS) {
      return replace(this._sentinel, `Showing the latest ${MAX_ITEMS} events. Use the filters to find older ones.`);
    }
    if (list.cursor) {
      return replace(this._sentinel, el("button", { class: "link-btn", onclick: () => this._loadMore() }, "Load more"));
    }
    return replace(this._sentinel);
  }

  _emptyList() {
    const f = this._filters;
    const list = this._list;
    const learning = this._cams.items.length > 0 && this._cams.items.every((cam) => !cam.learning.active);
    if (this._filtersSet()) {
      const body =
        f.unusual && learning
          ? "The cameras are still learning what is normal. Nothing is marked unusual until a camera has about a week of events."
          : "Nothing in the history matches these filters.";
      return empty(
        "mdi:filter-variant-remove",
        f.unusual ? "No unusual events" : "Nothing matches",
        body,
        el("button", { class: "btn", onclick: () => this._setFilter(NO_FILTERS) }, "Clear filters"),
      );
    }
    if (list.hidden) {
      const count = list.hiddenMore ? `More than ${list.hidden}` : String(list.hidden);
      return empty(
        "mdi:check-circle-outline",
        `${count} event${list.hidden === 1 && !list.hiddenMore ? " was" : "s were"} seen, but none needed keeping`,
        "Nothing was confirmed and nothing was unusual.",
        el("button", { class: "btn", onclick: () => this._setFilter({ dropped: true }) }, "Show them"),
      );
    }
    return empty(
      "mdi:cctv",
      "No events yet",
      "Events appear here as soon as a camera with the Stamina extension in Scrypted sees something.",
    );
  }

  _skeletonRows() {
    const rows = Array.from({ length: 6 }, () =>
      el(
        "div",
        { class: "row" },
        el("div", { class: "thumb skeleton" }),
        el(
          "div",
          { class: "row__body" },
          el("div", { class: "skeleton", style: { width: "40%", height: "14px" } }),
          el("div", { class: "skeleton", style: { width: "65%", height: "12px", marginTop: "6px" } }),
        ),
      ),
    );
    return [el("div", { class: "day" }, " "), el("div", { class: "rows", "aria-busy": "true", "aria-label": "Loading events" }, rows)];
  }

  /** The first signed thumbnail URL for an event, replaced before it expires. */
  _thumbnail(item) {
    if (!item.thumbnail) return null;
    const known = this._thumbs.get(item.id);
    if (!known || Date.now() - known.at > THUMB_TTL_MS) {
      this._thumbs.set(item.id, { url: item.thumbnail, at: Date.now() });
    }
    return this._thumbs.get(item.id).url;
  }

  _row(item) {
    const thumb = this._thumbnail(item);
    const learning = this._learningText(item.camera);
    const signature = JSON.stringify([
      learning,
      item.state, item.unusual, item.score, item.classes, item.reason, item.duration, item.ended, item.camera_name, item.area, thumb,
    ]);
    const cached = this._rows.get(item.id);
    if (cached?.signature === signature) return cached.node;

    const dropped = notKept(item.state);
    const live = item.ended === null || item.ended === undefined;
    let picture = icon(classIcon(item.classes[0]));
    if (thumb) {
      picture = el("img", { src: thumb, alt: "", loading: "lazy", decoding: "async" });
      const img = picture;
      img.addEventListener("error", () => img.replaceWith(icon(classIcon(item.classes[0]))), { once: true });
    }
    const node = el(
      "a",
      {
        class: `row${item.unusual ? " row--unusual" : ""}${dropped ? " row--dropped" : ""}`,
        href: this._url(`/event/${item.id}`),
        onclick: (ev) => {
          if (ev.button !== 0 || ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.altKey) return;
          ev.preventDefault();
          this._select(item.id);
        },
      },
      el("div", { class: "thumb" }, picture, live ? null : el("span", { class: "thumb__len", text: formatDuration(item.duration) })),
      el(
        "div",
        { class: "row__body" },
        el(
          "div",
          { class: "row__top" },
          el("span", { class: "row__time", text: formatTime(this._hass, item.started) }),
          el("span", { class: "row__camera", text: cameraLabel(item.area, item.camera_name) }),
        ),
        el(
          "div",
          { class: "row__what" },
          el("span", { text: classList(item.classes) }),
          live ? badge("Live", "error", el("span", { class: "live-dot" })) : null,
          dropped ? badge("Not kept", "neutral") : null,
        ),
        // Why, in a line, wherever it is not plain usual: the meter already says "Usual".
        !learning && item.reason && usualness(item.score, item.unusual).tone !== "low"
          ? el("div", { class: `row__why${item.unusual ? "" : " row__why--usual"}`, text: item.reason })
          : null,
      ),
      learning ? learningTag(learning) : meter(item.score, item.unusual),
    );
    this._rows.set(item.id, { signature, node });
    return node;
  }

  // ---------------------------------------------------------------- one event

  _stopVideo() {
    this._stopPlayer?.();
    this._stopPlayer = null;
  }

  _openEvent(id) {
    this._stopVideo();
    // `pressed`: the viewer asked for the video; nothing streams from the recorder before that.
    this._event = { id, data: null, error: null, playingKey: undefined, media: null, choice: null, open: true, pressed: false, tried: new Set() };
    this._renderEvent();
    this._loadEvent(id);
  }

  async _loadEvent(id) {
    try {
      const result = await this._ws({ type: `${DOMAIN}/event`, event_id: id });
      if (this._event?.id !== id) return;
      this._event.data = result.event;
      this._event.error = null;
    } catch (err) {
      if (this._event?.id !== id) return;
      if (!this._event.data) this._event.error = err;
    }
    this._renderEvent();
  }

  _renderEvent() {
    const state = this._event;
    const view = this._detail;
    if (!state) return;
    if (!state.data) {
      const missing = state.error?.code === "not_found";
      replace(
        view,
        state.error
          ? empty(
              missing ? "mdi:calendar-remove-outline" : "mdi:alert-circle-outline",
              missing ? "This event is no longer here" : "The event could not be loaded",
              missing ? "Old events are removed from the history after a while." : errorText(state.error),
            )
          : el(
              "div",
              { class: "page" },
              el(
                "div",
                { class: "event" },
                el("div", { class: "event__media" }, el("div", { class: "player" }, el("div", { class: "spinner", role: "status", "aria-label": "Loading" }))),
                el("div", { class: "event__details" }, el("div", { class: "card skeleton", style: { height: "140px" } })),
              ),
            ),
      );
      return;
    }

    const data = state.data;
    if (!state.media) {
      state.title = el("div", { class: "detail__title" });
      state.box = el("div", { class: "player" });
      // What goes full screen: the video with its control bar over it.
      state.wrap = el("div", { class: "player-wrap" }, state.box);
      state.switch = el("div", { class: "source-switch" });
      state.media = el("div", { class: "event__media" }, state.title, state.wrap, state.switch);
      state.details = el("div", { class: "event__details" });
      replace(view, el("div", { class: "event" }, state.media, state.details));
    }
    replace(
      state.title,
      el("span", { class: "detail__camera", text: cameraLabel(data.area, data.camera_name) }),
      el("span", { class: "detail__what", text: classList(data.classes) }),
      el("span", { class: "dim", text: `${formatDay(this._hass, data.started)}, ${formatTime(this._hass, data.started, true)}` }),
      // While the camera learns this is all there is to say about how unusual it was.
      data.learning ? learningTag(this._learningText(data.camera) || { short: "Learning", long: "Learning" }) : null,
    );
    // The first source in the configured order that has the event: the cloud copy unless
    // the recording comes first (it loads the recorder, and the recorder may have overwritten it).
    if (state.choice === null) {
      const order = data.play?.order || this._playbackOrder || "cloud_first";
      state.choice = order !== "recording_first" && data.play.cloud ? "cloud" : "auto";
    }
    // The recording itself when there is one: `url` plays the cloud copy first by default.
    const url = state.choice === "cloud" ? data.play.cloud : data.play.recorder || data.play.url;
    // `auto` plays the recording wherever the recorder has the event, else the cloud copy.
    const fromCloud = state.choice === "cloud" || !data.play.recorder;
    state.markers = eventMarkers(data, data.play.lead?.[fromCloud ? "cloud" : "recorder"]);
    // Compared without the signature: a refresh re-signs the URL, which is no reason to
    // restart the video.
    if (state.playingKey !== playKey(url)) this._play(url);
    this._renderSourceSwitch();
    replace(state.details, this._summaryCard(data), this._detailsCard(data));
  }

  _play(url) {
    const state = this._event;
    const data = state.data;
    this._stopVideo();
    state.playingKey = playKey(url);
    const stopControls = mountControls(state.wrap, state.box, () => state.markers);
    const stopPlayer = mountPlayer(state.box, {
      hass: this._hass,
      url,
      posterUrl: this._thumbs.get(data.id)?.url || data.thumbnail,
      unavailable: this._unavailable(data),
      play: state.pressed,
      onPlay: () => (state.pressed = true),
      onError: () => {
        // One source failed: the other plays instead, when there is one not tried yet.
        if (this._event !== state) return false;
        state.tried.add(state.choice);
        const next = state.choice === "cloud" ? "auto" : "cloud";
        const other = next === "cloud" ? state.data.play.cloud : state.data.play.recorder;
        if (!other || state.tried.has(next)) return false;
        state.choice = next;
        queueMicrotask(() => this._renderEvent());
        return true;
      },
    });
    this._stopPlayer = () => {
      stopControls();
      stopPlayer();
    };
  }

  _unavailable(data) {
    if (notKept(data.state)) {
      return { title: "No video: this event was not kept", body: "The camera's recorder does not have it either." };
    }
    if (data.ended === null || data.ended === undefined) {
      return {
        title: "Not available yet",
        body: "This camera has no recorder. The cloud copy plays once the event is kept and uploaded.",
      };
    }
    return { title: "No video for this event", body: "The recorder has nothing this old, and there is no finished cloud copy." };
  }

  _renderSourceSwitch() {
    const state = this._event;
    const play = state.data.play;
    const mark = (on, label, title) =>
      el(
        "span",
        { class: `avail${on ? " avail--on" : ""}`, title },
        icon(on ? "mdi:check-circle-outline" : "mdi:close-circle-outline", "icon--sm"),
        label,
      );
    // What the copies say, not only whether the cloud copy plays here: a copy kept in the
    // older layout is stored all the same, it just cannot be streamed from the panel.
    const copies = (state.data.copies || []).filter((copy) => !copy.deleted);
    const stored = Boolean(play.cloud) || copies.some((copy) => copy.committed);
    const uploading = !stored && copies.length > 0;
    const marks = el(
      "span",
      { class: "avails" },
      mark(Boolean(play.recorder), play.recorder ? "Recording" : "No recording"),
      mark(
        stored,
        stored ? "Cloud copy" : uploading ? "Uploading" : "No cloud copy",
        stored && !play.cloud ? "Stored, but this copy cannot be played here" : undefined,
      ),
    );
    // A choice only between two sources that both have the event; the marks are status only.
    if (!(play.recorder && play.url && play.cloud)) {
      replace(state.switch, marks);
      return;
    }
    const toCloud = state.choice !== "cloud";
    replace(
      state.switch,
      marks,
      el(
        "button",
        {
          class: "link-btn link-btn--small",
          onclick: () => {
            state.choice = toCloud ? "cloud" : "auto";
            state.pressed = true;
            state.tried.clear();
            this._renderEvent();
          },
        },
        toCloud ? "Play the cloud copy" : "Play the recording",
      ),
    );
  }

  /**
   * The one card under the player: how unusual, why, and how unusual each signal was. While
   * the camera learns there is no judgement to show (its title says "Learning"), so the card
   * is left out unless something is still being watched.
   */
  _summaryCard(data) {
    const now = Date.now() / 1000;
    const learning = Boolean(data.learning);
    const watching = !data.finalized && data.follow_up_until && data.follow_up_until > now;
    if (learning && !watching) return null;
    // Only the signals that made it rarer come through; whatever is not listed was usual.
    const reasons = learning ? [] : (data.reasons || []).filter((reason) => reason.label);
    return el(
      "section",
      { class: "card", "aria-label": "Verdict" },
      learning ? null : el("div", { class: "verdict" }, meter(data.score, data.unusual, true)),
      data.reason && !learning ? el("p", { class: "sentence", text: data.reason }) : null,
      reasons.length
        ? el(
            "div",
            { class: "reasons" },
            reasons.map((reason) => {
              const words = signalText(reason, this._hass);
              return el(
                "div",
                { class: "reason" },
                el("span", { class: "reason__term", text: words.name }),
                el("span", { class: "reason__label", text: words.value }),
                typeof reason.weight === "number" ? signalMeter(reason.weight) : null,
              );
            }),
            el("p", { class: "reason__rest", text: "Everything else about it was usual for this camera." }),
          )
        : null,
      watching
        ? el(
            "p",
            { class: "note" },
            icon("mdi:eye-outline", "icon--sm"),
            `Still watching ${sensorsText(data.watching)} until ${formatTime(this._hass, data.follow_up_until)}.`,
          )
        : null,
    );
  }

  /** Everything else, folded: what came before, what was seen, what came after, the decision, the copy. */
  _detailsCard(data) {
    const state = this._event;
    const verdict = decision(data);
    const subjects = mergeSubjects(data.subjects || []);
    const early = data.early_evidence || [];
    const late = data.late_evidence || [];
    const notes = (data.notes || []).map((note) => NOTES[note]).filter(Boolean);
    if (data.gaps) notes.push(`${data.gaps} segment${data.gaps === 1 ? " is" : "s are"} missing from the copy.`);

    const seen = subjects.length
      ? el(
          "div",
          { class: "items" },
          subjects.map(({ subject, check }) => {
            const by = joinWords((subject.seen_by || []).map(sourceName));
            const sub = [by ? `Seen by ${by}` : null, subject.dwell ? `for ${formatDuration(subject.dwell)}` : null]
              .filter(Boolean)
              .join(" ");
            return el(
              "div",
              { class: "item" },
              icon(classIcon(subject.class)),
              el(
                "div",
                { class: "item__body" },
                el(
                  "div",
                  { class: "item__head" },
                  el("span", { class: "item__name", text: subject.label || className(subject.class) }),
                  badge(check.text, check.tone),
                ),
                sub ? el("div", { class: "item__sub", text: sub }) : null,
              ),
            );
          }),
        )
      : el("p", { class: "note", text: "Motion only: nothing was recognised." });

    // Sensor changes around the event, as one line each: what changed, and when.
    const evidence = (title, items, when) =>
      items.length
        ? [
            el("h3", { class: "card__title", text: title }),
            el(
              "div",
              { class: "items" },
              items.map((item) =>
                el(
                  "div",
                  { class: "item" },
                  icon("mdi:timer-sand"),
                  el(
                    "div",
                    { class: "item__body" },
                    el("div", { class: "item__name", text: evidenceText(item) }),
                    el("div", {
                      class: "item__sub",
                      // One line per sensor: further changes of the same one are a count.
                      text: [when(item.t_rel_s), item.count > 1 ? `+${item.count - 1} more` : null].filter(Boolean).join(" · "),
                    }),
                  ),
                ),
              ),
            ),
          ]
        : null;

    const copies = (data.copies || []).map((copy) => {
      const status = copy.deleted ? "Removed to free space" : copy.committed ? "Stored" : "Uploading";
      return el(
        "div",
        { class: "item" },
        icon(copy.deleted ? "mdi:cloud-off-outline" : copy.committed ? "mdi:cloud-check-outline" : "mdi:cloud-upload-outline"),
        el("div", { class: "item__body" }, el("div", { class: "item__name", text: copy.destination }), el("div", { class: "item__sub", text: status })),
      );
    });

    const details = el(
      "details",
      { class: "card", ontoggle: (ev) => (state.open = ev.target.open) },
      el("summary", {}, "Details", badge(verdict.label, verdict.tone)),
      el(
        "div",
        { class: "details__body" },
        evidence("Before the event", early, formatBefore),
        el("h3", { class: "card__title", text: "What was seen" }),
        seen,
        evidence("After the event", late, formatAfter),
        el("h3", { class: "card__title", text: "Decision" }),
        verdict.text ? el("p", { class: "sentence", style: { marginTop: "0" }, text: verdict.text }) : null,
        notes.map((note) => el("p", { class: "note" }, icon("mdi:information-outline", "icon--sm"), note)),
        data.cloud_path
          ? [el("div", { class: "item__sub", style: { marginTop: "10px" }, text: "File" }), el("code", { class: "path", text: data.cloud_path })]
          : null,
        copies.length ? el("div", { class: "items", style: { marginTop: "12px" } }, copies) : null,
        el("p", { class: "event-id", text: `Event ${data.id}` }),
      ),
    );
    details.open = state.open;
    return details;
  }

  // ---------------------------------------------------------------- cameras

  async _loadCameras() {
    const cams = this._cams;
    try {
      const result = await this._ws({ type: `${DOMAIN}/cameras` });
      Object.assign(cams, { items: result.cameras, needs: result.learning_needs, loaded: true, error: null });
      // Which source plays first: "cloud_first" (the default) or "recording_first".
      this._playbackOrder = result.playback_order || "cloud_first";
      this._link = result.link_healthy;
    } catch (err) {
      cams.error = err;
    }
    // The list too: a camera that learns shows a tag, not a meter, on its rows.
    this._renderList();
    this._renderCamsPage();
  }

  /** "Learning · day 3 of 7" while a camera learns, else null (ready, or not known yet). */
  _learningText(cameraId) {
    const cam = this._cams.items.find((item) => item.id === cameraId);
    if (!cam || cam.learning?.active !== false) return null;
    const progress = this._learningProgress(cam);
    return { short: `Learning ${progress.short}`, long: `Learning · ${progress.text.toLowerCase()}` };
  }

  /** How far a learning camera is: "Day 3 of 7" until the days are in, then the events. */
  _learningProgress(cam) {
    const needs = this._cams.needs || { days: 7, events: 200 };
    const days = Math.floor(cam.learning?.days || 0);
    const events = cam.learning?.events || 0;
    const progress = Math.min(1, days / needs.days, events / needs.events);
    if (days < needs.days) {
      return { text: `Day ${days + 1} of ${needs.days}`, short: `${days + 1}/${needs.days}`, progress };
    }
    return { text: `${events} of ${needs.events} events`, short: `${events}/${needs.events}`, progress };
  }

  // ---------------------------------------------------------------- the Cameras tab

  /**
   * The last week per camera, from the list itself (newest first): today's kept and unusual
   * events, and every event of the week by hour of the day and class, for the recap.
   */
  async _loadWeek() {
    if (this._weekLoading || (this._today && Date.now() - this._today.at < RECAP_REFRESH_MS)) return;
    this._weekLoading = true;
    const now = Date.now() / 1000;
    const todayKey = dayKey(this._hass, now);
    const since = now - RECAP_DAYS * 86400;
    const counts = new Map();
    const hours = new Map();
    let cursor = null;
    let partial = false;
    try {
      for (let page = 0; ; page++) {
        const message = { type: `${DOMAIN}/events`, include_dropped: true, limit: RECAP_LIMIT };
        const result = await this._ws(cursor ? { ...message, cursor } : message);
        let older = false;
        for (const item of result.events) {
          if (item.started < since) {
            older = true;
            break;
          }
          if (!notKept(item.state) && dayKey(this._hass, item.started) === todayKey) {
            const count = counts.get(item.camera) || { all: 0, unusual: 0 };
            count.all += 1;
            if (item.unusual) count.unusual += 1;
            counts.set(item.camera, count);
          }
          if (!hours.has(item.camera)) hours.set(item.camera, new Map());
          const byClass = hours.get(item.camera);
          const hour = hourOf(this._hass, item.started);
          for (const cls of new Set(item.classes.map((name) => (RECAP_CLASSES.includes(name) ? name : "other")))) {
            if (!byClass.has(cls)) byClass.set(cls, new Array(24).fill(0));
            byClass.get(cls)[hour] += 1;
          }
        }
        cursor = result.cursor;
        if (older || !cursor) break;
        if (page + 1 >= RECAP_PAGES) {
          partial = true;
          break;
        }
      }
      this._today = { counts, hours, partial, at: Date.now() };
    } catch {
      // The counts are a nicety: without them the page still says everything else.
    }
    this._weekLoading = false;
    this._renderCamsPage();
  }

  /**
   * When a camera's events happen: one 24-hour strip per class over the last week, each hour
   * as dark as its share of the camera's busiest hour (as Reolink Stamina's learned view).
   */
  _recap(cam) {
    const byClass = this._today?.hours.get(cam.id);
    if (!this._today) return el("span", { class: "cam-recap cam-recap--empty", text: "…" });
    if (!byClass?.size) {
      return el("span", { class: "cam-recap cam-recap--empty", text: `No events in the last ${RECAP_DAYS} days` });
    }
    const order = [...RECAP_CLASSES, "other"].filter((cls) => byClass.has(cls));
    const busiest = Math.max(1, ...order.flatMap((cls) => byClass.get(cls)));
    const two = (n) => String(n).padStart(2, "0");
    return el(
      "span",
      { class: "cam-recap", "aria-label": `When ${cam.name}'s events happened over the last ${RECAP_DAYS} days` },
      order.map((cls) => {
        const counts = byClass.get(cls);
        const total = counts.reduce((sum, n) => sum + n, 0);
        const name = cls === "other" ? "Other" : className(cls);
        return [
          el("span", { class: "cam-recap__name", text: name }),
          el(
            "span",
            { class: "cam-recap__strip" },
            counts.map((n, hour) =>
              el("span", {
                class: "cam-recap__hour",
                title: `${name}, ${two(hour)}:00–${two((hour + 1) % 24)}:00: ${n} event${n === 1 ? "" : "s"}`,
                style: { opacity: n ? String(0.18 + 0.82 * (n / busiest)) : "" },
                dataset: { empty: n ? "false" : "true" },
              }),
            ),
          ),
          el("span", { class: "cam-recap__total", text: String(total) }),
        ];
      }),
      el("span"),
      el(
        "span",
        { class: "cam-recap__axis", "aria-hidden": "true" },
        ["00", "06", "12", "18"].map((text) => el("span", { text })),
      ),
    );
  }

  /** Every camera at a glance, grouped by area; a row opens its events. */
  _renderCamsPage() {
    if (this._view !== "cameras") return;
    const cams = this._cams.items;
    if (!this._cams.loaded) {
      replace(
        this._camsPage,
        this._cams.error
          ? empty("mdi:alert-circle-outline", "Cameras could not be loaded", errorText(this._cams.error))
          : el("div", { class: "more" }, el("div", { class: "spinner", role: "status", "aria-label": "Loading" })),
      );
      return;
    }
    if (!cams.length) {
      replace(
        this._camsPage,
        empty("mdi:cctv", "No cameras yet", "Cameras appear here once the Stamina extension is on in Scrypted."),
      );
      return;
    }
    const offline = cams.filter((cam) => cam.online === false).length;
    const learning = cams.filter((cam) => cam.learning?.active === false).length;
    const summary = [`${cams.length} camera${cams.length === 1 ? "" : "s"}`];
    if (offline) summary.push(`${offline} offline`);
    if (learning) summary.push(`${learning} learning`);

    const cell = (label, ...content) =>
      el("span", { class: "cam-cell" }, el("span", { class: "cam-cell__label", text: label }), content);
    const state = (tone, text) => [el("span", { class: "dot", dataset: { tone }, "aria-hidden": "true" }), text];

    const row = (cam) => {
      const now = Date.now() / 1000;
      const last = cam.last_event;
      const lastText = !last
        ? "None yet"
        : `${dayKey(this._hass, last.started) === dayKey(this._hass, now) ? formatTime(this._hass, last.started) : formatAgo(last.started)}${last.unusual ? " · unusual" : ""}`;
      const today = this._today?.counts.get(cam.id);
      const todayText = !this._today
        ? "…"
        : `${today?.all || 0}${this._today.partial ? "+" : ""}${today?.unusual ? ` · ${today.unusual} unusual` : ""}`;
      const learn = cam.learning?.active === false ? this._learningProgress(cam) : null;
      const names = joinWords((cam.verifiers || []).map(sourceName)) || "Double-check";
      const check = !cam.verifier_present
        ? state("none", "None")
        : cam.verifier_live === false
          ? state("warn", "Not responding")
          : state("ok", "Working");
      return el(
        "button",
        {
          class: "cam-row",
          title: `Show ${cam.name}'s events`,
          onclick: () => this._setRoute({ view: "events", id: null, filters: { ...NO_FILTERS, camera: cam.id }, filtering: true }),
        },
        el("span", { class: "cam-row__name", text: cam.name }),
        cell("Status", state(cam.online === false ? "error" : cam.online ? "ok" : "none", cam.online === false ? "Offline" : cam.online ? "Online" : "Unknown")),
        cell("Last event", lastText),
        cell("Today", el("span", { class: today?.unusual ? "cam-cell__unusual" : "", text: todayText })),
        cell(
          "Learning",
          learn
            ? [el("span", { class: "mini", "aria-hidden": "true" }, el("span", { style: { width: `${Math.round(learn.progress * 100)}%` } })), learn.text]
            : state("ok", "Ready"),
        ),
        el("span", { class: "cam-cell", title: names }, el("span", { class: "cam-cell__label", text: names }), check),
        this._recap(cam),
      );
    };

    replace(
      this._camsPage,
      el(
        "div",
        { class: "cams-page__inner" },
        el("p", { class: "cams-page__summary", text: summary.join(" · ") }),
        el("p", {
          class: "cams-page__note",
          text: `Under each camera: when each kind was seen over the last ${RECAP_DAYS} days, hour by hour${
            this._today?.partial ? ` (the latest ${RECAP_PAGES * RECAP_LIMIT} events)` : ""
          }. Darker is busier.`,
        }),
        el(
          "div",
          { class: "cam-table", role: "list" },
          el(
            "div",
            { class: "cam-head", "aria-hidden": "true" },
            ["Camera", "Status", "Last event", "Today", "Learning", "Double-check"].map((text) => el("span", { text })),
          ),
          areaGroups([...cams]).map((group) => [
            el("h2", { class: "cam-area", text: group.label }),
            group.cams.map(row),
          ]),
        ),
      ),
    );
  }
}

if (!customElements.get("scrypted-stamina-panel")) {
  customElements.define("scrypted-stamina-panel", ScryptedStaminaPanel);
}
