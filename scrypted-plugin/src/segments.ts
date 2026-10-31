// In-memory store of each event's fMP4 init, segments and thumbnail. Segments are released on ack or after their TTL;
// the total is bounded (the oldest segments are evicted first and reported as gaps).

import { Clock, realClock } from './util';

export interface StoredSegment {
    n: number;
    data: Buffer;
    /** media start (s) from the first segment, and duration (s) */
    start: number;
    duration: number;
    /** wall time it was cut (ms) */
    cut_at: number;
    /** which init it needs (0 = init.mp4, k = init-k.mp4) */
    init: number;
    /** the first segment after a new init (ffmpeg was restarted): a discontinuity */
    discontinuity: boolean;
}

export interface Thumbnail {
    jpeg: Buffer;
    at: number;
    kind: 'start' | 'confirmation';
}

interface Entry {
    id: string;
    camera: string;
    created: number;
    inits: Buffer[];
    segs: Map<number, StoredSegment>;
    /** next segment number to be cut */
    next: number;
    /** the segmenter is running (missing segments may still come: 425) */
    live: boolean;
    /** wall time the segmenter stopped */
    finishedAt?: number;
    failure?: string;
    acked: number;
    evicted: number;
    expired: number;
    thumb?: Thumbnail;
    /** the event can still produce a thumbnail */
    thumbPending: boolean;
}

export interface StoreConfig {
    ttlMs: number;
    maxBytes: number;
    maxEvents: number;
}

export const DEFAULT_STORE: StoreConfig = { ttlMs: 10 * 60_000, maxBytes: 64 * 1024 * 1024, maxEvents: 200 };

export type Fetch = { status: 200; data: Buffer; seg?: StoredSegment } | { status: 404 | 425 };

export interface EventSegmentsInfo {
    live: boolean;
    init: boolean;
    inits: number;
    next: number;
    retained: number[];
    acked_up_to: number;
    evicted: number;
    expired: number;
    bytes: number;
    failure: string | null;
}

export class SegmentStore {
    private events = new Map<string, Entry>();
    private total = 0;
    /** called when segments were dropped for memory: event id, segment numbers */
    onEvict?: (id: string, ns: number[]) => void;

    constructor(public cfg: StoreConfig = DEFAULT_STORE, private clock: Clock = realClock) { }

    get bytes() { return this.total; }
    get count() { return this.events.size; }
    has(id: string) { return this.events.has(id); }

    open(id: string, camera: string) {
        if (this.events.has(id)) return;
        this.events.set(id, { id, camera, created: this.clock.now(), inits: [], segs: new Map(), next: 0, live: true, acked: -1, evicted: 0, expired: 0, thumbPending: true });
        // bounded number of events: the oldest finished one without segments left goes first, then the oldest finished one,
        // then the oldest; segments still held are announced as evicted before they go
        while (this.events.size > this.cfg.maxEvents) {
            const all = [...this.events.values()].filter(e => e.id !== id);
            const victim = all.find(e => !e.live && !e.segs.size && !e.thumbPending) ?? all.find(e => !e.live && !e.segs.size)
                ?? all.find(e => !e.live) ?? all[0];
            if (!victim) break;
            if (victim.segs.size) this.onEvict?.(victim.id, [...victim.segs.keys()].sort((a, b) => a - b));
            this.drop(victim.id);
        }
    }

    /** Adds an init (a restarted ffmpeg brings a new one); returns its number, or undefined. At most 16 per event. */
    setInit(id: string, init: Buffer): number | undefined {
        const e = this.events.get(id);
        if (!e || e.inits.length >= 16) return undefined;
        e.inits.push(init);
        this.total += init.length;
        this.bound(id);
        return e.inits.length - 1;
    }

    /** The segmenter starts again for this event (a late confirmation, a freed slot): missing segments may come again. */
    reopen(id: string) {
        const e = this.events.get(id);
        if (!e) return false;
        e.live = true;
        e.finishedAt = undefined;
        return true;
    }

    /** Stores the next segment (it uses the latest init) and returns it; undefined when the event is unknown, finished or has no init. */
    addSegment(id: string, data: Buffer, start: number, duration: number, discontinuity = false): StoredSegment | undefined {
        const e = this.events.get(id);
        if (!e || !e.live || !e.inits.length) return undefined;
        const seg: StoredSegment = { n: e.next++, data, start, duration, cut_at: this.clock.now(), init: e.inits.length - 1, discontinuity };
        e.segs.set(seg.n, seg);
        this.total += data.length;
        this.bound(id);
        return seg;
    }

    /** The segmenter stopped: missing segments will never come (404 instead of 425). */
    finish(id: string, failure?: string) {
        const e = this.events.get(id);
        if (!e) return;
        e.live = false;
        e.finishedAt = this.clock.now();
        if (failure && !e.failure) e.failure = failure;
    }

    /** The event closed: no thumbnail will come any more. */
    closeThumbnail(id: string) {
        const e = this.events.get(id);
        if (e) e.thumbPending = false;
    }

