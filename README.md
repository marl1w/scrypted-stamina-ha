# Scrypted Stamina

Unusual-event flagging and a fast, verifier-gated cloud copy for any Scrypted camera, decided in Home Assistant.

- A **Scrypted plugin** turns each camera's detections (the camera's own AI, Coral Guard, any other Scrypted detector) into events. It cuts video segments from the prebuffer at the first detection and serves recordings through Scrypted's standard interfaces.
- A **Home Assistant integration** adds context from your entities, scores how unusual each event is, uploads segments to your NAS or cloud right away, and keeps the upload only when the event is unusual or confirms a class you chose (person and vehicle by default).

Status: design. See [docs/architecture.md](docs/architecture.md) and [docs/plan.md](docs/plan.md).

## Scrypted plugin (`scrypted-plugin/`, version 0.1.3)

`@marl1w/scrypted-stamina` turns every camera's detections into events and serves them, with video, to the Home Assistant integration. It provides facts only: it never decides what to keep.

**What it does**

- **Observes** each camera with the Stamina extension enabled: `ObjectDetector` events (the camera's own AI, Coral Guard, any other detector), `MotionSensor`, `OccupancySensor` and `AudioSensor` (per mixin layer, so the camera's raw signal and a gating mixin's signal are told apart), occupancy child devices, and `BinarySensor` children such as "Night mode" or "Tamper" as context.
- **Attributes and normalises** every observation to a source plugin with a role (trigger, verifier, enricher, ignore) and a mapped class (`people` -> `person`, `dog_cat` -> `animal`, ...).
- **Fuses** observations into subjects (same track id, box overlap, or same class within 2 s) and subjects into **events** that end 10 s after the last observation (10 min cap). Each subject gets a verdict: `confirmed`, `vetoed` (no verifier sighting within 5 s) or `unverified` (no verifier on this camera).
- **Cuts video** at event start: Rebroadcast's prebuffered low-resolution stream, remuxed with `ffmpeg -c copy` to fMP4 segments in memory (about 9 s of pre-roll within a quarter of a second; 2 s segments after that). Segments live until the integration acks them, or 10 min.
- **Serves** an HTTP API with a bearer token: an SSE event stream with resume, the segments, a 320 px thumbnail per event, the cameras' capabilities, VideoClips listings, and recording playback as HLS. See [docs/api.md](docs/api.md).

**Install**

1. Build and deploy: `cd scrypted-plugin && npm install && npm test && npx scrypted-deploy <scrypted-host>`.
2. In Scrypted, open each camera, Extensions, and enable **Scrypted Stamina**.
3. Open the plugin's settings: copy the **Integration URL** and the **API token** (shown until you press *Hide token*) into the Home Assistant integration.
4. Check **Detection sources** after the first detections: a row appears per source plugin with an inferred role. Change it there (it applies to every camera).

**Settings** (in this order)

| Group | What is there |
|---|---|
| Integration | The URL and the token to copy into Home Assistant (shown until *Hide token*; *Regenerate token* afterwards) |
| Detection sources | A one-line summary, then "What should Stamina do with <plugin>?" for every plugin on the cameras: *Start events*, *Double-check other detections*, *Add names / plates* or *Ignore*. Rows appear before the plugin sends anything, with the usual choice preset (the camera's plugin and Audio Guard start events, Coral Guard double-checks) |
| Class map | Classes a plugin sends that Stamina does not know yet: one select each (a class, *keep as is*, or *ignore*) |
| Advanced | Save video clips (on), clip piece length, video before the event, memory for clips, event end after quiet, longest event, double-check wait; and the classes Stamina already maps, per plugin |

**Memory**: segments, inits and thumbnails are bounded by the Segment memory setting (64 MB), playback by 2 sessions of 48 MB, the message ring by 8 MB (5000 messages); see [docs/api.md](docs/api.md#memory-budget).

**Development**: `npm test` (node:test, fakes for the SDK and ffmpeg), `npm run build`, `npm run typecheck`. Spike results that shaped the design are in [docs/spikes.md](docs/spikes.md).

## Home Assistant integration (`custom_components/scrypted_stamina`, version 0.1.0)

`scrypted_stamina` decides and stores: it scores each event against what is normal for its camera, uploads the video segments to your NAS or cloud as they are cut, and keeps the upload only when the event is unusual or confirms a class you chose (person and vehicle by default).

**What it does**

- **Link:** one SSE connection to the plugin, resumed by `seq`/`epoch` after any drop. It counts as healthy only once the replay of what was missed has been read. A plugin restart or a gap in the stream marks every undecided event *orphaned* (and what was missed is recovered from the plugin's event list); so does a link lost for more than 2 min. Orphans are kept: they keep uploading until the plugin has nothing more, then commit where they are.
- **Decides** per event: confirmed (or undecided) events are kept in `events/`; vetoed events wait in `pending/` for the **follow-up window** (10 min by default, per camera), during which changes of the camera's linked entities (doors, alarm panel, motion, shock…) are attached as late evidence. A vetoed event that turns out unusual is kept in `unusual-unconfirmed/`; an ordinary one is dropped. Nothing is deleted without that decision taken while the link is healthy and caught up, and every delete first re-reads the event from the plugin and re-scores it (a confirmation or an unusual score keeps it). Every decision is written down before it is carried out, so a Home Assistant restart resumes where it was.
- **Uploads** init and each 2 s segment as its own file to every destination, then writes `index.m3u8` (plays straight from the folder) and `event.json` (facts, context, scores, reasons, latency) and moves the folder:
  ```
  <folder>/pending|events|unusual-unconfirmed/<area>/<YYMMDD_HHMMSS>_<camera>_<event id>/
  ```
  `<area>` is the camera's Home Assistant area (or `no-area`). Quota and retention evict the oldest committed events; pending ones never, orphans only after their own retention (default 30 days). A destination that fails is skipped for a while and the others carry on; each segment is fetched from the plugin once.
- **Scores** with the relevance model ported from the earlier prototype: time of day and week, minutes from sunset, duration, what fired first on the other cameras, and every other fact as its own term (verdicts, sources, subjects, dwell, night mode, tamper, sound, weather, wind, sun elevation, linked entities, late evidence). No rules: an event is unusual only once its camera has about a week and 200 events behind it.
- **Context** needs no setup for the sun (`sun.sun`) and the weather (the first `weather.*`); linked entities are chosen per camera.

**Install**

1. Copy `custom_components/scrypted_stamina` into Home Assistant's `custom_components` (or add this repository to HACS), and restart.
2. Settings → Devices & services → Add integration → **Scrypted Stamina**: paste the plugin's **API token**. When Scrypted runs on the same host, keep the default URL `http://127.0.0.1:11080/endpoint/@marl1w/scrypted-stamina/public` (plain http over loopback, no certificate). For Scrypted on another host use its https **Integration URL**: the certificate is self-signed, so its SHA-256 fingerprint is shown and you accept it; it is then pinned and checked on every request, and a new certificate asks you to accept it again. Plain `http://` is refused to any host but this one.
3. On the integration's page, **Add destination**: choose a Synology DSM, WebDAV, SFTP Storage, OneDrive or Google Drive integration you already have, a folder (default `scrypted-stamina`), a quota (default 15 GB), a retention (0 keeps until the quota needs room) and how much the follow-up window may hold in `pending/` (default 1 GB). Every kept event goes to every destination.
4. **Configure**: how cameras are compared (all together, by area, or each on its own), how much to flag, how long the camera's recorder keeps footage, the history size (default 200 MB), then one page per camera with the linked entities (none until you pick them) and its follow-up window.

**Uploads.** Each area with a camera has its own Uploads select (e.g. `select.stamina_garden_uploads`), so each area's alarm automation can switch it on its own; cameras without an area share `select.stamina_no_area_uploads`. An event takes its camera's area's position. Three positions. **Chosen classes** (the default) keeps unusual events and ordinary events with a confirmed subject of a class chosen in the options ("Classes uploaded in 'Chosen classes'", default person and vehicle; an unverified subject counts on a camera without a verifier). **Unusual only** keeps unusual events only: an ordinary event is held in `pending/` during its follow-up window, because the plugin keeps segments only for 10 minutes, and is kept if the window finds it unusual, otherwise dropped. **Off** uploads nothing, not even to `pending/`; events are still indexed, scored and listed, so the model keeps learning, and committed copies are left alone. Each event takes the position at its start, so a change applies to new events only. Under any position, an ordinary event that is not kept goes through its follow-up window and is kept only if it turns unusual. The keep reason says which rule kept an event: `unusual` or `classes: person`. Over its quota, a destination still takes pending footage and a repair issue says so.

**Entities.** Per camera, on a Stamina device named after the camera, in its area and linked (`via_device_id`) to the camera's device in another integration (e.g. scrypted-mqtt-ha) when one is installed — Home Assistant no longer lets one integration add entities to another's device: `event.<camera>_stamina` (`normal` / `unusual`, again with `late: true` when the follow-up window turns an event unusual; attributes include the score, reasons, subjects, decision, why it was kept in plain words, cloud path and the camera's capabilities), `binary_sensor.<camera>_unusual`, `sensor.<camera>_last_event_score`. On the **Scrypted Stamina** hub device: `sensor.stamina_queue`, `sensor.stamina_orphans` and `sensor.stamina_latency` (detection to first segment uploaded, p50, with p95 as an attribute). On one device per destination, named after it and listed under that destination on the integration's page: quota used (GB) and as a percentage of the quota, pending (what the follow-up window holds in `pending/`, with its cap) and health (`ok`, or `failing` after three failed uploads, commits or evictions in a row, with the last error as an attribute). On one device per area, named after it and listed under that area on the integration's page (the integration adds, renames and removes these itself as cameras move between areas; there is nothing to add by hand): the Uploads select, quota (what that area's events hold on the destination, with the share of its quota) and status (`ok`, or `failing` while a destination fails, with what the area has uploading and held for its follow-up window). Home Assistant adds the area to entity ids of devices in an area.

**Review.** The **Scrypted Stamina** sidebar panel is one page: camera chips with each camera's state on top, then the filters and the events; on a wide screen the latest unusual event plays beside the list straight away, and on a phone a row opens under itself. Also Media → **Scrypted events**: camera, day, event. An event plays from the camera's recordings through the plugin (proxied by Home Assistant, the token never leaves it), falling back to the committed cloud copy when the recorder fails or no longer keeps footage that old (an option); each event also offers both sources explicitly.

**Index.** `scrypted_stamina.db` in the configuration directory: metadata and a 320 px thumbnail per event, never video. It is also the model's memory: 25 months of every event, dropped ones included, counted with a one-year half-life, so seasons (snow, storms) are seen twice before they would look unusual. For 30 days a row keeps its full record; after that it is slimmed to what the list, the media browser and the model read (the summary, scores and reason, the decision, and whether the recording or a cloud copy still has the footage). Thumbnails go after 30 days (7 for dropped events); an event without one shows a placeholder, and offers Play only while the recorder or a cloud copy has it. Over the **History size** option (default 200 MB) the oldest routine dropped events are thinned (one in four kept, counting for the others), never kept, unusual or rare-context ones. Removing the integration deletes it.

**Development**: `make setup` once, then `make check` (ruff lint and format, pytest with `pytest-homeassistant-custom-component` against a fake plugin).

## Licence

MIT
