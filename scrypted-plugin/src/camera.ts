// Per-camera core: listens to the camera (and its child devices), normalises, feeds the episode tracker, keeps context
// features, and tells the provider (sink) what to publish. Every Scrypted access goes through CameraEnv, so tests use fakes.

import { DEFAULT_EPISODE, EpisodeConfig, EpisodeTracker, EventRecord, publicEvent, publicObservation } from './episode';
import { publicSubject, Subject } from './fusion';
import { attribute, fromBinary, fromDetections, NormalizeContext, Observation } from './normalize';
import { RolesTable } from './roles';
import { Clock, errText } from './util';

export type Iface = 'ObjectDetector' | 'MotionSensor' | 'OccupancySensor' | 'AudioSensor' | 'BinarySensor' | 'Online';

export interface DeviceState {
    name?: string;
    pluginId?: string;
    nativeId?: string;
    /** the device that provides it (a crop camera's parent camera) */
    providerId?: string;
    interfaces: string[];
    mixins?: string[];
    motionDetected?: boolean;
    occupied?: boolean;
    audioDetected?: boolean;
    binaryState?: boolean;
    online?: boolean;
}

export interface Details { mixinId?: string; eventTime?: number }

export interface CameraEnv {
    id: string;
    name(): string;
    clock: Clock;
    roles: RolesTable;
    console: Pick<Console, 'log' | 'warn'>;
    pluginOf(deviceId: string): string | undefined;
    state(deviceId: string): DeviceState | undefined;
    /** ids of the camera's child devices (providerId = camera) */
    children(): string[];
    /**
     * Listens to one interface of a device. `layer`: listen to the events of one mixin layer only (its provider id, or the
     * device id for the device itself) when the server supports it.
     */
    listen(deviceId: string, iface: Iface, cb: (details: Details, data: any) => void, layer?: string): () => void;
    storageGet(key: string): string | null;
    storageSet(key: string, value: string): void;
    episode(): EpisodeConfig;
    /** this plugin's id: its own mixin layer is not listened to */
    selfPlugin?: string;
}

export interface CameraSink {
    publish(type: 'event.start' | 'event.update' | 'event.end' | 'subject.verdict', body: Record<string, unknown>): void;
    eventStarted(core: CameraCore, ev: EventRecord): void;
    eventEnded(core: CameraCore, ev: EventRecord): void;
    subjectDecided(core: CameraCore, ev: EventRecord, s: Subject): void;
}

/** A verifier counts as present on a camera when it emitted there within this long. */
export const VERIFIER_RECENT_MS = 7 * 24 * 3600_000;
/** A verifier is live when it showed activity (on this camera, or on any camera) within this long. */
export const VERIFIER_LIVE_MS = 10 * 60_000;
const VERIFIERS_KEY = 'stamina:verifiers';
const SOURCES_KEY = 'stamina:sources';
/** binary sensor child names that map to a context feature */
export const FEATURES: [RegExp, string][] = [[/night/i, 'night_mode'], [/tamper/i, 'tamper']];
const MAX_CHANGES = 20;
/** a plain-listener property event waits this long for the same edge from a layer listener before it counts on its own */
export const PLAIN_DEDUPE_MS = 300;
/** a layer edge and a plain edge with the same value this close together are the same event */
const SAME_EDGE_MS = 1000;
type BinaryKind = 'motion' | 'occupancy' | 'audio';
const KIND_IFACE: Record<BinaryKind, Iface> = { motion: 'MotionSensor', occupancy: 'OccupancySensor', audio: 'AudioSensor' };

/** A mirror camera repeats another camera's view (for another Home): never observed, so no duplicate events. */
export function isMirror(st: Pick<DeviceState, 'nativeId'> | undefined): boolean {
    return /-mirror-/i.test(String(st?.nativeId ?? ''));
}

export type Feature = 'night_mode' | 'tamper' | 'motion' | 'occupied' | 'audio' | 'online';

