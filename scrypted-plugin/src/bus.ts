// Message bus: every message gets a monotonically increasing `seq` within the plugin's `epoch` (new at every plugin start).
// The last RING_SIZE messages are kept so a client can resume after a reconnect.

import { randomId } from './util';

export const RING_SIZE = 5000;
/** the ring holds at most this many bytes (UTF-8) of serialised messages: the real bound (RING_SIZE is a backstop) */
export const RING_BYTES = 8 * 1024 * 1024;

export type MessageType = 'event.start' | 'event.update' | 'event.end' | 'subject.verdict' | 'event.segment';

export interface Message {
    v: 1;
    seq: number;
    epoch: string;
    type: MessageType;
    /** publish time (ms) */
    ts: number;
    [k: string]: unknown;
}

/**
 * A published message, serialised once at publish time: what was sent can never change afterwards (the event objects it was
 * built from keep changing), and the ring costs one string per message.
 */
export class Entry {
    constructor(readonly seq: number, readonly type: MessageType, readonly frame: string, private dataAt: number, readonly bytes: number) { }
    /** the message as JSON: sliced from the frame (one string kept per message) */
    get json(): string { return this.frame.slice(this.dataAt, -2); }
}

export interface Replay {
    messages: Entry[];
    /** the client asked for messages older than the ring holds (some were lost) */
    gap: boolean;
    /** the client's epoch is not ours (or it sent no epoch): the plugin restarted, its open events will never end */
    reset: boolean;
}

export class Bus {
    readonly epoch: string;
    private seq = 0;
    private ring: Entry[] = [];
    private ringBytes = 0;
    private subs = new Set<(m: Entry) => void>();

    constructor(epoch?: string, private ringSize = RING_SIZE, private now: () => number = Date.now, private maxBytes = RING_BYTES) {
        this.epoch = epoch ?? randomId('ep_').slice(0, 15);
    }

    get lastSeq() { return this.seq; }
    get oldestSeq() { return this.ring.length ? this.ring[0].seq : this.seq + 1; }
    get subscribers() { return this.subs.size; }
    get bytes() { return this.ringBytes; }

    publish(type: MessageType, body: Record<string, unknown>): Entry {
        const seq = ++this.seq;
        const json = JSON.stringify({ ...body, v: 1, seq, epoch: this.epoch, type, ts: this.now() });
        const head = `id: ${this.epoch}:${seq}\nevent: ${type}\ndata: `;
        const frame = `${head}${json}\n\n`;
        const e = new Entry(seq, type, frame, head.length, Buffer.byteLength(frame));
        this.ring.push(e);
        this.ringBytes += e.bytes;
        while (this.ring.length > this.ringSize || (this.ringBytes > this.maxBytes && this.ring.length > 1))
            this.ringBytes -= this.ring.shift()!.bytes;
        for (const s of [...this.subs]) {
            try { s(e); } catch { /* a subscriber's failure is its own */ }
        }
        return e;
    }

    /**
     * Messages after `since` (exclusive). No `since` and no epoch: nothing (live only). A different epoch, or a `since`
     * without an epoch: everything in the ring, with `reset`. `since` older than the ring: everything in the ring, with `gap`.
     * `since` beyond the last seq (impossible in this epoch): `reset`.
     */
    replay(since: number | undefined, epoch: string | undefined): Replay {
        if ((epoch !== undefined && epoch !== this.epoch) || (since !== undefined && epoch === undefined))
            return { messages: [...this.ring], gap: false, reset: true };
        if (since === undefined)
            return { messages: [], gap: false, reset: false };
        if (since > this.seq)
            return { messages: [...this.ring], gap: false, reset: true };
        const gap = since < this.oldestSeq - 1;
        return { messages: this.ring.filter(m => m.seq > since), gap, reset: false };
    }

    subscribe(fn: (m: Entry) => void): () => void {
        this.subs.add(fn);
        return () => { this.subs.delete(fn); };
    }
}

