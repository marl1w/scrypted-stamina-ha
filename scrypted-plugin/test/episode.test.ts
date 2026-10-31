import assert from 'node:assert/strict';
import { test } from 'node:test';
import { EpisodeTracker, EventRecord, publicEvent, DEFAULT_EPISODE } from '../src/episode';
import { iou, match, matchEnricher, newSubject, Subject } from '../src/fusion';
import { FakeClock, obs } from './helpers';

const V = 'coral';

function setup(verifier = true, cfg = DEFAULT_EPISODE) {
    const clock = new FakeClock();
    const log: { type: string; ev: EventRecord; s?: Subject; obs?: number }[] = [];
    const tr = new EpisodeTracker(cfg, { clock, camera: { id: '7', name: 'Cam' }, verifierPresent: () => verifier }, {
        onStart: (ev, o) => log.push({ type: 'start', ev, obs: o.length }),
        onUpdate: (ev, o) => log.push({ type: 'update', ev, obs: o.length }),
        onVerdict: (ev, s) => log.push({ type: 'verdict', ev, s: { ...s } }),
        onEnd: ev => log.push({ type: 'end', ev }),
    });
    return { clock, log, tr };
}

// ---- fusion ----------------------------------------------------------------------------------

test('fusion: the same track id of the same source joins; another source with the same id does not', () => {
    const c = new FakeClock();
    const s = newSubject('s1', obs(c, { track: '17', box: [0, 0, 0.1, 0.1] }));
    assert.equal(match([s], obs(c, { track: '17', box: [0.8, 0.8, 0.9, 0.9] })), s);
    assert.equal(match([s], obs(c, { source: 'other', track: '17', box: [0.8, 0.8, 0.9, 0.9] })), undefined);
});

test('fusion: box overlap IoU >= 0.3 joins, below does not; incompatible families never join', () => {
    const c = new FakeClock();
    const s = newSubject('s1', obs(c, { box: [0.1, 0.1, 0.5, 0.5] }));
    assert.ok(iou([0.1, 0.1, 0.5, 0.5], [0.2, 0.2, 0.6, 0.6]) >= 0.3);
    assert.equal(match([s], obs(c, { source: V, role: 'verifier', box: [0.2, 0.2, 0.6, 0.6] })), s);
    assert.ok(iou([0.1, 0.1, 0.5, 0.5], [0.4, 0.4, 0.8, 0.8]) < 0.3);
    assert.equal(match([s], obs(c, { box: [0.4, 0.4, 0.8, 0.8] })), undefined, 'both boxed, low IoU: a new subject');
    assert.equal(match([s], obs(c, { class: 'car', box: [0.1, 0.1, 0.5, 0.5] })), undefined, 'vehicle vs person');
});

test('fusion: class family within +-2 s when one side has no box; beyond 2 s a new subject', () => {
    const c = new FakeClock();
    const s = newSubject('s1', obs(c));
    c.advance(1500);
    assert.equal(match([s], obs(c, { source: V, class: 'person', box: [0, 0, 0.5, 0.5] })), s);
    c.advance(1000);
    assert.equal(match([s], obs(c)), undefined);
    // motion (family any) joins the latest subject of any family
    const p = newSubject('s2', obs(c, { class: 'car' }));
    assert.equal(match([p], obs(c, { kind: 'motion', class: 'motion' })), p);
});

test('fusion: an exact family match is preferred over an `any` subject', () => {
    const c = new FakeClock();
    const m = newSubject('s1', obs(c, { class: 'motion', kind: 'motion' }));
    const p = newSubject('s2', obs(c, { class: 'person' }));
    assert.equal(match([p, m], obs(c, { class: 'person' })), p);
    assert.equal(match([m, p], obs(c, { class: 'person' })), p);
});

test('fusion: enrichers attach to the subject containing their box', () => {
    const c = new FakeClock();
    const a = newSubject('s1', obs(c, { box: [0.1, 0.1, 0.4, 0.9] }));
    const b = newSubject('s2', obs(c, { box: [0.5, 0.1, 0.9, 0.9] }));
    assert.equal(matchEnricher([a, b], obs(c, { role: 'enricher', class: 'face', box: [0.6, 0.15, 0.7, 0.3] })), b);
    assert.equal(matchEnricher([a, b], obs(c, { role: 'enricher', class: 'face' })), b, 'no box: the latest compatible subject');
});

// ---- episodes and verdicts ---------------------------------------------------------------------

