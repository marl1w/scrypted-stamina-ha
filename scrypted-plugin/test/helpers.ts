// Test helpers: a manual clock, observation builders, synthetic fMP4 boxes.
/* eslint-disable @typescript-eslint/no-explicit-any */
import { Observation } from '../src/normalize';
import { Clock } from '../src/util';

export class FakeClock implements Clock {
    t = 1_700_000_000_000;
    private timers: { at: number; fn: () => void; id: number }[] = [];
    private ids = 0;
    now() { return this.t; }
    setTimeout(fn: () => void, ms: number) {
        const id = ++this.ids;
        this.timers.push({ at: this.t + Math.max(0, ms), fn, id });
        return id;
    }
    clearTimeout(id: any) { this.timers = this.timers.filter(x => x.id !== id); }
    /** Advances time, running due timers in order (timers they create run too when due). */
    advance(ms: number) {
        const end = this.t + ms;
        for (; ;) {
            this.timers.sort((a, b) => a.at - b.at || a.id - b.id);
            const next = this.timers[0];
            if (!next || next.at > end) break;
            this.timers.shift();
            this.t = Math.max(this.t, next.at);
            next.fn();
        }
        this.t = end;
    }
    get pending() { return this.timers.length; }
}

export function obs(clock: FakeClock, p: Partial<Observation> = {}): Observation {
    return {
        t: clock.now(), recv: clock.now(), source: 'cam-plugin', role: 'trigger', kind: 'object', class: 'person', raw: 'person',
        score: 0.9, box: null, track: null, label: null, zones: null, ...p,
    };
}

// ---- synthetic fragmented MP4 -------------------------------------------------------------------

export function box(type: string, ...payload: Buffer[]): Buffer {
    const body = Buffer.concat(payload);
    const h = Buffer.alloc(8);
    h.writeUInt32BE(8 + body.length, 0);
    h.write(type, 4, 'latin1');
    return Buffer.concat([h, body]);
}

function u32(...v: number[]) { const b = Buffer.alloc(4 * v.length); v.forEach((x, i) => b.writeUInt32BE(x >>> 0, i * 4)); return b; }

/** ftyp + moov with one video track (id 1, timescale 90000) and one audio track (id 2, timescale 16000). */
export function initSegment(): Buffer {
    const trak = (id: number, handler: string, timescale: number, fourcc: string) => box('trak',
        box('tkhd', u32(0, 0, 0, id, 0, 0)),
        box('mdia',
            box('mdhd', u32(0, 0, 0, timescale, 0, 0)),
            box('hdlr', u32(0, 0), Buffer.from(handler, 'latin1'), u32(0, 0, 0)),
            box('minf', box('stbl', box('stsd', u32(0, 1), box(fourcc, Buffer.alloc(8)))))));
    return Buffer.concat([
        box('ftyp', Buffer.from('isom', 'latin1'), u32(512)),
        box('moov', box('mvhd', Buffer.alloc(100)), trak(1, 'vide', 90000, 'avc1'), trak(2, 'soun', 16000, 'mp4a'),
            box('mvex', box('trex', u32(0, 1, 1, 0, 0, 0)), box('trex', u32(0, 2, 1, 1024, 0, 0)))),
    ]);
}

/** One moof+mdat fragment: video starting at `startS` lasting `durS` (20 samples with explicit durations), audio with trex defaults. */
export function fragment(seq: number, startS: number, durS: number, mdatBytes = 1000): Buffer {
    const samples = 20;
    const per = Math.round(durS * 90000 / samples);
    const vtrun = box('trun', u32(0x000301, samples, 0), ...Array.from({ length: samples }, () => u32(per, 50)));
    const vtraf = box('traf', box('tfhd', u32(0x020000, 1)), box('tfdt', u32(0x01000000), u32(0, Math.round(startS * 90000))), vtrun);
    const atraf = box('traf', box('tfhd', u32(0x020000, 2)), box('tfdt', u32(0, Math.round(startS * 16000))), box('trun', u32(0x000001, 15, 0)));
    return Buffer.concat([box('moof', box('mfhd', u32(0, seq)), vtraf, atraf), box('mdat', Buffer.alloc(mdatBytes, seq & 0xff))]);
}

export const sleep = (ms: number) => new Promise<void>(r => setTimeout(r, ms));

export function silentConsole() {
    const lines: string[] = [];
    return { lines, log: (...a: any[]) => { lines.push(a.join(' ')); }, warn: (...a: any[]) => { lines.push(a.join(' ')); } };
}
