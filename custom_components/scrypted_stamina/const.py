"""Constants for Scrypted Stamina.

The numbers here are the contract with the plugin (routes, timeouts) and the tuning of the
decider and the uploader. Each one says why it is what it is, so changing one is a decision
rather than a guess.
"""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "scrypted_stamina"
TITLE: Final = "Scrypted Stamina"

# The integration whose camera devices Stamina attaches to, when it is installed.
SCRYPTED_MQTT_DOMAIN: Final = "scrypted_mqtt"

PLATFORMS: Final = ("binary_sensor", "event", "select", "sensor")

# ------------------------------------------------------------------ config entry

CONF_URL: Final = "url"
CONF_TOKEN: Final = "token"
CONF_VERIFY_SSL: Final = "verify_ssl"
# The SHA-256 of the plugin's TLS certificate, pinned at setup (trust on first use).
CONF_FINGERPRINT: Final = "fingerprint"

# Home Assistant and Scrypted usually share a host: plain http over loopback, no certificate.
# A remote Scrypted is reached over https, whose self-signed certificate is pinned on first
# use (CA verification stays off by default). The `/public` path is the one the plugin's own
# token guards; the non-public one asks for a Scrypted login first (`docs/api.md`).
DEFAULT_URL: Final = "http://127.0.0.1:11080/endpoint/@marl1w/scrypted-stamina/public"
DEFAULT_VERIFY_SSL: Final = False

# Per-camera option: the linked entities.
CONF_LINKED: Final = "linked_entities"
# `select.stamina_uploads`: what is uploaded. Each event takes the position at its start.
# `classes`: unusual events, and ordinary ones with a confirmed subject of a chosen class.
# `unusual`: unusual events only. `off`: nothing uploads, not even to `pending/`.
UPLOADS_CLASSES: Final = "classes"
UPLOADS_UNUSUAL: Final = "unusual"
UPLOADS_OFF: Final = "off"
UPLOAD_MODES: Final = (UPLOADS_CLASSES, UPLOADS_UNUSUAL, UPLOADS_OFF)
# The classes an ordinary event is kept for under `classes`.
CONF_UPLOAD_CLASSES: Final = "upload_classes"
DEFAULT_UPLOAD_CLASSES: Final = ("person", "vehicle")
# Offered in the options even before any camera reports its own.
BASE_CLASSES: Final = ("person", "vehicle", "animal")
# The option the select replaced; read once, to migrate, then removed.
LEGACY_UNUSUAL_OVERRIDES: Final = "unusual_uploads_when_off"
# The no-verifier fallback (architecture §3.4): an unverified subject counts as confirmed
# once it lasted this long with at least this score.
CONF_UNVERIFIED_SECONDS: Final = "unverified_min_seconds"
CONF_UNVERIFIED_SCORE: Final = "unverified_min_score"
DEFAULT_UNVERIFIED_SECONDS: Final = 2.0
DEFAULT_UNVERIFIED_SCORE: Final = 0.5

# The follow-up window (architecture §3.4a): after `event.end` the event stays open for
# scoring this long, and linked entities' changes are attached as late evidence. Per camera.
CONF_FOLLOW_UP_MINUTES: Final = "follow_up_minutes"
DEFAULT_FOLLOW_UP_MINUTES: Final = 10
# Per-camera options live under this key, by Scrypted camera id.
CONF_CAMERAS: Final = "cameras"
# How long `binary_sensor.<cam>_unusual` pulses on for a late unusual.
UNUSUAL_PULSE_SECONDS: Final = 30.0

# How long the camera's recorder keeps footage: older events play from the cloud copy.
CONF_RECORDER_RETENTION_DAYS: Final = "recorder_retention_days"
DEFAULT_RECORDER_RETENTION_DAYS: Final = 30

