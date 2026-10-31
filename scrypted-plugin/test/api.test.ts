import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Api, ApiDeps, ApiRequest, apiPath, bearer } from '../src/api';
import { Bus } from '../src/bus';
import { PlaybackManager } from '../src/playback';
import { SegmentStore } from '../src/segments';
import { safeEqual } from '../src/util';
import { fakeSpawn } from './fakes';
import { FakeClock, silentConsole, sleep } from './helpers';

const TOKEN = 'tok_0123456789abcdefghijklmnopqrstuvwxyzABCDEFG';
const EV = 'ev_AAAAAAAAAAAAAAAAAAAAAA';

function setup(over: Partial<ApiDeps> = {}) {
    const clock = new FakeClock();
    const bus = new Bus('ep_test');
    const store = new SegmentStore(undefined, clock);
    const playback = new PlaybackManager(undefined, () => clock.now());
    const { procs, spawn } = fakeSpawn();
    const con = silentConsole();
    const api = new Api({
        token: () => TOKEN, bus, store, playback,
        cameras: () => [{ id: '7' }],
        camera: id => id === '7' ? {
            id, hasRecorder: true, hasClips: true,
            clips: async (s, e) => [{ id: 'c1', start: s, duration: 1000, event: 'motion', description: null, classes: [], thumbnail_id: null, video_id: 'c1' }].filter(() => e > s),
            recordingInput: async () => ({ args: ['-i', 'rtsp://192.0.2.1/rec'] }),
        } : undefined,
        health: () => ({ version: 'x' }),
        event: id => id === EV ? { id } : undefined,
        events: since => since <= 5 ? [{ id: EV, start: 5 }] : [],
        ffmpeg: { ffmpegPath: async () => 'ffmpeg', spawn: spawn as any, now: () => Date.now() },
        console: con, now: () => clock.now(),
        ...over,
    });
    const req = (path: string, o: Partial<ApiRequest> & { q?: string; auth?: string | null } = {}): ApiRequest => ({
        method: o.method ?? 'GET', path, query: new URLSearchParams(o.q ?? ''),
        headers: { ...(o.auth === null ? {} : { authorization: o.auth ?? `Bearer ${TOKEN}` }), ...(o.headers ?? {}) }, body: o.body,
    });
    return { api, bus, store, playback, clock, req, procs, con };
}

const body = (r: any) => JSON.parse(String(r.body));

test('token: constant-time comparison, bearer parsing', () => {
    assert.ok(safeEqual(TOKEN, TOKEN));
    assert.ok(!safeEqual(TOKEN, TOKEN + 'x'));
    assert.ok(!safeEqual('', ''), 'an empty token never matches');
    assert.equal(bearer({ authorization: 'Bearer abc' }), 'abc');
    assert.equal(bearer({ authorization: 'bearer   abc  ' }), 'abc');
    assert.equal(bearer({ authorization: 'Basic abc' }), undefined);
});

test('auth: every route refuses a missing or wrong token', async () => {
    const { api, req, con } = setup();
    for (const p of ['v1/health', 'v1/cameras', 'v1/events/stream', `v1/events/${EV}/init.mp4`, `v1/events/${EV}`, 'v1/cameras/7/clips'])
        for (const auth of [null, 'Bearer wrong', `Basic ${TOKEN}`]) {
            const r = await api.handle(req(p, { auth }));
            assert.equal(r.status, 401, `${p} ${auth}`);
        }
    assert.equal((await api.handle(req('v1/health'))).status, 200);
    assert.equal(con.lines.length, 1, 'refusals are logged at most once a minute');
});

test('health and cameras', async () => {
    const { api, req } = setup();
    const h = body(await api.handle(req('v1/health')));
    assert.equal(h.ok, true);
    assert.equal(h.api, 1);
    assert.deepEqual(body(await api.handle(req('v1/cameras'))), { cameras: [{ id: '7' }] });
    assert.equal((await api.handle(req('v1/cameras', { method: 'POST' }))).status, 405);
});

