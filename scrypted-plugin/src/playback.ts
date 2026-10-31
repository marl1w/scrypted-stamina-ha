// Recording playback as HLS: VideoRecorder.getRecordingStream -> ffmpeg -c copy -> fMP4 segments in memory -> a live playlist.
// One session per request, reached through an unguessable id; sessions are stopped when idle, bounded in number and memory.

import { SegmenterDeps, SegmenterHandle, startSegmenter, StreamInput } from './segmenter';
import { randomId } from './util';

export interface PlaybackConfig {
    maxSessions: number;
    idleMs: number;
    maxBytes: number;
    maxLifetimeMs: number;
    segmentS: number;
}

export const DEFAULT_PLAYBACK: PlaybackConfig = { maxSessions: 2, idleMs: 60_000, maxBytes: 48 * 1024 * 1024, maxLifetimeMs: 30 * 60_000, segmentS: 2 };
/** a session with a duration lives at least that long plus this margin */
export const LIFETIME_MARGIN_MS = 5 * 60_000;
/** a session whose player stopped fetching (reading paused this long) is stopped */
export const MAX_PAUSED_MS = 2 * 60_000;

interface PSeg { n: number; data: Buffer; start: number; duration: number; fetched: boolean }

export class PlaybackSession {
    readonly id = randomId('pb_');
    init?: Buffer;
    segs: PSeg[] = [];
    next = 0;
    ended = false;
    failure?: string;
    bytes = 0;
    lastAccess: number;
    readonly created: number;
    handle?: SegmenterHandle;
    /** when reading was paused for memory (undefined while reading) */
    pausedAt?: number;
    private waiters: (() => void)[] = [];

    constructor(readonly camera: string, readonly startTime: number, readonly durationMs: number | undefined, now: number, private maxBytes: number, private clock: () => number = Date.now) {
        this.created = now;
        this.lastAccess = now;
    }

    /** a media fetch (init or segment): the player is active */
    touch() { this.lastAccess = this.clock(); }

    addInit(b: Buffer) { this.init = b; this.bytes += b.length; this.wake(); }

    /** Over its memory: fetched segments drop out first; when only unfetched ones are left, reading pauses (back-pressure). */
    addSegment(data: Buffer, start: number, duration: number) {
        this.segs.push({ n: this.next++, data, start, duration, fetched: false });
        this.bytes += data.length;
        this.trim();
        this.wake();
    }

    /**
     * Over its memory, segments leave from the head of the playlist (it stays contiguous): fetched ones, and unfetched ones
     * numbered below the lowest fetched one (the player started later, e.g. at the live end, and will never ask for them).
     * When only segments the player still needs are left, reading pauses.
     */
    private trim() {
        const fetched = this.segs.filter(x => x.fetched).map(x => x.n);
        const lowest = fetched.length ? Math.min(...fetched) : -Infinity;
        while (this.bytes > this.maxBytes && this.segs.length > 1 && (this.segs[0].fetched || this.segs[0].n < lowest)) {
            const s = this.segs.shift()!;
            this.bytes -= s.data.length;
        }
        if (this.bytes > this.maxBytes) {
            if (this.pausedAt === undefined) this.pausedAt = this.clock();
            this.handle?.pause();
        }
        else {
            this.pausedAt = undefined;
            this.handle?.resume();
        }
    }

    get paused() { return this.bytes > this.maxBytes; }

    /** Lifetime: the configured maximum, or the duration asked plus a margin when longer. */
    lifetime(maxLifetimeMs: number): number {
        return Math.max(maxLifetimeMs, (this.durationMs ?? 0) + LIFETIME_MARGIN_MS);
    }

    end(failure?: string) {
        this.ended = true;
        if (failure && failure !== 'stopped') this.failure = failure;
        this.wake();
    }

    private wake() { const w = this.waiters; this.waiters = []; for (const f of w) f(); }

