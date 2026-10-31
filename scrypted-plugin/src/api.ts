// HTTP API (docs/api.md). Pure request -> response routing over injected dependencies, so it is tested without Scrypted.

import { Bus, DEFAULT_SSE, parseResume, SseOptions, sseStream } from './bus';
import { PlaybackManager, PlaybackSession } from './playback';
import { SegmentStore } from './segments';
import { StreamInput, SegmenterDeps } from './segmenter';
import { clampInt, DEVICE_ID_RE, errText, EVENT_ID_RE, safeEqual, safeField, SESSION_ID_RE } from './util';

export const API_VERSION = 1;

export interface ApiRequest {
    method: string;
    /** path below the plugin's endpoint, e.g. "v1/events/stream" (no leading slash, no query) */
    path: string;
    query: URLSearchParams;
    /** lower-case header names */
    headers: Record<string, string>;
    body?: string;
}

export interface ApiResponse {
    status: number;
    headers: Record<string, string>;
    body?: string | Buffer;
    stream?: AsyncGenerator<Buffer, void>;
}

export interface ClipInfo {
    id: string;
    start: number;
    duration: number | null;
    event: string | null;
    description: string | null;
    classes: string[];
    thumbnail_id: string | null;
    video_id: string | null;
}

/** A clip from another plugin, every string checked: a value carrying a URL (credentials, tokens) is dropped. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function clipInfo(c: any): ClipInfo {
    return {
        id: safeField(c?.id, 200) ?? '',
        start: Number(c?.startTime) || 0,
        duration: typeof c?.duration === 'number' && Number.isFinite(c.duration) ? c.duration : null,
        event: safeField(c?.event) ?? null,
        description: safeField(c?.description, 200) ?? null,
        classes: Array.isArray(c?.detectionClasses) ? c.detectionClasses.slice(0, 16).map((x: unknown) => safeField(x)).filter((x: unknown): x is string => !!x) : [],
        thumbnail_id: safeField(c?.thumbnailId, 200) ?? null,
        video_id: safeField(c?.videoId, 200) ?? null,
    };
}

export interface CameraApi {
    id: string;
    hasRecorder: boolean;
    hasClips: boolean;
    clips(start: number, end: number): Promise<ClipInfo[]>;
    recordingInput(startTime: number, durationMs: number | undefined): Promise<StreamInput>;
}

export interface ApiDeps {
    token(): string;
    bus: Bus;
    store: SegmentStore;
    playback: PlaybackManager;
    cameras(): unknown[];
    camera(id: string): CameraApi | undefined;
    health(): Record<string, unknown>;
    /** an event's facts (for GET /v1/events/<id>), when still known */
    event(id: string): Record<string, unknown> | undefined;
    /** recent events (newest last) that started or ended at or after `since` (ms), optionally of one camera */
    events(since: number, camera?: string): Record<string, unknown>[];
    ffmpeg: Omit<SegmenterDeps, 'input'>;
    console: Pick<Console, 'log' | 'warn'>;
    now(): number;
    sse?: SseOptions;
    /** concurrent SSE streams; the oldest is closed when a new one exceeds it */
    maxStreams?: number;
}

const NO_STORE = { 'Cache-Control': 'no-store' };

function json(status: number, body: unknown, headers: Record<string, string> = {}): ApiResponse {
    return { status, headers: { 'Content-Type': 'application/json', ...NO_STORE, ...headers }, body: JSON.stringify(body) };
}

function error(status: number, code: string, message?: string, headers: Record<string, string> = {}): ApiResponse {
    return json(status, { error: code, ...(message ? { message } : {}) }, headers);
}

function binary(data: Buffer, type: string): ApiResponse {
    return { status: 200, headers: { 'Content-Type': type, 'Content-Length': String(data.length), ...NO_STORE }, body: data };
}

const TOO_EARLY = () => error(425, 'not_yet_available', undefined, { 'Retry-After': '1' });

/** The bearer token of a request, if any. */
export function bearer(headers: Record<string, string>): string | undefined {
    const h = headers['authorization'];
    const m = h ? /^Bearer\s+(\S+)\s*$/i.exec(h) : null;
    return m?.[1];
}

/** Limits for the playback and clips queries. */
export const MAX_PLAYBACK_AGE_MS = 400 * 24 * 3600_000;
export const MAX_PLAYBACK_S = 3600;
export const MAX_CLIPS_SPAN_MS = 7 * 24 * 3600_000;
export const MAX_CLIPS = 500;
export const MAX_SEGMENT_N = 1_000_000;
export const PLAYLIST_READY_MS = 15_000;
/** concurrent long polls that wait */
export const MAX_POLLS = 8;

