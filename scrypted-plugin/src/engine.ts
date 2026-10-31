// The plugin's engine: message bus, segment store, playback, the API, and the per-camera cores, with every Scrypted call
// behind `Host` (main.ts implements it with the SDK; tests use a fake).

import type { ChildProcess, SpawnOptions } from 'child_process';
import { Api, ApiRequest, ApiResponse, CameraApi, ClipInfo } from './api';
import { Bus } from './bus';
import { CameraCore, CameraEnv, CameraSink } from './camera';
import { NumberKey, readNumber, VERSION } from './config';
import { EpisodeConfig, EventRecord, publicEvent } from './episode';
import { Subject } from './fusion';
import { jpegSize, MAX_THUMB_BYTES, scaleJpeg, THUMB_WIDTH } from './jpeg';
import { PlaybackManager } from './playback';
import { Discovered, RolesTable } from './roles';
import { SegmenterDeps, SegmenterHandle, startSegmenter, StreamInput } from './segmenter';
import { SegmentStore } from './segments';
import { Clock, errText, newToken, realClock, withTimeout } from './util';

export interface StreamOptionsInfo { prebufferMs: number; audio: boolean }

export interface Host {
    clock: Clock;
    console: Pick<Console, 'log' | 'warn'>;
    storageGet(key: string): string | null;
    storageSet(key: string, value: string): void;
    /** the camera's prebuffered low-resolution stream (or its default stream) as ffmpeg input */
    streamInput(cameraId: string, prerollMs: number, lowRes: boolean): Promise<StreamInput>;
    streamOptions(cameraId: string): Promise<StreamOptionsInfo>;
    recordingInput(cameraId: string, startTime: number, durationMs: number | undefined): Promise<StreamInput>;
    clips(cameraId: string, start: number, end: number): Promise<ClipInfo[]>;
    /** a JPEG about `width` px wide, or undefined when the camera has no snapshot */
    picture(cameraId: string, width: number): Promise<Buffer | undefined>;
    interfaces(cameraId: string): string[];
    /**
     * Sources on these cameras from the system state: the camera's own plugin and each mixin (but this one) that provides
     * ObjectDetector, MotionSensor, OccupancySensor or AudioSensor there; and the classes each declares (getObjectTypes).
     */
    discover?(cameraIds: string[]): Promise<{ found: Discovered[]; declared: Record<string, string[]> }>;
    ffmpegPath(): Promise<string>;
    spawn(cmd: string, args: string[], opts: SpawnOptions): ChildProcess;
}

export const MAX_SEGMENTERS = 8;
/** with this many segmenters running, a failing low-resolution stream is not retried on the (heavier) main stream */
export const DEFAULT_STREAM_LIMIT = 4;
/** ffmpeg restarts per event when it dies mid-event */
export const SEGMENTER_RESTARTS = 3;
/** a segmenter whose event has no confirmed or unverified subject after this long gives its slot back (ms) */
export const RELEASE_AFTER_MS = 30_000;
/** pictures taken at once, all cameras together */
export const SNAPSHOT_SLOTS = 2;
/** a mixin released and not re-created within this long: its camera's open event ends (ms) */
export const DETACH_GRACE_MS = 10_000;
export const RECENT_EVENTS = 200;
const OPTIONS_TTL_MS = 5 * 60_000;
const THUMB_TIMEOUT_MS = 8000;
const WARN_EVERY_MS = 60_000;

export type SegState = 'none' | 'running' | 'ended' | 'failed' | 'released';

export interface SegInfo {
    state: SegState;
    reason: string | null;
    segment_s: number;
    preroll_ms: number;
    init: boolean;
    inits: number;
    next: number;
    evicted: number;
    restarts: number;
}

interface Running {
    handle: SegmenterHandle;
    core: CameraCore;
    ev: EventRecord;
    seg: SegInfo;
    started: number;
    /** the latest init, handed to a continuation event */
    initBuf?: Buffer;
    /** restarted for an event that had segments already: its first segment is a discontinuity */
    resumeFirst?: boolean;
    /** media time already used by earlier events of this segmenter (handover) */
    offset: number;
    mediaEnd: number;
    released?: boolean;
    preempted?: boolean;
    /** waiting for the continuation of a capped event */
    handoverTimer?: any;
}

