# Architecture

Scrypted Stamina flags unusual camera events and keeps a cloud copy of the ones that matter. It starts uploading at the first detection and keeps the upload only when the event is confirmed. It works with any Scrypted camera and degrades gracefully when parts are missing.

Two packages, one repository:

| Package | Path | Runs in | Owns |
|---|---|---|---|
| **Stamina plugin** (`@marl1w/scrypted-stamina`) | `scrypted-plugin/` | Scrypted | Observations, events, video segments, recording playback. Facts only, no decisions |
| **Stamina integration** (`scrypted_stamina`) | `custom_components/scrypted_stamina/` | Home Assistant | Context, scoring, keep/delete decisions, uploads, the event index, entities, review |

Short version: **Scrypted provides the facts and the video; Home Assistant decides and stores.**

## 1. Where everything lives

```
 CAMERAS ── video, motion, camera AI
    │
┌───▼──────────────────────────── SCRYPTED ───────────────────────────────────┐
│                                                                             │
│  camera plugin (e.g. scrypted-reolink)                                      │
│    VideoCamera, MotionSensor, ObjectDetector (camera AI)                    │
│    VideoRecorder + VideoClips   ← recordings, by time range (standard)      │
│                                                                             │
│  Rebroadcast (@scrypted/prebuffer-mixin)   prebuffer, rebroadcast streams   │
│                                                                             │
│  optional detectors: Coral Guard, OpenVINO, face/plate plugins…             │
│    ObjectDetector events on the camera (standard)                           │
│                                                                             │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ STAMINA PLUGIN (@marl1w/scrypted-stamina)                             │  │
│  │  provider: settings (token, roles table, class map)                   │  │
│  │  mixin per camera:                                                    │  │
│  │    observe  ─► normalise ─► fuse into subjects ─► episode (event)     │  │
│  │    segmenter: prebuffer + live ─► 2 s fMP4 segments (memory)          │  │
│  │  HTTP API (/endpoint/@marl1w/scrypted-stamina/…, bearer token):       │  │
│  │    SSE event stream · segments · capabilities · clips · playback      │  │
│  └───────────────────────────────────┬───────────────────────────────────┘  │
└──────────────────────────────────────┼──────────────────────────────────────┘
                                       │ local HTTP (same host or LAN)
┌──────────────────────────────────────▼──────────── HOME ASSISTANT ──────────┐
│                                                                             │
│  scrypted_mqtt (separate repo): one device per camera                       │
│     ▲ Stamina entities attach to these devices (matched by Scrypted id)     │
│  ┌───────────────────────────────────────────────────────────────────────┐  │
│  │ STAMINA INTEGRATION (scrypted_stamina)                                │  │
│  │  link: SSE client (resume by event id), segment fetcher               │  │
│  │  context: linked HA entities per camera, state at event time          │  │
│  │  scoring: relevance model (learned normal) ─► score, unusual          │  │
│  │  decider: per-event state machine (keep / delete / orphan-keep)       │  │
│  │  uploader: pending/ ─► events/ through HA storage integrations        │  │
│  │  index: SQLite, metadata only                                         │  │
│  │  entities, media source (review), later a panel                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│  storage integrations (Synology DSM, WebDAV, SFTP, OneDrive, Google Drive)  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       ▼
                                  CLOUD / NAS
```

Stamina does not use MQTT. It attaches its entities to the `scrypted_mqtt` camera devices when that integration is present (one device per camera), and creates its own device per camera otherwise. `scrypted_mqtt` is optional, not a dependency.

Why a direct HTTP link rather than MQTT: event facts carry observations, boxes and subjects (larger and richer than an MQTT entity state), and segments and playback are HTTP anyway. One channel, one token. The Scrypted MQTT bridge is not changed and knows nothing about Stamina.

## 2. Scrypted side

### 2.1 Observations

The mixin listens on its camera for:

