import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CameraCore, CameraSink, PLAIN_DEDUPE_MS } from '../src/camera';
import { RolesTable } from '../src/roles';
import { FakeScrypted } from './fakes';
import { FakeClock } from './helpers';

const CAMP = '@marl1w/scrypted-reolink';
const CG = 'scrypted-coral-guard';

function setup(opts: { mixins?: string[]; interfaces?: string[] } = {}) {
    const sc = new FakeScrypted();
    const clock = new FakeClock();
    const roles = new RolesTable();
    sc.add({ id: '7', name: 'Cam', pluginId: CAMP, interfaces: opts.interfaces ?? ['VideoCamera', 'Camera', 'MotionSensor', 'OccupancySensor', 'ObjectDetector', 'Online'], mixins: opts.mixins ?? ['911', '913'], online: true });
    sc.add({ id: '911', pluginId: '@scrypted/prebuffer-mixin', interfaces: [] });
    sc.add({ id: '913', pluginId: CG, interfaces: [] });
    sc.add({ id: '9', name: 'Cam Night mode', pluginId: CAMP, providerId: '7', interfaces: ['BinarySensor'], binaryState: true });
    sc.add({ id: '10', name: 'Cam Siren', pluginId: CAMP, providerId: '7', interfaces: ['OnOff'] });
    sc.pluginIds.set(CAMP, CAMP);
    const out: { type: string; body: any }[] = [];
    const sink: CameraSink = {
        publish: (type, body) => out.push({ type, body }),
        eventStarted: () => { }, eventEnded: () => { }, subjectDecided: () => { },
    };
    const core = new CameraCore(sc.env('7', clock, roles), sink);
    core.bind();
    return { sc, clock, roles, core, out };
}

test('camera AI and Coral Guard are told apart by sourceId (plugin id or device id); verifier confirms', () => {
    const { sc, clock, core, out, roles } = setup();
    // first a verifier must have been seen on this camera for subjects to wait for one
    sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    clock.advance(20_000);
    out.length = 0;
    sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    assert.equal(out[0].type, 'event.start');
    assert.equal(out[0].body.event.trigger.source, CAMP);
    assert.equal(out[0].body.observations[0].class, 'person');
    assert.equal(out[0].body.event.subjects[0].verdict, 'pending');
    clock.advance(800);
    sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    const v = out.find(m => m.type === 'subject.verdict')!;
    assert.equal(v.body.subject.verdict, 'confirmed');
    assert.equal(v.body.subject.verdict_source, CG);
    assert.equal(roles.get(CAMP)!.role, 'trigger');
    assert.equal(roles.get(CG)!.role, 'verifier');
    assert.deepEqual(core.verifiers(), [CG]);
});

test('no verifier ever seen: subjects are unverified; a silent verifier mixin is reported, not counted', () => {
    const { sc, clock, core, out } = setup();
    sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    assert.equal(out.find(m => m.type === 'subject.verdict')!.body.subject.verdict, 'unverified');
    assert.deepEqual(core.silentVerifiers(), [CG]);
    assert.equal(core.verifierPresent(), false);
});

test('motion per layer: the camera layer is the camera plugin, the Coral Guard layer is Coral Guard; the plain duplicate is dropped', () => {
    const { sc, clock, out } = setup();
    sc.emit('7', 'MotionSensor', true, {}, '7');
    sc.emit('7', 'MotionSensor', true, {});
    clock.advance(PLAIN_DEDUPE_MS + 1);
    assert.equal(out[0].type, 'event.start');
    assert.equal(out[0].body.event.trigger.source, CAMP);
    assert.equal(out[0].body.event.trigger.kind, 'motion');
    assert.equal(out.filter(m => m.type === 'event.start').length, 1);
    clock.advance(1000);
    sc.emit('7', 'MotionSensor', true, {}, '913');
    clock.advance(1000);
    const up = out.find(m => m.type === 'event.update')!;
    assert.equal(up.body.observations[0].source, CG);
    assert.equal(up.body.event.observations, 2, 'the plain duplicate was not counted');
});

test('motion only on the plain listener (a layer we could not listen to): counted after the dedupe wait', () => {
    const { sc, clock, out } = setup();
    sc.emit('7', 'MotionSensor', true, {});
    assert.equal(out.length, 0);
    clock.advance(PLAIN_DEDUPE_MS);
    assert.equal(out[0].type, 'event.start');
    assert.equal(out[0].body.event.trigger.source, CAMP);
});

test('held motion keeps the event open until it turns off plus the margin', () => {
    const { sc, clock, out } = setup();
    sc.emit('7', 'MotionSensor', true, {}, '7');
    clock.advance(120_000);
    assert.ok(!out.some(m => m.type === 'event.end'));
    sc.emit('7', 'MotionSensor', false, {}, '7');
    clock.advance(10_000);
    assert.ok(out.some(m => m.type === 'event.end'));
});

