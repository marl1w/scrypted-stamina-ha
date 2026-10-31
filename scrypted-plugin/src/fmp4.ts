// Fragmented MP4 splitter. ffmpeg writes `-f mp4 -movflags frag_keyframe+empty_moov+default_base_moof` to a pipe:
// ftyp + moov (the init segment), then one moof + mdat pair per key frame. Fragments are grouped into segments of about
// the target duration (media time, from tfdt and trun, so a prebuffer burst is cut by its own timestamps).

export interface Box {
    type: string;
    start: number;
    size: number;
    headerSize: number;
}

/** Top-level boxes of a buffer, as far as they are complete. */
export function readBoxes(buf: Buffer, from = 0, to = buf.length): Box[] {
    const out: Box[] = [];
    let i = from;
    while (i + 8 <= to) {
        let size = buf.readUInt32BE(i);
        const type = buf.toString('latin1', i + 4, i + 8);
        let headerSize = 8;
        if (size === 1) {
            if (i + 16 > to) break;
            const big = buf.readBigUInt64BE(i + 8);
            if (big > BigInt(Number.MAX_SAFE_INTEGER)) throw new Error('mp4 box too large');
            size = Number(big);
            headerSize = 16;
        }
        else if (size === 0)
            size = to - i;
        if (size < headerSize) throw new Error(`malformed mp4 box ${JSON.stringify(type)}`);
        if (i + size > to) break;
        out.push({ type, start: i, size, headerSize });
        i += size;
    }
    return out;
}

function child(buf: Buffer, parent: Box, type: string): Box | undefined {
    return readBoxes(buf, parent.start + parent.headerSize, parent.start + parent.size).find(b => b.type === type);
}

function children(buf: Buffer, parent: Box, type: string): Box[] {
    return readBoxes(buf, parent.start + parent.headerSize, parent.start + parent.size).filter(b => b.type === type);
}

export interface TrackInfo {
    id: number;
    handler: string;
    timescale: number;
    defaultDuration: number;
    codec?: string;
}

/** Tracks of a moov: id, handler (vide/soun), timescale, trex default sample duration, sample entry fourcc. */
export function parseMoov(buf: Buffer, moov: Box): TrackInfo[] {
    const tracks: TrackInfo[] = [];
    const trex = new Map<number, number>();
    const mvex = child(buf, moov, 'mvex');
    if (mvex)
        for (const t of children(buf, mvex, 'trex')) {
            const p = t.start + t.headerSize;
            trex.set(buf.readUInt32BE(p + 4), buf.readUInt32BE(p + 12));
        }
    for (const trak of children(buf, moov, 'trak')) {
        const tkhd = child(buf, trak, 'tkhd');
        const mdia = child(buf, trak, 'mdia');
        if (!tkhd || !mdia) continue;
        const tp = tkhd.start + tkhd.headerSize;
        const id = buf.readUInt32BE(tp + (buf[tp] === 1 ? 20 : 12));
        const mdhd = child(buf, mdia, 'mdhd');
        const hdlr = child(buf, mdia, 'hdlr');
        if (!mdhd || !hdlr) continue;
        const mp = mdhd.start + mdhd.headerSize;
        const timescale = buf.readUInt32BE(mp + (buf[mp] === 1 ? 20 : 12));
        const handler = buf.toString('latin1', hdlr.start + hdlr.headerSize + 8, hdlr.start + hdlr.headerSize + 12);
        let codec: string | undefined;
        const minf = child(buf, mdia, 'minf');
        const stbl = minf && child(buf, minf, 'stbl');
        const stsd = stbl && child(buf, stbl, 'stsd');
        if (stsd && stsd.size >= stsd.headerSize + 16)
            codec = buf.toString('latin1', stsd.start + stsd.headerSize + 12, stsd.start + stsd.headerSize + 16);
        tracks.push({ id, handler, timescale, defaultDuration: trex.get(id) ?? 0, codec });
    }
    return tracks;
}

export interface FragmentTiming {
    /** track id -> [base decode time, summed duration] in the track's timescale */
    tracks: Map<number, { base: number; duration: number }>;
}

/** Decode time and duration of each track run in a moof. */
export function parseMoof(buf: Buffer, moof: Box, tracks: TrackInfo[]): FragmentTiming {
    const out = new Map<number, { base: number; duration: number }>();
    for (const traf of children(buf, moof, 'traf')) {
        const tfhd = child(buf, traf, 'tfhd');
        if (!tfhd) continue;
        const hp = tfhd.start + tfhd.headerSize;
        const tf = buf.readUInt32BE(hp) & 0xffffff;
        const id = buf.readUInt32BE(hp + 4);
        let q = hp + 8;
        if (tf & 0x01) q += 8; // base data offset
        if (tf & 0x02) q += 4; // sample description index
        let defDur = tracks.find(t => t.id === id)?.defaultDuration ?? 0;
        if (tf & 0x08) defDur = buf.readUInt32BE(q);
        let base = 0;
        const tfdt = child(buf, traf, 'tfdt');
        if (tfdt) {
            const p = tfdt.start + tfdt.headerSize;
            base = buf[p] === 1 ? Number(buf.readBigUInt64BE(p + 4)) : buf.readUInt32BE(p + 4);
        }
        let duration = 0;
        for (const trun of children(buf, traf, 'trun')) {
            const p = trun.start + trun.headerSize;
            const flags = buf.readUInt32BE(p) & 0xffffff;
            const count = buf.readUInt32BE(p + 4);
            let r = p + 8;
            if (flags & 0x01) r += 4;
            if (flags & 0x04) r += 4;
            const per = ((flags & 0x100) ? 4 : 0) + ((flags & 0x200) ? 4 : 0) + ((flags & 0x400) ? 4 : 0) + ((flags & 0x800) ? 4 : 0);
            if (flags & 0x100) {
                for (let k = 0; k < count && r + 4 <= trun.start + trun.size; k++, r += per)
                    duration += buf.readUInt32BE(r);
            }
            else
                duration += defDur * count;
        }
        out.set(id, { base, duration });
    }
    return { tracks: out };
}