export interface ContextSensor { id: string; name: string; kind: 'binary' | 'occupancy'; feature: string | null; on: boolean | null }

export class CameraCore {
    readonly tracker: EpisodeTracker;
    private unlisten: (() => void)[] = [];
    private boundKey = '';
    private verifiersSeen: Record<string, number> = {};
    private sourcesSeen: Record<string, number> = {};
    private ctxStart: Record<string, boolean | null> | undefined;
    private ctxChanges: { t: number; feature: string | null; id: string; name: string; on: boolean }[] = [];
    private persistTimer: any;
    private stopped = false;
    /** recent edges from layer listeners, for deduplicating the plain listener */
    private layerEdges: { kind: BinaryKind; on: boolean; at: number }[] = [];
    private plainTimers = new Set<any>();
    /** a plain `on` edge was counted for this kind: its `off` is always processed */
    private plainOn = new Set<BinaryKind>();
    /** exact last activity per verifier source on this camera (detections and signals) */
    private verifierActivity: Record<string, number> = {};
    /** child device ids, refreshed on every bind */
    private kids: string[] = [];

    constructor(public env: CameraEnv, private sink: CameraSink) {
        try { this.verifiersSeen = JSON.parse(env.storageGet(VERIFIERS_KEY) || '{}') || {}; } catch { this.verifiersSeen = {}; }
        try { this.sourcesSeen = JSON.parse(env.storageGet(SOURCES_KEY) || '{}') || {}; } catch { this.sourcesSeen = {}; }
        // liveness survives a plugin restart: the last persisted detection of each verifier (minute precision)
        for (const [src, t] of Object.entries(this.verifiersSeen)) if (typeof t === 'number') this.verifierActivity[src] = t;
        this.tracker = new EpisodeTracker(env.episode() ?? DEFAULT_EPISODE, { clock: env.clock, camera: { id: env.id, name: env.name() }, verifierPresent: () => this.verifierPresent(), verifierLive: () => this.verifierLive() }, {
            onStart: (ev, obs) => {
                this.ctxStart = this.context();
                this.ctxChanges = [];
                this.decorate(ev);
                sink.eventStarted(this, ev);
                sink.publish('event.start', { event: publicEvent(ev), observations: obs.map(publicObservation) });
            },
            onUpdate: (ev, obs) => {
                this.decorate(ev);
                sink.publish('event.update', { event: publicEvent(ev), observations: obs.map(publicObservation) });
            },
            onVerdict: (ev, s) => {
                sink.publish('subject.verdict', { event_id: ev.id, camera_id: env.id, subject: publicSubject(s) });
                sink.subjectDecided(this, ev, s);
            },
            onEnd: ev => {
                this.decorate(ev);
                // event.end first: what the sink publishes about the end (the final event.segment) follows it
                sink.publish('event.end', { event: publicEvent(ev) });
                sink.eventEnded(this, ev);
                this.ctxStart = undefined;
            },
        });
    }

    get id() { return this.env.id; }
    get cameraPlugin(): string { return this.env.state(this.env.id)?.pluginId ?? 'camera'; }

