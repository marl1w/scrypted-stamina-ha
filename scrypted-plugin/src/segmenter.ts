// Segmenter: Rebroadcast's prebuffered low-resolution stream -> ffmpeg -c copy -> fragmented MP4 on a pipe -> segments in memory.
// No transcoding and no files. Spike S2: the prebuffer burst gives ~9 s of pre-roll within ~0.2 s of the request.

import type { ChildProcess, SpawnOptions } from 'child_process';
import { Fmp4Splitter, Segment, TrackInfo } from './fmp4';
import { errText, maskUrls } from './util';

export interface StreamInput {
    /** ffmpeg input arguments ending with `-i <url>` */
    args: string[];
    ffmpegPath?: string;
    env?: Record<string, string>;
    /** prebuffer the stream reports (ms), when known */
    prebufferMs?: number;
    audioCodec?: string;
    name?: string;
}

export interface SegmenterDeps {
    /** resolves the camera's stream (getVideoStream + FFmpegInput); `lowRes` false asks the default stream */
    input(prerollMs: number, lowRes: boolean): Promise<StreamInput>;
    ffmpegPath(): Promise<string>;
    spawn(cmd: string, args: string[], opts: SpawnOptions): ChildProcess;
    now(): number;
}

export interface SegmenterOptions {
    segmentS: number;
    prerollMs: number;
    console: Pick<Console, 'log' | 'warn'>;
    label: string;
    /** `k`: the init's number (0 first; a restarted ffmpeg brings init k+1 and its first segment is a discontinuity) */
    onInit(init: Buffer, tracks: TrackInfo[], k: number): void;
    onSegment(seg: Segment): void;
    /** timing marks for the latency stamps (first run only) */
    onMark(mark: 'stream_requested' | 'stream_resolved' | 'first_byte' | 'init_cut' | 'first_segment_cut' | 'first_live_segment_cut', at: number): void;
    /** pre-roll actually received (ms), measured from the burst */
    onPreroll?(ms: number): void;
    /** ffmpeg died after producing output and is being restarted (no pre-roll) */
    onRestart?(reason: string): void;
    /** the segmenter ended; `failure` when it never produced anything or ffmpeg failed for good */
    onEnd(failure?: string): void;
    /** restart ffmpeg (without pre-roll) when it dies after producing output, up to this many times (default 0) */
    restarts?: number;
    /** may the default (main) stream be tried when the low-resolution one fails? (default yes) */
    allowDefault?(): boolean;
}

/** ffmpeg output arguments: copy video (and audio when allowed) into fragmented MP4 on stdout, one fragment per key frame. */
export function outputArgs(audio: boolean): string[] {
    return [
        '-map', '0:v:0', ...(audio ? ['-map', '0:a:0?'] : []),
        '-c', 'copy', ...(audio ? [] : ['-an']), '-sn', '-dn',
        '-f', 'mp4', '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
        'pipe:1',
    ];
}

/** Audio codecs MP4 carries as they are; others are dropped (no transcoding). */
export function audioCopyable(codec: string | undefined): boolean {
    if (!codec) return true;
    return /^(aac|mp4a|opus|mp3|ac3|eac3|alac|flac)/i.test(codec);
}

/** Scrypted's FFmpegInput arguments usually end with `-i <url>`; append it otherwise. */
export function buildInputArgs(inputArguments: string[] | undefined, url: string | undefined): string[] {
    const a = inputArguments || [];
    return a.includes('-i') || !url ? [...a] : [...a, '-i', url];
}

const STOP_GRACE_MS = 3000;
/** no init within this long after spawn: ffmpeg is stuck */
const FIRST_OUTPUT_MS = 15_000;
const RESTART_DELAY_MS = 1000;

export interface SegmenterHandle {
    stop(): void;
    /** pause reading ffmpeg's output (it blocks: back-pressure), and resume */
    pause(): void;
    resume(): void;
    done: Promise<void>;
}

