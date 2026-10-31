# Stamina plugin HTTP API (v1)

The contract between the Scrypted plugin (`@marl1w/scrypted-stamina`) and the Home Assistant integration (`scrypted_stamina`). Plugin version 0.1.1, API version 1. (0.1.1 changes only the plugin's settings page; the API is unchanged.)

## Changes after review (still 0.1.0, API v1)

Integration code written against the first draft must align with these:

1. **`event.segment` has a `kind`**: `cut` (a segment, as before), `evicted` (segments dropped for memory: `evicted: [n…]`, `segment: null`) and `last` (no more segments for this event: `last: true`, `segment: null`, final `next`, `state`, `reason`). Every message has `last` (bool). The `last` message comes for every event, including events without a segmenter (after `event.end`) and capped events whose segmenter moved to the continuation.
2. **`segment.init` is now a number** (the init the segment needs: `0` = `init.mp4`, `k` = `init-<k>.mp4`), not a boolean. New `segment.discontinuity` (bool): `true` on the first segment after ffmpeg was restarted mid-event (new init; add `#EXT-X-DISCONTINUITY` and a new `#EXT-X-MAP` before it).
3. **New route `GET /v1/events/<id>/init-<k>.mp4`** (k ≥ 1) for those later inits.
4. **New route `GET /v1/events?since=<ms>[&camera=<id>]`**: recent events with their segment state, to recover after a `gap` or `reset`.
5. **`segments` block** (in the `event` object): new `inits`, `restarts`; new `state` `released` (`reason` `not_confirmed` or `preempted`); `reason` `continued` for a continuation that took over the capped event's segmenter. Segment `info` (`GET /v1/events/<id>`, ack replies) has a new `inits` count.
6. **Verdicts**: a subject whose window ends while the verifier shows no sign of life (no activity on this camera or any camera for 10 min) is `unverified`, not `vetoed`. `capabilities.verifier_live` is new.
7. **Who confirms**: only a verifier observation with a concrete class (person, vehicle, animal, …) confirms. A classless verifier signal (motion, occupancy) confirms only for sources flagged **gated** in the roles table (Coral Guard by default). A verifier observation of the same family joins a `pending` subject within the whole verify window (not only ±2 s).
8. **`continues`** is also set when an event opens within the margin after a capped one (not only when a signal was held).
9. **Resume without `epoch`** (`since` alone) is a `reset` (SSE and poll).
10. **`ack`** is clamped to the last segment cut: acking ahead releases nothing that is not cut yet.
11. **Token regeneration** ends SSE streams, pending polls (they answer `401`) and playback sessions.
12. **Playback**: a session with `d` lives at least `d` + 5 min; when its memory is full of segments not fetched yet, reading pauses (nothing unfetched is dropped).
13. `GET /v1/health` has `ring_bytes`; `segmenters` includes segmenters waiting to hand over to a continuation.

## Changes in 0.1.1 (API v1)

23. **Split cameras** (a camera whose frame is split into views; see [Split cameras](#split-cameras)):
    - `capabilities` has `split` (bool), `views` (ids of its crop cameras) and `parent` (the parent camera's id for a crop, else `null`);
    - observations have `ambiguous` (bool), and a subject's `sources[<plugin>]` has `ambiguous: true` when that source's observations were the whole-frame AI;
    - a mirror camera (nativeId containing `-mirror-`) is never observed: it is not listed in `GET /v1/cameras` and has no events.
24. The settings page changed (one role per source, class maps as list rows); the API did not.

## Changes in the final pass (still 0.1.0, API v1)

14. **`kind: last` only after the event closed.** While an event is open, a `released` (or `busy`) segmenter can start again: `segments.state` goes back to `running` with `reason: "resumed"`, after a late `confirmed` verdict or when a slot frees. Its first segment has `discontinuity: true` and a new `init` index when the event had an init already; without one, it starts at `init.mp4`. The pre-roll is requested only if the event began less than the pre-roll ago. Treat `released` as provisional until `kind: last`.
15. **`kind: evicted`** is announced at most once a second per event (the numbers are batched), and also when the 200-event bound drops an entry that still held segments.
16. **Gated signals:** a classless gated signal confirms only the single `pending` subject. With none or several pending, it confirms its own motion/presence subject (`class` `motion` or `presence`). It never flips a `vetoed` subject.
17. **Verifier presence and liveness** are per camera: a verifier counts only while its plugin is still a mixin of the camera, and `verifier_live` means activity on this camera within 10 min (activity on other cameras no longer counts).
18. **Slots:** an event worth its video (any confirmed subject, or an unverified one with a class, here or in the capped event it continues) is never preempted or released. A motion-only `unverified` event can be preempted, and only after its segmenter has run 10 s.
19. **Ring:** 5000 messages and 8 MB (UTF-8): the byte cap is the real bound.
20. **Playback:**
    - the playlist has `#EXT-X-START:TIME-OFFSET=0`, and `#EXT-X-PLAYLIST-TYPE:EVENT` while nothing has left it;
    - over its memory, segments leave from the head: fetched ones, and unfetched ones numbered below the lowest fetched one;
    - only `init.mp4` and segment fetches count as activity (playlist polls do not);
    - a session paused for 2 min (a player that stopped fetching) stops.
21. **Long polls:** at most 8 wait at once; a 9th gets `429 too_many_requests` with `Retry-After: 1`. Every reply has `X-Content-Type-Options: nosniff`.
22. **The thumbnail:** an open event's store entry (and so its thumbnail and inits) is never dropped by the TTL sweep while the event is open. A start picture still queued after 5 s, or after the event closed, is not taken.

## Base URL and authentication

```
http://<scrypted-host>:11080/endpoint/@marl1w/scrypted-stamina/public/v1/...
https://<scrypted-host>:10443/endpoint/@marl1w/scrypted-stamina/public/v1/...
```

- Use the **`/public/`** path. Scrypted's non-public endpoint path requires a Scrypted login before the request reaches the plugin; the plugin checks its own token instead. The plugin's settings page shows the exact base URL (`Integration URL`).
- Every request carries `Authorization: Bearer <token>`. The token is generated on the plugin's first start, shown in its settings, and can be regenerated (the old one stops working at once).
- The token is compared in constant time. A missing or wrong token gets `401 {"error":"unauthorized"}`.
- The only routes that accept no token are the playback session files (see [Playback](#playback)), whose unguessable session id is the credential.
- Every response has `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. Errors are JSON: `{"error": "<code>", "message": "<optional text>"}`.

| Status | `error` | When |
|---|---|---|
| 400 | `bad_request` | invalid query or body |
| 401 | `unauthorized` | no or wrong token |
| 404 | `not_found` | unknown route, camera, event or segment; also an expired, acked or evicted segment |
| 404 | `not_supported` | the camera lacks the interface (VideoClips, VideoRecorder) |
| 405 | `method_not_allowed` | |
| 425 | `not_yet_available` | the segment, init or thumbnail is not cut yet; `Retry-After: 1` |
| 429 | `too_many_requests` | more than 8 long polls waiting; `Retry-After: 1` |
| 500 | `internal_error` | |
| 502 | `playback_failed` | the recording stream produced no video |

Identifiers are validated strictly: event ids match `^ev_[A-Za-z0-9_-]{22}$`, camera ids (Scrypted device ids) `^[A-Za-z0-9_-]{1,32}$`, playback session ids `^pb_[A-Za-z0-9_-]{22}$`, segment numbers `^\d{1,7}$`. Anything else is a `404`.

Times are Unix epoch **milliseconds** unless the field name says otherwise (`*_s` are seconds). Boxes are `[x1, y1, x2, y2]` as fractions of the frame (0..1).

## Routes

| Method and route | Purpose |
|---|---|
| `GET /v1/health` | Liveness and counters |
| `GET /v1/cameras` | Cameras with the mixin, their capabilities and sources |
| `GET /v1/events/stream?since=<seq>&epoch=<epoch>` | SSE event stream, resumable |
| `GET /v1/events/poll?since=<seq>&epoch=<epoch>&wait=<s>` | Long-poll fallback for the same messages |
| `GET /v1/events?since=<ms>&camera=<id>` | Recent events with their segment state (recovery after a gap) |
| `GET /v1/events/<id>` | One event's facts and segment state |
| `GET /v1/events/<id>/init.mp4`, `init-<k>.mp4` | fMP4 init segment (`k` ≥ 1 after an ffmpeg restart) |
| `GET /v1/events/<id>/<n>.m4s` | fMP4 media segment `n` (0-based) |
| `GET /v1/events/<id>/thumbnail.jpg` | 320 px JPEG |
| `POST /v1/events/<id>/ack` | Releases segments the integration holds |
| `GET /v1/cameras/<cam>/clips?start=<ms>&end=<ms>` | VideoClips passthrough |
| `GET /v1/cameras/<cam>/playback.m3u8?t=<ms>&d=<s>` | Recording playback as HLS (redirects to a session) |
| `GET /v1/cameras/<cam>/playback/<sid>/index.m3u8`, `init.mp4`, `<n>.m4s` | Playback session files |
| `DELETE /v1/cameras/<cam>/playback/<sid>` | Stops a playback session early |

### `GET /v1/health`

```json
{
  "ok": true,
  "api": 1,
  "version": "0.1.1",
  "epoch": "ep_Xb3kQ9vT0aLm",
  "last_seq": 1234,
  "uptime_s": 3600,
  "cameras": 12,
  "open_events": 1,
  "segment_bytes": 2400000,
  "segment_bytes_max": 67108864,
  "segmenters": 1,
  "ring_bytes": 120000,
  "playback_sessions": 0,
  "sse_clients": 1
}
```

### `GET /v1/cameras`

```json
{
  "cameras": [
    {
      "id": "101",
      "name": "Front door",
      "plugin": "@marl1w/scrypted-reolink",
      "online": true,
      "open_event": null,
      "capabilities": {
        "object_detector": true,
        "motion_sensor": true,
        "occupancy_sensor": true,
        "audio_sensor": false,
        "snapshot": true,
        "prebuffer": true,
        "prebuffer_ms": 10000,
        "video_recorder": false,
        "video_clips": false,
        "audio": true,
        "verifier_present": true,
        "verifier_live": true,
        "verifiers": ["scrypted-coral-guard"],
        "silent_verifiers": [],
        "split": false,
        "views": [],
        "parent": null
      },
      "sources": [
        { "source": "@marl1w/scrypted-reolink", "role": "trigger", "inferred": true, "last_seen": 1730370000123 },
        { "source": "scrypted-coral-guard", "role": "verifier", "inferred": true, "last_seen": 1730370001200 }
      ],
      "context_sensors": [
        { "id": "102", "name": "Front door Night mode", "kind": "binary", "feature": "night_mode", "on": false }
      ]
    }
  ]
}
```

- `prebuffer`: a stream of the camera is prebuffered by Rebroadcast; `prebuffer_ms` is the longest one. Without it, segments start at the detection. `prebuffer`, `prebuffer_ms` and `audio` are `null` until the plugin has read the camera's stream options (in the background on the first request or event, then every 5 min).
- `verifiers`: verifier sources that have emitted a detection on this camera in the last 7 days (classless signals do not count) and are still among its mixins. `verifier_present` is `verifiers.length > 0`. `verifier_live`: one of them showed activity (any detection or signal) on this camera within 10 min; when it is `false`, subjects the verifier does not confirm end `unverified` instead of `vetoed`. A verifier mixin that is enabled on the camera but has never emitted (for example Coral Guard in Shadow mode, which publishes nothing) is listed in `silent_verifiers` and does **not** count: otherwise every subject would be vetoed.
- `sources`: the plugin's roles table rows for the sources seen on this camera (`last_seen` on this camera). Roles are global (no per-camera override).
- `context_sensors`: BinarySensor and OccupancySensor child devices of the camera. `feature` is `night_mode` (name contains "night"), `tamper` (name contains "tamper"), or `null`.

### `GET /v1/events/stream` (SSE)

`Content-Type: text/event-stream`. Every message:

```
id: <epoch>:<seq>
event: <type>
data: <JSON on one line>

```

The JSON envelope of every message:

```json
{ "v": 1, "seq": 42, "epoch": "ep_Xb3kQ9vT0aLm", "type": "event.start", "ts": 1730370000150, "...": "type-specific fields" }
```

- `seq` increases by 1 per message within an `epoch`. A new `epoch` starts at every plugin start.
- **Resume:** reconnect with `?since=<last seq>&epoch=<epoch>`, or the standard `Last-Event-ID: <epoch>:<seq>` header (it wins over the query). The plugin replays every message after `since` from a ring of the last 5000 messages and at most 8 MB (the byte cap is usually the bound), then continues live. After a `gap` or `reset`, call `GET /v1/events?since=<ms>` to recover the state of events you missed.
- Messages are serialised when published: a replayed message is exactly what was sent then (it does not reflect later changes).
- The first frame of every connection is a `hello` (it has no `seq` and is not in the ring; do **not** store anything from it as your resume point):

  ```
  event: hello
  data: {"v":1,"type":"hello","epoch":"ep_Xb3kQ9vT0aLm","last_seq":1234,"oldest_seq":235,"resumed":true,"gap":false,"reset":false}
  ```

  - `resumed`: the replay is complete from `since`.
  - `gap`: `since` is older than the ring; the replay starts at `oldest_seq` and messages in between are lost.
  - `reset`: `epoch` is not the current one (the plugin restarted), or `since` came without `epoch`. The whole ring is replayed. Every event of the old epoch that is still open on your side will never get its `event.end`: mark it `orphaned`.
  - No `since` and no `epoch`: live only, no replay.
- Keepalive: a comment line `: keepalive` every 15 s without traffic.
- A stream ends after 1 h, when the client falls 2000 messages behind, or when a 5th client connects (the oldest of 4 is closed). Reconnect and resume.

Message types:

| `type` | When | Fields |
|---|---|---|
| `event.start` | first trigger (or verifier) observation of a camera with no open event | `event`, `observations` |
| `event.update` | new observations, context changes, segment state changes; at most 1 per second per event | `event`, `observations` (only the new ones, at most 50) |
| `subject.verdict` | a subject's verdict is decided or changes | `event_id`, `camera_id`, `subject` |
| `event.segment` | `kind: cut`: a media segment was cut (the first one also means its init is ready); `kind: evicted`: segments were dropped for memory; `kind: last`: no more segments for this event | `event_id`, `camera_id`, `kind`, `last`, `segment` (or `null`), `evicted`, `next`, `state`, `reason` |
| `event.end` | the event closed | `event` |

Order guarantees: `event.start` comes before any other message of that event, and `event.end` after all of them except `event.segment` (the segmenter flushes its last segment up to 3 s after the end, then sends `kind: last`). Every `pending` subject gets its final verdict (`vetoed` or `unverified`) before `event.end`.

#### `event` object

```json
{
  "id": "ev_q2Jx8sK4mZb1VtR6yN0aLw",
  "camera": { "id": "101", "name": "Front door" },
  "state": "open",
  "start": 1730370000123,
  "end": null,
  "last_observation": 1730370001100,
  "end_reason": null,
  "continues": null,
  "trigger": { "source": "@marl1w/scrypted-reolink", "role": "trigger", "kind": "object", "class": "person" },
  "subjects": [ { "...": "subject object" } ],
  "enrichments": [],
  "observations": 3,
  "sources": { "@marl1w/scrypted-reolink": 2, "scrypted-coral-guard": 1 },
  "verifier_present": true,
  "flags": { "camera_offline": false, "capped": false, "subjects_overflow": false },
  "latency": {
    "camera_event_time": 1730370000123,
    "plugin_receive": 1730370000310,
    "event_start_sent": 1730370000311,
    "stream_requested": 1730370000312,
    "stream_resolved": 1730370000420,
    "first_byte": 1730370000510,
    "init_cut": 1730370000512,
    "first_segment_cut": 1730370000515,
    "first_live_segment_cut": 1730370002330,
    "thumbnail_at": 1730370000900
  },
  "context": {
    "at_start": { "night_mode": false, "tamper": null, "motion": true, "occupied": false, "audio": null, "online": true },
    "current": { "night_mode": false, "tamper": null, "motion": true, "occupied": true, "audio": null, "online": true },
    "changes": [ { "t": 1730370001000, "feature": "occupied", "id": "101", "name": "Front door", "on": true } ]
  },
  "segments": {
    "state": "running",
    "reason": null,
    "segment_s": 2,
    "preroll_ms": 9500,
    "init": true,
    "inits": 1,
    "next": 5,
    "evicted": 0,
    "restarts": 0
  },
  "thumbnail": { "available": true, "kind": "start" }
}
```

- `state`: `open` or `closed`. `end` is `null` while open. For `end_reason: "quiet"`, `end` = `last_observation` + the event margin. `last_observation` includes the moment a held signal (motion, occupancy, audio) turned off.
- `end_reason`: `quiet` (margin passed after the last observation and no signal held), `cap` (the 10 min cap), `stopped` (plugin stopping, or the mixin removed and not re-created within 10 s; a mixin chain rebuilt by Scrypted keeps the open event).
- `continues`: the id of a capped event this one follows: set when the event opens within the margin after the cap (at once when a signal was still held).
- Held signals: a signal held on without a new `on` edge for 5 min is released; a camera going offline releases its held signals.
- `trigger.kind`: `object` (ObjectDetector), `motion`, `occupancy`, `audio`.
- `flags.camera_offline`: the camera's `Online` went false while the event was open.
- `latency`: hop timestamps (ms). Absent keys did not happen (no segmenter: no stream keys). `camera_event_time` is the detection's own timestamp (camera or detector clock; the receive time when that is more than 60 s off). `first_segment_cut` is usually a pre-roll segment; `first_live_segment_cut` is the first segment that reaches past the pre-roll.
- `context`: features from the camera and its child devices. `null` = the camera has no such signal. `changes` lists changes while the event is open (at most 20).
- `segments.state`:
  - `running`;
  - `ended` (the segmenter stopped after the event, or moved on to its continuation; all segments cut);
  - `failed` (`reason` says why; segments cut before the failure stay valid);
  - `released` (stopped early to free its slot: `reason` `not_confirmed`, no confirmed or unverified subject 30 s after start, or `preempted`, nothing worth keeping and a new event needed the slot; provisional while the event is open: a late confirmation or a freed slot sets it back to `running` with `reason: "resumed"`);
  - `none` (no segmenter: `reason` `disabled`, `busy` (8 already run), or `no_stream`).
- `preroll_ms` is the pre-roll actually received (0 without a prebuffer). `next` is the number of the next segment to be cut. `inits` is how many inits exist (more than 1 after ffmpeg restarts, `restarts`; it is restarted up to 3 times per event, live, without pre-roll). A continuation that took over a capped event's segmenter has `reason: "continued"`, numbers its segments from 0 and gets the same init as `init.mp4`.

#### `subject` object

```json
{
  "id": "s1",
  "class": "person",
  "family": "person",
  "tags": ["motion"],
  "first": 1730370000123,
  "last": 1730370003100,
  "box": [0.41, 0.30, 0.55, 0.80],
  "tracks": ["@marl1w/scrypted-reolink#17"],
  "sources": {
    "@marl1w/scrypted-reolink": { "role": "trigger", "count": 3, "max_score": 0.9, "first": 1730370000123, "last": 1730370003100 },
    "scrypted-coral-guard": { "role": "verifier", "count": 1, "max_score": 1, "first": 1730370001100, "last": 1730370001100 }
  },
  "max_score": 0.9,
  "verdict": "confirmed",
  "verdict_at": 1730370001110,
  "verdict_source": "scrypted-coral-guard",
  "enrichments": [ { "source": "@scrypted/face-recognition", "class": "face", "label": "Alice", "score": 0.8, "t": 1730370001500 } ]
}
```

- `family`: what fuses with what: `person`, `vehicle`, `animal`, `package`, `any` (motion, presence, audio, smart events), or the class itself. A subject that starts as `any` takes the class of the first concrete observation; the earlier class goes to `tags`.
- `verdict`:
  - `pending`: a verifier is present and the verify window (default 5 s from the subject's first observation) is open;
  - `confirmed`: a verifier observation joined the subject (`verdict_source`). Only an observation with a concrete class confirms (not motion, occupancy, audio or a smart event), except a classless signal from a source flagged **gated** (Coral Guard: its motion and occupancy turn on only after its own verification), and that one confirms only the single `pending` subject (otherwise its own motion/presence subject; never a `vetoed` one). While a subject is `pending`, a verifier observation of the same family within the verify window joins it, even beyond the ±2 s rule;
  - `vetoed`: the window passed (or the event ended) without one, while the verifier was live;
  - `unverified`: no verifier is present on the camera, or it showed no sign of life for 10 min when the window ended.
- A `vetoed` subject can still become `confirmed` if a verifier sees it later in the same event (a new `subject.verdict`). A `confirmed` subject never changes.
- `box`: the latest box, `null` when no source sent one (the camera's own AI usually sends none).

#### `observations` (in `event.start` and `event.update`)

```json
{ "t": 1730370000123, "source": "@marl1w/scrypted-reolink", "role": "trigger", "kind": "object",
  "class": "person", "raw": "people", "score": 0.9, "box": null, "track": null, "label": null, "zones": null }
```

`raw` is the class as the source sent it; `class` is after the source's class map. Motion, occupancy and audio rising edges have `class` `motion`, `presence`, `audio` and no score. `ambiguous: true` marks a split camera's whole-frame AI detection (see [Split cameras](#split-cameras)).

#### `subject.verdict`

```json
{ "v": 1, "seq": 43, "epoch": "ep_Xb3kQ9vT0aLm", "type": "subject.verdict", "ts": 1730370001110,
  "event_id": "ev_q2Jx8sK4mZb1VtR6yN0aLw", "camera_id": "101", "subject": { "...": "subject object" } }
```

Every subject gets at least one `subject.verdict` once its verdict is no longer `pending` (`unverified` subjects get it right after they appear).

#### `event.segment`

A segment was cut:

```json
{ "v": 1, "seq": 44, "epoch": "ep_Xb3kQ9vT0aLm", "type": "event.segment", "ts": 1730370000515,
  "event_id": "ev_q2Jx8sK4mZb1VtR6yN0aLw", "camera_id": "101", "kind": "cut", "last": false,
  "segment": { "n": 0, "init": 0, "discontinuity": false, "start_s": 0, "duration_s": 2.05, "bytes": 105797, "cut_at": 1730370000515, "preroll": true } }
```

Segments were dropped for memory (the integration records them as gaps; they answer `404`):

```json
{ "...": "envelope", "event_id": "ev_…", "camera_id": "101", "kind": "evicted", "last": false, "segment": null, "evicted": [3, 4] }
```

No more segments will come for this event:

```json
{ "...": "envelope", "event_id": "ev_…", "camera_id": "101", "kind": "last", "last": true, "segment": null, "next": 12, "state": "ended", "reason": null }
```

- `n` is the segment number for `/<n>.m4s`. `init` is the init it needs: `0` = `init.mp4`, `k` = `init-<k>.mp4`. A segment is only announced once stored, and its init always exists before it.
- `discontinuity: true`: the first segment after ffmpeg was restarted mid-event (a new init; timestamps restart). In a playlist put `#EXT-X-DISCONTINUITY` and `#EXT-X-MAP:URI="init-<k>.mp4"` before it.
- `start_s` is the media time from the start of segment 0. `preroll: true`: the segment was cut during the prebuffer burst (it lies before the detection).
- `kind: last` gives the final `next` (segments `0..next-1` exist or existed) and the final `segments.state` and `reason`. It comes only after `event.end`.
- `kind: evicted` messages are batched per event (at most one a second).
- The pre-roll arrives as a burst: about 4 segments within ~0.2 s of `event.start`, then one every `segment_s`.

### `GET /v1/events/poll`

The same messages as JSON, for clients that cannot keep an SSE stream:

```json
{ "epoch": "ep_Xb3kQ9vT0aLm", "last_seq": 1234, "oldest_seq": 235, "gap": false, "reset": false, "more": false, "messages": [ { "...": "message" } ] }
```

`wait` (0 to 25 s) holds the request until a message arrives. At most 500 messages per reply (`more: true` when there are more; poll again with the last `seq` and the `epoch`). Without `since` and `epoch`, it returns only messages published while waiting; `since` without `epoch` is a `reset`. A poll waiting when the token is regenerated answers `401`.

### `GET /v1/events?since=<ms>`

Recent events (the plugin remembers the last 200) that started or ended at or after `since`, plus every open event, oldest first; `camera=<id>` keeps one camera's. Use it after a `gap` or `reset` to recover the events you missed.

```json
{
  "since": 1730369000000,
  "events": [
    { "event": { "...": "event object" }, "segments": { "...": "segment info as in GET /v1/events/<id>, or null once released" } }
  ]
}
```

### `GET /v1/events/<id>`

```json
{
  "id": "ev_q2Jx8sK4mZb1VtR6yN0aLw",
  "event": { "...": "event object, or null once the plugin forgot it" },
  "segments": { "live": true, "init": true, "inits": 1, "next": 5, "retained": [2, 3, 4], "acked_up_to": 1, "evicted": 0, "expired": 0, "bytes": 330000, "failure": null },
  "thumbnail": { "at": 1730370000900, "kind": "start" }
}
```

`404` once neither the event nor its segments are known any more. The plugin remembers the last 200 events.

### Segments: `init.mp4` and `<n>.m4s`

- `init.mp4` (and `init-<k>.mp4`, see `segment.init`): `Content-Type: video/mp4`. `<n>.m4s`: `Content-Type: video/iso.segment`. Both are fMP4 (CMAF-style: `ftyp`+`moov`, then `moof`+`mdat` pairs, one fragment per key frame), H.264 or H.265 as the camera sends it, AAC audio when the camera has it, copied without re-encoding.
- `init.mp4` followed by any one segment plays on its own (every segment starts on a key frame).
- `425` while the segmenter is running and the segment is not cut yet; `404` when it was acked, expired (TTL, default 10 min after it was cut), evicted (memory bound, default 64 MB in total), or will never be cut (the segmenter ended).
- A playlist built from them:

  ```
  #EXTM3U
  #EXT-X-VERSION:7
  #EXT-X-TARGETDURATION:3
  #EXT-X-MAP:URI="init.mp4"
  #EXTINF:2.050,
  00000.m4s
  ...
  #EXT-X-ENDLIST
  ```

### `GET /v1/events/<id>/thumbnail.jpg`

`image/jpeg`, 320 px wide (height by aspect), about 15 KB. Taken at event start, replaced by one taken at the first `confirmed` verdict. `425` while the event is open and no picture is ready yet, `404` when there is none (the camera has no snapshot, or it failed) or it expired with the event's segments.

### `POST /v1/events/<id>/ack`

Body: `{"upTo": <n>}`. Releases segments `0..n` (inclusive). `-1` releases nothing (a no-op that returns the state). `n` beyond the last segment cut is clamped to it (`acked_up_to` in the reply shows the effective value): segments not cut yet are never released ahead.

```json
{ "ok": true, "released": 3, "segments": { "live": true, "init": true, "inits": 1, "next": 5, "retained": [3, 4], "acked_up_to": 2, "evicted": 0, "expired": 0, "bytes": 211000, "failure": null } }
```

Once the event has ended, its segmenter has stopped, and everything is acked, the plugin releases the whole event (init and thumbnail too) and `segments` is `null`. Fetch the thumbnail **before** the final ack.

### `GET /v1/cameras/<cam>/clips?start=<ms>&end=<ms>`

`VideoClips.getVideoClips({startTime, endTime})`. `end - start` at most 7 days; at most 500 clips.

```json
{
  "camera": "101", "start": 1730300000000, "end": 1730370000000,
  "clips": [
    { "id": "abc", "start": 1730360000000, "duration": 31000, "event": "motion", "description": null,
      "classes": ["person"], "thumbnail_id": "abc", "video_id": "abc" }
  ]
}
```

`404 not_supported` when the camera has no `VideoClips`. Strings come from another plugin: any value that contains a URL (`://`) is dropped (`null`, or left out of `classes`).

### Playback

`GET /v1/cameras/<cam>/playback.m3u8?t=<start ms>&d=<duration s>` (`d` optional, 1 to 3600; without it the stream runs until stopped or idle):

- The plugin calls `VideoRecorder.getRecordingStream({ startTime: t, duration: d * 1000 })`, remuxes it with `ffmpeg -c copy` to fMP4 in memory, waits up to 15 s for the first segment, then answers **`302`** with `Location: playback/<sid>/index.m3u8` (relative to the request URL).
- `index.m3u8` is a live HLS playlist (version 7, fMP4, `EXT-X-PROGRAM-DATE-TIME` per segment from `t`) that grows until the recording stream ends (`#EXT-X-ENDLIST`).
- The session files need no token (players cannot send headers); the session id is a 128-bit random value. A session stops 60 s after its last media request (`init.mp4` or a segment; playlist polls do not count), 2 min after reading paused for memory, on `DELETE`, when the token is regenerated, or after its lifetime: 30 min, or `d` + 5 min when that is longer. At most 2 sessions run at a time (a 3rd stops the least recently used).
- The playlist has `#EXT-X-START:TIME-OFFSET=0` (players start at the beginning, not the live end) and `#EXT-X-PLAYLIST-TYPE:EVENT` while no segment has left it.
- Each session keeps at most 48 MB. Segments leave from the head of the playlist: fetched ones, and unfetched ones numbered below the lowest fetched one (skipped by the player). When the memory is full of segments the player still needs, the plugin stops reading the recording stream until it catches up (back-pressure).
- `404 not_supported` when the camera has no `VideoRecorder`; `502 playback_failed` when the recording stream gave no video within 15 s.
- For a downloadable clip (the full-resolution upgrade), use `d` and fetch every segment; `#EXT-X-ENDLIST` marks the end.

## Split cameras

A camera whose stitched frame is split into views has VideoCamera children: crops show part of the frame (the parent's own default stream is its own crop), and mirrors repeat a crop for another Home. The parent and each crop are `split: true`.

- The camera's own AI reports a class and a score with no box for the whole stitched frame, so on a split camera its detections are **ambiguous**. They never open an event or create a subject. They join, as trigger evidence (`role: trigger`, `ambiguous: true`), a subject of the same family within ±2 s of it, in an event a view-specific signal opened (the view's verifier detections, or gated motion/occupancy). A crop takes these from its parent camera.
- A view's verdicts come from its own verifier; the ambiguous evidence never confirms.
- Mirrors are not observed: no duplicate events.

## Plugin settings that change behaviour

| Setting | Default | Range |
|---|---|---|
| Segment length | 2 s | 1 to 6 |
| Pre-roll | 10 s | 0 to 30 (limited by the prebuffer) |
| Event margin | 10 s | 2 to 120 |
| Event cap | 10 min | 1 to 30 |
| Verify window | 5 s | 1 to 30 |
| Segment TTL | 10 min | fixed |
| Segment memory | 64 MB | 16 to 512 |

Per source plugin ("Detection sources" in the plugin settings; no per-camera override): one role (trigger, verifier, enricher, ignore), which its motion, occupancy and audio follow (always triggers, ignored with the source, confirming only for a verifier whose motion counts as confirmation, i.e. **gated**, default on for Coral Guard), and its class map (one `name → class` row each).

### Memory budget

| Part | Bound |
|---|---|
| Event segments, inits, thumbnails | the Segment memory setting (64 MB) in total |
| Playback | 2 sessions × 48 MB |
| Message ring | 8 MB (and 5000 messages) |
| Per running segmenter (8 at most) | one partial fragment, at most 16 MB in a pathological stream, normally under 100 KB |
| Recent events | 200 event records |

With the defaults, about 170 MB at worst and a few MB typically.