| Interface | Gives |
|---|---|
| `ObjectDetector` events | detections with class, score, optional box (`inputDimensions`), optional track `id`, optional `label` |
| `MotionSensor` | motion on/off (a classless trigger) |
| `OccupancySensor` (camera and its child devices) | presence on/off, per class where a detector exposes it |

Each event is attributed to a **source plugin**, from `EventDetails.mixinId`, or from `ObjectsDetected.sourceId`, or else the camera's own plugin. It is then normalised:

```json
{ "t": 1730370000123, "source": "scrypted-coral-guard", "role": "verifier",
  "class": "person", "score": 0.87, "box": [0.41, 0.30, 0.55, 0.80],
  "track": "17", "label": null }
```

**Roles** are set in the plugin's settings, one row per source plugin. A row is added the first time that plugin emits a detection, with an inferred default that you can change:

| Default | When |
|---|---|
| trigger | the camera's own plugin; MotionSensor |
| verifier | a mixin that analyses frames (Coral Guard, Scrypted video analysis) |
| enricher | classes `face`, `plate`, or detections with a `label` |
| ignore | chosen by you |

There is no per-camera override. A **class map** per source normalises names (`people`→person, `dog_cat`→animal, …); unknown classes pass through unchanged.

### 2.2 Events (episodes) and subjects

- An event starts at the first trigger, or at the first verifier observation if there is no trigger. It ends a configurable margin (default 10 s) after the last observation, and is capped at 10 min.
- **Subjects** group observations of one physical object inside the event, by:
  1. the same track id;
  2. box overlap (IoU ≥ 0.3 after scaling to the same frame);
  3. the same class family within ±2 s, when one side has no box.

  Enrichers attach to the subject whose box contains theirs.
- **Verdict per subject:**
  - `confirmed`: a verifier saw it;
  - `vetoed`: a verifier ran and did not see it within the verify window, default 5 s;
  - `unverified`: no verifier is present on this camera.

### 2.3 Segmenter

- At event start, the plugin calls `getVideoStream({ prebuffer: preRollMs, destination: 'low-resolution' })` through Rebroadcast. Then `ffmpeg -c copy` writes fMP4 (an `init.mp4` plus 2 s `.m4s` segments) into memory.
- No transcoding. Memory holds about 60 KB/s per active event.
- Segments stay until the integration acknowledges them, or for 10 min (TTL), whichever comes first.
- Without a prebuffer, segments start at the detection. The capability is reported.

### 2.4 Playback and clips (standard interfaces only)

- **`VideoRecorder.getRecordingStream({ startTime, duration? })`:** live seekable playback, served as HLS by the plugin. With a `duration` it returns a downloadable clip, used for the full-resolution "upgrade" after a commit.
- **`VideoClips.getVideoClips({ startTime, endTime })`:** the recorder's own clips. Used for the timeline and for the "process later" fallback.
- A camera without these interfaces has no in-place playback; review then uses committed cloud copies. Nothing is ever copied locally.

### 2.5 HTTP API

All routes are under `/endpoint/@marl1w/scrypted-stamina/` with `Authorization: Bearer <token>`.

| Route | Purpose |
|---|---|
| `GET /v1/cameras` | Cameras with the mixin, and their capabilities: detectors and roles, prebuffer, VideoRecorder, VideoClips |
| `GET /v1/events/stream?since=<seq>` | SSE: `event.start`, `event.update`, `event.end`, `subject.verdict`. Every message has a monotonically increasing `seq`, and a reconnect resumes from it (a ring of the last 1000 messages) |
| `GET /v1/events/<id>/init.mp4`, `/v1/events/<id>/<n>.m4s` | Segments. `404` once expired, `425` if not cut yet |
| `POST /v1/events/<id>/ack {upTo}` | Releases segments the integration holds |
| `GET /v1/cameras/<cam>/clips?start&end` | VideoClips passthrough |
| `GET /v1/cameras/<cam>/playback.m3u8?t=&d=` | Recording playback as HLS |

## 3. Home Assistant side

### 3.1 Link