test('context: night mode child, motion, online at start; changes during the event; camera offline flagged', () => {
    const { sc, clock, out } = setup();
    sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    const ctx = out[0].body.event.context;
    assert.equal(ctx.at_start.night_mode, true);
    assert.equal(ctx.at_start.online, true);
    assert.equal(ctx.at_start.audio, null, 'no AudioSensor');
    sc.devices.get('9')!.binaryState = false;
    sc.emit('9', 'BinarySensor', false);
    sc.devices.get('7')!.online = false;
    sc.emit('7', 'Online', false);
    clock.advance(30_000);
    const end = out.find(m => m.type === 'event.end')!.body.event;
    assert.equal(end.flags.camera_offline, true);
    assert.equal(end.context.current.night_mode, false);
    assert.deepEqual(end.context.changes.map((c: any) => [c.feature, c.on]), [['night_mode', false], ['online', false]]);
});

test('child occupancy sensors are observations; other children are not listened to', () => {
    const { sc, clock, out, core } = setup();
    sc.add({ id: '11', name: 'Cam Person', pluginId: CAMP, providerId: '7', interfaces: ['OccupancySensor'] });
    core.bind();
    sc.emit('11', 'OccupancySensor', true);
    clock.advance(PLAIN_DEDUPE_MS);
    assert.equal(out[0].body.event.trigger.kind, 'occupancy');
    assert.ok(!sc.listeners.some(l => l.dev === '10'));
    assert.deepEqual(core.contextSensors().map(c => c.id).sort(), ['11', '9']);
});

test('roles: ignore drops a source entirely', () => {
    const { sc, clock, out, roles } = setup();
    sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    clock.advance(20_000);
    out.length = 0;
    roles.setRole(CAMP, 'ignore');
    sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 1 }] });
    assert.equal(out.length, 0);
});

test('verifier memory persists across restarts; stop closes the open event and unbinds', () => {
    const { sc, clock, core, out, roles } = setup();
    sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    core.stop();
    assert.equal(out[out.length - 1].type, 'event.end');
    assert.equal(out[out.length - 1].body.event.end_reason, 'stopped');
    assert.equal(sc.listeners.length, 0);
    const again = new CameraCore(sc.env('7', clock, roles), { publish: () => { }, eventStarted: () => { }, eventEnded: () => { }, subjectDecided: () => { } });
    assert.deepEqual(again.verifiers(), [CG]);
});

test('malformed payloads are ignored without throwing', () => {
    const { sc, out } = setup();
    for (const d of [null, 5, 'x', {}, { detections: 'x' }, { detections: [null, { className: 5 }] }])
        sc.emit('7', 'ObjectDetector', d);
    assert.equal(out.length, 0);
});

// ---- review fixes: who may confirm, holds, liveness, the verifier's window --------------------------------------------

/** a verifier seen on the camera a little while ago, then a person from the camera AI: a pending subject */
function pendingPerson(x: ReturnType<typeof setup>, verifierAgoMs = 20_000) {
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    x.clock.advance(verifierAgoMs);
    x.out.length = 0;
    x.sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now(), detections: [{ className: 'people', score: 1 }] });
    assert.equal(x.core.tracker.current!.subjects[0].verdict, 'pending');
}

test('a non-gated verifier\'s motion never confirms (it is a trigger signal); a gated source\'s motion does', () => {
    const x = setup({ mixins: ['911', '913', '914'] });
    x.sc.add({ id: '914', pluginId: 'some/analyzer', interfaces: [] });
    x.core.unbind(); x.core.bind();
    // some/analyzer is a verifier for its detections
    x.sc.emit('7', 'ObjectDetector', { sourceId: '914', timestamp: x.clock.now(), detections: [{ className: 'person', score: 1 }] });
    pendingPerson(x);
    x.clock.advance(500);
    x.sc.emit('7', 'MotionSensor', true, {}, '914');
    assert.equal(x.core.tracker.current!.subjects[0].verdict, 'pending', 'a non-gated verifier\'s motion does not confirm');
    x.sc.emit('7', 'MotionSensor', true, {}, '913');
    const s = x.core.tracker.current!.subjects[0];
    assert.equal(s.verdict, 'confirmed', 'Coral Guard is gated: its motion confirms');
    assert.equal(s.verdict_source, CG);
    assert.equal(x.roles.get(CG)!.gated, true);
    assert.equal(x.roles.get('some/analyzer')!.gated, false);
});

test('a classless verifier detection (smart event) does not confirm', () => {
    const x = setup();
    pendingPerson(x);
    x.sc.emit('7', 'ObjectDetector', { sourceId: 'some/analyzer', timestamp: x.clock.now(), detections: [{ className: 'line_crossing', score: 1 }] });
    assert.notEqual(x.core.tracker.current!.subjects[0].verdict, 'confirmed');
});