test('route validation: traversal, bad ids, bad numbers, unknown routes are 404', async () => {
    const { api, req } = setup();
    for (const p of ['v1/events/../health', 'v1//cameras', 'v2/health', 'v1/events/ev_short/init.mp4', `v1/events/${EV}/x.m4s`, `v1/events/${EV}/-1.m4s`, `v1/events/${EV}/99999999.m4s`,
        'v1/cameras/a%2Fb/clips', 'v1/cameras/7/../../health', 'v1/cameras/8/clips', `v1/events/${EV}/init.mp4/x`, 'v1/cameras/7/playback/pb_x/index.m3u8', 'v1/a/b/c/d/e/f/g'])
        assert.equal((await api.handle(req(p))).status, 404, p);
});

test('segments: 425 until cut, 200 with the right type, 404 after ack', async () => {
    const { api, req, store } = setup();
    store.open(EV, '7');
    let r = await api.handle(req(`v1/events/${EV}/init.mp4`));
    assert.equal(r.status, 425);
    assert.equal(r.headers['Retry-After'], '1');
    store.setInit(EV, Buffer.from('init'));
    store.addSegment(EV, Buffer.from('seg0'), 0, 2);
    r = await api.handle(req(`v1/events/${EV}/init.mp4`));
    assert.equal(r.status, 200);
    assert.equal(r.headers['Content-Type'], 'video/mp4');
    r = await api.handle(req(`v1/events/${EV}/0.m4s`));
    assert.equal(r.headers['Content-Type'], 'video/iso.segment');
    assert.equal(String(r.body), 'seg0');
    assert.equal((await api.handle(req(`v1/events/${EV}/1.m4s`))).status, 425);
    const a = await api.handle(req(`v1/events/${EV}/ack`, { method: 'POST', body: '{"upTo":0}' }));
    assert.equal(a.status, 200);
    assert.equal(body(a).released, 1);
    assert.equal((await api.handle(req(`v1/events/${EV}/0.m4s`))).status, 404);
    const info = body(await api.handle(req(`v1/events/${EV}`)));
    assert.equal(info.segments.acked_up_to, 0);
    assert.equal(info.event.id, EV);
});

test('ack: body validated', async () => {
    const { api, req, store } = setup();
    store.open(EV, '7');
    for (const b of ['', 'x', '{"upTo":"3"}', '{"upTo":1.5}', '{"upTo":-2}', '{"upTo":99999999}', '{}'])
        assert.equal((await api.handle(req(`v1/events/${EV}/ack`, { method: 'POST', body: b }))).status, 400, b);
    assert.equal((await api.handle(req(`v1/events/${EV}/ack`, { method: 'GET' }))).status, 405);
    assert.equal((await api.handle(req('v1/events/ev_BBBBBBBBBBBBBBBBBBBBBB/ack', { method: 'POST', body: '{"upTo":1}' }))).status, 404);
});

test('SSE route: event-stream headers, resume from since, invalid resume is 400', async () => {
    const { api, req, bus } = setup();
    bus.publish('event.start', { n: 1 });
    bus.publish('event.end', { n: 2 });
    const r = await api.handle(req('v1/events/stream', { q: 'since=1&epoch=ep_test' }));
    assert.equal(r.headers['Content-Type'], 'text/event-stream');
    const first = (await r.stream!.next()).value!.toString();
    assert.match(first, /event: hello/);
    assert.match(first, /id: ep_test:2\nevent: event.end/);
    assert.ok(!first.includes('ep_test:1\n'));
    assert.equal((await api.handle(req('v1/events/stream', { q: 'since=abc' }))).status, 400);
    assert.equal((await api.handle(req('v1/events/stream', { headers: { 'last-event-id': 'nonsense' } }))).status, 400);
    api.closeStreams();
});

test('SSE route: at most 4 streams, the oldest is closed', async () => {
    const { api, req } = setup();
    const rs = [];
    for (let i = 0; i < 5; i++) rs.push(await api.handle(req('v1/events/stream')));
    assert.equal(api.sseClients, 4);
    await rs[0].stream!.next();
    assert.equal((await rs[0].stream!.next()).done, true);
    api.closeStreams();
});

