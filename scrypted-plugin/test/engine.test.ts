import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CameraCore } from '../src/camera';
import { Engine } from '../src/engine';
import { fakeHost, FakeScrypted, fakeSpawn } from './fakes';
import { FakeClock, sleep } from './helpers';

const CAMP = '@marl1w/scrypted-reolink';
const TINY_JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0x00, 0x11, 0x08, 0x00, 0xb4, 0x01, 0x40, 0x03, 0x01, 0x22, 0x00, 0x02, 0x11, 0x01, 0x03, 0x11, 0x01, 0xff, 0xd9]);

function setup(over: Parameters<typeof fakeHost>[2] = {}) {
    const clock = new FakeClock();
    const { procs, spawn } = fakeSpawn();
    const host = fakeHost(clock, spawn, over);
    const engine = new Engine(host);
    const sc = new FakeScrypted();
    sc.add({ id: '7', name: 'Cam', pluginId: CAMP, interfaces: ['VideoCamera', 'Camera', 'MotionSensor', 'ObjectDetector'], mixins: [] });
    const core = new CameraCore(sc.env('7', clock, engine.roles, engine.episodeConfig()), engine);
    engine.register(core);
    const msgs: any[] = [];
    engine.bus.subscribe(m => msgs.push(JSON.parse(m.json)));
    const detect = () => sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    return { clock, procs, host, engine, sc, core, msgs, detect };
}

test('a token is generated on first start and kept; regenerating changes it', () => {
    const { engine, host } = setup();
    const t = engine.token();
    assert.match(t, /^[A-Za-z0-9_-]{43}$/);
    assert.equal(host.store.get('tokenShown'), 'true');
    const again = new Engine(host);
    assert.equal(again.token(), t);
    assert.notEqual(engine.regenerateToken(), t);
    again.stop();
    engine.stop();
});

test('event start starts the segmenter; segments are announced, stored and served; end stops ffmpeg', async () => {
    const { engine, procs, msgs, detect, clock } = setup();
    detect();
    assert.equal(msgs[0].type, 'event.start');
    assert.equal(msgs[0].event.segments.state, 'running');
    const id = msgs[0].event.id;
    await sleep(5);
    assert.equal(procs.length, 1);
    procs[0].burst(9);
    await sleep(5);
    const segs = msgs.filter(m => m.type === 'event.segment');
    assert.equal(segs.length, 4);
    assert.equal(segs[0].segment.n, 0);
    assert.equal(segs[0].segment.init, 0, 'segments name the init they need: 0 = init.mp4');
    assert.equal(segs[0].segment.discontinuity, false);
    assert.equal(segs[0].kind, 'cut');
    assert.equal(segs[0].event_id, id);
    assert.equal(engine.store.getSegment(id, 3).status, 200);
    clock.advance(10_000);
    const end = msgs.find(m => m.type === 'event.end');
    assert.ok(end);
    assert.deepEqual(procs[0].killed, ['SIGINT']);
    await sleep(10);
    const segMsgs = msgs.filter(m => m.type === 'event.segment');
    assert.equal(segMsgs.filter(m => m.kind === 'cut').length, 5, 'the tail is flushed after the end');
    const last = segMsgs[segMsgs.length - 1];
    assert.deepEqual([last.kind, last.last, last.next, last.state, last.segment], ['last', true, 5, 'ended', null]);
    assert.equal(engine.store.info(id)!.live, false);
    const lat = end.event.latency;
    for (const k of ['camera_event_time', 'plugin_receive', 'event_start_sent', 'stream_requested', 'stream_resolved', 'first_byte', 'init_cut', 'first_segment_cut'])
        assert.equal(typeof lat[k], 'number', k);
    engine.stop();
});

