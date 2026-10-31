import assert from 'node:assert/strict';
import { test } from 'node:test';
import { classMapRows, parseClassMapRows, RolesTable, signalRole } from '../src/roles';
import { applySourceSetting, CLASSMAP_GROUP, ROLE_CHOICES, roleFromChoice, sourceSettings, SOURCES_GROUP } from '../src/settings';

const CAM = '@marl1w/scrypted-reolink';
const CG = 'scrypted-coral-guard';

test('migration: 0.1.0 rows with a separate signal role load as one role per source, and are flagged for rewriting', () => {
    const old = JSON.stringify([
        { source: CAM, role: 'trigger', inferred: true, roleSet: true, signalRole: 'trigger', gated: false, classMap: { people: 'person' }, firstSeen: 1, lastSeen: 2 },
        { source: CG, role: 'verifier', inferred: true, roleSet: true, signalRole: 'verifier', gated: true, classMap: {}, firstSeen: 1, lastSeen: 2 },
        { source: 'some/audio', role: 'verifier', inferred: true, roleSet: false, signalRole: 'trigger', classMap: {}, firstSeen: 1, lastSeen: 2 },
    ]);
    assert.equal(RolesTable.needsMigration(old), true);
    const t = new RolesTable(old);
    assert.equal(RolesTable.needsMigration(t.toJSON()), false);
    assert.ok(!t.toJSON().includes('signalRole'));
    assert.equal(signalRole(t.get(CAM)!), 'trigger');
    assert.equal(signalRole(t.get(CG)!), 'verifier', 'a gated verifier\'s motion confirms');
    assert.equal(t.get('some/audio')!.gated, false);
    assert.equal(signalRole(t.get('some/audio')!), 'trigger', 'a non-gated verifier\'s signals are triggers');
    t.setRole(CG, 'ignore');
    assert.equal(signalRole(t.get(CG)!), 'ignore');
    // corrupt or empty stores need no migration
    assert.equal(RolesTable.needsMigration('{bad'), false);
    assert.equal(RolesTable.needsMigration(null), false);
});

test('class map rows: "→" or "=" accepted, shown with "→"; invalid rows reported', () => {
    const { map, invalid } = parseClassMapRows(['people → person', 'dog_cat=animal', ' car -> vehicle ', '', 'nonsense', 'a → b c', 'x → ']);
    assert.deepEqual(map, { people: 'person', dog_cat: 'animal', car: 'vehicle' });
    assert.deepEqual(invalid, ['nonsense', 'a → b c', 'x →']);
    assert.deepEqual(classMapRows(map), ['people → person', 'dog_cat → animal', 'car → vehicle']);
    assert.deepEqual(parseClassMapRows('people=person, dog_cat=animal').map, { people: 'person', dog_cat: 'animal' }, 'the old text form still parses');
});

test('rendering: one dropdown per source with its friendly name; gated only for a verifier; class map as list rows', () => {
    const t = new RolesTable();
    t.ensure({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }, 1);
    t.ensure({ source: CG, kind: 'object', className: 'verified_person', cameraPlugin: CAM }, 1);
    const names: Record<string, string> = { [CAM]: 'Reolink', [CG]: 'Coral Guard' };
    const s = sourceSettings(t.list(), src => names[src]);
    const roles = s.filter(x => x.group === SOURCES_GROUP);
    assert.deepEqual(roles.map(x => x.title), ['Reolink', 'Coral Guard', 'Coral Guard: its motion counts as confirmation']);
    assert.equal(roles[0].value, ROLE_CHOICES.trigger);
    assert.equal(roles[1].value, ROLE_CHOICES.verifier);
    assert.deepEqual(roles[0].choices, Object.values(ROLE_CHOICES));
    assert.match(String(roles[0].description), /start events/);
    assert.match(String(roles[0].description), /Plugin: @marl1w\/scrypted-reolink/);
    assert.equal(roles[2].type, 'boolean');
    assert.equal(roles[2].value, true);
    assert.ok(!s.some(x => String(x.key).startsWith('signal:')), 'no separate signal role');
    const cm = s.filter(x => x.group === CLASSMAP_GROUP);
    assert.equal(cm[0].multiple, true);
    assert.ok((cm[0].value as string[]).includes('people → person'));
    assert.ok((cm[1].value as string[]).includes('verified_person → person'));
    // an unknown plugin name falls back to its id
    assert.equal(sourceSettings(t.list(), () => undefined)[0].title, CAM);
    assert.equal(sourceSettings([], () => undefined)[0].readonly, true);
});

test('applying: the label or a bare role sets the role; gated; class map rows with invalid ones shown', () => {
    const t = new RolesTable();
    t.ensure({ source: CAM, kind: 'object', className: 'people', cameraPlugin: CAM }, 1);
    const bad = new Map<string, string[]>();
    assert.equal(roleFromChoice(ROLE_CHOICES.enricher), 'enricher');
    assert.equal(roleFromChoice('ignore'), 'ignore');
    assert.equal(roleFromChoice('nonsense'), undefined);
    assert.ok(applySourceSetting(t, `role:${CAM}`, ROLE_CHOICES.verifier, bad));
    assert.equal(t.get(CAM)!.role, 'verifier');
    assert.equal(t.get(CAM)!.inferred, false);
    applySourceSetting(t, `role:${CAM}`, 'nonsense', bad);
    assert.equal(t.get(CAM)!.role, 'verifier', 'an unknown choice changes nothing');
    applySourceSetting(t, `gated:${CAM}`, true, bad);
    assert.equal(t.get(CAM)!.gated, true);
    applySourceSetting(t, `classmap:${CAM}`, ['people → person', 'oops'], bad);
    assert.deepEqual(t.get(CAM)!.classMap, { people: 'person' });
    assert.deepEqual(bad.get(CAM), ['oops']);
    assert.match(String(sourceSettings(t.list(), () => undefined, bad).find(x => x.key === `classmap:${CAM}`)!.description), /Dropped .*oops/);
    applySourceSetting(t, `classmap:${CAM}`, ['people → person'], bad);
    assert.equal(bad.has(CAM), false);
    assert.equal(applySourceSetting(t, 'marginS', 5, bad), false);
});
