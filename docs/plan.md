# Implementation plan

Read [architecture.md](architecture.md) first. Each phase ends deployable and useful on its own, and goes through three reviews (adversarial, security, performance) before its release.

## Phase 0: spikes (prove the risky assumptions)

| Spike | Question | Done when |
|---|---|---|
| S1 attribution | Does a camera listener receive `EventDetails.mixinId` / `ObjectsDetected.sourceId` that tells the camera's own AI apart from a verifier such as Coral Guard (and from another detector)? | A log from one live camera showing both sources, correctly attributed |
| S2 segmenter | Can `getVideoStream({ prebuffer })` plus `ffmpeg -c copy -f hls -hls_segment_type fmp4 -hls_time 2` produce playable segments, pre-roll included, in about 1 s from start? | `init.mp4` plus segments that play in Safari and VLC, with the pre-roll present |
| S3 NVR recordings | Can the camera plugin (e.g. scrypted-reolink) implement `VideoRecorder.getRecordingStream({ startTime, duration? })` from the recorder's playback, and `VideoClips` from its search? | A 30 s clip downloaded through the Scrypted interface; a seekable stream starting at an arbitrary time |
| S4 destinations | Which destinations support a file per segment and a folder rename? | A table per provider: put, rename, delete, list |

Phase 0 answers decide the details of phases 2–4. S3 is done in the camera plugin's repo.

## Phase 1: events without video (shadow mode)

**Scrypted plugin**
- Provider settings: token, roles table (auto-rows, inferred defaults), class map.
- Camera mixin: observe → normalise → subjects → events. Verdicts follow the fallback policy when there is no verifier.
- `GET /v1/cameras` and the SSE `/v1/events/stream` with `seq` and resume.
- Unit tests for normalisation, fusion (track, IoU, class-and-time), verdicts and episode boundaries.

**HA integration**
- Config flow (URL, token), plus a check that a camera-device integration such as scrypted-mqtt-ha is present.
- SSE client with resume, and the index (SQLite, metadata only).
- Entities on the camera devices: `event.<cam>_stamina` (`normal` only for now) and `sensor.<cam>_last_event_score` (empty).

**Exit criteria:** a week of events with subjects and sources that look right on every camera, compared with the verifier's own review page.

## Phase 2: fast upload with commit/drop

- Plugin: segmenter, segment routes, `ack`, TTL.
- Integration:
  - fetcher;
  - uploader with the first destination;
  - the decider state machine (PENDING, KEEPING, DROPPING, COMMITTED, ORPHANED);
  - `index.m3u8` and `event.json` on commit;
  - quota and retention;
  - `select.stamina_uploads`, `sensor.stamina_queue`, `sensor.stamina_quota_used`.
- Fault injection tests:
  - kill the link mid-event: expect ORPHANED, kept;
  - restart the plugin: expect a new epoch and old events orphaned;
  - drop a segment: expect a gap recorded;
  - a veto: expect the pending folder deleted.

**Exit criteria:** on a real camera, the first segment reaches the cloud within 3 s of the first detection, and a vetoed event leaves nothing behind.

## Phase 3: context and scoring

- Linked entities per camera (options flow), and the context snapshot at start and end.
- Port `relevance/` from the earlier prototype: features, learned normal, score, and the predecessor ("what fired before") across cameras. Its pools become together / per HA area / per camera, with per area replacing per recorder.
- Follow-up window (architecture §3.4a): late evidence from linked entities, a provisional score at the end and a final score after the window, the `FOLLOW_UP` decider state with its own pending cap, and a late `unusual` event.
- Day/night: the camera's BinarySensor children (e.g. scrypted-reolink's "Night mode") as features.
- Source reliability learned from verdicts.
- `binary_sensor.<cam>_unusual`, and `unusual` events on `event.<cam>_stamina`.

**Exit criteria:** after a learning week, the unusual events match what you'd have picked by hand on a sample.

## Phase 4: review and playback

- Plugin: clips and playback routes (VideoClips and VideoRecorder through HLS).
- Integration: a media source (camera → day → event), with playback from the recorder or else from the cloud.
- Optional full-res upgrade after commit.
- Camera plugin (e.g. scrypted-reolink): ship S3 as a release (VideoRecorder and VideoClips from the recorder).