# Entity updates are coalesced over this long; record saves likewise.
UPDATE_DEBOUNCE_SECONDS: Final = 1.0
SAVE_DEBOUNCE_SECONDS: Final = 1.0
# The stream position is written at most this often (and on stop).
POSITION_SAVE_SECONDS: Final = 5.0
# The index's two tiers (architecture §3.6): a row keeps its full record this long, then is
# slimmed to what the list, the media browser and the model read. Thumbnails go after this
# long for kept events, and after a week for dropped ones (nobody scrolls back to those).
FRESH_DAYS: Final = 30
THUMBNAIL_KEEP_DAYS: Final = 30
THUMBNAIL_DROPPED_DAYS: Final = 7
# What the index may hold, in MB. Over it, ordinary dropped history is thinned (never cut).
CONF_HISTORY_SIZE_MB: Final = "history_size_mb"
DEFAULT_HISTORY_SIZE_MB: Final = 200

# ------------------------------------------------------------------ destinations

SUBENTRY_TYPE_DESTINATION: Final = "destination"
CONF_DESTINATION_ENTRY: Final = "destination_entry"
CONF_FOLDER: Final = "folder"
CONF_QUOTA_GB: Final = "quota_gb"
CONF_RETENTION_DAYS: Final = "retention_days"
# Pending storage held by FOLLOW_UP events, per destination; over it, the oldest go first.
CONF_FOLLOW_UP_CAP_GB: Final = "follow_up_cap_gb"
DEFAULT_FOLLOW_UP_CAP_GB: Final = 1.0
# Orphans are never evicted for room, only by age.
CONF_ORPHAN_RETENTION_DAYS: Final = "orphan_retention_days"
DEFAULT_ORPHAN_RETENTION_DAYS: Final = 30
DEFAULT_FOLDER: Final = "scrypted-stamina"
DEFAULT_QUOTA_GB: Final = 15
# Zero keeps events until the quota needs the room, which is what Reolink Stamina does.
DEFAULT_RETENTION_DAYS: Final = 0

# Top-level folders under a destination's folder (architecture §3.5).
FOLDER_PENDING: Final = "pending"
FOLDER_EVENTS: Final = "events"
FOLDER_UNUSUAL: Final = "unusual-unconfirmed"
NO_AREA: Final = "no-area"

# ------------------------------------------------------------------ plugin API

API_HEALTH: Final = "/v1/health"
API_CAMERAS: Final = "/v1/cameras"
API_STREAM: Final = "/v1/events/stream"

# How long one plain request may take. Segments are about 120 KB on a LAN.
REQUEST_TIMEOUT: Final = 15.0

# The SSE stream sends a keepalive comment at least this often. Silence for longer than this
# means the connection is dead even if the socket has not noticed.
STREAM_IDLE_TIMEOUT: Final = 45.0

# Reconnect backoff, doubling from the first to the last.
BACKOFF_INITIAL: Final = 1.0
BACKOFF_MAX: Final = 30.0

# Architecture §3.1: after this long without a link, every pending event becomes orphaned.
LINK_LOST_SECONDS: Final = 120.0

# The plugin keeps segments this long (architecture §2.3). An event older than this that
# was never committed can no longer be completed and is orphaned on resume.
SEGMENT_TTL_SECONDS: Final = 600.0

# How long `425 Too Early` is waited out before asking for the same segment again.
SEGMENT_NOT_READY_DELAY: Final = 1.0

# ------------------------------------------------------------------ uploader

# Segment uploads in flight at once, across every event and destination. Bounded by
# `capacity.slots`, which also takes memory into account, and never above this.
MAX_PARALLEL_UPLOADS: Final = 6
# What one segment is budgeted at before any has been measured: 2 s of a sub stream.
ASSUMED_SEGMENT_BYTES: Final = 256 * 1024
# A single upload is retried by the destination itself; this is how often the uploader
# re-queues a segment whose destination gave up, before marking it a gap.
SEGMENT_ATTEMPTS: Final = 5
# Upper bound on what one segment may be. Anything larger is not a 2 s low-res segment.
MAX_SEGMENT_BYTES: Final = 32 * 1024 * 1024
MAX_THUMBNAIL_BYTES: Final = 512 * 1024

# Latency samples kept for `sensor.stamina_latency`.
LATENCY_SAMPLES: Final = 200