test('segments off: events only; the segments block says why; the thumbnail still works', async () => {
    const { engine, host, procs, msgs, detect } = setup({ picture: async () => TINY_JPEG, interfaces: () => ['VideoCamera', 'Camera'] });
    host.store.set('segments', 'false');
    detect();
    assert.equal(msgs[0].event.segments.state, 'none');
    assert.equal(msgs[0].event.segments.reason, 'disabled');
    assert.equal(procs.length, 0);
    const id = msgs[0].event.id;
    assert.equal(engine.store.getSegment(id, 0).status, 404);
    await sleep(5);
    assert.equal(engine.store.getThumbnail(id).status, 200);
    engine.stop();
});

test('a failing stream marks the segments failed, the event goes on', async () => {
    const { engine, msgs, detect, clock } = setup({ streamInput: async () => { throw new Error('no stream'); } });
    detect();
    await sleep(10);
    clock.advance(1100);
    const up = msgs.filter(m => m.type === 'event.update').pop();
    assert.equal(up.event.segments.state, 'failed');
    assert.match(up.event.segments.reason, /no stream/);
    engine.stop();
});

test('thumbnail at start (small picture served as is), replaced at confirmation', async () => {
    let n = 0;
    const { engine, msgs, detect, sc, clock, host } = setup({ picture: async () => { n++; return TINY_JPEG; }, interfaces: () => ['VideoCamera', 'Camera'] });
    detect();
    await sleep(5);
    const id = msgs[0].event.id;
    const t = engine.store.getThumbnail(id);
    assert.equal(t.status, 200);
    assert.equal(t.thumb!.kind, 'start');
    sc.emit('7', 'ObjectDetector', { sourceId: 'some/analyzer', timestamp: clock.now(), detections: [{ className: 'person', score: 0.9 }] });
    await sleep(5);
    assert.equal(engine.store.getThumbnail(id).thumb!.kind, 'confirmation');
    assert.equal(n, 2);
    assert.equal(host.warns.length, 0);
    engine.stop();
});

test('cameras info reports capabilities and sources', async () => {
    const { engine, detect } = setup();
    detect();
    await engine.refreshOptions();
    const [c] = engine.camerasInfo();
    assert.equal(c.id, '7');
    assert.equal(c.capabilities.prebuffer, true);
    assert.equal(c.capabilities.prebuffer_ms, 10000);
    assert.equal(c.capabilities.verifier_present, false);
    assert.equal(c.sources[0].source, CAMP);
    assert.equal(c.open_event !== null, true);
    engine.stop();
});

test('end to end over the API: SSE start then segment, fetch, ack', async () => {
    const { engine, procs, detect } = setup();
    const tok = engine.token();
    const req = (path: string, method = 'GET', body?: string) => engine.handle({ method, path, query: new URLSearchParams(), headers: { authorization: `Bearer ${tok}` }, body });
    const sse = await req('v1/events/stream');
    await sse.stream!.next();
    detect();
    await sleep(5);
    procs[0].burst(3);
    await sleep(5);
    const chunk = (await sse.stream!.next()).value!.toString();
    assert.ok(chunk.indexOf('event: event.start') < chunk.indexOf('event: event.segment'));
    const id = /"id":"(ev_[A-Za-z0-9_-]{22})"/.exec(chunk)![1];
    assert.equal((await req(`v1/events/${id}/init.mp4`)).status, 200);
    assert.equal((await req(`v1/events/${id}/0.m4s`)).status, 200);
    assert.equal((await req(`v1/events/${id}/ack`, 'POST', '{"upTo":0}')).status, 200);
    assert.equal((await req(`v1/events/${id}/0.m4s`)).status, 404);
    // nothing in the stream leaks the stream url
    assert.ok(!chunk.includes('secret') && !chunk.includes('rtsp'));
    engine.stop();
});

test('segments off: the final event.segment (last, state none) follows event.end', () => {
    const { engine, host, msgs, detect, clock } = setup();
    host.store.set('segments', 'false');
    detect();
    clock.advance(10_000);
    const iEnd = msgs.findIndex(m => m.type === 'event.end');
    const last = msgs.findIndex(m => m.type === 'event.segment' && m.last);
    assert.ok(iEnd >= 0 && last > iEnd);
    assert.equal(msgs[last].state, 'none');
    engine.stop();
});

