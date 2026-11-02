import assert from 'node:assert/strict';
import { test } from 'node:test';
import { CameraCore, CameraSink, isMirror, PLAIN_DEDUPE_MS, VERIFIER_LIVE_MS, VERIFIER_RECENT_MS } from '../src/camera';
import { RolesTable, signalRole } from '../src/roles';
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

test('sound added by a mixin is never the camera plugin\'s: unclaimed edges go to that mixin, so its role applies', () => {
    const AG = '@marl1w/scrypted-audio-guard';
    const x = setup({ mixins: ['911', '913', '915'], interfaces: ['VideoCamera', 'Camera', 'MotionSensor', 'ObjectDetector', 'AudioSensor', 'Online'] });
    // the camera implements everything but AudioSensor, which the Audio Guard mixin adds
    x.sc.devices.get('7')!.providedInterfaces = ['VideoCamera', 'Camera', 'MotionSensor', 'ObjectDetector', 'Online'];
    x.sc.add({ id: '915', pluginId: AG, interfaces: [] });
    x.core.unbind();
    x.core.bind();
    // before the mixin was ever heard, an unclaimed edge starts nothing
    x.sc.emit('7', 'AudioSensor', true, {});
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    x.sc.emit('7', 'AudioSensor', false, {});
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    assert.ok(!x.out.some(m => m.type === 'event.start'), 'no owner yet: context only');
    // the camera's own layer is not listened to for a signal it does not implement
    x.sc.emit('7', 'AudioSensor', true, {}, '7');
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    assert.ok(!x.out.some(m => m.type === 'event.start'), 'the camera layer is not the camera plugin\'s sound');
    // Audio Guard reports on its layer: attributed to Audio Guard
    x.sc.emit('7', 'AudioSensor', true, {}, '915');
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    assert.equal(x.out.find(m => m.type === 'event.start')!.body.event.trigger.source, AG);
    x.sc.emit('7', 'AudioSensor', false, {}, '915');
    x.clock.advance(60_000);
    // set to ignore: a later unclaimed edge is Audio Guard's, so it starts nothing
    x.roles.setRole(AG, 'ignore');
    const before = x.out.filter(m => m.type === 'event.start').length;
    x.sc.emit('7', 'AudioSensor', true, {});
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    assert.equal(x.out.filter(m => m.type === 'event.start').length, before, 'ignored: no event');
});

test('a sensorless crop: Coral Guard\'s verified motion arrives plain only and opens a confirmed event', () => {
    const x = setup({ interfaces: ['VideoCamera', 'Camera', 'MotionSensor', 'OccupancySensor', 'Online'] });
    // the camera implements neither motion nor occupancy: Coral Guard (the outermost mixin) adds both
    x.sc.devices.get('7')!.providedInterfaces = ['VideoCamera', 'Settings'];
    x.core.unbind();
    x.core.bind();
    x.sc.emit('7', 'MotionSensor', true, {});
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    const start = x.out.find(m => m.type === 'event.start')!;
    assert.equal(start.body.event.trigger.source, CG);
    assert.equal(start.body.event.subjects[0].verdict, 'confirmed');
    x.sc.emit('7', 'MotionSensor', false, {});
    x.clock.advance(PLAIN_DEDUPE_MS + 10_000);
    assert.ok(x.out.some(m => m.type === 'event.end'), 'the off edge ends the hold');
});

test('a camera with motion of its own keeps an unclaimed mixin occupancy as context', () => {
    const x = setup();
    x.sc.devices.get('7')!.providedInterfaces = ['VideoCamera', 'Camera', 'MotionSensor', 'ObjectDetector', 'Online'];
    x.core.unbind();
    x.core.bind();
    x.sc.emit('7', 'OccupancySensor', true, {});
    x.clock.advance(PLAIN_DEDUPE_MS + 1);
    assert.ok(!x.out.some(m => m.type === 'event.start'), 'no owner: context only');
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
    assert.equal(row.role, 'trigger', 'a trigger until its first detection');
    assert.equal(signalRole(row), 'trigger', 'never a verifier from a signal');
    x.sc.emit('7', 'ObjectDetector', { sourceId: '914', timestamp: x.clock.now(), detections: [{ className: 'person', score: 1 }] });
    assert.equal(x.roles.get('some/analyzer')!.role, 'verifier');
    assert.equal(x.roles.get('some/analyzer')!.roleSet, true);
});