    getInit(id: string, k = 0): Fetch {
        const e = this.events.get(id);
        if (!e) return { status: 404 };
        if (e.inits[k]) return { status: 200, data: e.inits[k] };
        return { status: e.live && k === e.inits.length ? 425 : 404 };
    }

    getSegment(id: string, n: number): Fetch {
        const e = this.events.get(id);
        if (!e) return { status: 404 };
        const s = e.segs.get(n);
        if (s) return { status: 200, data: s.data, seg: s };
        return { status: n >= e.next && e.live ? 425 : 404 };
    }

    setThumbnail(id: string, jpeg: Buffer, kind: Thumbnail['kind']) {
        const e = this.events.get(id);
        if (!e) return false;
        if (e.thumb?.kind === 'confirmation' && kind === 'start') return false;
        this.total += jpeg.length - (e.thumb?.jpeg.length ?? 0);
        e.thumb = { jpeg, at: this.clock.now(), kind };
        return true;
    }

    getThumbnail(id: string): Fetch & { thumb?: Thumbnail } {
        const e = this.events.get(id);
        if (!e) return { status: 404 };
        if (e.thumb) return { status: 200, data: e.thumb.jpeg, thumb: e.thumb };
        return { status: e.thumbPending ? 425 : 404 };
    }

    /**
     * Releases segments up to and including `upTo`. Once the segmenter has finished and everything it cut is acked, the
     * whole event (init and thumbnail too) is released. Returns the number of segments released, or undefined for an unknown event.
     */
    ack(id: string, upTo: number): number | undefined {
        const e = this.events.get(id);
        if (!e) return undefined;
        // never beyond what was cut: a segment not cut yet cannot be released ahead of time
        upTo = Math.min(upTo, e.next - 1);
        let released = 0;
        for (const [n, s] of e.segs)
            if (n <= upTo) { e.segs.delete(n); this.total -= s.data.length; released++; }
        e.acked = Math.max(e.acked, upTo);
        if (!e.live && e.acked >= e.next - 1 && !e.thumbPending)
            this.drop(id);
        return released;
    }

    info(id: string): EventSegmentsInfo | undefined {
        const e = this.events.get(id);
        if (!e) return undefined;
        let bytes = e.inits.reduce((a, b) => a + b.length, 0);
        for (const s of e.segs.values()) bytes += s.data.length;
        return { live: e.live, init: e.inits.length > 0, inits: e.inits.length, next: e.next, retained: [...e.segs.keys()].sort((a, b) => a - b), acked_up_to: e.acked, evicted: e.evicted, expired: e.expired, bytes, failure: e.failure ?? null };
    }

    /** TTL: a segment lives ttl after it was cut; the event (init, thumbnail) ttl after its segmenter finished and its last segment went. */
    sweep() {
        const now = this.clock.now();
        for (const e of [...this.events.values()]) {
            for (const [n, s] of e.segs)
                if (now - s.cut_at >= this.cfg.ttlMs) { e.segs.delete(n); this.total -= s.data.length; e.expired++; }
            // an open event (a thumbnail may still come) keeps its entry
            if (!e.live && !e.segs.size && !e.thumbPending && now - (e.finishedAt ?? e.created) >= this.cfg.ttlMs)
                this.drop(e.id);
        }
    }

    /** Over the memory bound: evict the oldest segments of any event (never the one just added unless it is the only one). */
    private bound(keep: string) {
        if (this.total <= this.cfg.maxBytes) return;
        const all: { e: Entry; s: StoredSegment }[] = [];
        for (const e of this.events.values()) for (const s of e.segs.values()) all.push({ e, s });
        all.sort((a, b) => a.s.cut_at - b.s.cut_at || (a.e.id === keep ? 1 : 0) - (b.e.id === keep ? 1 : 0) || a.s.n - b.s.n);
        const byEvent = new Map<string, number[]>();
        for (const { e, s } of all) {
            if (this.total <= this.cfg.maxBytes) break;
            e.segs.delete(s.n);
            this.total -= s.data.length;
            e.evicted++;
            (byEvent.get(e.id) ?? byEvent.set(e.id, []).get(e.id)!).push(s.n);
        }
        for (const [id, ns] of byEvent) this.onEvict?.(id, ns);
        // still over (inits and thumbnails of many events): drop whole finished events, oldest first
        for (const e of [...this.events.values()]) {
            if (this.total <= this.cfg.maxBytes) break;
            if (!e.live && e.id !== keep) this.drop(e.id);
        }
    }

    drop(id: string) {
        const e = this.events.get(id);
        if (!e) return;
        for (const s of e.segs.values()) this.total -= s.data.length;
        this.total -= e.inits.reduce((a, b) => a + b.length, 0) + (e.thumb?.jpeg.length ?? 0);
        this.events.delete(id);
    }

    clear() {
        this.events.clear();
        this.total = 0;
    }
}