    /** (Re)binds the listeners when the camera's children or mixins changed. */
    bind() {
        if (this.stopped) return;
        const st = this.env.state(this.env.id);
        if (!st) return;
        const kids = this.env.children();
        this.kids = kids;
        const parent = this.parentId();
        const key = JSON.stringify([st.interfaces, st.mixins ?? [], kids, parent ?? null]);
        if (key === this.boundKey) return;
        this.unbind();
        this.boundKey = key;
        const id = this.env.id;
        const on = (dev: string, iface: Iface, fn: (d: Details, data: any) => void) => {
            try { this.unlisten.push(this.env.listen(dev, iface, (d, data) => { if (!this.stopped) this.safe(() => fn(d || {}, data)); })); }
            catch (e) { this.env.console.warn(`${this.env.name()}: cannot listen to ${iface}: ${errText(e)}`); }
        };
        const has = (s: DeviceState | undefined, i: string) => !!s?.interfaces?.includes(i);
        // ObjectDetector events are not masked by mixins and carry sourceId: one plain listener (spike S1)
        if (has(st, 'ObjectDetector')) on(id, 'ObjectDetector', (d, data) => this.onDetections(d, data));
        // a crop camera: its parent's whole-frame AI applies to it as ambiguous evidence (nothing else of the parent does)
        if (parent && has(this.env.state(parent), 'ObjectDetector')) on(parent, 'ObjectDetector', (d, data) => this.onDetections(d, data, parent));
        // property events (motion, occupancy, audio) carry no mixinId and a mixin that owns the interface masks the camera's
        // own value: listen per layer (the device itself, then each mixin), attributed to that layer's plugin; the plain
        // listener is the fallback for a layer that cannot be listened to, deduplicated against the layer edges
        const layers = [id, ...(st.mixins ?? []).filter(m => m !== id && this.env.pluginOf(m) !== this.env.selfPlugin)];
        for (const kind of ['motion', 'occupancy', 'audio'] as BinaryKind[]) {
            const iface = KIND_IFACE[kind];
            if (!has(st, iface)) continue;
            for (const layer of layers) {
                const src = layer === id ? this.cameraPlugin : (this.env.pluginOf(layer) ?? this.cameraPlugin);
                try {
                    this.unlisten.push(this.env.listen(id, iface, (_d, v) => {
                        if (this.stopped) return;
                        this.safe(() => {
                            this.layerEdges.push({ kind, on: !!v, at: this.env.clock.now() });
                            if (this.layerEdges.length > 64) this.layerEdges.splice(0, this.layerEdges.length - 64);
                            this.onBinary(kind, `${kind}:${layer}`, {}, !!v, id, src);
                        });
                    }, layer));
                }
                catch { /* this server cannot listen per layer: the plain listener covers it */ }
            }
            on(id, iface, (d, v) => this.onPlainBinary(kind, d, !!v));
        }
        if (has(st, 'Online')) on(id, 'Online', (_d, v) => { this.tracker.setOffline(v === false); this.contextChanged(id, 'online', v !== false); });
        for (const k of kids) {
            const ks = this.env.state(k);
            if (!ks) continue;
            // a child camera (a view: crop or mirror) has its own mixin: not ours to observe
            if (has(ks, 'VideoCamera')) continue;
            if (has(ks, 'OccupancySensor')) on(k, 'OccupancySensor', (d, v) => this.onBinary('occupancy', `occupancy:${k}`, d, !!v, k));
            if (has(ks, 'BinarySensor')) on(k, 'BinarySensor', (_d, v) => this.contextChanged(k, featureOf(ks.name), !!v));
            if (has(ks, 'ObjectDetector')) on(k, 'ObjectDetector', (d, data) => this.onDetections(d, data));
        }
        this.resync(st);
    }

    /**
     * After (re)binding, edges may have been missed: holds were released by unbind. A signal still on while an event is open
     * is held again (from the device's current state); nothing starts a new event here.
     */
    private resync(st: DeviceState) {
        if (!this.tracker.current) return;
        const state: Record<BinaryKind, boolean | undefined> = { motion: st.motionDetected, occupancy: st.occupied, audio: st.audioDetected };
        for (const kind of ['motion', 'occupancy', 'audio'] as BinaryKind[])
            if (state[kind] === true && st.interfaces.includes(KIND_IFACE[kind]))
                this.safe(() => this.onBinary(kind, `${kind}:sync`, {}, true, this.env.id));
    }

    /** Removes the listeners and releases every held signal (an `off` edge may be missed while unbound). */
    unbind() {
        for (const u of this.unlisten) { try { u(); } catch { /* gone */ } }
        this.unlisten = [];
        this.boundKey = '';
        for (const t of this.plainTimers) this.env.clock.clearTimeout(t);
        this.plainTimers.clear();
        this.plainOn.clear();
        this.tracker.releaseHolds();
    }

