/**
 * The player's own control bar, with the event marked on its scrub bar.
 *
 * The browser's controls cannot carry markers, so the video plays without them and this bar
 * sits over its bottom edge instead: play/pause, time, mute and full screen, and a scrub bar
 * showing the event as a band, a dot for each thing seen and a tick for each linked sensor
 * that changed within the clip. A press on a marker, or anywhere on the bar, seeks there. It
 * hides while the video plays and the pointer rests, like the browser's own; space plays or
 * pauses and the arrows step 5 s.
 *
 * It finds the <video> in the player box itself (its own, or the one inside <ha-hls-player>),
 * so it works whichever way the player plays HLS.
 *
 * Times come from the event, in epoch seconds, and `lead`: how much video the copy being
 * played has before the start (the recording's lead-in, or the cloud copy's pre-roll). Without
 * a lead nothing can be placed honestly, so the bar shows no markers.
 */

import { el, icon, replace } from "./dom.js";
import { classIcon, className, evidenceText } from "./format.js";

const FIND_MS = 250;
const IDLE_MS = 2500;
const STEP_S = 5;

// Material Design Icons paths, inline so the bar draws without <ha-icon>.
const PATHS = {
  play: "M8,5.14V19.14L19,12.14L8,5.14Z",
  pause: "M14,19H18V5H14M6,19H10V5H6V19Z",
  sound:
    "M14,3.23V5.29C16.89,6.15 19,8.83 19,12C19,15.17 16.89,17.84 14,18.7V20.77C18,19.86 21,16.28 21,12C21,7.72 18,4.14 14,3.23M16.5,12C16.5,10.23 15.5,8.71 14,7.97V16C15.5,15.29 16.5,13.76 16.5,12M3,9V15H7L12,20V4L7,9H3Z",
  muted:
    "M3,9H7L12,4V20L7,15H3V9M16.59,12L14,9.41L15.41,8L18,10.59L20.59,8L22,9.41L19.41,12L22,14.59L20.59,16L18,13.41L15.41,16L14,14.59L16.59,12Z",
  fullscreen: "M5,5H10V7H7V10H5V5M14,5H19V10H17V7H14V5M17,14H19V19H14V17H17V14M10,17V19H5V14H7V17H10Z",
  exit: "M14,14H19V16H16V19H14V14M5,14H10V19H8V16H5V14M8,5H10V10H5V8H8V5M19,8V10H14V5H16V8H19Z",
};

function glyph(name) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", PATHS[name]);
  svg.append(path);
  return svg;
}

