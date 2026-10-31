import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Segment } from '../src/fmp4';
import { audioCopyable, buildInputArgs, outputArgs, startSegmenter } from '../src/segmenter';
import { PlaybackManager, PlaybackSession } from '../src/playback';
import { fakeSpawn } from './fakes';
import { fragment, silentConsole, sleep } from './helpers';

function run(over: { input?: any; fail?: boolean } = {}) {
    const { procs, spawn } = fakeSpawn();
    const segs: Segment[] = [];
    const marks: string[] = [];
    const inits: Buffer[] = [];
    let ended: string | undefined | null = null;
    const con = silentConsole();
    const h = startSegmenter({
        segmentS: 2, prerollMs: 10_000, console: con, label: 'cam',
        onInit: b => inits.push(b), onSegment: s => segs.push(s), onMark: m => marks.push(m), onEnd: f => { ended = f; },
    }, {
        input: over.input ?? (async () => ({ args: ['-i', 'rtsp://u:p@192.0.2.1/x?token=t'], audioCodec: 'aac' })),
        ffmpegPath: async () => 'ffmpeg', spawn, now: () => Date.now(),
    });
    return { h, procs, segs, marks, inits, con, get ended() { return ended; } };
}

test('ffmpeg arguments: copy into fragmented MP4 on stdout, audio optional', () => {
    const a = outputArgs(true);
    assert.ok(a.includes('copy'));
    assert.equal(a[a.length - 1], 'pipe:1');
    assert.ok(a.join(' ').includes('frag_keyframe+empty_moov+default_base_moof'));
    assert.ok(outputArgs(false).includes('-an'));
    assert.ok(audioCopyable('aac') && audioCopyable(undefined) && !audioCopyable('pcm_alaw'));
    assert.deepEqual(buildInputArgs(['-f', 'rtsp', '-i', 'u'], 'u'), ['-f', 'rtsp', '-i', 'u']);
    assert.deepEqual(buildInputArgs(['-f', 'rtsp'], 'u'), ['-f', 'rtsp', '-i', 'u']);
});

test('segmenter: prebuffer burst becomes init + segments at once; stop flushes and ends cleanly', async () => {
    const r = run();
    await sleep(5);
    assert.equal(r.procs.length, 1);
    assert.ok(r.procs[0].args.includes('pipe:1'));
    r.procs[0].burst(9);
    await sleep(5);
    assert.equal(r.inits.length, 1);
    assert.equal(r.segs.length, 4, '9 one-second fragments: 4 segments of 2 s, 1 pending');
    assert.deepEqual(r.marks.slice(0, 5), ['stream_requested', 'stream_resolved', 'first_byte', 'init_cut', 'first_segment_cut']);
    assert.ok(r.segs.every(s => s.preroll), 'the burst is pre-roll');
    assert.ok(!r.marks.includes('first_live_segment_cut'));
    // live: fragments now arrive at real-time pace
    await sleep(600);
    r.procs[0].stdout.write(Buffer.concat([1, 2].map(i => fragment(20 + i, 9 + i, 1))));
    await sleep(5);
    assert.equal(r.segs[4].preroll, false);
    assert.ok(r.marks.includes('first_live_segment_cut'));
    r.h.stop();
    await r.h.done;
    assert.deepEqual(r.procs[0].killed, ['SIGINT']);
    assert.equal(r.segs.length, 6, 'the pending fragment is flushed');
    assert.equal(r.ended, undefined);
});

test('segmenter: a stream that fails to resolve falls back to the default stream; failures are masked', async () => {
    const asked: boolean[] = [];
    const r = run({ input: async (_p: number, lowRes: boolean) => { asked.push(lowRes); throw new Error('cannot open rtsp://admin:hunter2@192.0.2.5:554/path?x=1'); } });
    await r.h.done;
    assert.deepEqual(asked, [true, false]);
    assert.match(String(r.ended), /stream:/);
    assert.ok(!String(r.ended).includes('hunter2'));
    assert.ok(!r.con.lines.join().includes('hunter2'));
});

test('segmenter: ffmpeg dying before any output retries without audio, then the default stream', async () => {
    const r = run();
    await sleep(5);
    r.procs[0].stderr.write('Could not find tag for codec pcm_alaw in stream #1, codec not currently supported in container\n');
    r.procs[0].exit(1);
    await sleep(10);
    assert.equal(r.procs.length, 2);
    assert.ok(r.procs[1].args.includes('-an'));
    r.procs[1].exit(1);
    await sleep(10);
    assert.equal(r.procs.length, 3);
    r.procs[2].exit(1);
    await r.h.done;
    assert.match(String(r.ended), /ffmpeg exited 1/);
});

test('segmenter: ffmpeg ending after output is a failure, not a retry (segments stay valid)', async () => {
    const r = run();
    await sleep(5);
    r.procs[0].burst(4);
    await sleep(5);
    r.procs[0].exit(0);
    await r.h.done;
    assert.equal(r.procs.length, 1);
    assert.equal(r.segs.length, 2);
    assert.match(String(r.ended), /exited 0/);
});

test('segmenter: garbage on the pipe kills ffmpeg instead of growing memory', async () => {
    const r = run();
    await sleep(5);
    const bad = Buffer.alloc(16); bad.writeUInt32BE(3, 0); bad.write('moof', 4, 'latin1');
    r.procs[0].stdout.write(bad);
    await sleep(10);
    assert.ok(r.procs[0].killed.includes('SIGKILL'));
    // nothing was produced: one retry on the default stream
    assert.equal(r.procs.length, 2);
    r.procs[1].stdout.write(bad);
    await r.h.done;
    assert.match(String(r.ended), /malformed/);
});