test('ffmpeg dying mid-event restarts without pre-roll; the new init is announced with a discontinuity', async () => {
    const asked: number[] = [];
    const { engine, procs, msgs, detect, clock } = setup({ streamInput: async (_c, pre) => { asked.push(pre); return { args: ['-i', 'x'] }; } });
    detect();
    await sleep(5);
    procs[0].burst(4);
    await sleep(5);
    procs[0].exit(1);
    await sleep(1100);
    assert.equal(procs.length, 2);
    assert.deepEqual(asked, [10_000, 0]);
    procs[1].burst(2);
    await sleep(5);
    const cuts = msgs.filter(m => m.kind === 'cut');
    const after = cuts[cuts.length - 1];
    assert.equal(after.segment.init, 1);
    assert.equal(after.segment.discontinuity, true);
    const id = msgs[0].event.id;
    assert.equal((await engine.handle({ method: 'GET', path: `v1/events/${id}/init-1.mp4`, query: new URLSearchParams(), headers: { authorization: `Bearer ${engine.token()}` } })).status, 200);
    clock.advance(1000);
    assert.equal(msgs.filter(m => m.type === 'event.update').pop().event.segments.restarts, 1);
    engine.stop();
});

test('at the cap the running segmenter is handed over to the continuation: no new ffmpeg, no repeated pre-roll', async () => {
    const { engine, procs, msgs, sc, clock } = setup();
    sc.emit('7', 'MotionSensor', true, {}, '7');
    await sleep(5);
    procs[0].burst(4);
    await sleep(5);
    for (let i = 0; i < 5; i++) { clock.advance(2 * 60_000); sc.emit('7', 'MotionSensor', true, {}, '7'); }
    await sleep(5);
    const starts = msgs.filter(m => m.type === 'event.start');
    assert.equal(starts.length, 2);
    assert.equal(starts[1].event.continues, starts[0].event.id);
    assert.equal(procs.length, 1, 'same ffmpeg');
    assert.equal(procs[0].killed.length, 0);
    const lastOld = msgs.find(m => m.type === 'event.segment' && m.last && m.event_id === starts[0].event.id);
    assert.equal(lastOld.state, 'ended');
    procs[0].burst(2, 4);
    await sleep(5);
    const cut = msgs.filter(m => m.kind === 'cut').pop();
    assert.equal(cut.event_id, starts[1].event.id);
    assert.equal(cut.segment.n, 0);
    assert.equal(cut.segment.start_s, 0);
    assert.equal(engine.store.getInit(starts[1].event.id).status, 200, 'the init is carried over');
    engine.stop();
});