- One config entry, holding the plugin URL and the token.
- The SSE client reconnects with backoff and resumes from the last `seq`.
- If the link has been lost for more than 2 min, every event still pending becomes `orphaned`, and orphaned events are **kept** (see 3.4).

### 3.2 Context

**Configuration:** the integration's options have one page per camera with an entity picker. It is pre-filled with suggestions from the camera device's area (contacts, alarm panel, presence, lights), and you add or remove entities. Each entity's kind (door, alarm, presence, light, other) comes from its domain and device class, and tells scoring how to read it.

- Per camera device, an options picker of **linked entities** (contacts, alarm panel, presence, lights…).
- At `event.start` and `event.end`, the integration records each linked entity's state and its last change relative to the event (e.g. "door opened 5 s before"). These features feed scoring and are stored with the event.

### 3.3 Scoring

- The **relevance model** is ported from Reolink Stamina's `relevance/`. Its learned "normal" per camera uses hour of week, class, zone, duration and dwell, subject count, verdict mix, and context features. It outputs a rarity score from 0 to 1.
- **No explicit rules:** an event is flagged `unusual` only by the learned model. Flagging starts once a camera has about a week and a few hundred events behind it; until then every event is `normal` and only the confirmed/vetoed decision applies.
- **Source reliability:** precision per camera per source, learned from verifier outcomes and review labels, used as a weight in the model.

### 3.4 Decider: one state machine per event

```
             event.start
                 │
                 ▼
            ┌─────────┐  segments flow to pending/
            │ PENDING │──────────────────────────────┐
            └────┬────┘                              │ link lost > 2 min,
   any subject   │   all subjects vetoed             │ or camera offline mid-event
   confirmed, or │   AND not unusual                 ▼
   unusual       │          │                  ┌──────────┐
       ▼         │          ▼                  │ ORPHANED │─► kept as committed
 ┌──────────┐    │    ┌──────────┐             └──────────┘   (never auto-deleted)
 │ KEEPING  │    │    │ DROPPING │─► delete pending/<id>/ ─► DROPPED (index keeps metadata)
 └────┬─────┘    │    └──────────┘
      │ event.end + post-roll uploaded
      ▼
 ┌───────────┐  manifest written, pending/<id>/ → events/<id>/
 │ COMMITTED │─► optional: full-res upgrade from VideoRecorder (duration = event span)
 └───────────┘
```

No-verifier cameras follow the fallback policy: `unverified` subjects count as confirmed once they last at least N s (default 2) with a score of at least T. An event that is vetoed but unusual is **kept**, in `unusual-unconfirmed/` rather than `events/`.

### 3.4a Follow-up window (late evidence)

Some evidence comes after the camera has stopped seeing anything: a door opened a few minutes after a person was seen, a shock sensor, the alarm actually triggering.

- After `event.end` an event stays open for scoring for a **follow-up window**: default 10 min, set per camera on the same options page as its linked entities.
- Every state change of the camera's linked entities in that window is attached to the event as late evidence (`{entity, kind, from, to, t_rel_s}`).
- **Provisional score** at `event.end`; **final score** when the window closes, from facts, context and late evidence. The relevance model learns what late evidence is normal: a door opened at 6 p.m. with someone home is not unusual; at 3 a.m. with the alarm armed it is.
- If the final score turns the event unusual, `event.<cam>_stamina` fires again as `unusual` with `late: true`.
- **Decider:** an event that is vetoed and ordinary at the end enters `FOLLOW_UP`. Its pending upload is kept until the window closes, then dropped if it is still ordinary, or moved to `unusual-unconfirmed/` if it became unusual. Confirmed or unusual events commit as before; only their `event.json` and index row are updated after the window.
- `FOLLOW_UP` pending data has its own cap (default 1 GB per destination). Over it, the oldest vetoed-and-ordinary events are dropped first.
- The window end time is persisted, so evaluation resumes after a restart.