/** A non-negative integer query value, or undefined when absent; NaN when invalid. */
function intParam(q: URLSearchParams, k: string): number | undefined {
    const v = q.get(k);
    if (v === null || v === '') return undefined;
    return /^\d{1,15}$/.test(v) ? Number(v) : NaN;
}

export class Api {
    private streams: { close(): void; closed: boolean }[] = [];
    /** pending long polls: ending one makes it answer at once */
    private polls = new Set<() => void>();
    /** bumped when the token changes: requests authorised under an older one get a 401 when they finish */
    private generation = 0;
    private authFailLogAt = 0;

    constructor(private d: ApiDeps) { }

    get sseClients() { return this.streams.filter(s => !s.closed).length; }

    closeStreams() {
        for (const s of this.streams) s.close();
        this.streams = [];
    }

    /** The token changed: SSE streams, pending polls and playback sessions opened with the old one end. */
    revoke() {
        this.generation++;
        this.closeStreams();
        for (const end of [...this.polls]) end();
        this.d.playback.stopAll();
    }

    async handle(req: ApiRequest): Promise<ApiResponse> {
        let res: ApiResponse;
        try {
            res = await this.route(req);
        }
        catch (e) {
            this.d.console.warn(`api: ${req.method} ${req.path.replace(/[^\w./-]/g, '?').slice(0, 80)} failed: ${errText(e)}`);
            res = error(500, 'internal_error');
        }
        res.headers['X-Content-Type-Options'] = 'nosniff';
        return res;
    }

    private authorized(req: ApiRequest): boolean {
        const tok = bearer(req.headers);
        const ok = !!tok && safeEqual(tok, this.d.token());
        if (!ok) {
            const now = this.d.now();
            if (now - this.authFailLogAt > 60_000) {
                this.authFailLogAt = now;
                this.d.console.warn('api: request without a valid token refused (logged at most once a minute)');
            }
        }
        return ok;
    }

    private async route(req: ApiRequest): Promise<ApiResponse> {
        const parts = req.path.split('/');
        if (parts.some(p => p === '' || p === '.' || p === '..') || parts.length > 6 || parts[0] !== 'v1')
            return error(404, 'not_found');
        const method = (req.method || 'GET').toUpperCase();
        const [, a, b, c, d2, e] = parts;

        // playback session routes: the unguessable session id is the credential (players cannot send headers), or the token
        if (a === 'cameras' && c === 'playback' && d2 !== undefined) {
            if (!DEVICE_ID_RE.test(b) || !SESSION_ID_RE.test(d2)) return error(404, 'not_found');
            const s = this.d.playback.get(d2);
            if (!s || s.camera !== b) return error(404, 'not_found');
            if (method === 'DELETE' && e === undefined) {
                if (!this.authorized(req)) return error(401, 'unauthorized', undefined, { 'WWW-Authenticate': 'Bearer' });
                this.d.playback.stop(s.id);
                return json(200, { ok: true });
            }
            if (method !== 'GET') return error(405, 'method_not_allowed');
            return this.sessionFile(s, e);
        }

        if (!this.authorized(req))
            return error(401, 'unauthorized', undefined, { 'WWW-Authenticate': 'Bearer' });

        if (a === 'health' && b === undefined)
            return method === 'GET' ? json(200, { ok: true, api: API_VERSION, ...this.d.health(), sse_clients: this.sseClients }) : error(405, 'method_not_allowed');

        if (a === 'cameras' && b === undefined)
            return method === 'GET' ? json(200, { cameras: this.d.cameras() }) : error(405, 'method_not_allowed');

        if (a === 'events' && b === 'stream' && c === undefined)
            return method === 'GET' ? this.sse(req) : error(405, 'method_not_allowed');

        if (a === 'events' && b === 'poll' && c === undefined)
            return method === 'GET' ? this.poll(req) : error(405, 'method_not_allowed');

        if (a === 'events' && b === undefined)
            return method === 'GET' ? this.list(req.query) : error(405, 'method_not_allowed');

        if (a === 'events' && b !== undefined && d2 === undefined) {
            if (!EVENT_ID_RE.test(b)) return error(404, 'not_found');
            if (c === undefined)
                return method === 'GET' ? this.eventInfo(b) : error(405, 'method_not_allowed');
            if (c === 'ack')
                return method === 'POST' ? this.ack(b, req.body) : error(405, 'method_not_allowed');
            if (method !== 'GET') return error(405, 'method_not_allowed');
            const im = /^init(?:-(\d{1,2}))?\.mp4$/.exec(c);
            if (im) {
                const r = this.d.store.getInit(b, im[1] === undefined ? 0 : Number(im[1]));
                return r.status === 200 ? binary(r.data, 'video/mp4') : r.status === 425 ? TOO_EARLY() : error(404, 'not_found');
            }
            if (c === 'thumbnail.jpg') {
                const r = this.d.store.getThumbnail(b);
                return r.status === 200 ? binary(r.data, 'image/jpeg') : r.status === 425 ? TOO_EARLY() : error(404, 'not_found');
            }
            const m = /^(\d{1,7})\.m4s$/.exec(c);
            if (m) {
                const n = Number(m[1]);
                if (n > MAX_SEGMENT_N) return error(404, 'not_found');
                const r = this.d.store.getSegment(b, n);
                return r.status === 200 ? binary(r.data, 'video/iso.segment') : r.status === 425 ? TOO_EARLY() : error(404, 'not_found');
            }
            return error(404, 'not_found');
        }

        if (a === 'cameras' && b !== undefined && d2 === undefined) {
            if (!DEVICE_ID_RE.test(b)) return error(404, 'not_found');
            const cam = this.d.camera(b);
            if (!cam) return error(404, 'not_found');
            if (method !== 'GET') return error(405, 'method_not_allowed');
            if (c === 'clips') return this.clips(cam, req.query);
            if (c === 'playback.m3u8') return this.startPlayback(cam, req.query);
            return error(404, 'not_found');
        }
        return error(404, 'not_found');
    }

