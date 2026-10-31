/**
 * Scrypted Stamina: the review panel.
 *
 * One page, no navigation between views:
 *
 *   Wide (over WIDE_PX): master-detail. The event list is the main column; the selected event
 *   sits on the right at a capped width: it plays at once, with how unusual it was, the
 *   decision and what was seen beneath it. From ROOMY_PX the cameras get their own narrow
 *   column on the left, grouped by area; below that they fold into one line above the list. On open the latest unusual event is selected (else the latest), so
 *   "find the latest unusual and play it" takes no click; a row click or the arrow keys
 *   change only the right side.
 *   Narrow (a phone): the list only. Tapping a row opens that event inline under it; tapping
 *   it again, or another row, closes it.
 *
 * The camera list says how each camera is doing (learning shows as a small bar) and filters
 * the list to a camera or a whole area. Every row carries a usual-to-unusual meter.
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
  formatDay,
  formatDuration,
  formatTime,
  joinWords,
  notKept,
  sensorsText,
  sourceName,
  termName,
  usualness,
  verdictText,
} from "./format.js";
import { mountPlayer, playKey } from "./player.js";

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
// From this width the cameras get a column of their own, left of the list.
const ROOMY_PX = 1200;
// A camera with no event for this long reads "No recent events".
const QUIET_S = 24 * 3600;

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
    dropped: params.get("dropped") === "1",
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

/** "Area · Camera" where the area is known. */
function cameraLabel(area, name) {
  return area ? `${area} · ${name}` : name;
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
    this._camPane = el("nav", { class: "pane pane--cams", "aria-label": "Cameras", hidden: true });
    this._main = el("main", { class: "split" }, this._camPane, this._listPane, this._detailPane);
    this._root.append(this._header, this._main);

    this._filters = { ...NO_FILTERS };
    this._list = this._freshList();
    this._cams = { items: [], loaded: false, error: null, needs: null };
    this._event = null;
    this._selected = null;
    this._wide = false;
    this._roomy = false;
    this._camsOpen = false;
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
    this._roomy = width >= ROOMY_PX;
    this._placeCams();
    this._subscribe();
    this._loadCameras();
    if (this._list.loaded) this._refreshList();
    this._renderHeader();
    this._routeChanged();
  }

  /** Beside the list, or under its row: decided by the panel's own width. */
  _layout() {
    const width = this.getBoundingClientRect().width;
    if ((width >= ROOMY_PX) !== this._roomy) {
      this._roomy = width >= ROOMY_PX;
      this._placeCams();
    }
    const wide = width >= WIDE_PX;
    if (wide === this._wide) return;
    this._wide = wide;
    this._placeDetail();
    this._renderList();
    if (wide && !this._selected) this._preselect();
  }

  /** The cameras: a column of their own when roomy, else one folding line above the list. */
  _placeCams() {
    this._main.classList.toggle("split--roomy", this._roomy);
    this._camPane.hidden = !this._roomy;
    this._camBox.classList.toggle("is-open", this._roomy || this._camsOpen);
    const home = this._roomy ? this._camPane : this._camSlot;
    if (this._camBox.parentNode !== home) home.append(this._camBox);
  }

  _placeDetail() {
    this._main.classList.toggle("split--wide", this._wide);
    this._detailPane.hidden = !this._wide;
    if (!this._wide) return;
    this._idle ??= empty("mdi:gesture-tap", "Pick an event", "Its video and why it was flagged show here.");
    const target = this._selected ? this._detail : this._idle;
    // Moving a node that holds a playing video pauses it: only place it when it is elsewhere.
    if (this._detailPane.firstChild !== target || this._detailPane.childNodes.length !== 1) {
      replace(this._detailPane, target);
    }
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
    }, wait);
  }

  // ---------------------------------------------------------------- routing

  _search() {
    // Home Assistant's route has no query; the preview passes one in `route.search`.
    return this._route?.search ?? window.location.search;
  }

  _routeChanged() {
    const filtering = this._filtering;
    this._filtering = false;
    if (!this._hass || !this._started) return;
    const path = this._route?.path || "";
    const search = this._search();
    // Home Assistant may hand over the same route again; only a new one changes anything.
    if (`${path}${search}` === this._routeKey) return;
    this._routeKey = `${path}${search}`;

    const filters = filtersFromSearch(search);
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
    if (id !== this._selected) this._select(id, { route: false });
    this._placeDetail();
  }

  _url(path, filters = this._filters) {
    const prefix = this._route?.prefix ?? "/scrypted-stamina";
    return `${prefix}${path}${searchFromFilters(filters)}`;
  }

  /** Write the filters and the selection to the URL, replacing: Back leaves the panel. */
  _setRoute({ filters = this._filters, id = this._selected } = {}) {
    const path = id ? `/event/${id}` : "";
    history.replaceState(history.state, "", this._url(path, filters));
    window.dispatchEvent(new CustomEvent("location-changed", { detail: { replace: true } }));
    // Home Assistant does not always pass a new route when only the query changed.
    if (this._route && this._route.search === undefined) {
      this._route = { ...this._route, path };
    } else if (this._route) {
      this._route = { ...this._route, path, search: searchFromFilters(filters) };
    }
    this._routeChanged();
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
    this._unusualChip = el(
      "button",
      { class: "chip chip--unusual", "aria-pressed": "false", onclick: () => this._setFilter({ unusual: !this._filters.unusual }) },
      icon("mdi:alert-decagram-outline", "icon--sm"),
      "Unusual",
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
    this._droppedChip = el(
      "button",
      { class: "chip is-set", "aria-label": "Stop including events that were not kept", onclick: () => this._setFilter({ dropped: false }) },
      "Including not kept",
      icon("mdi:close", "icon--sm"),
    );
    this._clearButton = el("button", { class: "link-btn", onclick: () => this._setFilter(NO_FILTERS) }, "Clear");
    this._filterBar = el(
      "div",
      { class: "filters", role: "toolbar", "aria-label": "Filters" },
      this._unusualChip,
      this._classSelect,
      this._whereSelect,
      this._droppedChip,
      this._clearButton,
    );
    this._pillSlot = el("div");
    this._camSummary = el("span", { class: "cams__summary" });
    this._camToggle = el(
      "button",
      {
        class: "cams__toggle",
        "aria-expanded": "false",
        onclick: () => {
          this._camsOpen = !this._camsOpen;
          this._camToggle.setAttribute("aria-expanded", String(this._camsOpen));
          this._placeCams();
        },
      },
      icon("mdi:cctv", "icon--sm"),
      el("span", { class: "cams__title", text: "Cameras" }),
      this._camSummary,
    );
    this._camBody = el("div", { class: "cams__body" });
    this._camBox = el("section", { class: "cams", "aria-label": "Cameras" }, this._camToggle, this._camBody);
    this._camSlot = el("div");
    this._listBody = el("div");
    this._sentinel = el("div", { class: "more" });
    this._listPane.append(
      el("div", { class: "page" }, this._bannerSlot, this._camSlot, this._filterBar, this._pillSlot, this._listBody),
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
    this._filtering = true;
    this._setRoute({ filters: { ...this._filters, ...change } });
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
    this._unusualChip.setAttribute("aria-pressed", String(f.unusual));

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
      // One group per area, each with "all of it" first when it has more than one camera.
      replace(
        this._whereSelect,
        el("option", { value: "", text: "All cameras" }),
        groups.map((group) =>
          el(
            "optgroup",
            { label: group.label },
            group.area && group.cams.length > 1
              ? el("option", { value: `area:${group.area}`, text: `All in ${group.area}` })
              : null,
            group.cams.map((cam) => el("option", { value: `camera:${cam.id}`, text: cam.name })),
          ),
        ),
      );
    }
    const where = f.camera ? `camera:${f.camera}` : f.area ? `area:${f.area}` : "";
    // A camera the list has not heard of yet (opened from a link) still shows as chosen.
    if (where && ![...this._whereSelect.options].some((option) => option.value === where)) {
      this._whereSelect.append(el("option", { value: where, text: f.camera || f.area }));
    }
    this._whereSelect.value = where;
    this._whereSelect.classList.toggle("is-set", where !== "");
    this._whereSelect.hidden = cams.length < 2 && !where;
    this._droppedChip.hidden = !f.dropped;
    this._clearButton.hidden = !this._filtersSet();
  }

  _renderList() {
    const list = this._list;
    replace(this._bannerSlot, this._banner());
    this._renderCams();
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
        el("button", { class: "btn", onclick: () => this._setFilter(NO_FILTERS) }, "Show all events"),
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
    const signature = JSON.stringify([
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
        item.unusual && item.reason ? el("div", { class: "row__why", text: item.reason }) : null,
      ),
      meter(item.score, item.unusual),
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
    this._event = { id, data: null, error: null, playingKey: undefined, media: null, choice: "auto", open: true };
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
      state.switch = el("div", { class: "source-switch" });
      state.media = el("div", { class: "event__media" }, state.title, state.box, state.switch);
      state.details = el("div", { class: "event__details" });
      replace(view, el("div", { class: "event" }, state.media, state.details));
    }
    replace(
      state.title,
      el("span", { class: "detail__camera", text: cameraLabel(data.area, data.camera_name) }),
      el("span", { class: "dim", text: `${formatDay(this._hass, data.started)}, ${formatTime(this._hass, data.started, true)}` }),
    );
    const url = state.choice === "cloud" ? data.play.cloud : data.play.url;
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
    this._stopPlayer = mountPlayer(state.box, {
      hass: this._hass,
      url,
      posterUrl: this._thumbs.get(data.id)?.url || data.thumbnail,
      unavailable: this._unavailable(data),
      onError: () => {
        // The recording failed: the cloud copy, when there is one, plays instead.
        if (state.choice === "cloud" || !state.data.play.cloud || this._event !== state) return false;
        state.choice = "cloud";
        queueMicrotask(() => this._renderEvent());
        return true;
      },
    });
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
    const mark = (on, label) =>
      el("span", { class: `avail${on ? " avail--on" : ""}` }, icon(on ? "mdi:check-circle-outline" : "mdi:close-circle-outline", "icon--sm"), label);
    const marks = el(
      "span",
      { class: "avails" },
      mark(Boolean(play.recorder), play.recorder ? "Recording" : "No recording"),
      mark(Boolean(play.cloud), play.cloud ? "Offsite copy" : "No offsite copy"),
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
            this._renderEvent();
          },
        },
        toCloud ? "Play the cloud copy" : "Play the recording",
      ),
    );
  }

  /** The one card under the player: the verdict, and why. */
  _summaryCard(data) {
    const now = Date.now() / 1000;
    const reasons = data.unusual ? (data.reasons || []).filter((reason) => reason.label) : [];
    const top = Math.max(...reasons.map((reason) => reason.weight || 0), 0.01);
    return el(
      "section",
      { class: "card", "aria-label": "Verdict" },
      el(
        "div",
        { class: "verdict" },
        meter(data.score, data.unusual, true),
        el("span", { class: "dim", text: classList(data.classes) }),
      ),
      data.reason ? el("p", { class: "sentence", text: data.reason }) : null,
      reasons.length
        ? el(
            "div",
            { class: "reasons" },
            reasons.map((reason) =>
              el(
                "div",
                { class: "reason" },
                el("span", { class: "reason__term", text: termName(reason) }),
                el("span", { class: "reason__label", text: reason.label }),
                reason.weight
                  ? el("div", { class: "reason__bar", "aria-hidden": "true", style: { width: `${Math.max(4, (reason.weight / top) * 100)}%` } })
                  : null,
              ),
            ),
          )
        : null,
      data.state === "follow_up" ? el("p", { class: "note" }, icon("mdi:timer-sand", "icon--sm"), decision(data).text) : null,
      !data.finalized && data.follow_up_until && data.follow_up_until > now
        ? el(
            "p",
            { class: "note" },
            icon("mdi:eye-outline", "icon--sm"),
            `Still watching ${sensorsText(data.watching)} until ${formatTime(this._hass, data.follow_up_until)}.`,
          )
        : null,
      data.learning
        ? el(
            "p",
            { class: "note" },
            icon("mdi:school-outline", "icon--sm"),
            "This camera is still learning what is normal, so nothing on it is marked unusual yet.",
          )
        : null,
    );
  }

  /** Everything else, folded: what was seen, what came after, the decision, the copy. */
  _detailsCard(data) {
    const state = this._event;
    const verdict = decision(data);
    const subjects = data.subjects || [];
    const late = data.late_evidence || [];
    const notes = (data.notes || []).map((note) => NOTES[note]).filter(Boolean);
    if (data.gaps) notes.push(`${data.gaps} segment${data.gaps === 1 ? " is" : "s are"} missing from the copy.`);

    const seen = subjects.length
      ? el(
          "div",
          { class: "items" },
          subjects.map((subject) => {
            const check = verdictText(subject);
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

    const after = late.length
      ? [
          el("h3", { class: "card__title", text: "After the event" }),
          el(
            "div",
            { class: "items" },
            late.map((item) =>
              el(
                "div",
                { class: "item" },
                icon("mdi:timer-sand"),
                el(
                  "div",
                  { class: "item__body" },
                  el("div", { class: "item__name", text: item.name }),
                  el("div", {
                    class: "item__sub",
                    text: [`${item.from ?? "?"} → ${item.to}`, formatAfter(item.t_rel_s)].filter(Boolean).join(" · "),
                  }),
                ),
              ),
            ),
          ),
        ]
      : null;

    const copies = (data.copies || []).map((copy) => {
      const status = copy.deleted ? "Removed to free space" : copy.committed ? "Stored" : `Uploading, ${copy.files} files so far`;
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
        el("h3", { class: "card__title", text: "What was seen" }),
        seen,
        after,
        el("h3", { class: "card__title", text: "Decision" }),
        verdict.text ? el("p", { class: "sentence", style: { marginTop: "0" }, text: verdict.text }) : null,
        notes.map((note) => el("p", { class: "note" }, icon("mdi:information-outline", "icon--sm"), note)),
        data.cloud_path
          ? [el("div", { class: "item__sub", style: { marginTop: "10px" }, text: "Path in each destination" }), el("code", { class: "path", text: data.cloud_path })]
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
      this._link = result.link_healthy;
    } catch (err) {
      cams.error = err;
    }
    this._renderCams();
    this._syncFilters();
    replace(this._bannerSlot, this._banner());
  }

  /**
   * How one camera is doing: a short word for the list, the full sentence for its tooltip,
   * a tone, and while it is learning how far along it is (0..1).
   */
  _camState(cam) {
    const needs = this._cams.needs || { days: 7, events: 200 };
    if (cam.online === false) return { short: "Offline", text: "Offline", tone: "error" };
    if (cam.verifier_present && cam.verifier_live === false) {
      return { short: "Double-check down", text: "The double-check is not responding", tone: "warn" };
    }
    if (!cam.learning?.active) {
      const days = Math.floor(cam.learning?.days || 0);
      const events = cam.learning?.events || 0;
      const byDays = Math.min(1, days / needs.days);
      const byEvents = Math.min(1, events / needs.events);
      return {
        short: byDays <= byEvents ? `Learning ${days}/${needs.days} d` : `Learning ${events}/${needs.events} ev`,
        text: `Learning what is normal: ${days} of ${needs.days} days, ${events} of ${needs.events} events`,
        tone: "accent",
        progress: Math.min(byDays, byEvents),
      };
    }
    const last = cam.last_event;
    if (!last || Date.now() / 1000 - last.started > QUIET_S) {
      return { short: "Quiet 24 h", text: "No events in the last 24 hours", tone: "neutral" };
    }
    return { short: formatAgo(last.started), text: `Working, last event ${formatAgo(last.started)}`, tone: "ok" };
  }

  /** One line per camera, grouped by area: its state, and a tap filters the list to it. */
  _renderCams() {
    const cams = this._cams.items;
    this._camBox.hidden = cams.length === 0;
    const f = this._filters;
    const states = cams.map((cam) => this._camState(cam));
    const key = JSON.stringify([f.camera, f.area, cams.map((cam, at) => [cam.id, cam.name, cam.area, states[at]])]);
    if (key === this._camKey) return;
    this._camKey = key;

    // Folded, the line says only what needs a look.
    const count = (tone) => states.filter((state) => state.tone === tone).length;
    const parts = [`${cams.length}`];
    if (count("error")) parts.push(`${count("error")} offline`);
    if (count("warn")) parts.push(`${count("warn")} double-check down`);
    if (count("accent")) parts.push(`${count("accent")} learning`);
    if (parts.length === 1) parts.push("all working");
    const worst = ["error", "warn", "accent"].find((tone) => count(tone)) || "ok";
    replace(this._camSummary, el("span", { class: "dot", dataset: { tone: worst }, "aria-hidden": "true" }), parts.join(" · "));

    const row = (cam) => {
      const status = this._camState(cam);
      const on = f.camera === cam.id;
      return el(
        "button",
        {
          class: "cam-chip",
          dataset: { tone: status.tone },
          "aria-pressed": String(on),
          title: `${cam.name}: ${status.text}`,
          onclick: () => this._setFilter({ camera: on ? "" : cam.id, area: "" }),
        },
        el("span", { class: "dot", dataset: { tone: status.tone }, "aria-hidden": "true" }),
        el("span", { class: "cam-chip__name", text: cam.name }),
        el(
          "span",
          { class: "cam-chip__state" },
          status.progress === undefined
            ? null
            : el("span", { class: "mini", "aria-hidden": "true" }, el("span", { style: { width: `${Math.round(status.progress * 100)}%` } })),
          status.short,
        ),
      );
    };
    replace(
      this._camBody,
      el(
        "button",
        {
          class: "cam-all",
          "aria-pressed": String(!f.camera && !f.area),
          onclick: () => this._setFilter({ camera: "", area: "" }),
        },
        "All cameras",
      ),
      areaGroups([...cams]).map((group) => {
        const on = Boolean(group.area) && f.area === group.area && !f.camera;
        return el(
          "div",
          { class: "cam-group", role: "group", "aria-label": group.label },
          group.area
            ? el(
                "button",
                {
                  class: "cam-group__label",
                  "aria-pressed": String(on),
                  title: `Only ${group.area}`,
                  onclick: () => this._setFilter({ area: on ? "" : group.area, camera: "" }),
                },
                group.label,
              )
            : el("span", { class: "cam-group__label", text: group.label }),
          group.cams.map(row),
        );
      }),
    );
  }
}

if (!customElements.get("scrypted-stamina-panel")) {
  customElements.define("scrypted-stamina-panel", ScryptedStaminaPanel);
}