test('a verifier silent for over 3 min: the window ends unverified, not vetoed; verifier_live says so', () => {
    const x = setup();
    pendingPerson(x, VERIFIER_LIVE_MS + 1000);
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
    // Coral Guard is active elsewhere, but not on this camera for over 3 min: not live here
    x.clock.advance(VERIFIER_LIVE_MS + 1000);
    x.roles.ensure({ source: CG, kind: 'object', className: 'verified_person' }, x.clock.now());
    assert.equal(x.core.verifierLive(), false);
    x.sc.devices.get('7')!.mixins = ['911'];
    assert.deepEqual(x.core.verifiers(), [], 'removed from the camera: no longer present');
});

// ---- split cameras: a parent whose stitched frame is split into views (crop children; mirrors for another Home) ---------

function splitSetup() {
    const sc = new FakeScrypted();
    const clock = new FakeClock();
    const roles = new RolesTable();
    const cams = ['VideoCamera', 'Camera', 'MotionSensor', 'OccupancySensor', 'ObjectDetector'];
    sc.add({ id: '913', pluginId: CG, interfaces: [] });
    sc.add({ id: '501', name: 'Parent', pluginId: CAMP, nativeId: 'cam1', interfaces: cams, mixins: ['913'] });
    sc.add({ id: '502', name: 'Parent crop', pluginId: CAMP, nativeId: 'cam1-crop-gate', providerId: '501', interfaces: cams, mixins: ['913'] });
    sc.add({ id: '503', name: 'Parent mirror', pluginId: CAMP, nativeId: 'cam1-mirror-gate', providerId: '501', interfaces: cams, mixins: [] });
    sc.pluginIds.set(CAMP, CAMP);
    const out: Record<string, { type: string; body: any }[]> = { '501': [], '502': [] };
    const mk = (id: string) => {
        const core = new CameraCore(sc.env(id, clock, roles), {
            publish: (type, body) => out[id].push({ type, body }), eventStarted: () => { }, eventEnded: () => { }, subjectDecided: () => { },
        });
        core.bind();
        return core;
    };
    const parent = mk('501'), crop = mk('502');
    // Coral Guard was seen on both views earlier (so subjects wait for it), then the scene went quiet
    sc.emit('501', 'ObjectDetector', { sourceId: '913', timestamp: clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    sc.emit('502', 'ObjectDetector', { sourceId: '913', timestamp: clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    clock.advance(30_000);
    out['501'].length = 0; out['502'].length = 0;
    const ai = () => sc.emit('501', 'ObjectDetector', { sourceId: CAMP, timestamp: clock.now(), detections: [{ className: 'people', score: 0.9 }] });
    const coralOnCrop = () => sc.emit('502', 'ObjectDetector', { sourceId: '913', timestamp: clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    return { sc, clock, roles, parent, crop, out, ai, coralOnCrop };
}

test('split: capabilities name the views and the parent; mirrors are not views and are never observed', () => {
    const x = splitSetup();
    assert.equal(x.parent.isSplit(), true);
    assert.deepEqual(x.parent.views(), ['502']);
    assert.equal(x.parent.parentId(), undefined);
    assert.equal(x.crop.isSplit(), true);
    assert.equal(x.crop.parentId(), '501');
    assert.equal(isMirror(x.sc.devices.get('503')), true);
    assert.equal(isMirror(x.sc.devices.get('502')), false);
});

test('split: a mirror\'s presence child (a parked car) opens no event; its own motion does', () => {
    const x = splitSetup();
    x.sc.add({ id: '504', name: 'Parent mirror Vehicle presence', pluginId: CAMP, providerId: '503', interfaces: ['OccupancySensor'], mixins: [] });
    const out: { type: string; body: any }[] = [];
    const mirror = new CameraCore(x.sc.env('503', x.clock, x.roles), {
        publish: (type, body) => out.push({ type, body }), eventStarted: () => { }, eventEnded: () => { }, subjectDecided: () => { },
    });
    mirror.bind();
    x.sc.emit('504', 'OccupancySensor', true, {});
    x.clock.advance(5000);
    assert.equal(out.filter(m => m.type === 'event.start').length, 0, 'a parked car only sets context');
    x.sc.emit('503', 'MotionSensor', true, {});
    x.clock.advance(1000);
    assert.equal(out.filter(m => m.type === 'event.start').length, 1);
});

test('split: the parent\'s whole-frame AI alone opens no event, on the parent or on the crop', () => {
    const x = splitSetup();
    for (let i = 0; i < 5; i++) { x.ai(); x.clock.advance(1000); }
    x.clock.advance(20_000);
    assert.equal(x.out['501'].length, 0);
    assert.equal(x.out['502'].length, 0);
});

test('split: Coral Guard on the crop opens the event; the parent\'s AI within 2 s joins it as ambiguous trigger evidence', () => {
    const x = splitSetup();
    x.ai();                      // the camera AI is usually first (spike S1: 0.8-1 s before Coral Guard)
    x.clock.advance(900);
    x.coralOnCrop();
    x.clock.advance(800);
    x.ai();
    x.clock.advance(1000);
    const starts = x.out['502'].filter(m => m.type === 'event.start');
    assert.equal(starts.length, 1);
    assert.equal(x.out['501'].filter(m => m.type === 'event.start').length, 0, 'nothing on the parent view');
    const ev = x.out['502'].filter(m => m.type === 'event.update').pop()!.body.event;
    assert.equal(ev.subjects.length, 1);
    const s = ev.subjects[0];
    assert.equal(s.verdict, 'confirmed');
    assert.deepEqual(Object.keys(s.sources).sort(), [CAMP, CG].sort());
    assert.equal(s.sources[CAMP].ambiguous, true);
    assert.equal(s.sources[CAMP].role, 'trigger');
    assert.equal(s.sources[CAMP].count, 2, 'the buffered one and the later one');
    assert.equal(s.sources[CG].ambiguous, undefined);
    const obs = x.out['502'].filter(m => m.type === 'event.update').flatMap(m => m.body.observations);
    assert.ok(obs.some((o: any) => o.source === CAMP && o.ambiguous === true));
});

test('split: the parent\'s AI of another family, or later than 2 s, does not join', () => {
    const x = splitSetup();
    x.coralOnCrop();
    x.clock.advance(2500);
    x.ai();
    x.sc.emit('501', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now(), detections: [{ className: 'vehicle', score: 0.9 }] });
    x.clock.advance(1000);
    const ev = x.out['502'].filter(m => m.type === 'event.update').pop()?.body.event ?? x.out['502'][0].body.event;
    assert.equal(ev.subjects.length, 1);
    assert.equal(ev.subjects[0].sources[CAMP], undefined);
});

test('detections are taken from the plain, layer and system listeners, each payload once', () => {
    const x = setup();
    const env = x.core.env;
    const sys: ((dev: string, d: any, data: any) => void)[] = [];
    x.core.setEnv({ ...env, listenAll: (_iface, cb) => { sys.push(cb); return () => { }; } });
    const payload = { sourceId: CAMP, timestamp: x.clock.now(), detections: [{ className: 'people', score: 1 }] };
    x.sc.emit('7', 'ObjectDetector', payload);                 // plain
    x.sc.emit('7', 'ObjectDetector', payload, {}, '7');        // device layer
    sys.forEach(f => f('7', {}, payload));                     // system-wide
    sys.forEach(f => f('99', {}, payload));                    // another camera: not ours
    assert.equal(x.out.filter(m => m.type === 'event.start').length, 1);
    assert.equal(x.core.tracker.current!.observations, 1);
    assert.deepEqual({ ...x.core.detectionStats }, { total: 1, plain: 1, layer: 1, system: 1 });
    // only a layer delivers: still counted
    x.clock.advance(1000);
    x.sc.emit('7', 'ObjectDetector', { ...payload, timestamp: x.clock.now() }, {}, '7');
    assert.equal(x.core.detectionStats.total, 2);
});

// ---- liveness beats: an empty ObjectsDetected from a verifier (Coral Guard 0.3.50+, about every 60 s while it verifies) ----

const beat = (x: ReturnType<typeof setup>, extra: Record<string, unknown> = {}) =>
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [], ...extra });

test('liveness: the live window is about 3 min', () => {
    assert.equal(VERIFIER_LIVE_MS, 3 * 60_000);
});

test('liveness: a verifier\'s empty beats keep it live on a quiet camera, so its silence on a false event is a veto', () => {
    const x = setup();
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    // an hour of quiet with a beat every minute
    for (let i = 0; i < 60; i++) { x.clock.advance(60_000); beat(x); }
    assert.equal(x.core.verifierLive(), true);
    assert.equal(x.core.verifierLiveOf(CG), true);
    x.out.length = 0;
    x.sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now(), detections: [{ className: 'people', score: 1 }] });
    assert.equal(x.core.tracker.current!.subjects.length, 1, 'the beats created no subject');
    x.clock.advance(6000);
    assert.equal(x.out.find(m => m.type === 'subject.verdict')!.body.subject.verdict, 'vetoed');
});

test('liveness: the beats stop, and after 3 min the verifier is not live (unverified, not vetoed)', () => {
    const x = setup();
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    x.clock.advance(60_000);
    beat(x);
    x.clock.advance(2 * 60_000);
    assert.equal(x.core.verifierLiveOf(CG), true, 'within 3 min of the last beat');
    x.clock.advance(60_000 + 1);
    assert.equal(x.core.verifierLiveOf(CG), false);
    assert.equal(x.core.verifierLive(), false);
    x.out.length = 0;
    x.sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now(), detections: [{ className: 'people', score: 1 }] });
    x.clock.advance(6000);
    assert.equal(x.out.find(m => m.type === 'subject.verdict')!.body.subject.verdict, 'unverified');
});