test('a source first seen through motion is not locked as a trigger: its first detection decides its role', () => {
    const x = setup({ mixins: ['911', '913', '914'] });
    x.sc.add({ id: '914', pluginId: 'some/analyzer', interfaces: [] });
    x.core.unbind(); x.core.bind();
    x.sc.emit('7', 'MotionSensor', true, {}, '914');
    const row = x.roles.get('some/analyzer')!;
    assert.equal(row.roleSet, false);
    assert.equal(row.signalRole, 'trigger', 'never a verifier from a signal');
    x.sc.emit('7', 'ObjectDetector', { sourceId: '914', timestamp: x.clock.now(), detections: [{ className: 'person', score: 1 }] });
    assert.equal(x.roles.get('some/analyzer')!.role, 'verifier');
    assert.equal(x.roles.get('some/analyzer')!.roleSet, true);
});

test('a verifier silent for over 10 min: the window ends unverified, not vetoed; verifier_live says so', () => {
    const x = setup();
    pendingPerson(x, 11 * 60_000);
    assert.equal(x.core.verifierLive(), false);
    x.clock.advance(5000);
    const v = x.out.find(m => m.type === 'subject.verdict')!;
    assert.equal(v.body.subject.verdict, 'unverified');
    // alive: vetoed
    const y = setup();
    pendingPerson(y, 60_000);
    assert.equal(y.core.verifierLive(), true);
    y.clock.advance(5000);
    assert.equal(y.out.find(m => m.type === 'subject.verdict')!.body.subject.verdict, 'vetoed');
});

test('while pending, a same-family verifier observation within the verify window joins the subject (beyond the 2 s rule)', () => {
    const x = setup();
    pendingPerson(x);
    x.clock.advance(4000);
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    const ev = x.core.tracker.current!;
    assert.equal(ev.subjects.length, 1);
    assert.equal(ev.subjects[0].verdict, 'confirmed');
});

test('a plain `on` that was counted always gets its `off`, even when a layer reports the same edge', () => {
    const x = setup();
    x.sc.emit('7', 'MotionSensor', true, {});
    x.clock.advance(PLAIN_DEDUPE_MS);
    assert.equal(x.out[0].type, 'event.start');
    x.clock.advance(60_000);
    // the off edge arrives on a layer and on the plain listener together
    x.sc.emit('7', 'MotionSensor', false, {}, '913');
    x.sc.emit('7', 'MotionSensor', false, {});
    x.clock.advance(10_000);
    assert.ok(x.out.some(m => m.type === 'event.end'), 'the plain hold was released');
});

test('a camera going offline releases its holds; a rebind re-holds a signal still on from the device state', () => {
    const x = setup();
    x.sc.emit('7', 'MotionSensor', true, {}, '7');
    x.sc.devices.get('7')!.motionDetected = true;
    // mixins changed: rebind; the hold is released and taken again from the state
    x.sc.devices.get('7')!.mixins = ['911'];
    x.core.bind();
    assert.deepEqual(x.core.tracker.holdKeys, ['motion:sync']);
    x.clock.advance(60_000);
    assert.ok(!x.out.some(m => m.type === 'event.end'));
    x.sc.emit('7', 'Online', false);
    x.clock.advance(10_000);
    assert.ok(x.out.some(m => m.type === 'event.end'));
});

test('a resynced hold ends with an off edge from any layer', () => {
    const x = setup();
    x.sc.emit('7', 'MotionSensor', true, {}, '7');
    x.sc.devices.get('7')!.motionDetected = true;
    x.sc.devices.get('7')!.mixins = ['911', '913'];
    x.sc.devices.get('7')!.interfaces = [...x.sc.devices.get('7')!.interfaces, 'Battery'];
    x.core.bind();
    assert.deepEqual(x.core.tracker.holdKeys, ['motion:sync']);
    x.sc.emit('7', 'MotionSensor', false, {}, '913');
    assert.deepEqual(x.core.tracker.holdKeys, []);
});

test('a verifier is present only while it is a mixin of the camera; its liveness is per camera', () => {
    const x = setup();
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    assert.deepEqual(x.core.verifiers(), [CG]);
    assert.equal(x.core.verifierLive(), true);
    // Coral Guard is active elsewhere, but not on this camera for 11 min: not live here
    x.clock.advance(11 * 60_000);
    x.roles.ensure({ source: CG, kind: 'object', className: 'verified_person' }, x.clock.now());
    assert.equal(x.core.verifierLive(), false);
    x.sc.devices.get('7')!.mixins = ['911'];
    assert.deepEqual(x.core.verifiers(), [], 'removed from the camera: no longer present');
});