    private sse(req: ApiRequest): ApiResponse {
        const r = parseResume(req.headers['last-event-id'], req.query.get('since'), req.query.get('epoch'));
        if (r.invalid) return error(400, 'bad_request', 'since must be an integer, epoch an epoch id');
        const max = this.d.maxStreams ?? 4;
        this.streams = this.streams.filter(s => !s.closed);
        while (this.streams.length >= max) this.streams.shift()!.close();
        const s = sseStream(this.d.bus, { since: r.since, epoch: r.epoch }, this.d.sse ?? DEFAULT_SSE);
        this.streams.push(s);
        return {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-store', 'X-Accel-Buffering': 'no', Connection: 'keep-alive' },
            stream: s.stream,
        };
    }

    /** Long-poll fallback for clients (or proxies) that cannot hold an SSE stream. */
    private async poll(req: ApiRequest): Promise<ApiResponse> {
        const r = parseResume(undefined, req.query.get('since'), req.query.get('epoch'));
        if (r.invalid) return error(400, 'bad_request', 'since must be an integer, epoch an epoch id');
        const waitS = clampInt(req.query.get('wait') ?? '0', 0, 25, 0);
        if (waitS > 0 && this.polls.size >= MAX_POLLS)
            return error(429, 'too_many_requests', `at most ${MAX_POLLS} long polls at once`, { 'Retry-After': '1' });
        const bus = this.d.bus;
        const gen = this.generation;
        // no since and no epoch: only what is published while waiting
        const live = r.since === undefined && r.epoch === undefined;
        const base = live ? bus.lastSeq : r.since;
        const epoch = live ? bus.epoch : r.epoch;
        let rep = bus.replay(base, epoch);
        if (!rep.messages.length && waitS > 0 && !rep.reset) {
            await new Promise<void>(res => {
                const done = () => { clearTimeout(t); un(); this.polls.delete(done); res(); };
                const t = setTimeout(done, waitS * 1000);
                const un = bus.subscribe(done);
                this.polls.add(done);
            });
            if (gen !== this.generation) return error(401, 'unauthorized', undefined, { 'WWW-Authenticate': 'Bearer' });
            rep = bus.replay(base, epoch);
        }
        const messages = rep.messages.slice(0, 500);
        const head = JSON.stringify({ epoch: bus.epoch, last_seq: bus.lastSeq, oldest_seq: bus.oldestSeq, gap: rep.gap, reset: rep.reset, more: rep.messages.length > messages.length });
        // the messages are already serialised: splice them in instead of parsing and re-serialising
        const body = `${head.slice(0, -1)},"messages":[${messages.map(m => m.json).join(',')}]}`;
        return { status: 200, headers: { 'Content-Type': 'application/json', ...NO_STORE }, body };
    }

    /** Recent events with their segment state, to recover after a gap in the message stream. */
    private list(q: URLSearchParams): ApiResponse {
        const since = intParam(q, 'since');
        const cam = q.get('camera');
        if (since === undefined || Number.isNaN(since)) return error(400, 'bad_request', 'since is required: a time in ms');
        if (cam !== null && !DEVICE_ID_RE.test(cam)) return error(400, 'bad_request', 'camera must be a camera id');
        const events = this.d.events(since, cam ?? undefined).map(ev => {
            const id = String(ev.id);
            return { event: ev, segments: this.d.store.info(id) ?? null };
        });
        return json(200, { since, events });
    }

    private eventInfo(id: string): ApiResponse {
        const seg = this.d.store.info(id);
        const ev = this.d.event(id);
        if (!seg && !ev) return error(404, 'not_found');
        const th = this.d.store.getThumbnail(id);
        return json(200, { id, event: ev ?? null, segments: seg ?? null, thumbnail: th.status === 200 ? { at: th.thumb!.at, kind: th.thumb!.kind } : null });
    }