test('liveness: an empty payload creates no observation, no event, no source row, and is not counted as a detection', () => {
    const x = setup();
    beat(x);
    x.sc.emit('7', 'ObjectDetector', { sourceId: 'some/unknown', timestamp: x.clock.now() + 1, detections: [] });
    x.sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now() + 2 });
    assert.equal(x.out.length, 0);
    assert.equal(x.core.tracker.current, undefined);
    assert.equal(x.roles.get('some/unknown'), undefined, 'no row for a source first seen through an empty payload');
    assert.equal(x.core.detectionStats.total, 0);
    assert.deepEqual(x.core.verifiers(), [], 'a beat never makes a source present');
    assert.equal(x.core.verifierLive(), false);
});

test('liveness: an empty payload from a trigger source is not verifier activity', () => {
    const x = setup();
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    x.sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now(), detections: [{ className: 'people', score: 1 }] });
    x.clock.advance(VERIFIER_LIVE_MS + 1000);
    x.sc.emit('7', 'ObjectDetector', { sourceId: CAMP, timestamp: x.clock.now(), detections: [] });
    assert.equal(x.core.verifierLive(), false);
});

test('liveness: empty beats arrive by the plain, layer and system listeners, each counted once and none dropped', () => {
    for (const only of ['plain', 'layer', 'system', 'all'] as const) {
        const x = setup();
        const sys: ((dev: string, d: any, data: any) => void)[] = [];
        x.core.setEnv({ ...x.core.env, listenAll: (_iface, cb) => { sys.push(cb); return () => { }; } });
        x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
        x.clock.advance(VERIFIER_LIVE_MS + 1000);
        assert.equal(x.core.verifierLive(), false);
        const before = { ...x.core.detectionStats };
        x.out.length = 0;
        const payload = { sourceId: '913', timestamp: x.clock.now(), detections: [] };
        if (only === 'plain' || only === 'all') x.sc.emit('7', 'ObjectDetector', payload);
        if (only === 'layer' || only === 'all') x.sc.emit('7', 'ObjectDetector', payload, {}, '913');
        if (only === 'system' || only === 'all') sys.forEach(f => f('7', {}, payload));
        assert.equal(x.core.verifierLive(), true, only);
        assert.equal(x.core.detectionStats.total, before.total, `${only}: not a detection`);
        if (only === 'all') {
            assert.deepEqual([x.core.detectionStats.plain - before.plain, x.core.detectionStats.layer - before.layer, x.core.detectionStats.system - before.system], [1, 1, 1]);
            assert.equal(x.out.length, 0);
        }
    }
});