/** a subject that makes its event worth its video: confirmed, or unverified (a motion-only unverified one only when `motionKeeps`) */
function keeper(ev: EventRecord, motionKeeps = true): boolean {
    return ev.subjects.some(s => s.verdict === 'confirmed' || (s.verdict === 'unverified' && (motionKeeps || s.family !== 'any')));
}

/** a preempted segmenter must have run at least this long (ms) */
export const MIN_VICTIM_AGE_MS = 10_000;
/** a queued start picture older than this is not taken any more (ms) */
export const START_PICTURE_MAX_WAIT_MS = 5000;
/** eviction announcements per event at most this often (ms) */
const EVICT_ANNOUNCE_MS = 1000;

export class Engine implements CameraSink {
    readonly bus = new Bus();
    readonly store: SegmentStore;
    readonly playback: PlaybackManager;
    readonly roles: RolesTable;
    readonly api: Api;
    readonly cores = new Map<string, CameraCore>();
    private recent = new Map<string, EventRecord>();
    private segmenters = new Map<string, Running>();
    /** segmenters of capped events, waiting for the continuation, by the capped event's id */
    private handovers = new Map<string, Running>();
    private thumbs = new Map<string, Promise<unknown>>();
    private confirmedThumb = new Set<string>();
    private snapshotsBusy = 0;
    /** events that had a keeper subject at some point (a continuation inherits it) */
    private keepers = new Set<string>();
    /** open events that got no segmenter because all slots were taken */
    private busy = new Set<string>();
    /** released or preempted segmenters still flushing (their event's `last` waits for them) */
    private stopping = new Set<Running>();
    private evictPending = new Map<string, { ns: number[]; last: number; timer?: any }>();
    private snapshotWaiters: (() => void)[] = [];
    private options = new Map<string, { at: number; info?: StreamOptionsInfo; pending?: Promise<void> }>();
    private detachTimers = new Map<string, any>();
    private refreshTimer: any;
    private discoverTimer: any;
    /** classes each source declares (the cameras' getObjectTypes for the camera plugin) */
    readonly declared = new Map<string, string[]>();
    private evictWarnAt = -Infinity;
    private rolesSave: any;
    private sweepTimer: any;
    readonly started: number;

    constructor(private host: Host) {
        const clock = host.clock;
        this.started = clock.now();
        this.roles = new RolesTable(host.storageGet('roles'), () => this.saveRoles());
        // rows stored by 0.1.0 (a separate signal role) are rewritten once in the current shape
        if (RolesTable.needsMigration(host.storageGet('roles'))) host.storageSet('roles', this.roles.toJSON());
        this.store = new SegmentStore({ ttlMs: 10 * 60_000, maxBytes: this.num('memoryMB') * 1024 * 1024, maxEvents: RECENT_EVENTS }, clock);
        this.store.onEvict = (id, ns) => this.evicted(id, ns);
        this.playback = new PlaybackManager(undefined, () => clock.now());
        this.api = new Api({
            token: () => this.token(),
            bus: this.bus,
            store: this.store,
            playback: this.playback,
            cameras: () => this.camerasInfo(),
            camera: id => this.cameraApi(id),
            health: () => this.health(),
            event: id => { const ev = this.recent.get(id); return ev ? publicEvent(ev) as any : undefined; },
            events: (since, cam) => this.eventsSince(since, cam),
            ffmpeg: this.ffmpegDeps(),
            console: host.console,
            now: () => clock.now(),
        });
        if (!this.token()) this.regenerateToken();
        this.sweepTimer = setInterval(() => this.sweep(), 15_000);
        (this.sweepTimer as any).unref?.();
    }

    num(key: NumberKey): number {
        return readNumber(key, this.host.storageGet(key));
    }

    segmentsEnabled(): boolean {
        return this.host.storageGet('segments') !== 'false';
    }

    episodeConfig(): EpisodeConfig {
        return { marginMs: this.num('marginS') * 1000, capMs: this.num('capMin') * 60_000, verifyWindowMs: this.num('verifyWindowS') * 1000 };
    }

    /** settings changed: apply to the store and running trackers (new events pick up the rest) */
    applySettings() {
        this.store.cfg.maxBytes = this.num('memoryMB') * 1024 * 1024;
        this.playback.cfg.segmentS = this.num('segmentS');
        const ep = this.episodeConfig();
        for (const c of this.cores.values()) c.tracker.cfg = ep;
    }

