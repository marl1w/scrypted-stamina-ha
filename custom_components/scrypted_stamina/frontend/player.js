/**
 * The player: one signed HLS URL in, video out.
 *
 * Reolink Stamina's player had to convert FLV from the recorder in the browser. Here the
 * integration already serves HLS (fMP4), recorder first and the cloud copy as fallback, so
 * the player only has to pick a way to play HLS:
 *
 *   1. the browser's own <video> when it plays HLS (Safari, iOS, Android, the Companion app);
 *   2. otherwise Home Assistant's own <ha-hls-player> (hls.js, shipped with the frontend);
 *   3. otherwise it says so, rather than showing a black box.
 *
 * Nothing streams until it is asked for: playing pulls the video from the camera's recorder,
 * so an event opens on its thumbnail with a play button, and only a press (`play: true` once
 * the viewer has pressed it for this event) fetches anything. Then muted autoplay with
 * `playsinline`, so the press is all it takes on a phone too.
 */

import { el, icon, replace } from "./dom.js";

const HLS_TYPE = "application/vnd.apple.mpegurl";

/** What a play URL plays, without its signature: the same key survives a re-sign. */
export function playKey(url) {
  if (!url) return null;
  const [path, query = ""] = url.split("?");
  const kept = query
    .split("&")
    .filter((part) => part && !part.startsWith("exp=") && !part.startsWith("sig="));
  return `${path}?${kept.join("&")}`;
}

function nativeHls() {
  try {
    return document.createElement("video").canPlayType(HLS_TYPE) !== "";
  } catch {
    return false;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * Make sure <ha-hls-player> is defined. Home Assistant loads it lazily with its camera views;
 * building a picture card through the card helpers is what pulls it in.
 */
async function haHlsPlayer() {
  if (customElements.get("ha-hls-player")) return true;
  try {
    const helpers = await window.loadCardHelpers?.();
    helpers?.createCardElement({ type: "picture-glance", entities: [], camera_image: "camera.stamina_preload" });
  } catch {
    // Not inside Home Assistant, or its helpers changed: fall through to the timeout.
  }
  return Promise.race([
    customElements.whenDefined("ha-hls-player").then(() => true),
    sleep(4000).then(() => false),
  ]);
}

function poster(url) {
  if (!url) return null;
  const img = el("img", { class: "player__poster", src: url, alt: "" });
  // A thumbnail that has aged out of the index: no broken image, just the dark box.
  img.addEventListener("error", () => img.remove(), { once: true });
  return img;
}

function message(box, posterUrl, iconName, title, body) {
  replace(
    box,
    poster(posterUrl),
    el(
      "div",
      { class: "player__message", role: "status" },
      icon(iconName, "icon--lg"),
      el("div", { class: "player__title", text: title }),
      body ? el("div", { class: "player__body", text: body }) : null,
    ),
  );
}

/**
/**
 * Stream `url` into `box` at once. Returns a function that stops it; `isStopped()` tells a
 * late callback that the box has moved on.
 */
function stream(box, { hass, url, posterUrl, onError }, isStopped) {
  const failed = () => {
    if (isStopped() || onError?.()) return;
    message(
      box,
      posterUrl,
      "mdi:alert-circle-outline",
      "This video could not be played",
      "The recording may have been overwritten, and the cloud copy may not be ready yet.",
    );
  };

  if (nativeHls()) {
    const video = el("video", {
      class: "player__video",
      controls: false, // controls.js draws its own, with the event on it
      playsinline: true,
      autoplay: true,
      muted: true,
      poster: posterUrl || null,
      props: { muted: true },
    });
    video.addEventListener("error", failed, { once: true });
    video.src = url;
    replace(box, video);
    video.play().catch(() => {});
    return () => {
      video.removeEventListener("error", failed);
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  }

  let player = null;
  replace(box, poster(posterUrl), el("div", { class: "player__spinner spinner", role: "status", "aria-label": "Loading" }));
  haHlsPlayer().then((ok) => {
    if (isStopped()) return;
    if (!ok) {
      message(
        box,
        posterUrl,
        "mdi:web-off",
        "This browser cannot play the video here",
        "Open the event from Home Assistant's media browser, or use the Companion app.",
      );
      return;
    }
    player = document.createElement("ha-hls-player");
    Object.assign(player, {
      hass,
      url,
      posterUrl,
      controls: false, // controls.js draws its own, with the event on it
      muted: true,
      autoPlay: true,
      playsInline: true,
    });
    player.className = "player__video";
    replace(box, player);
  });
  return () => player?.remove();
}

/**
 * Play `url` in `box`. Returns a function that stops playback and releases the stream.
 *
 * `url` null shows `unavailable` ({title, body}) over the poster instead. Without `play` the
 * poster waits under a play button and `onPlay()` is called when it is pressed. When playback
 * fails, `onError()` is called first; if it returns true it has taken over (for example by
 * playing the cloud copy), otherwise the failure is shown.
 */
export function mountPlayer(box, options) {
  const { url, posterUrl, unavailable, play = false, onPlay } = options;
  let stopped = false;
  let stop = () => {};
  box.classList.add("player");

  if (!url) {
    message(box, posterUrl, "mdi:video-off-outline", unavailable?.title || "No video", unavailable?.body);
    return () => {};
  }

  const start = () => {
    if (stopped) return;
    onPlay?.();
    stop = stream(box, options, () => stopped);
  };
  if (play) {
    start();
  } else {
    const cover = posterUrl ? el("img", { class: "player__cover", src: posterUrl, alt: "" }) : null;
    cover?.addEventListener("error", () => cover.remove(), { once: true });
    replace(
      box,
      cover,
      el("button", { class: "player__play", title: "Play", "aria-label": "Play the video", onclick: start }, icon("mdi:play")),
    );
  }

  return () => {
    stopped = true;
    stop();
    box.replaceChildren();
  };
}