export function sseHello(body: Record<string, unknown>): string {
    return `event: hello\ndata: ${JSON.stringify({ v: 1, type: 'hello', ...body })}\n\n`;
}

/** Parses Last-Event-ID ("<epoch>:<seq>") or the since/epoch query. */
export function parseResume(lastEventId: string | undefined, since: string | null | undefined, epoch: string | null | undefined): { since?: number; epoch?: string; invalid?: boolean } {
    if (lastEventId) {
        const m = /^(ep_[A-Za-z0-9_-]{1,32}):(\d{1,15})$/.exec(lastEventId.trim());
        if (!m) return { invalid: true };
        return { epoch: m[1], since: Number(m[2]) };
    }
    const out: { since?: number; epoch?: string; invalid?: boolean } = {};
    if (since !== null && since !== undefined && since !== '') {
        if (!/^\d{1,15}$/.test(since)) return { invalid: true };
        out.since = Number(since);
    }
    if (epoch !== null && epoch !== undefined && epoch !== '') {
        if (!/^ep_[A-Za-z0-9_-]{1,32}$/.test(epoch)) return { invalid: true };
        out.epoch = epoch;
    }
    return out;
}

export interface SseOptions {
    keepaliveMs: number;
    /** the stream ends after this long; the client reconnects and resumes */
    maxLifetimeMs: number;
    /** a client that falls this far behind is cut off (it resumes from its seq) */
    maxQueue: number;
}

export const DEFAULT_SSE: SseOptions = { keepaliveMs: 15_000, maxLifetimeMs: 60 * 60_000, maxQueue: 2000 };

/**
 * An SSE stream as an async generator of Buffers (Scrypted's HttpResponse.sendStream): hello, the replay, then live messages,
 * a comment line as keepalive. `close()` ends it (a newer client replaced it, the plugin stops).
 */
export function sseStream(bus: Bus, resume: { since?: number; epoch?: string }, opts: SseOptions = DEFAULT_SSE, extraHello: Record<string, unknown> = {}) {
    const queue: string[] = [];
    let wake: (() => void) | undefined;
    let closed = false;
    const notify = () => { const w = wake; wake = undefined; w?.(); };
    // publish is synchronous: reading the replay and subscribing in the same tick loses and duplicates nothing
    const r = bus.replay(resume.since, resume.epoch);
    const unsub = bus.subscribe(m => {
        if (closed) return;
        if (queue.length >= opts.maxQueue) { close(); return; }
        queue.push(m.frame);
        notify();
    });
    const head = [sseHello({ epoch: bus.epoch, last_seq: bus.lastSeq, oldest_seq: bus.oldestSeq, resumed: resume.since !== undefined && !r.reset && !r.gap, gap: r.gap, reset: r.reset, ...extraHello }), ...r.messages.map(m => m.frame)];
    queue.unshift(...head);
    const started = Date.now();
    function close() {
        if (closed) return;
        closed = true;
        unsub();
        notify();
    }
    async function* gen(): AsyncGenerator<Buffer, void> {
        try {
            while (!closed || queue.length) {
                if (queue.length) {
                    yield Buffer.from(queue.splice(0, queue.length).join(''));
                    continue;
                }
                if (closed) break;
                const left = opts.maxLifetimeMs - (Date.now() - started);
                if (left <= 0) break;
                let timer: any;
                const ping = await new Promise<boolean>(res => {
                    wake = () => res(false);
                    timer = setTimeout(() => res(true), Math.min(opts.keepaliveMs, left));
                });
                clearTimeout(timer);
                if (Date.now() - started >= opts.maxLifetimeMs) break;
                if (ping && !queue.length && !closed) yield Buffer.from(': keepalive\n\n');
            }
        }
        finally {
            close();
        }
    }
    return { stream: gen(), close, get closed() { return closed; } };
}