    token(): string {
        return this.host.storageGet('token') || '';
    }

    /** A new token: everything opened with the old one ends (SSE streams, pending polls, playback sessions). */
    regenerateToken(): string {
        const t = newToken();
        this.host.storageSet('token', t);
        this.host.storageSet('tokenShown', 'true');
        this.api?.revoke();
        return t;
    }

    private saveRoles() {
        if (this.rolesSave) return;
        this.rolesSave = setTimeout(() => { this.rolesSave = undefined; this.host.storageSet('roles', this.roles.toJSON()); }, 2000);
        (this.rolesSave as any).unref?.();
    }

    flushRoles() {
        if (this.rolesSave) { clearTimeout(this.rolesSave); this.rolesSave = undefined; }
        this.host.storageSet('roles', this.roles.toJSON());
    }

    // ---- cameras ------------------------------------------------------------------------------

    /**
     * The mixin of a camera was created. A core that already exists for the camera (the mixin chain was rebuilt) is kept with
     * its tracker and open event, and rebound with the new mixin's env.
     */
    attach(env: CameraEnv): CameraCore {
        const t = this.detachTimers.get(env.id);
        if (t !== undefined) { this.host.clock.clearTimeout(t); this.detachTimers.delete(env.id); }
        const core = this.cores.get(env.id);
        if (core) {
            core.setEnv(env);
            return core;
        }
        const created = new CameraCore(env, this);
        this.register(created);
        return created;
    }

    /** The mixin was released: the core stops only if no new mixin attaches within the grace period. */
    detach(id: string) {
        if (!this.cores.has(id) || this.detachTimers.has(id)) return;
        this.detachTimers.set(id, this.host.clock.setTimeout(() => {
            this.detachTimers.delete(id);
            const core = this.cores.get(id);
            if (core) this.unregister(core);
        }, DETACH_GRACE_MS));
    }

    register(core: CameraCore) {
        const old = this.cores.get(core.id);
        if (old && old !== core) old.stop();
        this.cores.set(core.id, core);
        core.bind();
        this.scheduleRefresh();
        this.sourcesChanged();
    }

    unregister(core: CameraCore) {
        if (this.cores.get(core.id) === core) this.cores.delete(core.id);
        core.stop();
    }

    /** A camera's mixins or children changed: discover the sources again (debounced). */
    sourcesChanged() {
        if (this.discoverTimer !== undefined || !this.host.discover) return;
        this.discoverTimer = this.host.clock.setTimeout(() => {
            this.discoverTimer = undefined;
            this.discoverSources().catch(e => this.host.console.warn(`source discovery: ${errText(e)}`));
        }, 2000);
    }

    /** Rows for every source found in the system state, before it sends anything (its inferred role, "not seen yet"). */
    async discoverSources() {
        if (!this.host.discover || !this.cores.size) return;
        const r = await withTimeout(this.host.discover([...this.cores.keys()]), 30_000, 'source discovery');
        for (const d of r.found) this.roles.preseed(d);
        this.declared.clear();
        for (const [src, classes] of Object.entries(r.declared)) this.declared.set(src, classes.slice(0, 48));
    }

    /** Stream options are read once for all cameras shortly after the mixins register (then every 5 min on demand). */
    private scheduleRefresh() {
        if (this.refreshTimer !== undefined) return;
        this.refreshTimer = this.host.clock.setTimeout(() => {
            this.refreshTimer = undefined;
            this.refreshOptions().catch(() => { });
        }, 2000);
    }

    handle(req: ApiRequest): Promise<ApiResponse> {
        return this.api.handle(req);
    }

    stop() {
        clearInterval(this.sweepTimer);
        if (this.discoverTimer !== undefined) this.host.clock.clearTimeout(this.discoverTimer);
        for (const t of this.detachTimers.values()) this.host.clock.clearTimeout(t);
        this.detachTimers.clear();
        for (const c of [...this.cores.values()]) this.unregister(c);
        for (const r of [...this.segmenters.values(), ...this.handovers.values()]) r.handle.stop();
        this.playback.stopAll();
        this.api.closeStreams();
        this.flushRoles();
    }