A later extension: **incidents** link events across cameras in the same or adjacent areas, close in time (gate, then garden, then patio door), so the score sees the whole path.

### 3.5 Uploader

**Destinations** are subentries ("Add destination"), as in Reolink Stamina. Each one picks an account or server from the storage integrations already set up: Synology DSM, WebDAV, SFTP Storage, OneDrive, Google Drive. Each has its own folder, quota (default 15 GB), and retention (oldest evicted first). Every kept event goes to every destination.

- Destinations are ported from Reolink Stamina's `cloud/destinations`. Each must support writing a file per segment; append is not required.
- Layout per event:
  ```
  <folder>/pending/<area>/<YYMMDD_HHMMSS>_<camera>_<id>/init.mp4, 00001.m4s, …
  <folder>/events/<area>/<YYMMDD_HHMMSS>_<camera>_<id>/init.mp4, 0000N.m4s, index.m3u8, event.json
  <folder>/unusual-unconfirmed/<area>/<YYMMDD_HHMMSS>_<camera>_<id>/…   (vetoed but unusual: kept, apart)
  ```
  `<area>` is the camera device's Home Assistant area (slugified), or `no-area` when it has none. `index.m3u8` (HLS with fMP4) plays straight from the folder. `event.json` holds the event facts, the context, the score and the reasons.
- Commit is a folder rename, where the provider supports it; otherwise copy then delete.
- Quota and retention evict the oldest committed events first, never pending or orphaned ones.
- Uploads run in parallel per event, with a bounded queue. The low-res segment is the default, so upstream bandwidth is about 60 KB/s per active event.

### 3.6 Index and review

- SQLite in HA's config directory holds metadata only: event id, camera, times, subjects with sources and verdicts, context, score, reasons, decision state, and cloud path.
- A **thumbnail** per event: a 320 px JPEG (about 15 KB) of the frame at confirmation, or at event start, served by the plugin. It is stored next to the index and kept as long as the index row. At about 300 events a day that is about 5 MB a day.
- A **media source** ("Scrypted events") lists events by camera, then day. Playback resolves to the plugin's recording HLS, or else the cloud `index.m3u8`.
- A timeline panel comes later, porting Reolink Stamina's frontend.

### 3.7 Entities

They attach to each camera's `scrypted_mqtt` device, matched by Scrypted device id.

| Entity | Content |
|---|---|
| `event.<cam>_stamina` | Event types `normal` / `unusual`. Attributes: score, reasons, subjects (class, sources, verdict), decision, cloud path |
| `binary_sensor.<cam>_unusual` | On while an unusual event is open |
| `sensor.<cam>_last_event_score` | Latest score, 0 to 1 |

Integration-wide entities: `switch.stamina_uploads` (arms ordinary uploads; unusual events still upload if that setting is on), `sensor.stamina_queue`, and `sensor.stamina_quota_used`.

## 4. Latency budget

Reolink Stamina waits for the recorder to finish writing a clip (about a minute). Here the upload starts at the first detection.

| Hop | Target | How |
|---|---|---|
| Camera detection → plugin | < 0.5 s | Push events from the camera plugin (Baichuan for Reolink) |
| Plugin → integration (`event.start`) | < 50 ms | SSE push, never polled |
| Pre-roll segments available | at event start | Cut from Rebroadcast's prebuffer, already in memory |
| First segment uploaded | < 3 s from detection, plus uplink time | Fetched at once and uploaded in parallel; 1–2 s segments, copied without re-encoding |
| Verifier verdict | 1–3 s | Doesn't block anything: uploads start before it |

Every hop is timestamped into `event.json`: camera event time, plugin receive, segment cut, HA fetch, upload done. `sensor.stamina_latency` reports p50/p95 time-to-first-segment-uploaded, so the target is measured continuously.

## 5. Data flow of one event

