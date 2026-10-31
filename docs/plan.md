# Implementation plan

Read [architecture.md](architecture.md) first. Each phase ends deployable and useful on its own, and goes through three reviews (adversarial, security, performance) before its release.

## Phase 0: spikes (prove the risky assumptions)

| Spike | Question | Done when |
|---|---|---|
| S1 attribution | Does a camera listener receive `EventDetails.mixinId` / `ObjectsDetected.sourceId` that tells the camera's own AI apart from Coral Guard (and from another detector)? | A log from one live camera showing both sources, correctly attributed |
| S2 segmenter | Can `getVideoStream({ prebuffer })` plus `ffmpeg -c copy -f hls -hls_segment_type fmp4 -hls_time 2` produce playable segments, pre-roll included, in about 1 s from start? | `init.mp4` plus segments that play in Safari and VLC, with the pre-roll present |
| S3 NVR recordings | Can scrypted-reolink implement `VideoRecorder.getRecordingStream({ startTime, duration? })` from the NVR's playback, and `VideoClips` from its search? | A 30 s clip downloaded through the Scrypted interface; a seekable stream starting at an arbitrary time |
| S4 destinations | Which destinations support a file per segment and a folder rename? | A table per provider: put, rename, delete, list |

Phase 0 answers decide the details of phases 2–4. S3 is done in the scrypted-reolink repo.

## Phase 1: events without video (shadow mode)

**Scrypted plugin**
- Provider settings: token, roles table (auto-rows, inferred defaults), class map.
- Camera mixin: observe → normalise → subjects → events. Verdicts follow the fallback policy when there is no verifier.
- `GET /v1/cameras` and the SSE `/v1/events/stream` with `seq` and resume.
- Unit tests for normalisation, fusion (track, IoU, class-and-time), verdicts and episode boundaries.

**HA integration**
- Config flow (URL, token), plus a check that `scrypted_mqtt` is present.
- SSE client with resume, and the index (SQLite, metadata only).
- Entities on the camera devices: `event.<cam>_stamina` (`normal` only for now) and `sensor.<cam>_last_event_score` (empty).

**Exit criteria:** a week of events with subjects and sources that look right on every camera, compared with Coral Guard's own review page.

## Phase 2: fast upload with commit/drop

- Plugin: segmenter, segment routes, `ack`, TTL.
- Integration:
  - fetcher;
  - uploader with the first destination;
  - the decider state machine (PENDING, KEEPING, DROPPING, COMMITTED, ORPHANED);
  - `index.m3u8` and `event.json` on commit;
  - quota and retention;
  - `switch.stamina_uploads`, `sensor.stamina_queue`, `sensor.stamina_quota_used`.
- Fault injection tests:
  - kill the link mid-event: expect ORPHANED, kept;
  - restart the plugin: expect a new epoch and old events orphaned;
  - drop a segment: expect a gap recorded;
  - a veto: expect the pending folder deleted.

**Exit criteria:** on a real camera, the first segment reaches the cloud within 3 s of the first detection, and a vetoed event leaves nothing behind.

## Phase 3: context and scoring

- Linked entities per camera (options flow), and the context snapshot at start and end.
- Port `relevance/` from Reolink Stamina: features, learned normal, score.
- A rules engine: rules defined in options, each hit with a reason.
- Source reliability learned from verdicts.
- `binary_sensor.<cam>_unusual`, and `unusual` events on `event.<cam>_stamina`.

**Exit criteria:** after a learning week, the unusual events match what you'd have picked by hand on a sample.

## Phase 4: review and playback

- Plugin: clips and playback routes (VideoClips and VideoRecorder through HLS).
- Integration: a media source (camera → day → event), with playback from the recorder or else from the cloud.
- Optional full-res upgrade after commit.
- scrypted-reolink: ship S3 as a release (VideoRecorder and VideoClips from the NVR).

**Exit criteria:** any event of the last month can be played from HA, from the NVR when it's reachable and from the cloud when it isn't.

## Phase 5: remaining fallbacks and polish

- "Process later" for cameras without live alerts (scheduled scan of VideoClips or recordings, with a verifier if present).
- Thumbnails (if enabled), the timeline panel (ported from Reolink Stamina's frontend), more destinations.
- Docs, HACS packaging, brand icon.

## Parallel track: bridge reads standard interfaces (scrypted-mqtt-ha)

This is independent of Stamina but follows the same principle:
- `scrypted_mqtt` reads occupancy child devices and raw camera signals through standard interfaces (a tap mixin below the other mixins), keeping today's unique ids;
- the add-on contract stays only for extras such as the Last detection image.

It can run alongside phases 0–1.

## Parallel track: audio guard (new Scrypted plugin)

A Coral Guard-like mixin. It decodes the camera's audio from the prebuffered substream and keeps a rolling noise floor. It sets the standard **AudioSensor** `audioDetected` on a spike well above that floor (silence means no spike). Measure the CPU per camera before rolling it out to all cameras. The Scrypted MQTT bridge shows it as a sound sensor; Stamina uses it as a trigger. Classifying sounds (glass breaking, barking, shouting) comes later.

## Decisions taken

- Destinations: all those Reolink Stamina supports (Synology DSM, WebDAV, SFTP, OneDrive, Google Drive).
- Vetoed but unusual events: kept, in `unusual-unconfirmed/`.
- Thumbnails: yes, 320 px JPEG, about 15 KB each.
- Quota and retention: as in Reolink Stamina (15 GB per destination, oldest first).
- Roles: per source plugin, in the Stamina plugin's settings. No per-camera override.
- No MQTT for Stamina; `scrypted_mqtt` is optional (its camera devices are reused when present).

## Open questions

1. **Day-one rules:** which of these? Person while the alarm is armed; person while nobody is home; person at night; loitering longer than N s; camera offline during an event (always on); a sound spike plus a person.

## Conventions

- Scrypted plugin: TypeScript, `npm test` (node:test), `npm run build`, deployed with `npx scrypted-deploy`.
- HA integration: Python, pytest with `pytest-homeassistant-custom-component`.
- Both are versioned together (one tag per release).
- Public repository: no camera names, addresses or other installation data in code, tests or docs.
- Short commit messages, no AI attribution.