    /** Resolves once init and one segment are there, the session ended, or the timeout passed. */
    ready(timeoutMs: number): Promise<boolean> {
        const ok = () => !!this.init && this.segs.length > 0;
        if (ok() || this.ended) return Promise.resolve(ok());
        return new Promise(res => {
            const t = setTimeout(() => res(ok()), timeoutMs);
            const check = () => {
                if (ok() || this.ended) { clearTimeout(t); res(ok()); }
                else this.waiters.push(check);
            };
            this.waiters.push(check);
        });
    }

    segment(n: number): PSeg | undefined {
        const s = this.segs.find(x => x.n === n);
        if (s && !s.fetched) { s.fetched = true; this.trim(); }
        return s;
    }

    playlist(): string {
        const target = Math.max(1, Math.ceil(Math.max(0, ...this.segs.map(s => s.duration))));
        const first = this.segs[0]?.n ?? this.next;
        const lines = ['#EXTM3U', '#EXT-X-VERSION:7', `#EXT-X-TARGETDURATION:${target}`, `#EXT-X-MEDIA-SEQUENCE:${first}`,
            // an EVENT playlist (only grows) while nothing was dropped; players start at its beginning, not at the live end
            ...(first === 0 ? ['#EXT-X-PLAYLIST-TYPE:EVENT'] : []), '#EXT-X-START:TIME-OFFSET=0',
            '#EXT-X-INDEPENDENT-SEGMENTS', '#EXT-X-MAP:URI="init.mp4"'];
        for (const s of this.segs) {
            lines.push(`#EXT-X-PROGRAM-DATE-TIME:${new Date(this.startTime + Math.round(s.start * 1000)).toISOString()}`);
            lines.push(`#EXTINF:${s.duration.toFixed(3)},`, `${s.n}.m4s`);
        }
        if (this.ended) lines.push('#EXT-X-ENDLIST');
        return lines.join('\n') + '\n';
    }
}

export class PlaybackManager {
    private sessions = new Map<string, PlaybackSession>();

    constructor(public cfg: PlaybackConfig = DEFAULT_PLAYBACK, private now: () => number = Date.now) { }

    get count() { return this.sessions.size; }

    /** The session; its idle timer is refreshed only by media fetches (session.touch), not playlist polls. */
    get(id: string): PlaybackSession | undefined {
        return this.sessions.get(id);
    }

    /**
     * Starts a session for a camera's recording from `startTime` (ms) for `durationMs` (live seekable stream when undefined).
     * The oldest session is stopped when the limit is reached.
     */
    start(camera: string, startTime: number, durationMs: number | undefined, input: () => Promise<StreamInput>, deps: Omit<SegmenterDeps, 'input'>, console: Pick<Console, 'log' | 'warn'>): PlaybackSession {
        while (this.sessions.size >= this.cfg.maxSessions) {
            const oldest = [...this.sessions.values()].sort((a, b) => a.lastAccess - b.lastAccess)[0];
            this.stop(oldest.id);
        }
        const s = new PlaybackSession(camera, startTime, durationMs, this.now(), this.cfg.maxBytes, this.now);
        this.sessions.set(s.id, s);
        s.handle = startSegmenter({
            segmentS: this.cfg.segmentS, prerollMs: 0, console, label: `playback ${camera}`,
            onInit: b => s.addInit(b),
            onSegment: seg => s.addSegment(seg.data, seg.start, seg.duration),
            onMark: () => { },
            onEnd: f => s.end(f),
        }, { ...deps, input: () => input() });
        return s;
    }

    stop(id: string) {
        const s = this.sessions.get(id);
        if (!s) return;
        this.sessions.delete(id);
        s.handle?.stop();
        s.end();
    }

    /** Stops idle and too old sessions. */
    sweep() {
        const now = this.now();
        for (const s of [...this.sessions.values()])
            if (now - s.lastAccess >= this.cfg.idleMs || now - s.created >= s.lifetime(this.cfg.maxLifetimeMs)
                || (s.pausedAt !== undefined && now - s.pausedAt >= MAX_PAUSED_MS))
                this.stop(s.id);
    }

    stopAll() {
        for (const id of [...this.sessions.keys()]) this.stop(id);
    }
}