    sweep() {
        this.store.sweep();
        this.playback.sweep();
        for (const c of this.cores.values()) c.bind();
        this.releaseIdleSlots();
    }

    // ---- CameraSink ---------------------------------------------------------------------------

    publish(type: 'event.start' | 'event.update' | 'event.end' | 'subject.verdict', body: Record<string, unknown>) {
        this.bus.publish(type, body);
    }

    eventStarted(core: CameraCore, ev: EventRecord) {
        this.remember(ev);
        ev.extra.thumbnail = { available: false, kind: null };
        const seg: SegInfo = { state: 'none', reason: null, segment_s: this.num('segmentS'), preroll_ms: 0, init: false, inits: 0, next: 0, evicted: 0, restarts: 0 };
        ev.extra.segments = seg;
        // the store holds the thumbnail too: every event gets an entry, finished at once when no segmenter runs
        this.store.open(ev.id, core.id);
        this.thumbnail(core, ev, 'start');
        const none = (reason: string) => { seg.reason = reason; this.store.finish(ev.id); };
        if (!this.segmentsEnabled()) return none('disabled');
        if (!this.host.interfaces(core.id).includes('VideoCamera')) return none('no_stream');
        // a capped event's segmenter carries on into its continuation: no restart, no repeated pre-roll
        const prev = ev.continues ? this.handovers.get(ev.continues) : undefined;
        if (prev && prev.core === core) return this.handOver(prev, ev, seg);
        if (this.segmenters.size >= MAX_SEGMENTERS && !this.preempt()) {
            this.host.console.warn(`${core.env.name()}: no segments for this event yet: ${MAX_SEGMENTERS} segmenters already run`);
            this.busy.add(ev.id);
            return none('busy');
        }
        this.startSegments(core, ev, seg);
    }

    /** an event worth its video: a keeper now, before, or in the capped event it continues */
    private isKeeper(ev: EventRecord, motionKeeps = true): boolean {
        if (keeper(ev, false)) this.keepers.add(ev.id);
        if (this.keepers.size > RECENT_EVENTS * 2) this.keepers.delete(this.keepers.values().next().value!);
        return keeper(ev, motionKeeps) || this.keepers.has(ev.id) || (!!ev.continues && this.keepers.has(ev.continues));
    }

    /**
     * Segments again for an open event that had none or lost its slot (a freed slot, a late confirmation): the pre-roll
     * still covers the event start when it began less than the pre-roll ago, otherwise the stream starts live.
     */
    private resume(core: CameraCore, ev: EventRecord) {
        const seg = ev.extra.segments as SegInfo;
        if (ev.state !== 'open' || this.segmenters.has(ev.id) || !this.store.reopen(ev.id)) return;
        this.busy.delete(ev.id);
        const prerollMs = this.num('prerollS') * 1000;
        const elapsed = this.host.clock.now() - ev.startRecv;
        seg.reason = 'resumed';
        this.startSegments(core, ev, seg, elapsed <= prerollMs ? prerollMs : 0, seg.inits > 0);
        core.tracker.touch();
    }

    /** A slot freed: the newest open busy event that still deserves video gets it. */
    private fillSlots() {
        while (this.segmenters.size < MAX_SEGMENTERS) {
            const ev = [...this.busy].map(id => this.recent.get(id)).filter((e): e is EventRecord => !!e && e.state === 'open'
                && e.subjects.some(s => s.verdict === 'pending' || s.verdict === 'confirmed' || s.verdict === 'unverified'))
                .sort((a, b) => b.startRecv - a.startRecv)[0];
            if (!ev) return;
            const core = this.cores.get(ev.camera.id);
            this.busy.delete(ev.id);
            if (core) this.resume(core, ev);
        }
    }