test('liveness: a duplicate beat (same payload by another path) is deduplicated before the bookkeeping', () => {
    const x = setup();
    const env = x.core.env;
    const sys: ((dev: string, d: any, data: any) => void)[] = [];
    x.core.setEnv({ ...env, listenAll: (_iface, cb) => { sys.push(cb); return () => { }; } });
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    x.clock.advance(VERIFIER_LIVE_MS + 1000);
    const payload = { sourceId: '913', timestamp: x.clock.now(), detections: [] };
    x.sc.emit('7', 'ObjectDetector', payload);
    const at = x.clock.now();
    x.clock.advance(2000);
    sys.forEach(f => f('7', {}, payload)); // the same beat, late by another path: dropped, liveness stays at the first arrival
    x.clock.advance(VERIFIER_LIVE_MS - 2000 + 1);
    assert.ok(x.clock.now() - at > VERIFIER_LIVE_MS);
    assert.equal(x.core.verifierLive(), false);
});

test('liveness: a crop does not take its parent\'s beats (Coral Guard verifies each view on its own)', () => {
    const x = splitSetup();
    x.clock.advance(VERIFIER_LIVE_MS);
    x.sc.emit('501', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [] });
    assert.equal(x.parent.verifierLive(), true);
    assert.equal(x.crop.verifierLive(), false);
    assert.equal(x.out['501'].length + x.out['502'].length, 0);
});

test('liveness: beats keep a present verifier present past 7 days', () => {
    const x = setup();
    x.sc.emit('7', 'ObjectDetector', { sourceId: '913', timestamp: x.clock.now(), detections: [{ className: 'verified_person', score: 1 }] });
    for (let d = 0; d < 8; d++) {
        for (let i = 0; i < 24; i++) { x.clock.advance(3600_000); beat(x); }
    }
    assert.ok(8 * 24 * 3600_000 > VERIFIER_RECENT_MS);
    assert.deepEqual(x.core.verifiers(), [CG], 'still present: it kept beating');
    assert.equal(x.core.verifierPresent(), true);
});

test('liveness: a verifier mixin that never detected here but beats is responding (page), yet does not count for verdicts', () => {
    const x = setup();
    assert.equal(x.roles.get(CG), undefined, 'no row yet: a known verifier counts by name');
    beat(x);
    assert.deepEqual(x.core.silentVerifiers(), [CG]);
    assert.equal(x.core.verifierLiveOf(CG), true);
    assert.equal(x.core.verifierPresent(), false);
    x.clock.advance(VERIFIER_LIVE_MS + 1);
    assert.equal(x.core.verifierLiveOf(CG), false);
});