export interface Fragment {
    data: Buffer;
    /** media start and duration of the reference track (s) */
    start: number;
    duration: number;
}

export interface Segment {
    data: Buffer;
    /** media start (s) relative to the first fragment */
    start: number;
    duration: number;
    fragments: number;
    /** set by the segmenter: cut during the prebuffer burst (entirely before the request) */
    preroll?: boolean;
    /** set by the segmenter: the first segment after a new init (ffmpeg restarted) */
    discontinuity?: boolean;
}

export interface SplitterHooks {
    onInit(init: Buffer, tracks: TrackInfo[]): void;
    onSegment(seg: Segment): void;
    /** a fragment arrived (for latency and pre-roll measurement) */
    onFragment?(f: Fragment, mediaEnd: number): void;
}

/** Limits that protect against a misbehaving stream. */
export const MAX_PENDING_BYTES = 16 * 1024 * 1024;

/**
 * Splits a fragmented MP4 byte stream into an init segment and segments of about `targetS` seconds (media time).
 * A segment is cut as soon as its fragments add up to at least 90% of the target, so 1 s key frames give 2 s segments
 * with no extra wait. `flush()` emits what is left at the end.
 */
export class Fmp4Splitter {
    private acc: Buffer = Buffer.alloc(0);
    private initParts: Buffer[] = [];
    private tracks: TrackInfo[] = [];
    private ref?: TrackInfo;
    private gotInit = false;
    private pendingMoof?: Buffer;
    private pendingTiming?: FragmentTiming;
    private frags: Fragment[] = [];
    private origin?: number;
    private mediaEnd = 0;
    bytes = 0;

    constructor(public targetS: number, private hooks: SplitterHooks) { }

    push(chunk: Buffer) {
        this.bytes += chunk.length;
        this.acc = this.acc.length ? Buffer.concat([this.acc, chunk]) : chunk;
        if (this.acc.length > MAX_PENDING_BYTES)
            throw new Error('mp4 box larger than the limit');
        const boxes = readBoxes(this.acc);
        let used = 0;
        for (const b of boxes) {
            this.box(this.acc.subarray(b.start, b.start + b.size), b.type);
            used = b.start + b.size;
        }
        if (used) this.acc = Buffer.from(this.acc.subarray(used));
    }

    private box(data: Buffer, type: string) {
        if (!this.gotInit) {
            if (type === 'moof') {
                this.finishInit();
            }
            else {
                if (type === 'ftyp' || type === 'moov') this.initParts.push(Buffer.from(data));
                return;
            }
        }
        if (type === 'moof') {
            this.pendingMoof = Buffer.from(data);
            this.pendingTiming = parseMoof(this.pendingMoof, { type, start: 0, size: data.length, headerSize: 8 }, this.tracks);
            return;
        }
        if (type === 'mdat' && this.pendingMoof) {
            const t = this.pendingTiming?.tracks;
            const ref = this.ref;
            const rt = ref && t?.get(ref.id) || (t && [...t.values()][0]);
            const scale = ref && t?.get(ref.id) ? ref.timescale : (this.tracks.find(x => t?.get(x.id) === rt)?.timescale || 1);
            const abs = rt ? rt.base / scale : this.mediaEnd;
            if (this.origin === undefined) this.origin = abs;
            const f: Fragment = { data: Buffer.concat([this.pendingMoof, data]), start: abs - this.origin, duration: rt ? rt.duration / scale : 0 };
            this.pendingMoof = undefined;
            this.pendingTiming = undefined;
            this.mediaEnd = Math.max(this.mediaEnd, f.start + f.duration);
            this.hooks.onFragment?.(f, this.mediaEnd);
            this.frags.push(f);
            const total = this.frags.reduce((a, x) => a + x.duration, 0);
            if (total >= this.targetS * 0.9) this.cut();
        }
        // other boxes (mfra, free, styp, sidx) are ignored
    }

    private finishInit() {
        this.gotInit = true;
        const init = Buffer.concat(this.initParts);
        this.initParts = [];
        const moov = readBoxes(init).find(b => b.type === 'moov');
        this.tracks = moov ? parseMoov(init, moov) : [];
        this.ref = this.tracks.find(t => t.handler === 'vide') ?? this.tracks[0];
        this.hooks.onInit(init, this.tracks);
    }

    private cut() {
        if (!this.frags.length) return;
        const fr = this.frags;
        this.frags = [];
        this.hooks.onSegment({
            data: Buffer.concat(fr.map(f => f.data)),
            start: fr[0].start,
            duration: fr.reduce((a, f) => a + f.duration, 0),
            fragments: fr.length,
        });
    }

    /** End of stream: what is left becomes the last segment. */
    flush() {
        this.cut();
    }
}