    private ack(id: string, body: string | undefined): ApiResponse {
        let upTo: unknown;
        try { upTo = body ? JSON.parse(body)?.upTo : undefined; }
        catch { return error(400, 'bad_request', 'body must be JSON'); }
        if (typeof upTo !== 'number' || !Number.isInteger(upTo) || upTo < -1 || upTo > MAX_SEGMENT_N)
            return error(400, 'bad_request', 'upTo must be an integer segment number (-1 to release none)');
        const released = this.d.store.ack(id, upTo);
        if (released === undefined) return error(404, 'not_found');
        return json(200, { ok: true, released, segments: this.d.store.info(id) ?? null });
    }

    private async clips(cam: CameraApi, q: URLSearchParams): Promise<ApiResponse> {
        if (!cam.hasClips) return error(404, 'not_supported', 'the camera has no VideoClips');
        const start = intParam(q, 'start'), end = intParam(q, 'end');
        if (start === undefined || end === undefined || Number.isNaN(start) || Number.isNaN(end) || end <= start || end - start > MAX_CLIPS_SPAN_MS)
            return error(400, 'bad_request', 'start and end are required, in ms, end after start, at most 7 days apart');
        const clips = (await cam.clips(start, end)).slice(0, MAX_CLIPS);
        return json(200, { camera: cam.id, start, end, clips });
    }

    private async startPlayback(cam: CameraApi, q: URLSearchParams): Promise<ApiResponse> {
        if (!cam.hasRecorder) return error(404, 'not_supported', 'the camera has no VideoRecorder');
        const t = intParam(q, 't');
        const dRaw = q.get('d');
        const now = this.d.now();
        if (t === undefined || Number.isNaN(t) || t < now - MAX_PLAYBACK_AGE_MS || t > now + 60_000)
            return error(400, 'bad_request', 't is required: the start time in ms, within the last 400 days');
        let durationMs: number | undefined;
        if (dRaw !== null && dRaw !== '') {
            if (!/^\d{1,5}$/.test(dRaw) || Number(dRaw) < 1 || Number(dRaw) > MAX_PLAYBACK_S)
                return error(400, 'bad_request', 'd is the duration in s, 1 to 3600');
            durationMs = Number(dRaw) * 1000;
        }
        const s = this.d.playback.start(cam.id, t, durationMs, () => cam.recordingInput(t, durationMs), this.d.ffmpeg, this.d.console);
        const ok = await s.ready(PLAYLIST_READY_MS);
        if (!ok) {
            const why = s.failure ?? 'no video within 15 s';
            this.d.playback.stop(s.id);
            return error(502, 'playback_failed', why);
        }
        return { status: 302, headers: { Location: `playback/${s.id}/index.m3u8`, ...NO_STORE }, body: '' };
    }

    private sessionFile(s: PlaybackSession, file: string | undefined): ApiResponse {
        if (file === 'index.m3u8')
            return { status: 200, headers: { 'Content-Type': 'application/vnd.apple.mpegurl', ...NO_STORE }, body: s.playlist() };
        if (file === 'init.mp4') {
            if (!s.init) return TOO_EARLY();
            s.touch();
            return binary(s.init, 'video/mp4');
        }
        const m = file ? /^(\d{1,7})\.m4s$/.exec(file) : null;
        if (m) {
            const n = Number(m[1]);
            const seg = s.segment(n);
            if (seg) { s.touch(); return binary(seg.data, 'video/iso.segment'); }
            return n >= s.next && !s.ended ? TOO_EARLY() : error(404, 'not_found');
        }
        return error(404, 'not_found');
    }
}

/**
 * The API path of a Scrypted request: everything after "/endpoint/<plugin>/" and an optional "public/", without the query.
 * Undefined when the URL is not below the endpoint.
 */
export function apiPath(url: string | undefined, rootPath: string | undefined, pluginId: string): { path: string; query: URLSearchParams } | undefined {
    if (!url) return undefined;
    const q = url.indexOf('?');
    let path = q >= 0 ? url.slice(0, q) : url;
    const query = new URLSearchParams(q >= 0 ? url.slice(q + 1) : '');
    const roots = [rootPath, `/endpoint/${pluginId}`].filter((r): r is string => !!r).map(r => r.replace(/\/+$/, ''));
    const root = roots.find(r => path === r || path.startsWith(r + '/'));
    if (root === undefined) return undefined;
    path = path.slice(root.length).replace(/^\/+/, '');
    if (path === 'public' || path.startsWith('public/')) path = path.slice(6).replace(/^\/+/, '');
    return { path, query };
}