test('event starts at the first trigger and ends a margin after the last observation', () => {
    const { clock, log, tr } = setup(false);
    tr.observe(obs(clock));
    assert.equal(log[0].type, 'start');
    assert.equal(log[1].type, 'verdict');
    assert.equal(log[1].s!.verdict, 'unverified', 'no verifier: unverified at once');
    clock.advance(1500);
    tr.observe(obs(clock));
    clock.advance(9999);
    assert.ok(!log.some(l => l.type === 'end'));
    clock.advance(1);
    const end = log.find(l => l.type === 'end')!;
    assert.equal(end.ev.end_reason, 'quiet');
    assert.equal(end.ev.end, end.ev.last_observation + 10_000);
    assert.equal(end.ev.subjects.length, 1);
    assert.equal(tr.current, undefined);
});

test('a verifier observation starts an event when there is no trigger; enrichers alone do not', () => {
    const { clock, log, tr } = setup(true);
    tr.observe(obs(clock, { role: 'enricher', class: 'face' }));
    assert.equal(log.length, 0);
    tr.observe(obs(clock, { source: V, role: 'verifier' }));
    assert.equal(log[0].type, 'start');
    assert.equal(log[0].ev.trigger.role, 'verifier');
    assert.equal(log[1].s!.verdict, 'confirmed');
});

test('held signals keep the event open; it ends a margin after the last one turns off', () => {
    const { clock, log, tr } = setup(false);
    tr.hold('motion', true, obs(clock, { kind: 'motion', class: 'motion' }));
    clock.advance(60_000);
    assert.ok(!log.some(l => l.type === 'end'));
    tr.hold('motion', false);
    clock.advance(9_999);
    assert.ok(!log.some(l => l.type === 'end'));
    clock.advance(1);
    const end = log.find(l => l.type === 'end')!.ev;
    assert.equal(end.end! - end.start, 70_000, 'held 60 s, then the margin');
});

test('the 10 min cap closes the event; a held signal starts a continuation at once', () => {
    const { clock, log, tr } = setup(false);
    // the signal is refreshed (a new on edge) within the max hold age
    for (let i = 0; i < 5; i++) { tr.hold('motion', true, obs(clock, { kind: 'motion', class: 'motion' })); clock.advance(2 * 60_000); }
    const ends = log.filter(l => l.type === 'end');
    assert.equal(ends.length, 1);
    assert.equal(ends[0].ev.end_reason, 'cap');
    assert.equal(ends[0].ev.flags.capped, true);
    const starts = log.filter(l => l.type === 'start');
    assert.equal(starts.length, 2);
    assert.equal(starts[1].ev.continues, ends[0].ev.id);
    assert.notEqual(starts[1].ev.id, starts[0].ev.id);
});

test('the cap without held signals: the next observation within the margin continues the capped event, later ones do not', () => {
    const { clock, log, tr } = setup(false);
    for (let i = 0; i < 70; i++) { tr.observe(obs(clock)); clock.advance(9_000); }
    const ends = log.filter(l => l.type === 'end');
    assert.equal(ends[0].ev.end_reason, 'cap');
    assert.ok(ends[0].ev.end! - ends[0].ev.start <= 10 * 60_000);
    const starts = log.filter(l => l.type === 'start');
    assert.equal(starts.length, 2);
    assert.equal(starts[1].ev.continues, ends[0].ev.id, 'opened 9 s after the cap, within the 10 s margin');
    // a later event after a quiet end continues nothing
    clock.advance(60_000);
    tr.observe(obs(clock));
    assert.equal(log.filter(l => l.type === 'start')[2].ev.continues, null);
});

test('a held signal never refreshed is released after the max hold age', () => {
    const { clock, log, tr } = setup(false);
    tr.hold('motion', true, obs(clock, { kind: 'motion', class: 'motion' }));
    clock.advance(5 * 60_000 + 10_000);
    const end = log.find(l => l.type === 'end')!;
    assert.equal(end.ev.end_reason, 'quiet');
    assert.equal(tr.held('motion'), false);
});

test('releaseHolds (unbind, offline) ends a held event after the margin', () => {
    const { clock, log, tr } = setup(false);
    tr.hold('motion', true, obs(clock, { kind: 'motion', class: 'motion' }));
    clock.advance(30_000);
    tr.setOffline(true);
    clock.advance(10_000);
    assert.ok(log.some(l => l.type === 'end'));
});

test('verify window: confirmed when the verifier joins in time, vetoed after 5 s without', () => {
    const { clock, log, tr } = setup(true);
    tr.observe(obs(clock, { class: 'person' }));
    tr.observe(obs(clock, { class: 'car', box: [0.6, 0.6, 0.9, 0.9] }));
    assert.equal(tr.current!.subjects.map(s => s.verdict).join(), 'pending,pending');
    clock.advance(1100);
    tr.observe(obs(clock, { source: V, role: 'verifier', class: 'person' }));
    const confirmed = log.filter(l => l.type === 'verdict' && l.s!.verdict === 'confirmed');
    assert.equal(confirmed.length, 1);
    assert.equal(confirmed[0].s!.verdict_source, V);
    clock.advance(3900);
    const vetoed = log.filter(l => l.type === 'verdict' && l.s!.verdict === 'vetoed');
    assert.equal(vetoed.length, 1);
    assert.equal(vetoed[0].s!.class, 'car');
});

