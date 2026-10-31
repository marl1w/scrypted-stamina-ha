import assert from 'node:assert/strict';
import { test } from 'node:test';
import { fromDetections } from '../src/normalize';
import { classMapRows, IGNORE_CLASS, parseClassMapRows, RolesTable, signalRole } from '../src/roles';
import { ADVANCED_GROUP, applySourceSetting, cameraStatusLines, CLASSMAP_GROUP, classesOf, IGNORE, KEEP, ROLE_CHOICES, roleFromChoice, sourceSettings, SOURCES_GROUP } from '../src/settings';

const CAM = '@marl1w/scrypted-reolink';
const CG = 'scrypted-coral-guard';
const AG = '@marl1w/scrypted-audio-guard';
const names: Record<string, string> = { [CAM]: 'Reolink', [CG]: 'Coral Guard', [AG]: 'Audio Guard' };
const nameOf = (s: string) => names[s];

function table() {
    const t = new RolesTable();
    t.preseed({ source: CAM, cameraPlugin: true, ifaces: ['ObjectDetector', 'MotionSensor'] });
    t.preseed({ source: CG, cameraPlugin: false, ifaces: ['ObjectDetector', 'MotionSensor', 'OccupancySensor'] });
    t.preseed({ source: AG, cameraPlugin: false, ifaces: ['AudioSensor'] });
    return t;
}

test('migration: 0.1.0/0.1.1 rows load as one role per source; verified_* leave the sources they do not belong to', () => {
    const old = JSON.stringify([
        { source: CAM, role: 'trigger', inferred: true, roleSet: false, signalRole: 'trigger', gated: false, classMap: { people: 'person', verified_person: 'person', verified_animal: 'animal', mine: 'person' }, firstSeen: 1, lastSeen: 2 },
        { source: CG, role: 'verifier', inferred: true, roleSet: true, gated: true, classMap: { verified_person: 'person' }, firstSeen: 1, lastSeen: 2 },
    ]);
    assert.equal(RolesTable.needsMigration(old), true);
    const t = new RolesTable(old);
    assert.deepEqual(t.get(CAM)!.classMap, { people: 'person', mine: 'person' });
    assert.deepEqual(t.get(CG)!.classMap, { verified_person: 'person' });
    assert.deepEqual(t.get(CAM)!.classes, []);
    assert.equal(RolesTable.needsMigration(t.toJSON()), false);
    assert.equal(signalRole(t.get(CG)!), 'verifier');
    assert.equal(RolesTable.needsMigration('{bad'), false);
});

test('discovered sources: inferred roles before they send anything; "Not seen yet" until the first sighting', () => {
    const t = table();
    assert.equal(t.get(CAM)!.role, 'trigger');
    assert.equal(t.get(CG)!.role, 'verifier');
    assert.equal(t.get(CG)!.gated, true, 'Coral Guard: its motion counts as confirmation');
    assert.equal(t.get(AG)!.role, 'trigger');
    let s = sourceSettings(t.list(), nameOf).sources;
    assert.match(String(s.find(x => x.key === `role:${CG}`)!.description), /Not seen yet/);
    // a detection: seen, and its detection role decided (still Coral Guard's verifier)
    fromDetections({ timestamp: 5, detections: [{ className: 'verified_person', score: 1 }] }, CG, 5, { roles: t, cameraPlugin: CAM, now: 5 });
    assert.equal(t.get(CG)!.firstSeen, 5);
    assert.equal(t.get(CG)!.roleSet, true);
    s = sourceSettings(t.list(), nameOf).sources;
    assert.ok(!/Not seen yet/.test(String(s.find(x => x.key === `role:${CG}`)!.description)));
    // existing rows are never overwritten by discovery
    t.setRole(CAM, 'ignore');
    t.preseed({ source: CAM, cameraPlugin: true, ifaces: ['ObjectDetector'] });
    assert.equal(t.get(CAM)!.role, 'ignore');
});

test('rendering: a summary, then one question per source ordered by role; no gated toggle, no "(Default.)"', () => {
    const { sources } = sourceSettings(table().list(), nameOf);
    assert.equal(sources[0].key, 'sourcesSummary');
    assert.equal(sources[0].value, 'Audio Guard starts events · Reolink starts events · Coral Guard double-checks. Change only if this is wrong.');
    assert.deepEqual(sources.slice(1).map(x => x.title), [
        'What should Stamina do with Audio Guard?', 'What should Stamina do with Reolink?', 'What should Stamina do with Coral Guard?']);
    assert.deepEqual(sources[1].choices, ['Start events', 'Double-check other detections', 'Add names / plates', 'Ignore']);
    assert.equal(sources[3].value, 'Double-check other detections');
    assert.ok(sources.every(x => x.group === SOURCES_GROUP));
    assert.ok(!sources.some(x => String(x.key).startsWith('gated:')));
    assert.ok(!sources.some(x => /Default/.test(String(x.description))));
    assert.equal(sourceSettings([], nameOf).sources[0].readonly, true);
});

