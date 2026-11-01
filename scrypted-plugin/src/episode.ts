// Events (episodes) per camera: start at the first trigger (or verifier) observation, end a margin after the last observation
// (or after the last held signal turns off; a verifier's repeated confirmation is waited for), capped. Subjects inside an event, with a verdict each.

import { addTo, enrich, Enrichment, FusionConfig, DEFAULT_FUSION, iou, match, matchEnricher, newSubject, publicSubject, Subject, Verdict } from './fusion';
import { Observation } from './normalize';
import { familyOf } from './roles';
import { Clock, randomId } from './util';

export interface EpisodeConfig {
    /** the event ends this long after the last observation (ms) */
    marginMs: number;
    /** an event is closed after this long and a new one starts if activity continues (ms) */
    capMs: number;
    /** a verifier must see a subject within this long of its first observation, or it is vetoed (ms) */
    verifyWindowMs: number;
    /** a held signal not refreshed for this long is released (a lost `off` edge must not hold events forever) (ms) */
    maxHoldMs?: number;
    fusion?: FusionConfig;
}

export const DEFAULT_EPISODE: EpisodeConfig = { marginMs: 10_000, capMs: 10 * 60_000, verifyWindowMs: 5_000 };
export const DEFAULT_MAX_HOLD_MS = 5 * 60_000;
/**
 * A verifier repeats its confirmation while it still holds a subject (Coral Guard: every 10 s). A repeat of the same class from
 * the same verifier within this gap joins that subject and the open event waits this long for it, so a held subject is one
 * subject in one event, not a new event (or subject) per repeat (ms).
 */
export const VERIFIER_REPEAT_GAP_MS = 15_000;

/** A verifier observation confirms only with a concrete class, or as a classless signal of a gated source. */
export function confirms(o: Observation): boolean {
    return o.role === 'verifier' && (familyOf(o.class) !== 'any' || !!o.gated);
}

export const MAX_SUBJECTS = 50;
/** observations carried by one start/update message */
export const MAX_OBS_PER_MESSAGE = 50;
/** at most one event.update per event this often (ms) */
export const UPDATE_INTERVAL_MS = 1000;

export type EndReason = 'quiet' | 'cap' | 'stopped';

export interface Latency {
    /** event time of the first observation (camera or detector clock) */
    camera_event_time: number;
    /** when the plugin received it */
    plugin_receive: number;
    /** when event.start was published */
    event_start_sent?: number;
    stream_requested?: number;
    stream_resolved?: number;
    first_byte?: number;
    init_cut?: number;
    first_segment_cut?: number;
    first_live_segment_cut?: number;
    thumbnail_at?: number;
}

export interface EventRecord {
    id: string;
    camera: { id: string; name: string };
    state: 'open' | 'closed';
    start: number;
    end: number | null;
    last_observation: number;
    end_reason: EndReason | null;
    /** the event before this one was capped and activity carried on */
    continues: string | null;
    trigger: { source: string; role: string; kind: string; class: string };
    subjects: Subject[];
    enrichments: Enrichment[];
    observations: number;
    sources: Record<string, number>;
    verifier_present: boolean;
    flags: { camera_offline: boolean; capped: boolean; subjects_overflow: boolean };
    latency: Latency;
    /** set by the mixin (context features, segments, thumbnail) */
    extra: Record<string, unknown>;
    /** internal */
    startRecv: number;
    lastRecv: number;
}

export interface EpisodeHooks {
    onStart(ev: EventRecord, obs: Observation[]): void;
    onUpdate(ev: EventRecord, obs: Observation[]): void;
    onVerdict(ev: EventRecord, s: Subject): void;
    onEnd(ev: EventRecord): void;
}

export interface EpisodeEnv {
    clock: Clock;
    camera: { id: string; name: string };
    /** a verifier is present on this camera (decides pending vs unverified for a new subject) */
    verifierPresent(): boolean;
    /** a verifier showed activity recently: a subject it did not confirm is vetoed, otherwise only unverified */
    verifierLive?(): boolean;
    newId?: () => string;
}