    private startSegments(core: CameraCore, ev: EventRecord, seg: SegInfo, prerollOverride?: number, resumed = false) {
        seg.state = 'running';
        const opts = this.optionsFor(core.id);
        const prerollMs = prerollOverride ?? this.num('prerollS') * 1000;
        const r = { core, ev, seg, started: this.host.clock.now(), offset: 0, mediaEnd: 0, resumeFirst: resumed } as Running;
        r.handle = startSegmenter({
            segmentS: this.num('segmentS'),
            // without a prebuffer the request is the same; the stream simply starts live
            prerollMs: opts && opts.prebufferMs <= 0 ? 0 : prerollMs,
            console: this.host.console,
            label: core.env.name(),
            restarts: SEGMENTER_RESTARTS,
            allowDefault: () => this.segmenters.size < DEFAULT_STREAM_LIMIT,
            onInit: init => {
                r.initBuf = init;
                const k = this.store.setInit(r.ev.id, init);
                if (k !== undefined) { r.seg.init = true; r.seg.inits = k + 1; }
            },
            onSegment: s => {
                r.mediaEnd = s.start + s.duration;
                const disc = !!s.discontinuity || !!r.resumeFirst;
                r.resumeFirst = false;
                const st = this.store.addSegment(r.ev.id, s.data, s.start - r.offset, s.duration, disc);
                // a segment the store did not take is never announced
                if (!st) return;
                r.seg.next = st.n + 1;
                this.bus.publish('event.segment', {
                    event_id: r.ev.id, camera_id: core.id, kind: 'cut', last: false,
                    segment: { n: st.n, init: st.init, discontinuity: st.discontinuity, start_s: round3(st.start), duration_s: round3(st.duration), bytes: st.data.length, cut_at: st.cut_at, preroll: !!s.preroll },
                });
            },
            onMark: (m, at) => { const lat = r.ev.latency as any; if (lat[m] === undefined) lat[m] = at; },
            onPreroll: ms => { r.seg.preroll_ms = Math.min(ms, Math.max(prerollMs, 0) + 2000); },
            onRestart: () => { r.seg.restarts++; if (r.ev.state === 'open') r.core.tracker.touch(); },
            onEnd: failure => this.segmentsEnded(r, failure),
        }, { ...this.ffmpegDeps(), input: (pre, lowRes) => this.host.streamInput(core.id, pre, lowRes) });
        this.segmenters.set(ev.id, r);
    }

    private segmentsEnded(r: Running, failure: string | undefined) {
        if (r.handoverTimer !== undefined) { this.host.clock.clearTimeout(r.handoverTimer); r.handoverTimer = undefined; }
        for (const [k, v] of this.handovers) if (v === r) this.handovers.delete(k);
        if (this.segmenters.get(r.ev.id) === r) this.segmenters.delete(r.ev.id);
        this.stopping.delete(r);
        // a newer segmenter already took over this event (resumed): it owns the store entry and the state
        if (this.segmenters.has(r.ev.id)) { this.fillSlots(); return; }
        const failed = !!failure && failure !== 'stopped';
        this.store.finish(r.ev.id, failed ? failure : undefined);
        r.seg.state = r.released || r.preempted ? 'released' : failed ? 'failed' : 'ended';
        r.seg.reason = r.preempted ? 'preempted' : r.released ? 'not_confirmed' : failed ? failure! : null;
        if (failed) this.host.console.warn(`${r.core.env.name()}: segmenter: ${failure}`);
        // `last` only once the event is closed: an open event may still get segments again (resume)
        if (r.ev.state === 'closed') this.publishLast(r.ev);
        else r.core.tracker.touch();
        this.fillSlots();
    }

    /** The final `event.segment` of an event: no segment will follow. */
    private publishLast(ev: EventRecord) {
        const seg = ev.extra.segments as SegInfo | undefined;
        if (!seg || (seg as any).lastSent) return;
        Object.defineProperty(seg, 'lastSent', { value: true, enumerable: false });
        this.bus.publish('event.segment', {
            event_id: ev.id, camera_id: ev.camera.id, kind: 'last', last: true, segment: null,
            next: seg.next, state: seg.state, reason: seg.reason,
        });
    }

