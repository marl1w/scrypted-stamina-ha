import assert from 'node:assert/strict';
import { test } from 'node:test';
import { attribute, fromBinary, fromDetections, normBox } from '../src/normalize';
import { compatible, familyOf, formatClassMap, inferRole, parseClassMap, RolesTable } from '../src/roles';

const CAM = '@marl1w/scrypted-reolink';

test('inferred roles: camera plugin trigger, known detector verifier, face/label enricher, motion trigger, other mixin verifier', () => {
    assert.equal(inferRole({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }), 'trigger');
    assert.equal(inferRole({ source: 'scrypted-coral-guard', kind: 'object', className: 'verified_person', cameraPlugin: CAM }), 'verifier');
    assert.equal(inferRole({ source: 'some/face-plugin', kind: 'object', className: 'face', cameraPlugin: CAM }), 'enricher');
    assert.equal(inferRole({ source: 'some/lpr', kind: 'object', className: 'vehicle', label: 'AB123CD', cameraPlugin: CAM }), 'enricher');
    assert.equal(inferRole({ source: 'some/analyzer', kind: 'object', className: 'person', cameraPlugin: CAM }), 'verifier');
});

test('roles table: rows added on first sight, persisted, role changes stick, bounded', () => {
    let saves = 0;
    const t = new RolesTable(null, () => saves++);
    const r = t.ensure({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }, 1000)!;
    assert.equal(r.role, 'trigger');
    assert.equal(r.inferred, true);
    assert.equal(saves, 1);
    t.ensure({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }, 2000);
    assert.equal(saves, 1, 'lastSeen is not persisted on every observation');
    t.setRole(CAM, 'ignore');
    const t2 = new RolesTable(t.toJSON());
    assert.equal(t2.get(CAM)!.role, 'ignore');
    assert.equal(t2.get(CAM)!.inferred, false);
    // invalid names are refused, corrupt JSON gives an empty table
    assert.equal(t.ensure({ source: 'bad name with spaces', kind: 'object' }, 1), undefined);
    assert.equal(new RolesTable('{not json').list().length, 0);
    const big = new RolesTable();
    for (let i = 0; i < 100; i++) big.ensure({ source: `p${i}`, kind: 'object' }, i);
    assert.equal(big.list().length, RolesTable.MAX_ROWS);
});

test('class map: per-source defaults for scrypted-reolink and coral guard, unknown classes pass through', () => {
    const t = new RolesTable();
    t.ensure({ source: CAM, kind: 'object', cameraPlugin: CAM }, 1);
    t.ensure({ source: 'scrypted-coral-guard', kind: 'object' }, 1);
    assert.equal(t.mapClass(CAM, 'people'), 'person');
    assert.equal(t.mapClass(CAM, 'dog_cat'), 'animal');
    assert.equal(t.mapClass(CAM, 'vehicle'), 'vehicle');
    for (const c of ['line_crossing', 'intrusion', 'loitering', 'forgotten_item', 'taken_item'])
        assert.equal(t.mapClass(CAM, c), c);
    assert.equal(t.mapClass('scrypted-coral-guard', 'verified_person'), 'person');
    assert.equal(t.mapClass('scrypted-coral-guard', 'verified_animal'), 'animal');
    assert.equal(t.mapClass(CAM, 'Package'), 'package');
    assert.equal(t.mapClass('never-seen', 'people'), 'person', 'common aliases apply before a row exists');
});

test('class map text: parsed strictly, round-trips', () => {
    const m = parseClassMap('people=person, dog_cat = animal\nbad, a=b=c, x=y z, car=vehicle');
    assert.deepEqual(m, { people: 'person', dog_cat: 'animal', car: 'vehicle' });
    assert.deepEqual(parseClassMap(formatClassMap(m)), m);
});

test('families: aliases fuse, any fuses with all', () => {
    assert.equal(familyOf('car'), 'vehicle');
    assert.equal(familyOf('line_crossing'), 'any');
    assert.equal(familyOf('unknown_thing'), 'unknown_thing');
    assert.ok(compatible('person', 'any'));
    assert.ok(!compatible('person', 'vehicle'));
});