/** The event as sent over the API. */
export function publicEvent(ev: EventRecord) {
    const { startRecv, lastRecv, subjects, extra, ...rest } = ev;
    return { ...rest, ...extra, subjects: subjects.map(publicSubject), sources: { ...ev.sources }, flags: { ...ev.flags }, latency: { ...ev.latency }, enrichments: [...ev.enrichments] };
}

export function publicObservation(o: Observation) {
    return { t: o.t, source: o.source, role: o.role, kind: o.kind, class: o.class, raw: o.raw, score: o.score, box: o.box, track: o.track, label: o.label, zones: o.zones, ambiguous: !!o.ambiguous };
}

export class EpisodeTracker {
    current: EventRecord | undefined;
    private holds = new Map<string, { o: Observation; timer: any }>();
    private endTimer: any;
    private capTimer: any;
    private updateTimer: any;
    private lastUpdateAt = 0;
    private pendingObs: Observation[] = [];
    private windows = new Map<string, any>();
    private offline = false;
    private stopped = false;
    /** the last event closed by the cap, remembered for the margin so the next event continues it */
    private capped: { id: string; at: number } | undefined;
    /** the open event waits at least until then for a verifier's repeated confirmation (receive time, ms) */
    private repeatUntil = 0;

    constructor(public cfg: EpisodeConfig, private env: EpisodeEnv, private hooks: EpisodeHooks) { }

    private get fusion() { return this.cfg.fusion ?? DEFAULT_FUSION; }
    private now() { return this.env.clock.now(); }

    /** recent ambiguous observations, attached when a view-specific subject of their family appears within the class-time window */
    private ambiguousBuf: Observation[] = [];

    /**
     * An ambiguous observation (a split camera's whole-frame AI): never opens an event or creates a subject; it is trigger
     * evidence for a subject of the same family within the class-time window, now or when one appears shortly after.
     */
    private observeAmbiguous(o: Observation) {
        const fam = familyOf(o.class);
        if (fam === 'any') return;
        const ev = this.current;
        const s = ev?.subjects.filter(x => x.family === fam && Math.abs(o.recv - x.lastRecv) <= this.fusion.classTimeMs)
            .sort((a, b) => b.lastRecv - a.lastRecv)[0];
        if (!ev || !s) {
            this.ambiguousBuf = [...this.ambiguousBuf.filter(x => this.now() - x.recv <= this.fusion.classTimeMs), o].slice(-16);
            return;
        }
        this.attachAmbiguous(ev, s, o);
        this.queueUpdate(o);
        this.scheduleEnd();
    }

    private attachAmbiguous(ev: EventRecord, s: Subject, o: Observation) {
        this.count(ev, o);
        addTo(s, o);
        ev.last_observation = Math.max(ev.last_observation, o.t);
        ev.lastRecv = Math.max(ev.lastRecv, o.recv);
    }

    /** A new view-specific subject: buffered ambiguous observations of its family from the last seconds join it. */
    private claimBuffered(ev: EventRecord, s: Subject) {
        const keep: Observation[] = [];
        for (const o of this.ambiguousBuf) {
            if (familyOf(o.class) === s.family && Math.abs(s.firstRecv - o.recv) <= this.fusion.classTimeMs) this.attachAmbiguous(ev, s, o);
            else keep.push(o);
        }
        this.ambiguousBuf = keep;
    }