```
t      SCRYPTED (stamina plugin)              HA (stamina integration)             CLOUD
0.0    camera AI "person" (trigger)
       ─ event.start {id, cam, obs} ───────►  index: PENDING; snapshot context
       segmenter: init + pre-roll segments
0.3                                           GET init.mp4, pre-roll .m4s  ──────► pending/<id>/
1.1    Coral Guard "person" 0.87 (verifier)
       ─ subject.verdict confirmed ────────►  decider: KEEPING
2.3 …  segments every 2 s ─────────────────►  fetch + upload, ack  ─────────────► pending/<id>/…
40.0   last observation + 10 s margin
       ─ event.end {subjects, span} ───────►  score; context at end
                                              upload tail; write index.m3u8, event.json
                                              rename pending/<id> → events/<id>  ► events/<id>/
                                              entities: event.<cam>_stamina fires
later                                         optional full-res upgrade via playback ► events/<id>/full.mp4
```

Vetoed path: the same until the verdict. Then `DROPPING` deletes `pending/<id>/` and the index keeps a metadata row (`dropped`).

## 6. Extra signals

- **Day/night (colour or IR):** taken from the camera plugin. scrypted-reolink gets the NVR's day/night push and exposes a child device per camera, "Night mode", with the standard **BinarySensor** interface (on means IR/black-and-white). The Scrypted MQTT bridge shows it as `binary_sensor.<cam>_night_mode`. Stamina reads every BinarySensor child of a camera as a scoring feature, so any camera plugin that exposes a similar child device works too. Cameras without one have no day/night feature.
- **Tamper (covered, moved, blurred):** from Coral Guard, which already decodes the frames. It compares each frame to a rolling reference image and exposes a **BinarySensor** child, "Tamper". Stamina reads it like any other BinarySensor child; a tamper during an event is a strong feature.
- **Camera smart events** (line crossing, intrusion, loitering, forgotten or taken item): only those the installed cameras actually send. They come from the camera plugin as standard ObjectDetector detections with those classes (role trigger), or as BinarySensor children.
- **Weather and sun:** read on the Home Assistant side, from the default `weather.*` entity and `sun.sun`, with no linking needed. They are scoring context: wind and rain explain motion-only events, and the sun's elevation separates dusk from night.
- **Audio:** read through Scrypted's standard **AudioSensor** (`audioDetected`), with role trigger by default. It is produced by the separate audio-guard plugin (plan, parallel track), or by any plugin that implements AudioSensor.

## 6. Capabilities and fallbacks

| Missing | Behaviour |
|---|---|
| Verifier | Subjects are `unverified`. The trigger policy decides (duration and score). The verify window is skipped |
| Camera alerts (no ObjectDetector or motion) | No live events. **Process later**: a scheduled scan of `VideoClips` or the recordings, run through a verifier if one exists. Upload happens after the fact; there is no fast path |
| Prebuffer | No pre-roll; segments start at the detection |
| VideoRecorder / VideoClips | No in-place playback, no full-res upgrade, no process-later. Review uses cloud copies only |
| HA context | Scoring uses video facts only |
| Link to HA | The plugin keeps segments for their TTL; the integration marks open events `orphaned` and keeps them |

Each camera's capabilities are shown as attributes on `event.<cam>_stamina` and in `GET /v1/cameras`.

## 7. Failure and safety rules

- Nothing pending is deleted without an explicit `DROPPING` decision made while the link is healthy.
- A camera going offline during an event keeps the event (safety behaviour, not a rule), with `camera_offline` noted in `event.json`.
- Segments are uploaded in order. A missing segment is retried until its TTL expires, then marked as a gap in `event.json`. Playback tolerates gaps.
- Restarts:
  - **The integration** resumes SSE from its stored `seq`, and events still pending in the index are re-evaluated (an expired event becomes `orphaned`, so it is kept).
  - **The plugin** starts a new event sequence (a new `epoch` in `seq`), so the integration closes the old epoch's open events as `orphaned`.
- Security:
  - token auth on every route, compared in constant time;
  - no credentials or stream URLs in logs or in `event.json`;
  - box and score values clamped;
  - event ids are opaque random strings.