test('attribution: mixinId, then sourceId (device id or plugin id), then the camera plugin', () => {
    const devices: Record<string, string> = { '913': 'scrypted-coral-guard', '@marl1w/scrypted-reolink': CAM };
    const pluginOf = (id: string) => devices[id];
    assert.equal(attribute({ mixinId: '913', sourceId: 'x' }, pluginOf, CAM), 'scrypted-coral-guard');
    assert.equal(attribute({ sourceId: '913' }, pluginOf, CAM), 'scrypted-coral-guard');
    assert.equal(attribute({ sourceId: CAM }, pluginOf, CAM), CAM);
    assert.equal(attribute({ sourceId: 'some-name' }, () => undefined, CAM), 'some-name');
    assert.equal(attribute({ sourceId: 'bad id!' }, () => undefined, CAM), CAM);
    assert.equal(attribute({}, pluginOf, CAM), CAM);
});

test('normalised box: pixels scaled by inputDimensions, clamped, degenerate dropped', () => {
    assert.deepEqual(normBox([64, 48, 128, 96], [640, 480]), [0.1, 0.1, 0.3, 0.3]);
    assert.deepEqual(normBox([600, 400, 100, 100], [640, 480]), [0.9375, 0.8333, 1, 1]);
    assert.deepEqual(normBox([0.1, 0.2, 0.3, 0.4], undefined), [0.1, 0.2, 0.4, 0.6000]);
    assert.equal(normBox([10, 10, 50, 50], undefined), null, 'pixels without dimensions');
    assert.equal(normBox([10, 10, 0, 50], [640, 480]), null);
    assert.equal(normBox([NaN, 1, 2, 3], [640, 480]), null);
    assert.equal(normBox('x', [640, 480]), null);
});

test('detections normalise: class map, clamped score, track, label, ignore role, bounded count, sane time', () => {
    const roles = new RolesTable();
    const now = 1_700_000_000_000;
    const ctx = { roles, cameraPlugin: CAM, now };
    const out = fromDetections({
        timestamp: now - 200, inputDimensions: [640, 480],
        detections: [
            { className: 'people', score: 1.7, boundingBox: [64, 48, 128, 96], id: 17 },
            { className: 'dog_cat', score: -1 },
            { className: '' },
            { className: 'face', score: 0.8, label: 'x\u0000y' },
        ],
    }, CAM, now, ctx);
    assert.equal(out.length, 3);
    assert.deepEqual([out[0].class, out[0].raw, out[0].score, out[0].track, out[0].t, out[0].role], ['person', 'people', 1, '17', now - 200, 'trigger']);
    assert.deepEqual(out[0].box, [0.1, 0.1, 0.3, 0.3]);
    assert.equal(out[1].score, 0);
    assert.equal(out[2].label, 'xy');
    // a detection timestamp far off is replaced by the receive time
    assert.equal(fromDetections({ timestamp: now - 3600_000, detections: [{ className: 'people', score: 1 }] }, CAM, undefined, ctx)[0].t, now);
    roles.setRole(CAM, 'ignore');
    assert.equal(fromDetections({ timestamp: now, detections: [{ className: 'people', score: 1 }] }, CAM, now, ctx).length, 0);
    const many = { timestamp: now, detections: Array.from({ length: 100 }, () => ({ className: 'person', score: 1 })) };
    assert.equal(fromDetections(many, 'other', now, ctx).length, 32);
});

test('binary edges: only rising edges observe; class from the kind', () => {
    const roles = new RolesTable();
    const ctx = { roles, cameraPlugin: CAM, now: 5 };
    assert.equal(fromBinary('motion', true, CAM, 4, ctx)!.class, 'motion');
    assert.equal(fromBinary('occupancy', true, 'scrypted-coral-guard', 4, ctx)!.class, 'presence');
    assert.equal(fromBinary('occupancy', true, 'scrypted-coral-guard', 4, ctx)!.role, 'verifier');
    assert.equal(fromBinary('motion', false, CAM, 4, ctx), undefined);
});

test('lastSeen is exact in memory and persisted at most once a minute', () => {
    let saves = 0;
    const t = new RolesTable(null, () => saves++);
    t.ensure({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }, 0);
    for (let i = 1; i <= 120; i++) t.ensure({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }, i * 1000);
    assert.equal(t.get(CAM)!.lastSeen, 120_000);
    assert.equal(saves, 4, 'created, its class noted, then at 60 s and 120 s');
});
