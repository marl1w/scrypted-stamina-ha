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
 * Muted autoplay with `playsinline`, so a tap on an event plays it at once on a phone too.
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
 * Play `url` in `box`. Returns a function that stops playback and releases the stream.
 *
 * `url` null shows `unavailable` ({title, body}) over the poster instead. When playback
 * fails, `onError()` is called first; if it returns true it has taken over (for example by
 * playing the cloud copy), otherwise the failure is shown.
 */
export function mountPlayer(box, { hass, url, posterUrl, unavailable, onError }) {
  let stopped = false;
  let stop = () => {};
  box.classList.add("player");

  if (!url) {
    message(box, posterUrl, "mdi:video-off-outline", unavailable?.title || "No video", unavailable?.body);
    return () => {};
  }

  const failed = () => {
    if (stopped || onError?.()) return;
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
      controls: true,
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
    stop = () => {
      video.removeEventListener("error", failed);
      video.pause();
      video.removeAttribute("src");
      video.load();
    };
  } else {
    replace(box, poster(posterUrl), el("div", { class: "player__spinner spinner", role: "status", "aria-label": "Loading" }));
    haHlsPlayer().then((ok) => {
      if (stopped) return;
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
      const player = document.createElement("ha-hls-player");
      Object.assign(player, {
        hass,
        url,
        posterUrl,
        controls: true,
        muted: true,
        autoPlay: true,
        playsInline: true,
      });
      player.className = "player__video";
      replace(box, player);
      stop = () => player.remove();
    });
  }

  return () => {
    stopped = true;
    stop();
    box.replaceChildren();
  };
}