    /** Segments dropped for memory: announced, so the integration records the gap without waiting. */
    private evicted(id: string, ns: number[]) {
        const ev = this.recent.get(id);
        const seg = ev?.extra.segments as SegInfo | undefined;
        if (seg) seg.evicted += ns.length;
        // announced per event at most once a second (batched)
        const now = this.host.clock.now();
        const p = this.evictPending.get(id) ?? { ns: [], last: -Infinity };
        this.evictPending.set(id, p);
        p.ns.push(...ns);
        const flush = () => {
            p.timer = undefined;
            if (!p.ns.length) return;
            p.last = this.host.clock.now();
            const list = p.ns.sort((a, b) => a - b);
            p.ns = [];
            this.bus.publish('event.segment', { event_id: id, camera_id: ev?.camera.id ?? null, kind: 'evicted', last: false, segment: null, evicted: list });
            if (this.evictPending.size > RECENT_EVENTS) this.evictPending.delete(this.evictPending.keys().next().value!);
        };
        if (now - p.last >= EVICT_ANNOUNCE_MS) flush();
        else if (p.timer === undefined) p.timer = this.host.clock.setTimeout(flush, p.last + EVICT_ANNOUNCE_MS - now);
        if (ev?.state === 'open') this.cores.get(ev.camera.id)?.tracker.touch();
        if (now - this.evictWarnAt >= WARN_EVERY_MS) {
            this.evictWarnAt = now;
            this.host.console.warn(`segments: memory full (${Math.round(this.store.cfg.maxBytes / 1048576)} MB), the oldest segments are dropped (logged at most once a minute)`);
        }
    }

    /**
     * Makes room for a new event: stops the oldest segmenter (running at least MIN_VICTIM_AGE_MS) whose event is not worth
     * keeping: no confirmed subject, no unverified one with a class (motion-only unverified does not count), none pending,
     * and no keeper in the capped event it continues.
     */
    private preempt(): boolean {
        const now = this.host.clock.now();
        const victim = [...this.segmenters.values()]
            .filter(r => r.ev.subjects.length > 0 && now - r.started >= MIN_VICTIM_AGE_MS
                && !r.ev.subjects.some(s => s.verdict === 'pending') && !this.isKeeper(r.ev, false))
            .sort((a, b) => a.started - b.started)[0];
        if (!victim) return false;
        victim.preempted = true;
        victim.seg.state = 'released';
        victim.seg.reason = 'preempted';
        this.segmenters.delete(victim.ev.id);
        this.stopping.add(victim);
        victim.handle.stop();
        return true;
    }

    /** A segmenter whose event still has no confirmed or unverified subject after RELEASE_AFTER_MS gives its slot back. */
    private releaseIdleSlots() {
        const now = this.host.clock.now();
        for (const r of [...this.segmenters.values()]) {
            if (r.released || now - r.started < RELEASE_AFTER_MS || this.isKeeper(r.ev)) continue;
            if (r.ev.subjects.some(s => s.verdict === 'pending')) continue;
            r.released = true;
            r.seg.state = 'released';
            r.seg.reason = 'not_confirmed';
            this.segmenters.delete(r.ev.id);
            this.stopping.add(r);
            r.handle.stop();
        }
        this.fillSlots();
    }

    /** The capped event's segmenter now cuts for its continuation: same ffmpeg, same init, segments numbered from 0. */
    private handOver(r: Running, ev: EventRecord, seg: SegInfo) {
        const old = r.ev;
        this.handovers.delete(old.id);
        if (r.handoverTimer !== undefined) { this.host.clock.clearTimeout(r.handoverTimer); r.handoverTimer = undefined; }
        this.store.finish(old.id);
        (old.extra.segments as SegInfo).state = 'ended';
        this.publishLast(old);
        r.ev = ev;
        r.seg = seg;
        r.started = this.host.clock.now();
        r.offset = r.mediaEnd;
        seg.state = 'running';
        seg.reason = 'continued';
        if (r.initBuf) {
            const k = this.store.setInit(ev.id, r.initBuf);
            if (k !== undefined) { seg.init = true; seg.inits = 1; }
        }
        this.segmenters.set(ev.id, r);
    }

    eventEnded(core: CameraCore, ev: EventRecord) {
        this.remember(ev);
        const r = this.segmenters.get(ev.id);
        if (r) {
            if (ev.end_reason === 'cap') {
                // keep cutting for the continuation, if one opens within the margin
                this.segmenters.delete(ev.id);
                this.handovers.set(ev.id, r);
                r.handoverTimer = this.host.clock.setTimeout(() => {
                    r.handoverTimer = undefined;
                    if (this.handovers.get(ev.id) === r) { this.handovers.delete(ev.id); r.handle.stop(); }
                }, this.episodeConfig().marginMs);
            }
            else r.handle.stop();
        }
        else if (!this.handovers.has(ev.id) && ![...this.stopping].some(x => x.ev === ev)) this.publishLast(ev);
        this.busy.delete(ev.id);
        const pending = this.thumbs.get(core.id) ?? Promise.resolve();
        pending.finally(() => this.store.closeThumbnail(ev.id));
        this.confirmedThumb.delete(ev.id);
    }