type Once = { ok: boolean; produced: boolean; error?: string; audioSuspect?: boolean; mediaEnd: number };

export function startSegmenter(o: SegmenterOptions, deps: SegmenterDeps): SegmenterHandle {
    let stopped = false;
    let paused = false;
    let cp: ChildProcess | undefined;
    let killTimer: any;
    let wakeDelay: (() => void) | undefined;
    const kill = (sig: NodeJS.Signals) => { try { cp?.kill(sig); } catch { /* gone */ } };
    const stop = () => {
        if (stopped) return;
        stopped = true;
        wakeDelay?.();
        if (cp && cp.exitCode === null) {
            if (paused) cp.stdout?.resume();
            kill('SIGINT');
            killTimer = setTimeout(() => kill('SIGKILL'), STOP_GRACE_MS);
            (killTimer as any).unref?.();
        }
    };
    let inits = 0;
    let marks = true;

    const run = async (): Promise<void> => {
        let lowRes = true;
        let audio = true;
        let prerollMs = o.prerollMs;
        let restarts = 0;
        let offset = 0;
        let produced = false;
        for (let fails = 0; !stopped;) {
            const r = await once(lowRes, audio, prerollMs, offset);
            produced ||= r.produced;
            offset = r.mediaEnd;
            if (stopped) { o.onEnd(produced ? undefined : 'stopped'); return; }
            if (r.produced) {
                // ffmpeg died mid-stream: start again live (no pre-roll: it would repeat video already cut)
                if (restarts >= (o.restarts ?? 0)) { o.onEnd(r.error); return; }
                restarts++;
                prerollMs = 0;
                o.onRestart?.(r.error ?? 'ffmpeg ended');
                o.console.warn(`${o.label}: segmenter restarting (${restarts}) after: ${r.error}`);
                await new Promise<void>(res => { const t = setTimeout(res, RESTART_DELAY_MS); wakeDelay = () => { clearTimeout(t); res(); }; });
                continue;
            }
            // nothing produced: retry without audio, then with the default stream
            if (++fails > 2) { o.onEnd(r.error); return; }
            if (audio && r.audioSuspect) audio = false;
            else if (lowRes && (o.allowDefault?.() ?? true)) lowRes = false;
            else { o.onEnd(r.error); return; }
            o.console.warn(`${o.label}: segmenter retry (${lowRes ? 'low-resolution' : 'default stream'}, audio ${audio ? 'on' : 'off'}) after: ${r.error}`);
        }
        o.onEnd(produced ? undefined : 'stopped');
    };

    const once = async (lowRes: boolean, audioWanted: boolean, prerollMs: number, offset: number): Promise<Once> => {
        const mark = (m: Parameters<SegmenterOptions['onMark']>[0], at: number) => { if (marks) o.onMark(m, at); };
        mark('stream_requested', deps.now());
        let input: StreamInput;
        try {
            input = await deps.input(prerollMs, lowRes);
        }
        catch (e) {
            return { ok: false, produced: false, error: `stream: ${errText(e)}`, mediaEnd: offset };
        }
        mark('stream_resolved', deps.now());
        if (stopped) return { ok: true, produced: false, mediaEnd: offset };
        const audio = audioWanted && audioCopyable(input.audioCodec);
        let path: string;
        try { path = input.ffmpegPath || await deps.ffmpegPath(); }
        catch (e) { return { ok: false, produced: false, error: `ffmpeg path: ${errText(e)}`, mediaEnd: offset }; }
        if (stopped) return { ok: true, produced: false, mediaEnd: offset };
        const args = ['-hide_banner', '-loglevel', 'error', '-nostdin', ...input.args, ...outputArgs(audio)];
        let produced = false;
        let firstByte = false;
        let firstSeg = false;
        let firstLive = false;
        let stderr = '';
        let splitError: string | undefined;
        let firstByteAt = 0;
        let preroll = 0;
        let inBurst = prerollMs > 0;
        let lastFragAt = 0;
        let mediaEnd = offset;
        let newInit = false;
        const splitter = new Fmp4Splitter(o.segmentS, {
            onInit: (init, tracks) => {
                produced = true;
                mark('init_cut', deps.now());
                newInit = inits > 0;
                o.onInit(init, tracks, inits++);
            },
            onFragment: (f, end) => {
                const now = deps.now();
                // the prebuffer arrives as a burst, far faster than real time; it is over once a fragment takes at least
                // half its own duration to arrive
                if (inBurst && lastFragAt && now - lastFragAt >= f.duration * 500) inBurst = false;
                lastFragAt = now;
                if (inBurst) {
                    // media received beyond the wall time elapsed is pre-roll
                    const p = Math.max(0, Math.round((end - (now - firstByteAt) / 1000) * 1000));
                    if (p > preroll) { preroll = p; o.onPreroll?.(p); }
                }
            },
            onSegment: seg => {
                const now = deps.now();
                seg.preroll = inBurst;
                seg.start += offset;
                seg.discontinuity = newInit;
                newInit = false;
                mediaEnd = seg.start + seg.duration;
                if (!firstSeg) { firstSeg = true; mark('first_segment_cut', now); }
                if (!firstLive && !inBurst) { firstLive = true; mark('first_live_segment_cut', now); }
                o.onSegment(seg);
            },
        });
        return new Promise(resolve => {
            try {
                cp = deps.spawn(path, args, { env: { ...process.env, ...(input.env || {}) }, stdio: ['ignore', 'pipe', 'pipe'] });
            }
            catch (e) {
                resolve({ ok: false, produced: false, error: `spawn: ${errText(e)}`, mediaEnd });
                return;
            }
            const proc = cp;
            // stopped while spawning: no orphan ffmpeg
            if (stopped) kill('SIGKILL');
            else if (paused) proc.stdout?.pause();
            const firstTimer = setTimeout(() => {
                if (!produced) { splitError = 'no output within 15 s'; try { proc.kill('SIGKILL'); } catch { /* gone */ } }
            }, FIRST_OUTPUT_MS);
            (firstTimer as any).unref?.();
            proc.stdout?.on('data', (d: Buffer) => {
                if (!firstByte) { firstByte = true; firstByteAt = deps.now(); mark('first_byte', firstByteAt); }
                if (splitError) return;
                try { splitter.push(d); }
                catch (e) {
                    splitError = errText(e);
                    try { proc.kill('SIGKILL'); } catch { /* gone */ }
                }
            });
            proc.stderr?.on('data', (d: Buffer) => { stderr = (stderr + d.toString()).slice(-4000); });
            let settled = false;
            const end = (code: number | null, err?: Error) => {
                if (settled) return;
                settled = true;
                clearTimeout(firstTimer);
                clearTimeout(killTimer);
                if (!splitError) {
                    try { splitter.flush(); } catch { /* ignore */ }
                }
                if (produced) marks = false;
                const tail = maskUrls(stderr).split(/\r?\n/).filter(Boolean).slice(-3).join(' | ');
                if (stopped && !splitError) { resolve({ ok: true, produced, mediaEnd }); return; }
                const error = splitError ?? (err ? errText(err) : `ffmpeg exited ${code}${tail ? ': ' + tail : ''}`);
                resolve({ ok: false, produced, error, audioSuspect: audio && /audio|codec|could not find tag|Tag .* incompatible/i.test(stderr), mediaEnd });
            };
            proc.on('error', e => end(null, e));
            proc.on('close', code => end(code));
        });
    };

    const done = run().catch(e => { o.onEnd(errText(e)); });
    return {
        stop, done,
        pause: () => { if (!paused) { paused = true; cp?.stdout?.pause(); } },
        resume: () => { if (paused) { paused = false; cp?.stdout?.resume(); } },
    };
}
