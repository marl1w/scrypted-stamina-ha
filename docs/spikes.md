# Phase 0 spikes

Results of the spikes in [plan.md](plan.md#phase-0-spikes-prove-the-risky-assumptions) that the Scrypted plugin depends on (S1, S2). S3 (recordings) is done in the camera plugin's repo, S4 (destinations) on the Home Assistant side.

Setup: one Scrypted server (3 cores) with several cameras through a camera plugin (`@marl1w/scrypted-reolink`), Rebroadcast (`@scrypted/prebuffer-mixin`) on every camera, and a verifier mixin (`scrypted-coral-guard`, Gate mode, publishing verified classes) on every camera. The probes ran from another machine on the LAN through `@scrypted/client`: listeners only, and short stream captures (one at a time).

## S1: attribution

**Question:** does a camera listener receive `EventDetails.mixinId` / `ObjectsDetected.sourceId` that tells the camera's own AI apart from the verifier mixin?

**Method:** listeners for `ObjectDetector`, `MotionSensor`, `OccupancySensor`, `AudioSensor` and `Online` on the primary test camera for 6 min (it stayed quiet), then on every camera for 10 min, then per mixin layer (`listen({ event, mixinId: <layer> })` for the device itself and each of its mixins) for 8 and 12 min.

**Findings:**

| Signal | Plain listener | Per-layer listener (`mixinId` set) |
|---|---|---|
| `ObjectDetector`, camera AI | received; `details.mixinId` absent; `sourceId: "@marl1w/scrypted-reolink"` (a **plugin id**) | not received on any layer |
| `ObjectDetector`, Coral Guard | received; `details.mixinId` absent; `sourceId: "<Coral Guard's device id>"` (a **device id**; `getDeviceById(id).pluginId` = `scrypted-coral-guard`) | not received |
| `MotionSensor`, camera's raw motion | **not received** (a gating verifier mixin owns MotionSensor and masks it) | received on the device layer, `details.mixinId` = the camera id (resolves to the camera plugin) |
| `MotionSensor` / `OccupancySensor`, the gated verifier's values | received; `details.mixinId` absent | see note below |

- 55 events from 2 active cameras in the 10 min listen: 34 camera-AI detections (`people`, score 1), 13 Coral Guard detections (`verified_person`, score 1), 4 motion and 4 occupancy edges. All 47 detections were attributed correctly from `sourceId` alone; `mixinId` was absent on every plain-listener event.
- The camera AI sends **no box, no track id and no `inputDimensions`**; Coral Guard sends class only. Fusion therefore relies on the third rule (same class family within 2 s) for these two sources; IoU and track rules apply to detectors that send boxes.
- Detection timestamps vs. receive time on the listener: p50 5 ms, p95 93 ms, max 98 ms (n = 55). The camera AI repeats a detection every ~1 s while the person stays.
- Coral Guard's `verified_person` arrived 0.8 to 1.0 s after the camera's first `people` on the same camera.

**Design (implemented):**

1. `ObjectDetector`: one plain listener; source = `mixinId` if ever present, else `sourceId` resolved through `getDeviceById` (which accepts both a device id and a plugin id), else the camera's own plugin.
2. `MotionSensor`, `OccupancySensor`, `AudioSensor`: one listener **per layer** (the camera itself, then each mixin except Stamina), each attributed to its layer's plugin. This yields the camera's raw motion as a `trigger` even when a gating mixin owns the interface.
3. Fallback: the plain listener for those interfaces is kept. Its edge waits 300 ms and counts only if no layer listener reported the same edge within 1 s; it is then attributed to its `mixinId` or the camera plugin. Limitation: when a mixin layer cannot be listened to and its edge coincides (within 1 s) with the camera's raw edge, it is taken as a duplicate. This only loses a classless verifier signal; the verifier's `ObjectDetector` detection still confirms.
4. A verifier counts as present on a camera only once it has emitted there (in the last 7 days). A verifier mixin that never emits (a verifier in Shadow mode publishes nothing) is reported as `silent_verifiers` and does not count, otherwise every subject would be vetoed.

**Note (the verifier's own layer):** in 20 min of per-layer listening, 15 raw camera motion edges arrived on the device layer (all attributed to the camera plugin), but the verifier confirmed nothing in that window, so no gated edge was seen on its own layer (`mixinId` = the verifier's id). Whether a mixin layer delivers its own property events is therefore **unconfirmed**; the plain-listener fallback above covers either outcome. Re-check after deploy.

## S2: segmenter

**Question:** can `getVideoStream({ prebuffer })` plus `ffmpeg -c copy` produce playable fMP4 segments, pre-roll included, in about 1 s?

**Method:** `getVideoStream({ prebuffer: 10000, destination: 'low-resolution' })` on the test camera, `convertMediaObjectToJSON(…, FFmpegInput)`, then ffmpeg 9 with two outputs from the same input: the HLS muxer (`-c copy -f hls -hls_segment_type fmp4 -hls_time 2`) to a directory, and `-f mp4 -movflags frag_keyframe+empty_moov+default_base_moof` to a pipe. Then the plugin's own segmenter module (pipe + in-memory splitter) against the same stream. Captures of 12 to 20 s.

**Findings:**

| Measure | Result |
|---|---|
| Stream | Rebroadcast `sub.bcs` (prebuffered, 10 s), H.264 896x512 at 20 fps (the stream options report 640x480: do not trust them), AAC mono 16 kHz |
| `getVideoStream` + FFmpegInput | 20 to 110 ms + 7 to 110 ms |
| Request to first byte from ffmpeg | 190 to 270 ms |
| Request to init + first segment | 197 ms (HLS muxer), 224 to 272 ms (plugin module) |
| Pre-roll received | 8.9 to 9.9 s, as a burst: 4 segments of 2 s within 0.03 s of the first byte |
| First segment that reaches past the pre-roll (contains the request moment) | 1.1 to 1.3 s after the request |
| Key frame interval | ~1.0 s (0.9 to 1.2 s) |
| Segment size | 85 to 106 KB per 2 s, i.e. 42 to 53 KB/s |
| `-hls_time 2` | segments of 2.00 to 2.15 s |
| `-hls_time 1` | works: segments of 1.00 to 1.10 s (one key frame each) |
| Audio | AAC copied as is; `init.mp4` has `avc1` + `mp4a` |
| Playback | every playlist decodes with ffmpeg (`-f null`, no errors), ffprobe reports h264 + aac; `init.mp4` + any single segment plays on its own |

- The in-memory splitter (fragments grouped by media time from `tfdt`/`trun`) reproduces exactly the HLS muxer's cut (2.05 / 2.00 / 2.15 s …) and its segments decode identically. The plugin uses the pipe (no files, no temp directory).
- ffmpeg warns `packet … has duration 0` on the HLS muxer; harmless (it disappears with the mp4 muxer).
- The Rebroadcast FFmpegInput URL is a per-request `rtsp://127.0.0.1:<port>/…` (a single-client listener on the Scrypted host); it carries a session token in its path, so it must never be logged. The plugin logs only masked URLs.
- A 320 px snapshot (`takePicture({ picture: { width: 320 } })`): 566 ms, 320x183, 10 KB. The snapshot plugin honours the width, so no rescale is needed (the plugin rescales with ffmpeg only when a camera returns something wider).

**Design (implemented):** ffmpeg `-c copy` to a fragmented MP4 pipe, split in memory into `init.mp4` and segments of the configured length (default 2 s, 1 s works). Segments cut during the prebuffer burst are flagged `preroll`. Latency stamps per event: `stream_requested`, `stream_resolved`, `first_byte`, `init_cut`, `first_segment_cut`, `first_live_segment_cut`. If the low-resolution stream cannot be opened the segmenter retries once on the default stream; if ffmpeg fails before any output with an audio error it retries without audio (copyable audio only: AAC, Opus, MP3, AC-3, ALAC, FLAC).

## What still needs a live check after deploy

- `HttpResponse.sendStream` with a long-lived async generator (SSE): that chunks reach the client at once (no buffering), and that a client disconnect ends the generator. The plugin bounds streams (1 h lifetime, 2000-message backlog, 4 clients) so a leak is limited either way; `/v1/events/poll` is the fallback.
- The `/public/` endpoint path with the plugin's own bearer token (no Scrypted login), over both 11080 and 10443.
- Per-layer listeners from inside a plugin (the spike used `@scrypted/client`); the mixin-layer edge of a gated verifier's motion.
- `getVideoStream` with `prebuffer` from inside the plugin (the spike rewrote the 127.0.0.1 address to reach it from another machine; inside Scrypted it is local).
- `VideoRecorder` / `VideoClips`: no camera implements them yet (spike S3 in the camera plugin's repo). The playback and clips routes are tested with fakes only.

## S4: destinations (Home Assistant side)

**Question:** which destinations support a file per segment and a folder rename?

**Method:** read from the ported code (`custom_components/scrypted_stamina/destinations/`), the client libraries Home Assistant installs for each storage integration (aiowebdav2 0.6, py-synologydsm-api 2.11, asyncssh 2.24), and the providers' API documentation (Microsoft Graph, Google Drive v3, Synology File Station, RFC 4918). No live run: every destination reuses the session or credentials of the Home Assistant integration that owns it, so a live check needs those set up (see below).

**Findings:**

| Provider (HA integration, client) | Put (a file per segment) | Rename / move a folder | Delete | List | Read back |
|---|---|---|---|---|---|
| Synology DSM (`synology_dsm`, py-synologydsm-api `SynoFileStation`) | yes: `upload_file(create_parents=True)` (`SYNO.FileStation.Upload`) | yes, server-side, **not wrapped** by the library: `SYNO.FileStation.CopyMove` (`start` with `remove_src=true`, then poll `status`) through the integration's logged-in session (`runtime_data.api.dsm.get`). Keeps the folder name, which is all a commit needs. If the NAS does not list the API, the uploader copies then deletes | yes: `delete_file` (non-recursive). A folder: `SYNO.FileStation.Delete` with `recursive=true` through the same session, else file by file | yes: `get_files`, paged (500) | yes: `download_file` |
| WebDAV (`webdav`, aiowebdav2) | yes: `upload_iter` (PUT); parents made with MKCOL (`check`/`mkdir`) | yes: `move` (MOVE, RFC 4918 §9.9, `Overwrite: F`) | yes: `clean` (DELETE; recursive on a collection, RFC 4918 §9.6.1) | yes: `list_with_infos` (PROPFIND, depth 1) | yes: `download_iter` |
| SFTP Storage (`sftp_storage`, asyncssh) | yes: `open(…, "wb")` + `write`, `makedirs` | yes: `rename` (`SSH_FXP_RENAME`; refuses an existing target, which a commit never has) | yes: `unlink`; a folder with `rmtree` | yes: `readdir` | yes: `open(…, "rb")` |
| OneDrive (`onedrive`, Microsoft Graph with the integration's OAuth session, `Files.ReadWrite.AppFolder`) | yes: `PUT …:/path:/content` (simple upload up to 250 MB; missing parents are created) | yes: `PATCH` the folder item with `parentReference.id` and `name`; the target parent is created first (`POST children`, `conflictBehavior: fail`, 409 = exists) | yes: `DELETE` the item (a folder goes with its children) | yes: `GET …/children`, paged by `@odata.nextLink` | yes: `GET …/content` |
| Google Drive (`google_drive`, Drive v3 with the integration's OAuth session, `drive.file`) | yes: create the file's metadata, then `PATCH` media (`uploadType=media`); folder ids resolved per path segment and cached | yes: `files.update` with `addParents` / `removeParents` (and `name`) | yes: `files.delete` (a folder takes its descendants that this app created) | yes: `files.list` with `'<id>' in parents`, paged | yes: `files.get?alt=media` |

- **Every ported destination supports a file per segment, a folder move, delete and list.** The interface grew `async_read`, `async_move` (with `can_move`) and `async_delete_folder`; the commit uses one server-side move where there is one and falls back to read + store + delete per file (`MoveUnsupportedError`, or `can_move = False`). Read is also what lets Home Assistant play a committed cloud copy (`index.m3u8`) without a public link.
- **Request cost per 2 s segment:** WebDAV 3 to 4 (check, upload, info; MKCOL once per folder), SFTP 3 (makedirs, write, stat) on one held connection, Synology 1, OneDrive 1, Google Drive 2 to 3 (look up by name, create, upload: Drive keys on ids, and a name lookup is what prevents twins after a retried create). Google Drive is the one to watch against the 3 s first-segment target.
- **Deleted is not always gone:** OneDrive's `DELETE` moves an item to the recycle bin, where it still counts against the account until emptied. Drive's `files.delete` is permanent. Synology and WebDAV-on-NAS depend on the share's recycle-bin setting. The quota here counts what Stamina holds, not what the provider's bin holds.
- **Synology without File Station** (the package uninstalled) leaves `file_station` unset: the destination says so plainly instead of failing per file (kept from the earlier prototype).

**Needs a live check after deploy:** CopyMove and the recursive Delete on a real DSM (both via the integration's session, outside the library's wrappers); WebDAV MOVE of a collection on the NAS in use (some servers answer `502` across shares); OneDrive PATCH-move inside the app folder; Google Drive first-segment latency.