test('a vetoed subject is upgraded when the verifier sees it later in the event; confirmed never changes', () => {
    const { clock, log, tr } = setup(true);
    tr.observe(obs(clock, { track: '1' }));
    clock.advance(5000);
    assert.equal(tr.current!.subjects[0].verdict, 'vetoed');
    tr.observe(obs(clock, { track: '1' }));
    tr.observe(obs(clock, { source: V, role: 'verifier' }));
    assert.equal(tr.current!.subjects[0].verdict, 'confirmed');
    tr.observe(obs(clock, { source: V, role: 'verifier' }));
    assert.equal(log.filter(l => l.type === 'verdict').length, 2);
});

test('pending subjects are vetoed when the event closes before their window', () => {
    const cfg = { ...DEFAULT_EPISODE, marginMs: 2000, verifyWindowMs: 5000 };
    const { clock, log, tr } = setup(true, cfg);
    tr.observe(obs(clock));
    clock.advance(2000);
    const v = log.filter(l => l.type === 'verdict');
    assert.equal(v.length, 1);
    assert.equal(v[0].s!.verdict, 'vetoed');
    assert.ok(log.findIndex(l => l.type === 'verdict') < log.findIndex(l => l.type === 'end'));
});

test('no verifier: subjects are unverified, the window is skipped', () => {
    const { clock, tr } = setup(false);
    tr.observe(obs(clock));
    assert.equal(tr.current!.subjects[0].verdict, 'unverified');
    assert.equal(clock.pending, 2, 'cap and end timers only, no verify window');
});

test('updates are coalesced to one per second and carry the new observations', () => {
    const { clock, log, tr } = setup(false);
    tr.observe(obs(clock));
    for (let i = 0; i < 10; i++) { clock.advance(50); tr.observe(obs(clock)); }
    clock.advance(1000);
    const ups = log.filter(l => l.type === 'update');
    assert.equal(ups.length, 1);
    assert.equal(ups[0].obs, 10);
});

test('camera offline during an event is flagged', () => {
    const { clock, tr, log } = setup(false);
    tr.observe(obs(clock));
    tr.setOffline(true);
    clock.advance(20_000);
    assert.equal(log.find(l => l.type === 'end')!.ev.flags.camera_offline, true);
});

test('subject count is bounded', () => {
    const { clock, tr } = setup(false);
    for (let i = 0; i < 60; i++) tr.observe(obs(clock, { box: [i / 60, 0, i / 60 + 0.005, 0.01] }));
    assert.equal(tr.current!.subjects.length, 50);
    assert.equal(tr.current!.flags.subjects_overflow, true);
});

test('public event drops internal fields', () => {
    const { clock, tr } = setup(false);
    tr.observe(obs(clock));
    const p: any = publicEvent(tr.current!);
    assert.equal(p.startRecv, undefined);
    assert.equal(p.subjects[0].firstRecv, undefined);
    assert.match(p.id, /^ev_[A-Za-z0-9_-]{22}$/);
    tr.stop();
});

test('a gated classless signal confirms only the single pending subject; with two pending it goes to its own subject; never a vetoed one', () => {
    const gated = (c: FakeClock) => obs(c, { source: V, role: 'verifier', kind: 'motion', class: 'motion', gated: true });
    const one = setup(true);
    one.tr.observe(obs(one.clock));
    one.tr.observe(gated(one.clock));
    assert.equal(one.tr.current!.subjects.length, 1);
    assert.equal(one.tr.current!.subjects[0].verdict, 'confirmed');

    const two = setup(true);
    two.tr.observe(obs(two.clock, { class: 'person' }));
    two.tr.observe(obs(two.clock, { class: 'car' }));
    two.tr.observe(gated(two.clock));
    const subs = two.tr.current!.subjects;
    assert.deepEqual(subs.map(s => s.verdict), ['pending', 'pending', 'confirmed']);
    assert.equal(subs[2].class, 'motion');

    const vet = setup(true);
    vet.tr.observe(obs(vet.clock));
    vet.clock.advance(5000);
    vet.tr.observe(gated(vet.clock));
    assert.equal(vet.tr.current!.subjects[0].verdict, 'vetoed', 'a vetoed subject is not flipped by a classless signal');
});
