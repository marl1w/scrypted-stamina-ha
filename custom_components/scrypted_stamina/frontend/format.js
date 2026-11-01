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
  if (reason && reason.startsWith("classes: ")) return `a ${reason.slice(9)} was confirmed (a chosen class)`;
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

const TERMS = {
  kind: "What",
  clock: "Time",
  solar: "Daylight",
  duration: "Duration",
  weekend: "Day",
  predecessor: "Sequence",
};

export function termName(reason) {
  if (reason.term === "signal" && reason.subject) return capitalise(reason.subject);
  return TERMS[reason.term] || capitalise(reason.term || "");
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