**Exit criteria:** any event of the last month can be played from HA, from the camera's recorder when it's reachable and from the cloud when it isn't.

## Phase 4b: panel

- A sidebar panel to review events: timeline per camera, filters (unusual, class, area), a player (camera recording, or else the cloud copy), the evidence for each score, and each camera's capabilities.
- The earlier prototype's frontend is the starting point, not a template: keep what works, cut the rest. The criteria are fewer steps, KISS, and less is more: one obvious action per screen, no settings the user must understand before seeing events, mobile first.
- **A UI/UX review** runs before release, on top of the adversarial, security and performance reviews. It walks the main tasks and flags every extra click, unclear label, empty state and inconsistent pattern:
  - find the latest unusual event and play it;
  - see why it was flagged;
  - check that a camera is working.

  Its findings are fixed before the panel ships.

## Phase 5: remaining fallbacks and polish


- "Process later" for cameras without live alerts (scheduled scan of VideoClips or recordings, with a verifier if present).
- Remaining polish and more destinations.
- Docs, HACS packaging, brand icon.

## Parallel track: camera-device integration reads standard interfaces (e.g. scrypted-mqtt-ha)

This is independent of Stamina but follows the same principle:
- the integration reads occupancy child devices and raw camera signals through standard interfaces (a tap mixin below the other mixins), keeping today's unique ids;
- any contract beyond standard interfaces stays only for extras such as the Last detection image.

It can run alongside phases 0–1.

## Parallel track: verifier tamper (e.g. Coral Guard)

A rolling reference frame per camera, plus a covered/moved/blurred detector, exposed as a BinarySensor child "Tamper".

## Parallel track: audio detector (e.g. repo `marl1w/scrypted-audio-guard`)

A mixin, like a verifier such as Coral Guard. It decodes the camera's audio from the prebuffered substream and keeps a rolling noise floor. It sets the standard **AudioSensor** `audioDetected` on a spike well above that floor (silence means no spike). Measure the CPU per camera before rolling it out to all cameras. A camera-device integration may show it as a sound sensor; Stamina uses it as a trigger. Classifying sounds (glass breaking, barking, shouting) comes later.

## Decisions taken

- Destinations: all those the earlier prototype supports (Synology DSM, WebDAV, SFTP, OneDrive, Google Drive).
- Vetoed but unusual events: kept, in `unusual-unconfirmed/`.
- Thumbnails: yes, 320 px JPEG, about 15 KB each; kept 30 days for kept events, 7 days for dropped ones.
- Model history: 25 months, dropped events included, with a one-year half-life.
- Index tiers: the full record for 30 days, then a slim row.
- History cap: 200 MB by default, met by thinning common dropped events (one in four kept, with a weight), never kept, unusual or rare-context ones.
- To check after the first live week: the real event rate and the row size (fresh and slim), against the cap.
- Quota and retention: as in the earlier prototype (15 GB per destination, oldest first).
- Folder layout: one subfolder per camera HA area under `pending/`, `events/` and `unusual-unconfirmed/`.
- Weather and sun: default context, read in Home Assistant.
- Roles: per source plugin, in the Stamina plugin's settings. No per-camera override.
- No MQTT for Stamina; a camera-device integration such as scrypted-mqtt-ha is optional (its camera devices are reused when present).

- No explicit rules: unusual comes only from the learned model, after about a week of learning.
- Day/night comes from the camera plugin as a standard BinarySensor child device (e.g. "Night mode" in scrypted-reolink), not from the image.

## Parallel track: camera plugin smart events (e.g. scrypted-reolink)

Probe which smart events (line crossing, intrusion, loitering, forgotten or taken item) the installed cameras send through the camera plugin, and expose only those.

## Parallel track: camera plugin "Night mode" (e.g. scrypted-reolink)

Add a BinarySensor child device per camera, driven by the day/night signal that the plugin already receives.

## Conventions

- Scrypted plugin: TypeScript, `npm test` (node:test), `npm run build`, deployed with `npx scrypted-deploy`.
- HA integration: Python, pytest with `pytest-homeassistant-custom-component`.
- Both are versioned together (one tag per release).
- Public repository: no camera names, addresses or other installation data in code, tests or docs.
- Short commit messages, no AI attribution.