    /** The mixin was re-created for the same camera: keep the tracker and the open event, rebind with the new mixin's env. */
    setEnv(env: CameraEnv) {
        if (this.stopped) return;
        this.unbind();
        this.env = env;
        this.bind();
    }

    /**
     * A property edge from the plain listener: the outermost layer's value. It counts only when no layer listener reported
     * the same edge around it (then it came from a layer we could not listen to, attributed to its mixinId or the camera plugin).
     */
    onPlainBinary(kind: BinaryKind, d: Details, on: boolean) {
        // the off edge of a plain `on` that was counted always counts: the hold must end
        if (!on && this.plainOn.has(kind)) {
            this.plainOn.delete(kind);
            this.safe(() => this.onBinary(kind, `${kind}:plain`, d, false, this.env.id));
            return;
        }
        const at = this.env.clock.now();
        const t = this.env.clock.setTimeout(() => {
            this.plainTimers.delete(t);
            if (this.stopped) return;
            if (this.layerEdges.some(e => e.kind === kind && e.on === on && Math.abs(e.at - at) <= SAME_EDGE_MS)) return;
            if (on) this.plainOn.add(kind);
            this.safe(() => this.onBinary(kind, `${kind}:plain`, d, on, this.env.id));
        }, PLAIN_DEDUPE_MS);
        this.plainTimers.add(t);
    }

    stop() {
        this.unbind();
        this.tracker.stop();
        this.persist();
        this.stopped = true;
    }

    private safe(fn: () => void) {
        try { fn(); }
        catch (e) { this.env.console.warn(`${this.env.name()}: event handling failed: ${errText(e)}`); }
    }

    private ctx(): NormalizeContext {
        return { roles: this.env.roles, cameraPlugin: this.cameraPlugin, now: this.env.clock.now() };
    }

    /** The parent camera, when this camera is a view (crop) of another camera. */
    parentId(): string | undefined {
        const p = this.env.state(this.env.id)?.providerId;
        return p && p !== this.env.id && this.env.state(p)?.interfaces?.includes('VideoCamera') ? p : undefined;
    }

    /** The views of this camera: its VideoCamera children that are not mirrors (crops). */
    views(): string[] {
        return this.kids.filter(k => this.env.state(k)?.interfaces?.includes('VideoCamera') && !isMirror(this.env.state(k))).sort();
    }

    /** A split camera: its frame is shared with views (it has VideoCamera children), or it is a view of another camera. */
    isSplit(): boolean {
        return !!this.parentId() || this.kids.some(k => this.env.state(k)?.interfaces?.includes('VideoCamera'));
    }

    /**
     * Detections of this camera, or (`fromParent`) of its parent camera. On a split camera a box-less detection from the camera's
     * own plugin (or the parent's) covers the whole stitched frame: it is ambiguous, evidence only for a view-specific subject.
     * From the parent only those ambiguous ones are taken (the parent's other detections belong to the parent's own view).
     */
    onDetections(d: Details, data: any, fromParent?: string) {
        if (!data || typeof data !== 'object') return;
        const ctx = this.ctx();
        const ownPlugin = fromParent ? (this.env.state(fromParent)?.pluginId ?? ctx.cameraPlugin) : ctx.cameraPlugin;
        const source = attribute({ mixinId: d.mixinId, sourceId: typeof data.sourceId === 'string' ? data.sourceId : undefined }, x => this.env.pluginOf(x), ownPlugin);
        const split = this.isSplit();
        const parentPlugin = this.parentId() ? this.env.state(this.parentId()!)?.pluginId : undefined;
        const obs = fromDetections(data, source, d.eventTime, { ...ctx, cameraPlugin: ownPlugin });
        for (const o of obs) {
            if (split && !o.box && o.role !== 'enricher' && (o.source === ctx.cameraPlugin || o.source === parentPlugin)) {
                o.ambiguous = true;
                o.role = 'trigger';
            }
            if (fromParent && !o.ambiguous) continue;
            this.feed(o);
        }
    }