    /** A trigger, verifier or enricher observation (ignore is filtered out before). */
    observe(o: Observation) {
        if (this.stopped || o.role === 'ignore')
            return;
        if (o.ambiguous) { this.observeAmbiguous(o); return; }
        if (o.role === 'enricher') {
            const ev = this.current;
            if (!ev) return;
            const s = matchEnricher(ev.subjects, o, this.fusion);
            const e = enrich(s, o);
            if (!s && ev.enrichments.length < 32) ev.enrichments.push(e);
            this.count(ev, o);
            this.queueUpdate(o);
            return;
        }
        let ev = this.current;
        const starting = !ev;
        if (!ev)
            ev = this.open(o);
        this.count(ev, o);
        ev.last_observation = Math.max(ev.last_observation, o.t);
        ev.lastRecv = Math.max(ev.lastRecv, o.recv);
        // a verifier (Coral Guard) repeats a held confirmation every 10 s, as long as the margin: wait for the repeat, or the event
        // closes just before it and each repeat opens a new one
        if (o.role === 'verifier' && familyOf(o.class) !== 'any')
            this.repeatUntil = Math.max(this.repeatUntil, o.recv + VERIFIER_REPEAT_GAP_MS);
        const decided = this.place(ev, o);
        if (starting) {
            ev.latency.event_start_sent = this.now();
            this.hooks.onStart(ev, [o]);
        }
        else
            this.queueUpdate(o);
        if (decided) this.hooks.onVerdict(ev, decided);
        this.scheduleEnd();
    }

    /**
     * A binary signal (motion, occupancy, audio) held on: the event stays open while any hold is on and ends a margin after
     * the last one turns off. `on` with an observation also observes it.
     */
    hold(key: string, on: boolean, o?: Observation) {
        if (this.stopped) return;
        if (on && o) {
            const prev = this.holds.get(key);
            if (prev) this.env.clock.clearTimeout(prev.timer);
            const timer = this.env.clock.setTimeout(() => {
                if (this.holds.get(key)?.timer === timer) this.hold(key, false);
            }, this.cfg.maxHoldMs ?? DEFAULT_MAX_HOLD_MS);
            this.holds.set(key, { o, timer });
            this.observe(o);
        }
        else if (!on) {
            const h = this.holds.get(key);
            if (!h) return;
            this.env.clock.clearTimeout(h.timer);
            this.holds.delete(key);
            if (this.current) {
                // the signal was seen until now: its release counts as the last observation
                const now = this.now();
                this.current.lastRecv = Math.max(this.current.lastRecv, now);
                this.current.last_observation = Math.max(this.current.last_observation, now);
                this.scheduleEnd();
            }
        }
    }

    held(key: string): boolean {
        return this.holds.has(key);
    }

    get holdKeys(): string[] {
        return [...this.holds.keys()];
    }

    /** Releases every held signal (listeners unbound, camera offline, mixin removed). */
    releaseHolds() {
        for (const k of [...this.holds.keys()]) this.hold(k, false);
    }

    setOffline(offline: boolean) {
        this.offline = offline;
        if (offline) this.releaseHolds();
        if (offline && this.current && !this.current.flags.camera_offline) {
            this.current.flags.camera_offline = true;
            this.queueUpdate();
        }
    }

    /** Closes the open event (plugin stopping, camera removed). */
    stop() {
        for (const h of this.holds.values()) this.env.clock.clearTimeout(h.timer);
        this.holds.clear();
        if (this.current) this.close('stopped');
        this.stopped = true;
    }

    private open(o: Observation): EventRecord {
        const now = this.now();
        const ev: EventRecord = {
            id: this.env.newId?.() ?? randomId('ev_'),
            camera: { ...this.env.camera },
            state: 'open', start: o.t, end: null, last_observation: o.t, end_reason: null,
            continues: this.capped && now - this.capped.at <= this.cfg.marginMs ? this.capped.id : null,
            trigger: { source: o.source, role: o.role, kind: o.kind, class: o.class },
            subjects: [], enrichments: [], observations: 0, sources: {},
            verifier_present: this.env.verifierPresent(),
            flags: { camera_offline: this.offline, capped: false, subjects_overflow: false },
            latency: { camera_event_time: o.t, plugin_receive: o.recv },
            extra: {},
            startRecv: now, lastRecv: o.recv,
        };
        this.capped = undefined;
        this.repeatUntil = 0;
        this.current = ev;
        this.lastUpdateAt = now;
        this.capTimer = this.env.clock.setTimeout(() => this.close('cap'), this.cfg.capMs);
        return ev;
    }