test('class map: one select per (source, class); each source lists only its own classes; known ones go to Advanced', () => {
    const t = table();
    t.noteClass(CAM, 'people');
    t.noteClass(CAM, 'mystery_thing');
    // the cameras' object types include Coral Guard's published classes: they are Coral Guard's, not Reolink's
    const declared = new Map([[CAM, ['people', 'vehicle', 'dog_cat', 'verified_person', 'verified_vehicle', 'verified_animal']]]);
    assert.deepEqual(classesOf(t.get(CAM)!, declared.get(CAM)), ['people', 'vehicle', 'dog_cat', 'mystery_thing']);
    assert.deepEqual(classesOf(t.get(CG)!), ['verified_person', 'verified_vehicle', 'verified_animal']);
    const { classes, advanced } = sourceSettings(t.list(), nameOf, { declared });
    // unknown: only Reolink's mystery_thing, in Class map
    assert.deepEqual(classes.map(x => [x.group, x.subgroup, x.title, x.value]), [[CLASSMAP_GROUP, 'Unknown classes from Reolink', 'mystery_thing', KEEP]]);
    assert.ok(classes[0].description);
    // known ones: in Advanced, under their own source; the description on the first row of each source only
    const reo = advanced.filter(x => x.subgroup === 'Classes from Reolink');
    const cor = advanced.filter(x => x.subgroup === 'Classes from Coral Guard');
    assert.deepEqual(reo.map(x => [x.title, x.value]), [['people', 'person'], ['vehicle', 'vehicle'], ['dog_cat', 'animal']]);
    assert.deepEqual(cor.map(x => [x.title, x.value]), [['verified_person', 'person'], ['verified_vehicle', 'vehicle'], ['verified_animal', 'animal']]);
    assert.ok(reo[0].description && !reo[1].description && !reo[2].description);
    assert.ok(cor[0].description && !cor[1].description);
    assert.ok(advanced.every(x => x.group === ADVANCED_GROUP));
    assert.deepEqual(reo[0].choices, ['person', 'vehicle', 'animal', 'package', 'face', 'plate', 'line_crossing', 'intrusion', 'loitering', 'forgotten_item', 'taken_item', KEEP, IGNORE]);
});

test('class select: a class, keep as is, ignore (the detection is dropped); roles by answer', () => {
    const t = table();
    assert.ok(applySourceSetting(t, `class:${CAM}:mystery_thing`, 'animal'));
    assert.equal(t.mapClass(CAM, 'mystery_thing'), 'animal');
    applySourceSetting(t, `class:${CAM}:people`, KEEP);
    assert.equal(t.mapClass(CAM, 'people'), 'people');
    applySourceSetting(t, `class:${CAM}:dog_cat`, IGNORE);
    assert.equal(t.get(CAM)!.classMap.dog_cat, IGNORE_CLASS);
    const out = fromDetections({ timestamp: 1, detections: [{ className: 'dog_cat', score: 1 }, { className: 'mystery_thing', score: 1 }] }, CAM, 1, { roles: t, cameraPlugin: CAM, now: 1 });
    assert.deepEqual(out.map(o => o.class), ['animal']);
    applySourceSetting(t, `class:${CAM}:x`, 'bad value!');
    assert.equal(t.get(CAM)!.classMap.x, undefined);
    assert.equal(roleFromChoice('Double-check other detections'), 'verifier');
    assert.equal(roleFromChoice('ignore'), 'ignore');
    assert.ok(applySourceSetting(t, `role:${AG}`, ROLE_CHOICES.ignore));
    assert.equal(t.get(AG)!.role, 'ignore');
    assert.equal(applySourceSetting(t, 'marginS', 5), false);
});

test('the old list form of a class map still parses (kept for the 0.1.1 migration)', () => {
    const { map, invalid } = parseClassMapRows(['people → person', 'dog_cat=animal', 'nonsense']);
    assert.deepEqual(map, { people: 'person', dog_cat: 'animal' });
    assert.deepEqual(invalid, ['nonsense']);
    assert.deepEqual(classMapRows(map), ['people → person', 'dog_cat → animal']);
});

test('camera page: plain words and display names', () => {
    const lines = cameraStatusLines({
        triggers: [CAM, AG], verifiers: [], silent: [CG], prebufferMs: 10_000, recordings: true, clips: false, split: false, parent: null, detections: 12,
    }, nameOf);
    assert.deepEqual(lines, [
        'Starts events: Reolink, Audio Guard',
        'Double-check: Coral Guard (not responding)',
        'Video before the event: 10 s',
        'Recordings: yes · Clips: no',
        'Detections received: 12',
    ]);
    assert.ok(!lines.join(' ').match(/VideoRecorder|ObjectDetector|prebuffer/i));
});

test('camera page: a verifier is "not responding" only when it is not live (no activity or beat within 3 min)', () => {
    const line = (live: boolean) => cameraStatusLines({
        triggers: [CAM], verifiers: [{ source: CG, live }], silent: [], prebufferMs: null, recordings: false, clips: false, split: false, parent: null, detections: 0,
    }, nameOf)[1];
    assert.equal(line(true), 'Double-check: Coral Guard');
    assert.equal(line(false), 'Double-check: Coral Guard (not responding)');
});
