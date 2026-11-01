/**
 * Words and times: everything the panel says, in one place, in plain words.
 *
 * One word per state, used the same way everywhere: Kept, Waiting, Deciding, Not kept,
 * Undecided. The verifier is "the double-check".
 */

const CLASS_ICONS = {
  person: "mdi:account",
  vehicle: "mdi:car",
  animal: "mdi:paw",
  package: "mdi:package-variant-closed",
  face: "mdi:face-recognition",
  plate: "mdi:card-text-outline",
  motion: "mdi:motion-sensor",
};

export const DEFAULT_CLASSES = ["person", "vehicle", "animal"];

export function classIcon(cls) {
  return CLASS_ICONS[cls] || "mdi:shape-outline";
}

export function capitalise(text) {
  const value = String(text || "");
  return value.charAt(0).toUpperCase() + value.slice(1);
}

export function className(cls) {
  return capitalise(cls);
}

export function classList(classes) {
  return (classes || []).map(className).join(", ");
}

/** "@scope/scrypted-coral-guard" → "Coral Guard". */
export function sourceName(source) {
  const bare = String(source || "").replace(/^@[^/]+\//, "").replace(/^scrypted-/, "");
  return bare
    .split(/[-_\s]+/)
    .filter(Boolean)
    .map(capitalise)
    .join(" ");
}

export function joinWords(items) {
  const list = [...items];
  if (list.length < 2) return list.join("");
  return `${list.slice(0, -1).join(", ")} and ${list.at(-1)}`;
}

/** "Confirmed by Coral Guard", "Not confirmed", "Not checked". */
export function verdictText(subject) {
  const by = joinWords((subject.verified_by || []).map(sourceName));
  switch (subject.verdict) {
    case "confirmed":
      return { text: by ? `Confirmed by ${by}` : "Confirmed", tone: "ok" };
    case "vetoed":
      return { text: by ? `Not confirmed by ${by}` : "Not confirmed", tone: "neutral" };
    case "unverified":
      return { text: "Not checked", tone: "warn" };
    default:
      return { text: "Waiting for the double-check", tone: "neutral" };
  }
}

const KEEP_REASONS = {
  confirmed: "something was confirmed",
  unusual: "it was unusual",
  undecided: "the double-check never answered",
  orphaned: "it could not be decided in time",
};

/** Why an event was kept, in words: `classes: person` is a chosen class. */
function keepReason(reason) {
  if (reason && reason.startsWith("classes: ")) {
    const cls = reason.slice(9);
    return `${/^[aeiou]/i.test(cls) ? "an" : "a"} ${cls} was confirmed (a chosen class)`;
  }
  return KEEP_REASONS[reason];
}

/** The decision, as one word and a sentence. */
export function decision(event) {
  const why = keepReason(event.keep_reason);
  switch (event.state) {
    case "committed":
      return { label: "Kept", tone: "ok", text: why ? `Kept because ${why}.` : "Kept." };
    case "keeping":
      return { label: "Kept", tone: "ok", text: `Kept${why ? ` because ${why}` : ""}; still uploading.` };
    case "follow_up":
      return { label: "Waiting", tone: "neutral", text: "Not kept unless something else happens in the next few minutes." };
    case "pending":
      return { label: "Deciding", tone: "neutral", text: "Still happening: waiting for the double-check and for the event to end." };
    case "dropping":
    case "dropped":
      return { label: "Not kept", tone: "neutral", text: "Not kept: no chosen class was confirmed and nothing about it was unusual." };
    case "orphaned":
      return {
        label: "Undecided: connection lost",
        tone: "warn",
        text: "Kept as it was: the connection to Scrypted was lost before it could be decided.",
      };
    default:
      return { label: capitalise(event.state), tone: "neutral", text: "" };
  }
}

export function notKept(state) {
  return state === "dropped" || state === "dropping";
}

export const NOTES = {
  camera_offline: "The camera went offline during the event.",
  dropped_for_follow_up_cap: "Not kept early: the space for waiting events was full.",
};

// ------------------------------------------------------------------ signals in plain words

/** A number from a reading or from one of the model's bands ("3 to 8", "< 3", "18 and up"). */
function reading(value) {
  const text = String(value ?? "").trim();
  let match = text.match(/^(-?[\d.]+) to (-?[\d.]+)$/);
  if (match) return { low: Number(match[1]), high: Number(match[2]), mid: (Number(match[1]) + Number(match[2])) / 2 };
  match = text.match(/^< ?(-?[\d.]+)$/);
  if (match) return { high: Number(match[1]), mid: Number(match[1]) - 1 };
  match = text.match(/^(-?[\d.]+) and up$/);
  if (match) return { low: Number(match[1]), mid: Number(match[1]) + 1 };
  const number = Number(text);
  return text !== "" && Number.isFinite(number) ? { exact: number, mid: number } : null;
}

const round = (n) => String(Math.round(n));

/** "6 s", "3–8 s", "under 3 s", "18 s or more". */
function seconds(value) {
  const r = reading(value);
  if (!r) return humanise(value);
  if (r.exact !== undefined) return `${round(r.exact)} s`;
  if (r.low !== undefined && r.high !== undefined) return `${round(r.low)}–${round(r.high)} s`;
  return r.high !== undefined ? `Under ${round(r.high)} s` : `${round(r.low)} s or more`;
}

/** "on" → "On", "not_home" → "Not home", "partlycloudy" → "Partly cloudy". */
const WORDS = {
  partlycloudy: "partly cloudy",
  "clear-night": "clear night",
  lightning: "thunder",
  "lightning-rainy": "thunderstorm",
  "snowy-rainy": "sleet",
  exceptional: "unusual weather",
  not_home: "away",
  unknown: "unknown",
  unavailable: "unavailable",
};
export function humanise(value) {
  const text = String(value ?? "");
  const known = WORDS[text.toLowerCase()];
  if (known) return capitalise(known);
  const r = reading(text);
  if (r && r.exact === undefined) {
    if (r.low !== undefined && r.high !== undefined) return `${r.low}–${r.high}`;
    return r.high !== undefined ? `Under ${r.high}` : `${r.low} or more`;
  }
  return capitalise(text.replace(/[_-]+/g, " "));
}

const ON_OFF = (on, off, unknown = "Unknown") => (value) =>
  value === "on" ? on : value === "off" ? off : value === "unknown" ? unknown : humanise(value);

const VERDICTS = { confirmed: "Confirmed", vetoed: "Not confirmed", undecided: "No answer", none: "Nothing to check" };
const COUNTS = { 0: "Nothing", 1: "One thing", 2: "Two things", "3+": "Three or more" };

/** Sun elevation in degrees: what a person sees. */
function daylight(value) {
  const r = reading(value);
  if (!r) return humanise(value);
  if (r.mid < 0) return "Dark";
  if (r.mid < 6) return "Dusk or dawn";
  return "Daylight";
}

/** Wind speed in the unit Home Assistant shows: calm to strong. */
function windWords(value, hass) {
  const r = reading(value);
  if (!r) return humanise(value);
  const unit = hass?.config?.unit_system?.wind_speed || "km/h";
  const kmh = unit === "mph" ? r.mid * 1.609 : unit === "m/s" ? r.mid * 3.6 : unit === "kn" ? r.mid * 1.852 : r.mid;
  if (kmh < 6) return "Calm";
  if (kmh < 20) return "Light wind";
  if (kmh < 40) return "Windy";
  return "Strong wind";
}

/** "@scope/scrypted-coral-guard+whole-frame" → "Coral Guard and the whole picture". */
function sources(value) {
  if (!value || value === "none") return "Nothing";
  return joinWords(String(value).split("+").map((source) => (source === "whole-frame" ? "the whole picture" : sourceName(source))));
}

// Each of the model's own signals (relevance/features.py `LABELS`, and `trigger`), by the
// subject the backend names it with: what the row is called, and its value in words.
const SIGNALS = {
  verdicts: ["Double-check", (v) => VERDICTS[v] || humanise(v)],
  "detected by": ["Seen by", sources],
  subjects: ["Things seen", (v) => COUNTS[v] || humanise(v)],
  dwell: ["Stayed", seconds],
  "night mode": ["Night vision", ON_OFF("On", "Off")],
  tamper: ["Tampering", ON_OFF("Yes", "No")],
  sound: ["Sound", ON_OFF("Heard", "Quiet")],
  weather: ["Weather", humanise],
  wind: ["Wind", windWords],
  "sun elevation": ["Light", daylight],
  trigger: ["Started by", humanise],
};

/** "a Sunday" → "Sunday". */
const day = (label) => capitalise(String(label || "").replace(/^an? /, ""));

/** "~8s" → "About 8 s". */
function length(label) {
  if (label === "open") return "Still going";
  if (label === "instant") return "Under a second";
  const match = String(label || "").match(/^~(\d+)s$/);
  return match ? `About ${match[1]} s` : humanise(label);
}

/** "90s after Gate" → "90 s after Gate"; "nothing fired first" → "Nothing just before". */
function sequence(label) {
  const text = String(label || "");
  if (text === "nothing fired first") return "Nothing just before";
  if (text.startsWith("alongside ")) return `Same time as ${text.slice(10)}`;
  return capitalise(text.replace(/^(\d+)s after /, "$1 s after "));
}

const TERMS = {
  kind: ["What", (label) => capitalise(label)],
  clock: ["Time", (label) => label],
  solar: ["Light", (label) => capitalise(label)],
  duration: ["Length", length],
  weekend: ["Day", day],
  predecessor: ["Other cameras", sequence],
};

/**
 * One reason in plain words: what it is called and what it was. Never a raw key or number:
 * anything unknown is at least un-snake-cased. A linked entity keeps its own name.
 */
export function signalText(reason, hass) {
  if (reason.term === "signal") {
    const subject = reason.subject || "";
    const known = SIGNALS[subject.toLowerCase()];
    if (known) return { name: known[0], value: known[1](reason.label, hass) };
    // Early and late evidence arrive as sentences already ("changed within 30 s before").
    return { name: capitalise(subject.replace(/[_.]+/g, " ")), value: humanise(reason.label) };
  }
  const known = TERMS[reason.term];
  if (known) return { name: known[0], value: known[1](reason.label) };
  return { name: humanise(reason.term || ""), value: humanise(reason.label) };
}

// ------------------------------------------------------------------ evidence in plain words

// What a linked sensor did, by its kind and the state it went to.
const CHANGES = {
  door: { on: "opened", off: "closed", open: "opened", closed: "closed" },
  lock: { locked: "locked", unlocked: "unlocked", jammed: "jammed" },
  light: { on: "turned on", off: "turned off" },
  motion: { on: "detected motion", off: "went quiet" },
  presence: { on: "detected someone", off: "cleared", home: "arrived", not_home: "left" },
  vibration: { on: "felt a vibration", off: "went still" },
  sound: { on: "heard a sound", off: "went quiet" },
  alarm: { triggered: "went off", disarmed: "was disarmed", armed_away: "was armed (away)", armed_home: "was armed (home)", armed_night: "was armed (night)" },
};

/** "Front gate opened", "Alarm went off", "Porch light changed to dim". */
export function evidenceText(item) {
  const name = item.name || "A linked sensor";
  const to = String(item.to ?? "");
  const verb = CHANGES[item.kind]?.[to] || (to ? `changed to ${humanise(to).toLowerCase()}` : "changed");
  return `${name} ${verb}`;
}

const KIND_WORDS = { door: "door", presence: "presence", alarm: "alarm", light: "light", motion: "motion" };

/** "door and motion sensors", "the linked sensors". */
export function sensorsText(kinds) {
  const words = [...new Set((kinds || []).map((kind) => KIND_WORDS[kind]).filter(Boolean))];
  return words.length ? `${joinWords(words)} sensors` : "the linked sensors";
}

// ------------------------------------------------------------------ time

function zone(hass) {
  return hass?.locale?.time_zone === "server" ? hass?.config?.time_zone : undefined;
}

function language(hass) {
  return hass?.locale?.language || hass?.language || navigator.language;
}

function formatter(hass, options) {
  try {
    return new Intl.DateTimeFormat(language(hass), { timeZone: zone(hass), ...options });
  } catch {
    return new Intl.DateTimeFormat(undefined, options);
  }
}

export function formatTime(hass, epochS, seconds = false) {
  return formatter(hass, { hour: "2-digit", minute: "2-digit", second: seconds ? "2-digit" : undefined }).format(
    new Date(epochS * 1000),
  );
}

/** A key that is the same for two times on the same local day. */
export function dayKey(hass, epochS) {
  return formatter(hass, { year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(epochS * 1000));
}

export function formatDay(hass, epochS, now = Date.now() / 1000) {
  const key = dayKey(hass, epochS);
  if (key === dayKey(hass, now)) return "Today";
  if (key === dayKey(hass, now - 86400)) return "Yesterday";
  const year = (t) => formatter(hass, { year: "numeric" }).format(new Date(t * 1000));
  const sameYear = year(epochS) === year(now);
  return formatter(hass, { weekday: "long", day: "numeric", month: "long", year: sameYear ? undefined : "numeric" }).format(
    new Date(epochS * 1000),
  );
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.max(0, Math.round(seconds));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}:${String(s % 60).padStart(2, "0")}`;
  return `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function formatAgo(epochS, now = Date.now() / 1000) {
  const s = Math.max(0, now - epochS);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const days = Math.round(s / 86400);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

/** "20 s before", "3 min before", for early evidence: `seconds` is negative, from the start. */
export function formatBefore(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.abs(Math.round(seconds));
  if (s === 0) return "as it started";
  return s < 90 ? `${s} s before` : `${Math.round(s / 60)} min before`;
}

/** "40 s after", "3 min after", for late evidence. */
export function formatAfter(seconds) {
  if (seconds === null || seconds === undefined) return "";
  const s = Math.round(seconds);
  if (s < 0) return "before the end";
  return s < 90 ? `${s} s after` : `${Math.round(s / 60)} min after`;
}

// ------------------------------------------------------------------ how unusual

/**
 * Where an event sits between usual and unusual, as a word, a tone and a fill.
 *
 * `score` is 0..1 with 0.5 at the camera's own line (what `sensor.<cam>_last_event_score`
 * shows), so the bar is comparable between events and cameras. `unusual` is the verdict and
 * wins over the number: only a flagged event is called unusual, so a camera still learning
 * tops out at "Uncommon".
 */
export function usualness(score, unusual) {
  if (typeof score !== "number" || Number.isNaN(score)) {
    return unusual
      ? { label: "Unusual", tone: "unusual", fill: null }
      : { label: "Not scored", tone: "none", fill: null };
  }
  const fill = Math.max(0.04, Math.min(1, score));
  if (unusual) return { label: score >= 0.85 ? "Very unusual" : "Unusual", tone: score >= 0.85 ? "high" : "unusual", fill };
  if (score >= 0.35) return { label: "Uncommon", tone: "mid", fill };
  return { label: "Usual", tone: "low", fill };
}

/**
 * How unusual one signal was, from its share of the score: nats rarer than chance, only ever
 * positive (the common ones are not sent). Half a nat is where the sentence starts naming a
 * signal, so below it a signal reads "Usual".
 */
export function signalLevel(weight) {
  const w = Math.max(0, Number(weight) || 0);
  const fill = Math.max(0.04, Math.min(1, 1 - Math.exp(-w)));
  if (w >= 2) return { label: "Very rare", tone: "high", fill };
  if (w >= 1) return { label: "Rare", tone: "unusual", fill };
  if (w >= 0.5) return { label: "Uncommon", tone: "mid", fill };
  return { label: "Usual", tone: "low", fill };
}