    private count(ev: EventRecord, o: Observation) {
        ev.observations++;
        if (ev.sources[o.source] !== undefined || Object.keys(ev.sources).length < 32)
            ev.sources[o.source] = (ev.sources[o.source] ?? 0) + 1;
    }

    /** Places the observation; returns the subject whose verdict was just decided (announced by the caller), if any. */
    private place(ev: EventRecord, o: Observation): Subject | undefined {
        const confirming = confirms(o);
        // a classless gated signal (Coral Guard's motion) says "something was verified", not what: it confirms only an
        // unambiguous subject (the single pending one), else it goes to its own motion/presence subject; never a vetoed one
        const gatedSignal = confirming && familyOf(o.class) === 'any';
        let s: Subject | undefined;
        if (gatedSignal) {
            const pending = ev.subjects.filter(x => x.verdict === 'pending');
            s = pending.length === 1 ? pending[0]
                : ev.subjects.find(x => x.family === 'any' && x.verdict !== 'vetoed' && x.sources[o.source] !== undefined);
        }
        else {
            s = match(ev.subjects, o, this.fusion);
            if (!s && confirming) s = this.pendingMatch(ev, o) ?? this.repeatMatch(ev, o);
        }
        if (!s) {
            if (ev.subjects.length >= MAX_SUBJECTS) {
                ev.flags.subjects_overflow = true;
                return undefined;
            }
            s = newSubject(`s${ev.subjects.length + 1}`, o);
            ev.subjects.push(s);
            if (this.ambiguousBuf.length) this.claimBuffered(ev, s);
            if (confirming) {
                this.verdict(ev, s, 'confirmed', o.source, false);
                return s;
            }
            if (this.env.verifierPresent() || ev.verifier_present) {
                s.verdict = 'pending';
                const sid = s.id;
                this.windows.set(`${ev.id}/${sid}`, this.env.clock.setTimeout(() => {
                    this.windows.delete(`${ev.id}/${sid}`);
                    if (this.current === ev && s!.verdict === 'pending')
                        this.verdict(ev, s!, this.unconfirmed(), null, true);
                }, this.cfg.verifyWindowMs));
                return undefined;
            }
            this.verdict(ev, s, 'unverified', null, false);
            return s;
        }
        addTo(s, o);
        if (confirming && s.verdict !== 'confirmed') {
            this.verdict(ev, s, 'confirmed', o.source, false);
            return s;
        }
        return undefined;
    }

    /**
     * While a subject waits for its verifier, a same-family verifier observation within the verify window of the subject's
     * first observation joins it (the verifier is often slower than the 2 s class-and-time rule), unless both have boxes
     * that do not overlap.
     */
    private pendingMatch(ev: EventRecord, o: Observation): Subject | undefined {
        const fam = familyOf(o.class);
        if (fam === 'any') return undefined;
        return ev.subjects.find(s => s.verdict === 'pending' && s.family === fam && o.recv - s.firstRecv <= this.cfg.verifyWindowMs
            && !(o.box && s.box && iou(o.box, s.box) < this.fusion.iou));
    }

    /**
     * A verifier repeating its confirmation of a held subject: the same class family from the same verifier within
     * VERIFIER_REPEAT_GAP_MS of that subject's last observation joins it (the 2 s class-and-time rule would start a new subject
     * per repeat), unless both have boxes that do not overlap.
     */
    private repeatMatch(ev: EventRecord, o: Observation): Subject | undefined {
        const fam = familyOf(o.class);
        if (fam === 'any') return undefined;
        let best: Subject | undefined;
        for (const s of ev.subjects) {
            if (s.family !== fam || s.verdict !== 'confirmed' || s.sources[o.source] === undefined || o.recv - s.lastRecv > VERIFIER_REPEAT_GAP_MS) continue;
            if (o.box && s.box && iou(o.box, s.box) < this.fusion.iou) continue;
            if (!best || s.lastRecv > best.lastRecv) best = s;
        }
        return best;
    }