    onBinary(kind: BinaryKind, key: string, d: Details, on: boolean, device: string, layerSource?: string) {
        const ctx = this.ctx();
        // property events carry no mixinId in practice (spike S1): the layer's plugin, else the device's own plugin
        const owner = layerSource ?? (device === this.env.id ? ctx.cameraPlugin : (this.env.state(device)?.pluginId ?? ctx.cameraPlugin));
        const source = layerSource ?? attribute({ mixinId: d.mixinId }, x => this.env.pluginOf(x), owner);
        const o = fromBinary(kind, on, source, d.eventTime, ctx);
        // any signal (on or off) from a verifier source is a sign of life
        if (this.env.roles.get(source)?.role === 'verifier') this.verifierActivity[source] = ctx.now;
        const feature = kind === 'occupancy' ? 'occupied' : kind;
        this.contextChanged(device, device === this.env.id ? feature : featureOf(this.env.state(device)?.name) ?? feature, on);
        if (o) { this.noteSource(o); this.tracker.hold(key, true, o); }
        else if (!on) {
            this.tracker.hold(key, false);
            // holds without a layer of their own (resynced from the state, or from the plain listener) end with any off
            if (device === this.env.id) {
                this.tracker.hold(`${kind}:sync`, false);
                if (key !== `${kind}:plain`) { this.tracker.hold(`${kind}:plain`, false); this.plainOn.delete(kind); }
            }
        }
    }

    private feed(o: Observation) {
        this.noteSource(o);
        this.tracker.observe(o);
    }

    private noteSource(o: Observation) {
        const now = this.env.clock.now();
        let dirty = false;
        if (o.role === 'verifier') this.verifierActivity[o.source] = now;
        // presence counts detections only: a classless signal never makes a source a verifier of this camera
        if (o.role === 'verifier' && o.kind === 'object' && now - (this.verifiersSeen[o.source] ?? 0) > 60_000) { this.verifiersSeen[o.source] = now; dirty = true; }
        if (now - (this.sourcesSeen[o.source] ?? 0) > 60_000) {
            if (this.sourcesSeen[o.source] !== undefined || Object.keys(this.sourcesSeen).length < 32) { this.sourcesSeen[o.source] = now; dirty = true; }
        }
        if (dirty && this.persistTimer === undefined)
            this.persistTimer = this.env.clock.setTimeout(() => { this.persistTimer = undefined; this.persist(); }, 5000);
    }

    private persist() {
        if (this.persistTimer !== undefined) { this.env.clock.clearTimeout(this.persistTimer); this.persistTimer = undefined; }
        try {
            this.env.storageSet(VERIFIERS_KEY, JSON.stringify(this.verifiersSeen));
            this.env.storageSet(SOURCES_KEY, JSON.stringify(this.sourcesSeen));
        }
        catch { /* storage gone */ }
    }

    /** plugins of the camera's mixins (other than ours) */
    private mixinPlugins(): Set<string> {
        const out = new Set<string>();
        for (const m of this.env.state(this.env.id)?.mixins ?? []) {
            const p = this.env.pluginOf(m);
            if (p && p !== this.env.selfPlugin) out.add(p);
        }
        return out;
    }

    /**
     * Verifier sources (role verifier now) that emitted a detection on this camera in the last 7 days and are still among
     * its mixins (a verifier removed from the camera is no longer present).
     */
    verifiers(): string[] {
        const now = this.env.clock.now();
        const mixins = this.mixinPlugins();
        return Object.entries(this.verifiersSeen)
            .filter(([s, t]) => now - t <= VERIFIER_RECENT_MS && this.env.roles.get(s)?.role === 'verifier' && mixins.has(s))
            .map(([s]) => s).sort();
    }