test('long poll: returns messages after since, waits for new ones', async () => {
    const { api, req, bus } = setup();
    bus.publish('event.start', {});
    const r = body(await api.handle(req('v1/events/poll', { q: 'since=0&epoch=ep_test' })));
    assert.equal(r.messages.length, 1);
    const p = api.handle(req('v1/events/poll', { q: 'since=1&epoch=ep_test&wait=5' }));
    await sleep(5);
    bus.publish('event.end', {});
    const w = body(await p);
    assert.equal(w.messages[0].type, 'event.end');
    const reset = body(await api.handle(req('v1/events/poll', { q: 'since=1&epoch=ep_other' })));
    assert.equal(reset.reset, true);
});

test('clips: validated range, passthrough', async () => {
    const { api, req, clock } = setup();
    const now = clock.now();
    for (const q of ['', `start=${now}`, `start=${now}&end=${now - 1}`, `start=0&end=${8 * 24 * 3600_000}`, 'start=x&end=1', 'start=-1&end=5'])
        assert.equal((await api.handle(req('v1/cameras/7/clips', { q }))).status, 400, q);
    const r = body(await api.handle(req('v1/cameras/7/clips', { q: `start=${now - 1000}&end=${now}` })));
    assert.equal(r.clips[0].id, 'c1');
});