    subjectDecided(core: CameraCore, ev: EventRecord, s: Subject) {
        if (s.verdict === 'confirmed' && !this.confirmedThumb.has(ev.id) && ev.state === 'open') {
            this.confirmedThumb.add(ev.id);
            this.thumbnail(core, ev, 'confirmation');
        }
        this.isKeeper(ev);
        // a late confirmation (vetoed -> confirmed) of an event whose segmenter was released: segments again, live
        if (s.verdict === 'confirmed' && ev.state === 'open' && (ev.extra.segments as SegInfo | undefined)?.state === 'released'
            && this.segmenters.size < MAX_SEGMENTERS)
            this.resume(core, ev);
        if (s.verdict === 'vetoed') this.releaseIdleSlots();
    }

    private remember(ev: EventRecord) {
        this.recent.delete(ev.id);
        this.recent.set(ev.id, ev);
        while (this.recent.size > RECENT_EVENTS) this.recent.delete(this.recent.keys().next().value!);
    }

    /** Recent events that started, or ended, at or after `since`; open events always. Oldest first. */
    eventsSince(since: number, camera?: string): Record<string, unknown>[] {
        return [...this.recent.values()]
            .filter(ev => (!camera || ev.camera.id === camera) && (ev.state === 'open' || ev.start >= since || (ev.end ?? 0) >= since))
            .sort((a, b) => a.start - b.start)
            .map(ev => publicEvent(ev) as any);
    }

    /** A snapshot slot; a released slot passes straight to the next waiter (no window where a newcomer jumps the queue). */
    private async snapshotSlot(): Promise<() => void> {
        if (this.snapshotsBusy < SNAPSHOT_SLOTS) this.snapshotsBusy++;
        else await new Promise<void>(res => this.snapshotWaiters.push(res));
        let done = false;
        return () => {
            if (done) return;
            done = true;
            const next = this.snapshotWaiters.shift();
            if (next) next();
            else this.snapshotsBusy--;
        };
    }

    /**
     * One picture at a time per camera and SNAPSHOT_SLOTS for all cameras; a confirmation picture waits for the start
     * picture, and a start picture still waiting when the confirmation is asked is skipped.
     */
    private thumbnail(core: CameraCore, ev: EventRecord, kind: 'start' | 'confirmation') {
        if (!this.host.interfaces(core.id).includes('Camera')) return;
        const prev = this.thumbs.get(core.id) ?? Promise.resolve();
        const queuedAt = this.host.clock.now();
        const run = prev.catch(() => { }).then(async () => {
            const release = await this.snapshotSlot();
            try {
                // a start picture is useless once a confirmation picture is asked, the event closed, or it waited too long
                if (kind === 'start' && (this.confirmedThumb.has(ev.id) || ev.state === 'closed' || this.host.clock.now() - queuedAt > START_PICTURE_MAX_WAIT_MS)) return;
                let jpeg = await withTimeout(this.host.picture(core.id, THUMB_WIDTH), THUMB_TIMEOUT_MS, 'snapshot');
                if (!jpeg?.length) return;
                const size = jpegSize(jpeg);
                if (!size || size.w > THUMB_WIDTH * 1.25)
                    jpeg = await scaleJpeg((c, a, o) => this.host.spawn(c, a, o), await this.host.ffmpegPath(), jpeg, THUMB_WIDTH);
                if (jpeg.length > MAX_THUMB_BYTES) throw new Error(`thumbnail too large (${jpeg.length} bytes)`);
                if (this.store.setThumbnail(ev.id, jpeg, kind)) {
                    const lat = ev.latency as any;
                    if (lat.thumbnail_at === undefined) lat.thumbnail_at = this.host.clock.now();
                    ev.extra.thumbnail = { available: true, kind };
                    if (ev.state === 'open') core.tracker.touch();
                }
            }
            finally { release(); }
        }).catch(e => this.host.console.warn(`${core.env.name()}: thumbnail failed: ${errText(e)}`));
        this.thumbs.set(core.id, run);
        run.finally(() => { if (this.thumbs.get(core.id) === run) this.thumbs.delete(core.id); });
    }