test('playback session: live playlist with program date time; over its memory it drops fetched segments, else pauses; ENDLIST at the end', async () => {
    const s = new PlaybackSession('7', Date.UTC(2026, 0, 1), undefined, 0, 250);
    const pauses: string[] = [];
    s.handle = { stop() { }, pause: () => pauses.push('pause'), resume: () => pauses.push('resume'), done: Promise.resolve() };
    const ready = s.ready(1000);
    s.addInit(Buffer.alloc(10));
    s.addSegment(Buffer.alloc(100), 0, 2);
    assert.equal(await ready, true);
    s.addSegment(Buffer.alloc(100), 2, 2);
    s.addSegment(Buffer.alloc(100), 4, 2);
    // nothing fetched yet: nothing is dropped, reading pauses
    assert.equal(s.segs.length, 3);
    assert.equal(pauses[pauses.length - 1], 'pause');
    s.segment(0);
    assert.equal(s.segs.length, 2, 'the fetched segment made room');
    assert.equal(pauses[pauses.length - 1], 'resume');
    const pl = s.playlist();
    assert.match(pl, /#EXT-X-MEDIA-SEQUENCE:1/);
    assert.match(pl, /#EXT-X-PROGRAM-DATE-TIME:2026-01-01T00:00:02.000Z/);
    assert.ok(!pl.includes('ENDLIST'));
    s.end();
    assert.match(s.playlist(), /#EXT-X-ENDLIST/);
    assert.equal(await new PlaybackSession('7', 0, undefined, 0, 1e6).ready(10), false);
    // a session asked for an hour lives at least an hour plus the margin
    assert.equal(new PlaybackSession('7', 0, 3600_000, 0, 1e6).lifetime(30 * 60_000), 3600_000 + 5 * 60_000);
});

test('stopped while ffmpeg\'s path is being looked up: nothing is spawned', async () => {
    const { procs, spawn } = fakeSpawn();
    let release!: (p: string) => void;
    let ended: string | undefined | null = null;
    const h = startSegmenter({ segmentS: 2, prerollMs: 0, console: silentConsole(), label: 'cam', onInit: () => { }, onSegment: () => { }, onMark: () => { }, onEnd: f => { ended = f; } },
        { input: async () => ({ args: ['-i', 'x'] }), ffmpegPath: () => new Promise(r => { release = r; }), spawn, now: () => Date.now() });
    await sleep(5);
    h.stop();
    release('ffmpeg');
    await h.done;
    assert.equal(procs.length, 0);
    assert.equal(ended, 'stopped');
});

test('pause and resume reach ffmpeg\'s stdout (back-pressure)', async () => {
    const r = run();
    await sleep(5);
    r.h.pause();
    assert.equal(r.procs[0].stdout.isPaused(), true);
    r.h.resume();
    assert.equal(r.procs[0].stdout.isPaused(), false);
    r.h.stop();
    await r.h.done;
});

test('playback: a player starting at the live end lets the skipped head go; the playlist starts at its beginning', () => {
    const s = new PlaybackSession('7', 0, undefined, 0, 250);
    const pauses: string[] = [];
    s.handle = { stop() { }, pause: () => pauses.push('pause'), resume: () => pauses.push('resume'), done: Promise.resolve() };
    s.addInit(Buffer.alloc(10));
    assert.match(s.playlist(), /#EXT-X-PLAYLIST-TYPE:EVENT/);
    assert.match(s.playlist(), /#EXT-X-START:TIME-OFFSET=0/);
    for (let i = 0; i < 3; i++) s.addSegment(Buffer.alloc(100), i * 2, 2);
    assert.equal(pauses[pauses.length - 1], 'pause');
    // the player asks for the newest segment first: 0 and 1 will never be fetched
    s.segment(2);
    assert.deepEqual(s.segs.map(x => x.n), [1, 2], 'unfetched 0 lies below the fetched 2: dropped (enough to fit)');
    assert.equal(pauses[pauses.length - 1], 'resume');
    const pl = s.playlist();
    assert.match(pl, /#EXT-X-MEDIA-SEQUENCE:1/);
    assert.ok(!pl.includes('PLAYLIST-TYPE:EVENT'), 'not an EVENT playlist once segments left it');
});

test('playback: only media fetches keep a session alive; a session paused for 2 min stops', () => {
    let now = 0;
    const m = new PlaybackManager(undefined, () => now);
    const { spawn } = fakeSpawn();
    const deps = { ffmpegPath: async () => 'ffmpeg', spawn: spawn as any, now: () => now };
    const s = m.start('7', 0, undefined, async () => ({ args: ['-i', 'x'] }), deps, silentConsole());
    now = 50_000;
    m.get(s.id)!.playlist();
    now = 61_000;
    m.sweep();
    assert.equal(m.count, 0, 'playlist polls do not count as activity');
    const t = m.start('7', 0, undefined, async () => ({ args: ['-i', 'x'] }), deps, silentConsole());
    t.pausedAt = now;
    t.touch();
    now += 2 * 60_000;
    t.touch();
    m.sweep();
    assert.equal(m.count, 0, 'paused too long');
});