# ------------------------------------------------------------------ storage

INDEX_FILENAME: Final = "scrypted_stamina.db"
STORAGE_VERSION: Final = 1

# ------------------------------------------------------------------ relevance (ported)

# Five-minute bins over the day. Fine enough that a school run and an evening return do not
# land in the same bucket, coarse enough that a year of events still fills it.
RATE_BINS: Final = 288
RATE_BIN_MINUTES: Final = 1440 // RATE_BINS
# Smoothing, in minutes. Hard bins would call 23:59 rare while 23:01 is common.
RATE_BANDWIDTH_MINUTES: Final = 45.0
# How fast the past stops counting. A year, so last winter's snow still counts this winter.
RATE_HALF_LIFE_DAYS: Final = 365.0
# Below this much evidence a camera-and-kind profile is blended towards broader ones.
RATE_BACKOFF_WEIGHT: Final = 40.0
# Laplace smoothing for the categorical terms.
RATE_SMOOTHING: Final = 0.5
# The same idea for the smooth distributions, and deliberately much smaller.
RATE_FLOOR: Final = 0.05
# Where the predecessor term stops looking, as quiet seconds between two events.
SCORE_LAG_BUCKETS: Final = (10.0, 30.0, 120.0)
# How rare an event has to be, as a quantile of that camera's own scores.
SCORE_QUANTILE: Final = 0.95
# The absolute floor, in nats: four times rarer than chance.
SCORE_FLOOR: Final = 1.4
# Nothing is flagged until a camera has this much behind it (architecture §3.3).
SCORE_MIN_DAYS: Final = 7.0
SCORE_MIN_EVENTS: Final = 200
# Numeric context is counted as the quantile band its reading fell in.
SIGNAL_BANDS: Final = 5
SIGNAL_BAND_MIN: Final = 20
# What an event is compared with when its own camera has little history (as in Reolink
# Stamina, with "per recorder" replaced by "per area": the camera device's HA area, and
# cameras without one forming their own pool).
CONF_RELEVANCE_SCOPE: Final = "relevance_scope"
RELEVANCE_SCOPE_TOGETHER: Final = "together"
RELEVANCE_SCOPE_AREA: Final = "area"
RELEVANCE_SCOPE_CAMERA: Final = "camera"
RELEVANCE_SCOPES: Final = (
    RELEVANCE_SCOPE_TOGETHER,
    RELEVANCE_SCOPE_AREA,
    RELEVANCE_SCOPE_CAMERA,
)
DEFAULT_RELEVANCE_SCOPE: Final = RELEVANCE_SCOPE_TOGETHER
# How much gets flagged, as the floor in nats (Reolink Stamina's three words).
CONF_RELEVANCE_SENSITIVITY: Final = "relevance_sensitivity"
RELEVANCE_SENSITIVITY_FLOORS: Final = {"few": 2.1, "balanced": 1.4, "many": 0.7}
DEFAULT_RELEVANCE_SENSITIVITY: Final = "balanced"
# How much history the model is rebuilt from: 25 months, so every season is seen twice.
RELEVANCE_HISTORY_DAYS: Final = 760

# ------------------------------------------------------------------ dispatcher signals


def signal_update(entry_id: str) -> str:
    """Return the dispatcher signal fired when anything entities show has changed."""
    return f"{DOMAIN}_{entry_id}_update"


def signal_event(entry_id: str) -> str:
    """Return the dispatcher signal fired when an event is decided or ends."""
    return f"{DOMAIN}_{entry_id}_event"


def signal_camera(entry_id: str, camera: str) -> str:
    """Return the dispatcher signal for one camera's entities."""
    return f"{DOMAIN}_{entry_id}_camera_{camera}"


def signal_quota(entry_id: str) -> str:
    """Return the dispatcher signal fired when a destination's holdings change."""
    return f"{DOMAIN}_{entry_id}_quota"


def signal_cameras(entry_id: str) -> str:
    """Return the dispatcher signal fired when the camera list changes."""
    return f"{DOMAIN}_{entry_id}_cameras"