    private ffmpegDeps(): Omit<SegmenterDeps, 'input'> {
        return { ffmpegPath: () => this.host.ffmpegPath(), spawn: (c, a, o) => this.host.spawn(c, a, o), now: () => this.host.clock.now() };
    }

    /** Cached stream options (prebuffer, audio); refreshed in the background when stale. */
    private optionsFor(id: string): StreamOptionsInfo | undefined {
        const now = this.host.clock.now();
        let o = this.options.get(id);
        if (!o) { o = { at: -Infinity }; this.options.set(id, o); }
        if (now - o.at > OPTIONS_TTL_MS && !o.pending) {
            const entry = o;
            entry.pending = withTimeout(this.host.streamOptions(id), 10_000, 'stream options')
                .then(info => { entry.info = info; entry.at = this.host.clock.now(); })
                .catch(e => { entry.at = this.host.clock.now() - OPTIONS_TTL_MS + 30_000; this.host.console.warn(`stream options: ${errText(e)}`); })
                .finally(() => { entry.pending = undefined; });
        }
        return o.info;
    }

    async refreshOptions(): Promise<void> {
        for (const id of this.cores.keys()) this.optionsFor(id);
        await Promise.all([...this.options.values()].map(o => o.pending).filter(Boolean));
    }

    camerasInfo() {
        return [...this.cores.values()].map(c => {
            const ifs = this.host.interfaces(c.id);
            const st = c.env.state(c.id);
            const opt = this.optionsFor(c.id);
            const verifiers = c.verifiers();
            return {
                id: c.id,
                name: c.env.name(),
                plugin: st?.pluginId ?? null,
                online: typeof st?.online === 'boolean' ? st.online : null,
                open_event: c.tracker.current?.id ?? null,
                capabilities: {
                    object_detector: ifs.includes('ObjectDetector'),
                    motion_sensor: ifs.includes('MotionSensor'),
                    occupancy_sensor: ifs.includes('OccupancySensor'),
                    audio_sensor: ifs.includes('AudioSensor'),
                    snapshot: ifs.includes('Camera'),
                    prebuffer: opt ? opt.prebufferMs > 0 : null,
                    prebuffer_ms: opt ? opt.prebufferMs : null,
                    video_recorder: ifs.includes('VideoRecorder'),
                    video_clips: ifs.includes('VideoClips'),
                    audio: opt ? opt.audio : null,
                    detections: c.detectionStats.total,
                    split: c.isSplit(),
                    views: c.views(),
                    parent: c.parentId() ?? null,
                    verifier_present: verifiers.length > 0,
                    verifier_live: c.verifierLive(),
                    verifiers,
                    silent_verifiers: c.silentVerifiers(),
                },
                sources: c.sources(),
                context_sensors: c.contextSensors(),
            };
        });
    }

    private cameraApi(id: string): CameraApi | undefined {
        if (!this.cores.has(id)) return undefined;
        const ifs = this.host.interfaces(id);
        return {
            id,
            hasRecorder: ifs.includes('VideoRecorder'),
            hasClips: ifs.includes('VideoClips'),
            clips: (s, e) => withTimeout(this.host.clips(id, s, e), 20_000, 'clips'),
            recordingInput: (t, d) => withTimeout(this.host.recordingInput(id, t, d), 15_000, 'recording stream'),
        };
    }

    health() {
        return {
            version: VERSION,
            epoch: this.bus.epoch,
            last_seq: this.bus.lastSeq,
            uptime_s: Math.round((this.host.clock.now() - this.started) / 1000),
            cameras: this.cores.size,
            open_events: [...this.cores.values()].filter(c => c.tracker.current).length,
            segment_bytes: this.store.bytes,
            segment_bytes_max: this.store.cfg.maxBytes,
            segmenters: this.segmenters.size + this.handovers.size,
            ring_bytes: this.bus.bytes,
            playback_sessions: this.playback.count,
        };
    }
}

function round3(v: number) { return Math.round(v * 1000) / 1000; }

export { realClock };