test('playback: validated query, 302 to a session, session files need no token, idle sessions stop', async () => {
    const { api, req, clock, procs, playback } = setup();
    const now = clock.now();
    for (const q of ['', 't=abc', `t=${now + 3600_000}`, `t=${now - 401 * 24 * 3600_000}`, `t=${now}&d=0`, `t=${now}&d=4000`, `t=${now}&d=1.5`])
        assert.equal((await api.handle(req('v1/cameras/7/playback.m3u8', { q }))).status, 400, q);
    const pending = api.handle(req('v1/cameras/7/playback.m3u8', { q: `t=${now - 60_000}&d=30` }));
    await sleep(5);
    procs[0].burst(3);
    const r = await pending;
    assert.equal(r.status, 302);
    const loc = r.headers.Location;
    assert.match(loc, /^playback\/pb_[A-Za-z0-9_-]{22}\/index\.m3u8$/);
    const sid = loc.split('/')[1];
    const pl = await api.handle(req(`v1/cameras/7/playback/${sid}/index.m3u8`, { auth: null }));
    assert.equal(pl.status, 200);
    assert.match(String(pl.body), /#EXT-X-MAP:URI="init.mp4"/);
    assert.equal((await api.handle(req(`v1/cameras/7/playback/${sid}/init.mp4`, { auth: null }))).status, 200);
    assert.equal((await api.handle(req(`v1/cameras/7/playback/${sid}/0.m4s`, { auth: null }))).status, 200);
    assert.equal((await api.handle(req(`v1/cameras/7/playback/${sid}/5.m4s`, { auth: null }))).status, 425);
    assert.equal((await api.handle(req(`v1/cameras/8/playback/${sid}/0.m4s`, { auth: null }))).status, 404, 'another camera');
    assert.equal((await api.handle(req(`v1/cameras/7/playback/${sid}`, { method: 'DELETE', auth: null }))).status, 401);
    clock.advance(61_000);
    playback.sweep();
    assert.deepEqual(procs[0].killed, ['SIGINT']);
    assert.equal((await api.handle(req(`v1/cameras/7/playback/${sid}/index.m3u8`, { auth: null }))).status, 404);
});

test('playback: at most 2 sessions; a stream with no video is a 502', async () => {
    const { api, req, clock, procs, playback } = setup();
    const t = clock.now() - 60_000;
    for (let i = 0; i < 3; i++) {
        const p = api.handle(req('v1/cameras/7/playback.m3u8', { q: `t=${t}` }));
        await sleep(5);
        procs[i].burst(3);
        assert.equal((await p).status, 302);
    }
    assert.equal(playback.count, 2);
    assert.ok(procs[0].killed.length > 0);
    const p = api.handle(req('v1/cameras/7/playback.m3u8', { q: `t=${t}` }));
    await sleep(5);
    procs[3].exit(1);
    await sleep(5);
    procs[4]?.exit(1);
    await sleep(5);
    procs[5]?.exit(1);
    const r = await p;
    assert.equal(r.status, 502);
    playback.stopAll();
});

test('apiPath: endpoint, public prefix, query', () => {
    const p = apiPath('/endpoint/@marl1w/scrypted-stamina/public/v1/events/stream?since=3', '/endpoint/@marl1w/scrypted-stamina', '@marl1w/scrypted-stamina')!;
    assert.equal(p.path, 'v1/events/stream');
    assert.equal(p.query.get('since'), '3');
    assert.equal(apiPath('/endpoint/@marl1w/scrypted-stamina/v1/health', undefined, '@marl1w/scrypted-stamina')!.path, 'v1/health');
    assert.equal(apiPath('/endpoint/other/v1/health', undefined, '@marl1w/scrypted-stamina'), undefined);
});

test('errors never leak internals', async () => {
    const { api, req } = setup({ cameras: () => { throw new Error('boom rtsp://u:p@192.0.2.1/x'); } });
    const r = await api.handle(req('v1/cameras'));
    assert.equal(r.status, 500);
    assert.deepEqual(body(r), { error: 'internal_error' });
});

test('resume without an epoch is a reset (the client cannot know the ring is ours)', async () => {
    const { api, req, bus } = setup();
    bus.publish('event.start', {});
    const r = await api.handle(req('v1/events/stream', { q: 'since=1' }));
    const first = (await r.stream!.next()).value!.toString();
    assert.match(first, /"reset":true/);
    const p = JSON.parse(String((await api.handle(req('v1/events/poll', { q: 'since=1' }))).body));
    assert.equal(p.reset, true);
    assert.equal(p.messages.length, 1);
    api.closeStreams();
});

test('ack is clamped to what was cut; init-k routes; DELETE 401 carries WWW-Authenticate', async () => {
    const { api, req, store, clock, procs } = setup();
    store.open(EV, '7');
    store.setInit(EV, Buffer.from('i0'));
    store.addSegment(EV, Buffer.from('s0'), 0, 2);
    const a = JSON.parse(String((await api.handle(req(`v1/events/${EV}/ack`, { method: 'POST', body: '{"upTo":50}' }))).body));
    assert.equal(a.released, 1);
    assert.equal(a.segments.acked_up_to, 0, 'not 50: segments 1..50 are not cut yet');
    store.addSegment(EV, Buffer.from('s1'), 2, 2);
    assert.equal((await api.handle(req(`v1/events/${EV}/1.m4s`))).status, 200, 'a later segment is still served');
    assert.equal((await api.handle(req(`v1/events/${EV}/init-1.mp4`))).status, 425);
    store.setInit(EV, Buffer.from('i1'));
    assert.equal(String((await api.handle(req(`v1/events/${EV}/init-1.mp4`))).body), 'i1');
    assert.equal((await api.handle(req(`v1/events/${EV}/init-123.mp4`))).status, 404);
    const p = api.handle(req('v1/cameras/7/playback.m3u8', { q: `t=${clock.now() - 1000}` }));
    await sleep(5);
    procs[0].burst(2);
    const sid = (await p).headers.Location.split('/')[1];
    const d = await api.handle(req(`v1/cameras/7/playback/${sid}`, { method: 'DELETE', auth: null }));
    assert.equal(d.status, 401);
    assert.equal(d.headers['WWW-Authenticate'], 'Bearer');
    api.revoke();
});

test('GET /v1/events: since required and validated', async () => {
    const { api, req } = setup();
    for (const q of ['', 'since=x', 'since=1&camera=../x'])
        assert.equal((await api.handle(req('v1/events', { q }))).status, 400, q);
    assert.equal(JSON.parse(String((await api.handle(req('v1/events', { q: 'since=1' }))).body)).events.length, 1);
});

test('every reply has nosniff; at most 8 long polls wait at once (429)', async () => {
    const { api, req } = setup();
    assert.equal((await api.handle(req('v1/health'))).headers['X-Content-Type-Options'], 'nosniff');
    assert.equal((await api.handle(req('v1/health', { auth: null }))).headers['X-Content-Type-Options'], 'nosniff');
    const polls = Array.from({ length: 8 }, () => api.handle(req('v1/events/poll', { q: 'wait=20' })));
    await sleep(5);
    const r = await api.handle(req('v1/events/poll', { q: 'wait=20' }));
    assert.equal(r.status, 429);
    api.revoke();
    await Promise.all(polls);
});