    verifierPresent(): boolean {
        return this.verifiers().length > 0;
    }

    /**
     * A present verifier is live when it showed activity (a detection or a signal) on THIS camera within VERIFIER_LIVE_MS
     * (it is a mixin here, so activity elsewhere says nothing about this camera). A subject a live verifier did not confirm is
     * vetoed; with no live verifier it is unverified.
     */
    verifierLive(): boolean {
        const now = this.env.clock.now();
        return this.verifiers().some(s => now - (this.verifierActivity[s] ?? -Infinity) <= VERIFIER_LIVE_MS);
    }

    /** Verifier plugins that are mixins on the camera but have not emitted recently (e.g. Coral Guard in Shadow mode). */
    silentVerifiers(): string[] {
        const st = this.env.state(this.env.id);
        const seen = new Set(this.verifiers());
        const out = new Set<string>();
        for (const m of st?.mixins ?? []) {
            const p = this.env.pluginOf(m);
            if (!p || seen.has(p)) continue;
            const row = this.env.roles.get(p);
            if (row?.role === 'verifier' || (!row && /coral-guard|objectdetector|openvino|coreml|onnx|tensorflow|ncnn/.test(p))) out.add(p);
        }
        return [...out].sort();
    }

    sources() {
        return Object.entries(this.sourcesSeen).map(([source, last_seen]) => {
            const r = this.env.roles.get(source);
            return { source, role: r?.role ?? null, inferred: r?.inferred ?? null, last_seen };
        }).sort((a, b) => a.source.localeCompare(b.source));
    }

    contextSensors(): ContextSensor[] {
        const out: ContextSensor[] = [];
        for (const k of this.kids) {
            const s = this.env.state(k);
            if (!s || s.interfaces.includes('VideoCamera')) continue;
            if (s.interfaces.includes('BinarySensor')) out.push({ id: k, name: s.name ?? k, kind: 'binary', feature: featureOf(s.name), on: typeof s.binaryState === 'boolean' ? s.binaryState : null });
            else if (s.interfaces.includes('OccupancySensor')) out.push({ id: k, name: s.name ?? k, kind: 'occupancy', feature: null, on: typeof s.occupied === 'boolean' ? s.occupied : null });
        }
        return out;
    }

    /** Current context features; null where the camera has no such signal. */
    context(): Record<Feature, boolean | null> {
        const st = this.env.state(this.env.id);
        const has = (i: string) => !!st?.interfaces?.includes(i);
        const b = (v: unknown) => typeof v === 'boolean' ? v : null;
        const out: Record<Feature, boolean | null> = {
            night_mode: null, tamper: null,
            motion: has('MotionSensor') ? b(st!.motionDetected) ?? false : null,
            occupied: has('OccupancySensor') ? b(st!.occupied) ?? false : null,
            audio: has('AudioSensor') ? b(st!.audioDetected) ?? false : null,
            online: has('Online') ? b(st!.online) : null,
        };
        for (const c of this.contextSensors())
            if (c.feature === 'night_mode' || c.feature === 'tamper') out[c.feature] = c.on;
        return out;
    }

    private contextChanged(device: string, feature: string | null, on: boolean) {
        const ev = this.tracker.current;
        if (!ev) return;
        if (this.ctxChanges.length < MAX_CHANGES)
            this.ctxChanges.push({ t: this.env.clock.now(), feature, id: device, name: this.env.state(device)?.name ?? device, on });
        this.tracker.touch();
    }

    /** Adds the context to the event's extras (segments and thumbnail extras are set by the provider). */
    private decorate(ev: EventRecord) {
        ev.extra.context = { at_start: this.ctxStart ?? this.context(), current: this.context(), changes: [...this.ctxChanges] };
    }
}

export function featureOf(name: string | undefined): string | null {
    for (const [re, f] of FEATURES) if (name && re.test(name)) return f;
    return null;
}