    /** The verdict of a subject no verifier confirmed: vetoed when the verifier is alive, unverified when it went silent. */
    private unconfirmed(): Verdict {
        return this.env.verifierLive && !this.env.verifierLive() ? 'unverified' : 'vetoed';
    }

    /** `announce`: send subject.verdict now (otherwise the caller announces it once the observation is placed). */
    private verdict(ev: EventRecord, s: Subject, v: Verdict, source: string | null, announce: boolean) {
        s.verdict = v;
        s.verdict_at = this.now();
        s.verdict_source = source;
        const t = this.windows.get(`${ev.id}/${s.id}`);
        if (t !== undefined && v !== 'pending') {
            this.env.clock.clearTimeout(t);
            this.windows.delete(`${ev.id}/${s.id}`);
        }
        if (announce)
            this.hooks.onVerdict(ev, s);
    }

    private queueUpdate(o?: Observation) {
        if (o && this.pendingObs.length < MAX_OBS_PER_MESSAGE) this.pendingObs.push(o);
        if (this.updateTimer !== undefined) return;
        const wait = Math.max(0, this.lastUpdateAt + UPDATE_INTERVAL_MS - this.now());
        this.updateTimer = this.env.clock.setTimeout(() => { this.updateTimer = undefined; this.flushUpdate(); }, wait);
    }

    private flushUpdate() {
        const ev = this.current;
        if (this.updateTimer !== undefined) { this.env.clock.clearTimeout(this.updateTimer); this.updateTimer = undefined; }
        if (!ev) { this.pendingObs = []; return; }
        const obs = this.pendingObs;
        this.pendingObs = [];
        this.lastUpdateAt = this.now();
        this.hooks.onUpdate(ev, obs);
    }

    /** Asks for an event.update soon (the mixin changed something in `extra`). */
    touch() {
        if (this.current) this.queueUpdate();
    }

    private scheduleEnd() {
        const ev = this.current;
        if (!ev) return;
        if (this.endTimer !== undefined) { this.env.clock.clearTimeout(this.endTimer); this.endTimer = undefined; }
        if (this.holds.size) return;
        const wait = Math.max(0, ev.lastRecv + this.cfg.marginMs - this.now(), this.repeatUntil - this.now());
        this.endTimer = this.env.clock.setTimeout(() => { this.endTimer = undefined; if (this.current === ev) this.close('quiet'); }, wait);
    }

    private close(reason: EndReason) {
        const ev = this.current;
        if (!ev) return;
        if (this.pendingObs.length || this.updateTimer !== undefined) this.flushUpdate();
        for (const t of [this.endTimer, this.capTimer]) if (t !== undefined) this.env.clock.clearTimeout(t);
        this.endTimer = this.capTimer = undefined;
        // a subject still waiting for its verifier when the event closes: its window is cut short, it is vetoed
        for (const s of ev.subjects) {
            const k = `${ev.id}/${s.id}`;
            const t = this.windows.get(k);
            if (t !== undefined) { this.env.clock.clearTimeout(t); this.windows.delete(k); }
            if (s.verdict === 'pending') this.verdict(ev, s, this.unconfirmed(), null, true);
        }
        ev.state = 'closed';
        ev.end_reason = reason;
        ev.flags.capped = reason === 'cap';
        ev.end = reason === 'quiet' ? Math.max(ev.last_observation, ev.start) + this.cfg.marginMs : Math.max(ev.start, ev.last_observation, this.now());
        this.current = undefined;
        if (reason === 'cap') this.capped = { id: ev.id, at: this.now() };
        this.hooks.onEnd(ev);
        // capped while a signal is still held: activity carries on in a new event at once
        if (reason === 'cap' && this.holds.size && !this.stopped) {
            const now = this.now();
            const first = [...this.holds.values()][0].o;
            this.observe({ ...first, t: now, recv: now });
        }
    }
}