test('slots: a new event preempts the oldest segmenter (10 s or older) whose subjects are all vetoed; idle ones give their slot back', async () => {
    const { engine, procs, msgs, clock, host } = setup();
    const sc = new FakeScrypted();
    sc.add({ id: '950', pluginId: 'some/analyzer', interfaces: [] });
    for (let i = 0; i < 9; i++) {
        sc.add({ id: `c${i}`, name: `C${i}`, pluginId: CAMP, interfaces: ['VideoCamera', 'ObjectDetector', 'MotionSensor'], mixins: ['950'] });
        engine.register(new CameraCore(sc.env(`c${i}`, clock, engine.roles, engine.episodeConfig()), engine));
    }
    (engine as any).segmentsEnabled = () => false;
    // the verifier (a mixin of every camera) is seen on each camera, so subjects wait for it
    for (let i = 0; i < 9; i++) sc.emit(`c${i}`, 'ObjectDetector', { sourceId: '950', timestamp: clock.now(), detections: [{ className: 'person', score: 1 }] });
    clock.advance(30_000);
    (engine as any).segmentsEnabled = () => true;
    msgs.length = 0;
    host.warns.length = 0;
    // 8 events held open by motion; the camera's person joins the motion subject; the verifier sees nothing
    for (let i = 0; i < 8; i++) {
        sc.emit(`c${i}`, 'MotionSensor', true, {}, `c${i}`);
        sc.emit(`c${i}`, 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    }
    await sleep(5);
    assert.equal(procs.length, 8);
    clock.advance(6000);
    assert.ok(msgs.filter(m => m.type === 'subject.verdict').every(m => m.subject.verdict === 'vetoed'));
    // younger than 10 s: no victim yet, the ninth event waits (busy)
    sc.emit('c8', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    await sleep(5);
    assert.equal(procs.length, 8);
    assert.equal(msgs.find(m => m.type === 'event.start' && m.event.camera.id === 'c8').event.segments.reason, 'busy');
    // older than 10 s: the sweep releases idle ones only at 30 s, but a slot frees when one is preempted for a new event
    clock.advance(5000);
    sc.emit('c8', 'MotionSensor', true, {}, 'c8');
    await sleep(5);
    // the verifier confirms the busy event: it deserves a slot as soon as one frees
    sc.emit('c8', 'ObjectDetector', { sourceId: '950', timestamp: clock.now(), detections: [{ className: 'person', score: 1 }] });
    // after RELEASE_AFTER_MS without a keeper, idle ones give their slot back and the busy event gets one
    clock.advance(20_000);
    engine.sweep();
    await sleep(10);
    assert.ok(procs.slice(0, 8).every(p => p.killed.length > 0), 'the vetoed ones were released');
    assert.equal(procs.length, 9, 'the busy event got a segmenter when slots freed');
    clock.advance(1000);
    const c8 = msgs.filter(m => m.type === 'event.update' && m.event.camera.id === 'c8').pop();
    assert.deepEqual([c8.event.segments.state, c8.event.segments.reason], ['running', 'resumed']);
    const rel = msgs.filter(m => m.type === 'event.update' && m.event.segments.state === 'released');
    assert.ok(rel.length > 0);
    assert.ok(!msgs.some(m => m.type === 'event.segment' && m.last), 'no `last` while the events are open');
    // a late confirmation of a released event starts its segments again
    sc.emit('c0', 'ObjectDetector', { sourceId: '950', timestamp: clock.now(), detections: [{ className: 'person', score: 1 }] });
    await sleep(5);
    assert.equal(procs.length, 10);
    clock.advance(1000);
    const c0 = msgs.filter(m => m.type === 'event.update' && m.event.camera.id === 'c0').pop();
    assert.deepEqual([c0.event.segments.state, c0.event.segments.reason], ['running', 'resumed']);
    engine.stop();
});

test('preemption: a fresh event takes the slot of a vetoed one older than 10 s', async () => {
    const { engine, procs, msgs, clock } = setup();
    const sc = new FakeScrypted();
    sc.add({ id: '950', pluginId: 'some/analyzer', interfaces: [] });
    for (let i = 0; i < 9; i++) {
        sc.add({ id: `c${i}`, name: `C${i}`, pluginId: CAMP, interfaces: ['VideoCamera', 'ObjectDetector', 'MotionSensor'], mixins: ['950'] });
        engine.register(new CameraCore(sc.env(`c${i}`, clock, engine.roles, engine.episodeConfig()), engine));
    }
    (engine as any).segmentsEnabled = () => false;
    for (let i = 0; i < 9; i++) sc.emit(`c${i}`, 'ObjectDetector', { sourceId: '950', timestamp: clock.now(), detections: [{ className: 'person', score: 1 }] });
    clock.advance(30_000);
    (engine as any).segmentsEnabled = () => true;
    msgs.length = 0;
    for (let i = 0; i < 8; i++) {
        sc.emit(`c${i}`, 'MotionSensor', true, {}, `c${i}`);
        sc.emit(`c${i}`, 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    }
    await sleep(5);
    clock.advance(11_000);
    sc.emit('c8', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    await sleep(10);
    assert.equal(procs.length, 9);
    assert.ok(procs[0].killed.length > 0, 'the oldest vetoed one was preempted');
    assert.equal(msgs.find(m => m.type === 'event.start' && m.event.camera.id === 'c8').event.segments.state, 'running');
    engine.stop();
});

test('a camera mixin re-created keeps its core and open event (attach); a released one ends it after the grace', () => {
    const clock = new FakeClock();
    const { spawn } = fakeSpawn();
    const engine = new Engine(fakeHost(clock, spawn, { interfaces: () => ['VideoCamera'] }));
    const sc = new FakeScrypted();
    sc.add({ id: '7', name: 'Cam', pluginId: CAMP, interfaces: ['VideoCamera', 'ObjectDetector'], mixins: [] });
    const msgs: any[] = [];
    engine.bus.subscribe(m => msgs.push(JSON.parse(m.json)));
    const ep = { marginMs: 60_000, capMs: 600_000, verifyWindowMs: 5000 };
    const a = engine.attach(sc.env('7', clock, engine.roles, ep));
    sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    const id = a.tracker.current!.id;
    engine.detach('7');
    const b = engine.attach(sc.env('7', clock, engine.roles, ep));
    assert.equal(b, a);
    assert.equal(b.tracker.current!.id, id);
    assert.ok(!msgs.some(m => m.type === 'event.end'));
    assert.equal(sc.listeners.filter(l => l.iface === 'ObjectDetector').length, 1, 'listeners rebound, not doubled');
    engine.detach('7');
    clock.advance(10_000);
    assert.equal(msgs.find(m => m.type === 'event.end').event.end_reason, 'stopped');
    engine.stop();
});

test('eviction is announced as event.segment kind evicted, also for closed events', () => {
    const { engine, msgs } = setup();
    engine.store.cfg.maxBytes = 300;
    engine.store.open('ev_x', '7');
    engine.store.setInit('ev_x', Buffer.alloc(0));
    for (let i = 0; i < 4; i++) engine.store.addSegment('ev_x', Buffer.alloc(100), i * 2, 2);
    const ev = msgs.find(m => m.kind === 'evicted');
    assert.deepEqual(ev.evicted, [0]);
    assert.equal(ev.last, false);
    engine.stop();
});

test('regenerating the token ends SSE streams, polls and playback sessions', async () => {
    const { engine } = setup();
    const old = engine.token();
    const req = (path: string, q = '') => engine.handle({ method: 'GET', path, query: new URLSearchParams(q), headers: { authorization: `Bearer ${old}` } });
    const sse = await req('v1/events/stream');
    await sse.stream!.next();
    const poll = req('v1/events/poll', 'wait=20');
    await sleep(5);
    let stopped = 0;
    (engine.playback as any).stopAll = () => { stopped++; };
    engine.regenerateToken();
    assert.equal((await poll).status, 401);
    assert.equal((await sse.stream!.next()).done, true);
    assert.equal(stopped, 1);
    engine.stop();
});

test('thumbnails: at most 2 snapshots at once for all cameras; a start picture is skipped once a confirmation is queued', async () => {
    let busy = 0, peak = 0, calls = 0;
    const gate: (() => void)[] = [];
    const clock = new FakeClock();
    const { spawn } = fakeSpawn();
    const engine = new Engine(fakeHost(clock, spawn, {
        interfaces: () => ['VideoCamera', 'Camera'],
        picture: async () => { calls++; busy++; peak = Math.max(peak, busy); await new Promise<void>(r => gate.push(r)); busy--; return TINY_JPEG; },
    }));
    engine.store.cfg.maxBytes = 1e9;
    (engine as any).segmentsEnabled = () => false;
    const sc = new FakeScrypted();
    for (let i = 0; i < 4; i++) {
        sc.add({ id: `c${i}`, name: `C${i}`, pluginId: CAMP, interfaces: ['VideoCamera', 'Camera', 'ObjectDetector'], mixins: [] });
        engine.register(new CameraCore(sc.env(`c${i}`, clock, engine.roles), engine));
        sc.emit(`c${i}`, 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    }
    await sleep(5);
    assert.equal(peak, 2);
    // c3's start picture is still queued when its confirmation is asked: the start one is skipped
    sc.emit('c3', 'ObjectDetector', { sourceId: 'some/analyzer', timestamp: clock.now(), detections: [{ className: 'person', score: 1 }] });
    for (let k = 0; k < 10; k++) { gate.splice(0).forEach(f => f()); await sleep(2); }
    assert.equal(calls, 4, 'c0..c2 start pictures and c3 confirmation only');
    assert.equal(peak, 2);
    engine.stop();
});

test('GET /v1/events lists recent events with their segment state', async () => {
    const { engine, detect, clock } = setup();
    const t0 = clock.now();
    detect();
    clock.advance(20_000);
    const r = await engine.handle({ method: 'GET', path: 'v1/events', query: new URLSearchParams(`since=${t0}`), headers: { authorization: `Bearer ${engine.token()}` } });
    const body = JSON.parse(String(r.body));
    assert.equal(body.events.length, 1);
    assert.equal(body.events[0].event.state, 'closed');
    assert.equal(body.events[0].segments.live !== undefined, true);
    const none = JSON.parse(String((await engine.handle({ method: 'GET', path: 'v1/events', query: new URLSearchParams(`since=${clock.now()}`), headers: { authorization: `Bearer ${engine.token()}` } })).body));
    assert.equal(none.events.length, 0);
    engine.stop();
});

test('keepers: a continuation inherits its capped event\'s keeper status; a motion-only unverified event is preemptable', () => {
    const { engine } = setup();
    const e = engine as any;
    const ev = (id: string, subjects: any[], continues: string | null = null) => ({ id, continues, subjects });
    const capped = ev('ev_a', [{ verdict: 'confirmed', family: 'person' }]);
    assert.equal(e.isKeeper(capped), true);
    const cont = ev('ev_b', [{ verdict: 'vetoed', family: 'any' }], 'ev_a');
    assert.equal(e.isKeeper(cont), true, 'inherited');
    assert.equal(e.isKeeper(cont, false), true);
    const motionOnly = ev('ev_c', [{ verdict: 'unverified', family: 'any' }]);
    assert.equal(e.isKeeper(motionOnly), true, 'kept from release');
    assert.equal(e.isKeeper(motionOnly, false), false, 'but preemptable');
    engine.stop();
});

test('eviction announcements are batched per event, at most one a second', () => {
    const { engine, msgs, clock } = setup();
    const e = engine as any;
    e.evicted('ev_x', [0]);
    e.evicted('ev_x', [1]);
    e.evicted('ev_x', [2]);
    let ev = msgs.filter(m => m.kind === 'evicted');
    assert.deepEqual(ev.map(m => m.evicted), [[0]]);
    clock.advance(1000);
    ev = msgs.filter(m => m.kind === 'evicted');
    assert.deepEqual(ev.map(m => m.evicted), [[0], [1, 2]]);
    engine.stop();
});

test('a start picture still queued after 5 s is not taken', async () => {
    let calls = 0;
    const gate: (() => void)[] = [];
    const clock = new FakeClock();
    const { spawn } = fakeSpawn();
    const engine = new Engine(fakeHost(clock, spawn, {
        interfaces: () => ['VideoCamera', 'Camera'],
        picture: async () => { calls++; await new Promise<void>(r => gate.push(r)); return TINY_JPEG; },
    }));
    (engine as any).segmentsEnabled = () => false;
    const sc = new FakeScrypted();
    for (let i = 0; i < 3; i++) {
        sc.add({ id: `c${i}`, name: `C${i}`, pluginId: CAMP, interfaces: ['VideoCamera', 'Camera', 'ObjectDetector'], mixins: [] });
        engine.register(new CameraCore(sc.env(`c${i}`, clock, engine.roles), engine));
        sc.emit(`c${i}`, 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    }
    await sleep(5);
    assert.equal(calls, 2, 'two slots');
    clock.advance(6000);
    for (let k = 0; k < 5; k++) { gate.splice(0).forEach(f => f()); await sleep(2); }
    assert.equal(calls, 2, 'the third camera\'s start picture waited too long');
    engine.stop();
});