/** "0:12", "1:05": a time in the clip. */
export function clipTime(seconds) {
  const s = Math.max(0, Math.round(seconds || 0));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/**
 * Where things happen in the clip, in seconds from its start, or null when the lead of the
 * copy being played is not known. `span` is the event (its end open while it lasts).
 */
export function eventMarkers(data, lead) {
  if (typeof lead !== "number" || !Number.isFinite(lead)) return null;
  const started = data.started;
  const ended = typeof data.ended === "number" ? data.ended : null;
  const marks = [];
  for (const subject of data.subjects || []) {
    if (typeof subject.first_seen !== "number") continue;
    const at = lead + subject.first_seen - started;
    // The same thing from a second source, a moment apart, is one marker.
    if (marks.some((m) => m.cls === subject.class && Math.abs(m.at - at) < 1)) continue;
    marks.push({ kind: "subject", at, cls: subject.class, icon: classIcon(subject.class), label: subject.label || className(subject.class) });
  }
  // Early changes count from the start, late ones from the end.
  for (const item of data.early_evidence || []) {
    if (typeof item.t_rel_s === "number") marks.push({ kind: "sensor", at: lead + item.t_rel_s, label: evidenceText(item) });
  }
  if (ended !== null) {
    for (const item of data.late_evidence || []) {
      if (typeof item.t_rel_s === "number") marks.push({ kind: "sensor", at: lead + ended - started + item.t_rel_s, label: evidenceText(item) });
    }
  }
  return {
    span: [lead, ended === null ? null : lead + ended - started],
    marks: marks.filter((m) => m.at >= 0).sort((a, b) => a.at - b.at),
  };
}

/** The <video> in the player box: its own, or the one inside <ha-hls-player>. */
function videoIn(box) {
  return box.querySelector("video") || box.querySelector("ha-hls-player")?.shadowRoot?.querySelector("video") || null;
}

const pct = (at, duration) => `${Math.min(100, Math.max(0, (at / duration) * 100))}%`;

/**
 * Put the control bar over the player `box`, inside `wrap` (what goes full screen), with the
 * markers `markers()` returns. Returns a function that removes it.
 */
export function mountControls(wrap, box, markers) {
  let video = null;
  let duration = 0;
  let drawn;
  let look = "";
  let idleTimer = null;
  let wasIdle = false;

  const button = (onclick) => el("button", { class: "controls__btn", onclick });
  const playBtn = button(() => toggle());
  const muteBtn = button(() => video && (video.muted = !video.muted));
  const fullBtn = button(() => fullscreen());
  const time = el("span", { class: "controls__time" });
  const fill = el("span", { class: "controls__fill" });
  const knob = el("span", { class: "controls__knob" });
  const band = el("span", { class: "controls__band", hidden: true });
  const marksBox = el("div", { class: "controls__marks" });
  const scrub = el(
    "div",
    { class: "controls__scrub", role: "slider", "aria-label": "Seek", "aria-valuemin": "0" },
    el("span", { class: "controls__rail" }, band, fill),
    knob,
    marksBox,
  );
  const bar = el(
    "div",
    { class: "controls", hidden: true },
    scrub,
    el("div", { class: "controls__row" }, playBtn, time, el("span", { class: "controls__gap" }), muteBtn, fullBtn),
  );
  wrap.append(bar);
  wrap.tabIndex = 0;

  const isFull = () => wrap.getRootNode().fullscreenElement === wrap;
  const seek = (at) => {
    if (!video || !duration) return;
    video.currentTime = Math.min(duration, Math.max(0, at));
    show();
  };
  const toggle = () => {
    if (!video) return;
    if (video.paused) video.play()?.catch?.(() => {});
    else video.pause();
  };
  const fullscreen = () => {
    if (isFull()) document.exitFullscreen?.();
    else if (wrap.requestFullscreen) wrap.requestFullscreen().catch(() => {});
    else video?.webkitEnterFullscreen?.(); // iPhone: only the video itself goes full screen
  };

  // Hidden while playing and the pointer rests; always there while paused.
  const show = () => {
    wrap.classList.remove("is-idle");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => video && !video.paused && wrap.classList.add("is-idle"), IDLE_MS);
  };

  const drawMarks = (current) => {
    drawn = current;
    band.hidden = !current || !duration || current.span[0] >= duration;
    if (!current || !duration) return replace(marksBox);
    const [from, to] = current.span;
    band.style.left = pct(from, duration);
    band.style.width = pct(Math.min(to ?? duration, duration) - from, duration);
    replace(
      marksBox,
      current.marks
        .filter((m) => m.at <= duration)
        .map((m) => {
          const tip = `${m.label} · ${clipTime(m.at)}`;
          const where = m.at / duration;
          const edge = where < 0.12 ? " controls__mark--start" : where > 0.88 ? " controls__mark--end" : "";
          return el(
            "button",
            {
              class: `controls__mark controls__mark--${m.kind}${edge}`,
              style: { left: pct(m.at, duration) },
              "data-tip": tip,
              "aria-label": tip,
              onclick: () => seek(m.at),
              // Not a scrub: the marker's own time, not where the press landed.
              onpointerdown: (ev) => ev.stopPropagation(),
            },
            m.kind === "subject" ? icon(m.icon, "controls__icon") : null,
          );
        }),
    );
  };

  const label = (node, text) => {
    node.setAttribute("aria-label", text);
    node.setAttribute("title", text);
  };
  const update = () => {
    if (!video) return;
    const length = Number.isFinite(video.duration) ? video.duration : 0;
    const current = markers();
    if (length !== duration || current !== drawn) {
      duration = length;
      drawMarks(current);
    }
    const at = video.currentTime || 0;
    fill.style.width = knob.style.left = duration ? pct(at, duration) : "0%";
    scrub.setAttribute("aria-valuemax", String(Math.round(duration)));
    scrub.setAttribute("aria-valuenow", String(Math.round(at)));
    scrub.setAttribute("aria-valuetext", clipTime(at));
    time.textContent = `${clipTime(at)} / ${clipTime(duration)}`;
    const next = `${video.paused}|${video.muted}|${isFull()}`;
    if (next !== look) {
      look = next;
      replace(playBtn, glyph(video.paused ? "play" : "pause"));
      label(playBtn, video.paused ? "Play" : "Pause");
      replace(muteBtn, glyph(video.muted ? "muted" : "sound"));
      label(muteBtn, video.muted ? "Unmute" : "Mute");
      replace(fullBtn, glyph(isFull() ? "exit" : "fullscreen"));
      label(fullBtn, isFull() ? "Leave full screen" : "Full screen");
    }
    if (video.paused) wrap.classList.remove("is-idle");
  };

  const EVENTS = ["timeupdate", "play", "pause", "volumechange", "durationchange", "loadedmetadata", "seeked"];
  const attach = (next) => {
    for (const name of EVENTS) video?.removeEventListener(name, update);
    video = next;
    duration = 0;
    drawn = undefined;
    look = "";
    bar.hidden = !video;
    if (!video) return;
    video.controls = false;
    for (const name of EVENTS) video.addEventListener(name, update);
    update();
    show();
  };
  // The video comes and goes with the player (a source switch, an error message): look for it.
  const find = () => {
    const next = videoIn(box);
    if (next !== video) attach(next);
    else if (video && markers() !== drawn) update();
  };

  // Scrubbing: press and drag along the bar.
  const timeAt = (ev) => {
    const rect = scrub.getBoundingClientRect();
    return rect.width ? ((ev.clientX - rect.left) / rect.width) * duration : 0;
  };
  scrub.addEventListener("pointerdown", (ev) => {
    scrub.setPointerCapture?.(ev.pointerId);
    seek(timeAt(ev));
  });
  scrub.addEventListener("pointermove", (ev) => {
    if (scrub.hasPointerCapture?.(ev.pointerId)) seek(timeAt(ev));
  });

  // A tap on the picture plays or pauses; while the bar is hidden it only brings it back.
  const onBoxClick = () => {
    if (video && !wasIdle) toggle();
  };
  const onPointerDown = () => {
    wasIdle = wrap.classList.contains("is-idle");
    show();
  };
  const onKey = (ev) => {
    if (!video || ev.altKey || ev.ctrlKey || ev.metaKey) return;
    if (ev.key === " " && !(ev.target instanceof HTMLButtonElement)) toggle();
    else if (ev.key === "ArrowLeft") seek(video.currentTime - STEP_S);
    else if (ev.key === "ArrowRight") seek(video.currentTime + STEP_S);
    else return;
    ev.preventDefault();
    show();
  };
  box.addEventListener("click", onBoxClick);
  wrap.addEventListener("keydown", onKey);
  wrap.addEventListener("pointermove", show);
  wrap.addEventListener("pointerdown", onPointerDown);
  document.addEventListener("fullscreenchange", update);

  find();
  const timer = setInterval(find, FIND_MS);
  return () => {
    clearInterval(timer);
    clearTimeout(idleTimer);
    attach(null);
    box.removeEventListener("click", onBoxClick);
    wrap.removeEventListener("keydown", onKey);
    wrap.removeEventListener("pointermove", show);
    wrap.removeEventListener("pointerdown", onPointerDown);
    document.removeEventListener("fullscreenchange", update);
    wrap.classList.remove("is-idle");
    wrap.removeAttribute("tabindex");
    bar.remove();
  };
}
