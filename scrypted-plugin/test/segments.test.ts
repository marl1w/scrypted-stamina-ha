import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Fmp4Splitter, Segment } from '../src/fmp4';
import { SegmentStore } from '../src/segments';
import { FakeClock, fragment, initSegment } from './helpers';

const buf = (n: number, fill = 1) => Buffer.alloc(n, fill);

test('fmp4: init, then segments of ~2 s from 1 s fragments, cut by media time even in a burst', () => {
    const inits: Buffer[] = [];
    const segs: Segment[] = [];
    const sp = new Fmp4Splitter(2, { onInit: b => inits.push(b), onSegment: s => segs.push(s) });
    const stream = Buffer.concat([initSegment(), ...Array.from({ length: 7 }, (_, i) => fragment(i + 1, 100 + i, 1))]);
    // feed in awkward chunk sizes
    for (let i = 0; i < stream.length; i += 37) sp.push(stream.subarray(i, i + 37));
    assert.equal(inits.length, 1);
    assert.equal(inits[0].toString('latin1', 4, 8), 'ftyp');
    assert.equal(segs.length, 3);
    assert.deepEqual(segs.map(s => [Math.round(s.start * 100) / 100, Math.round(s.duration * 100) / 100, s.fragments]), [[0, 2, 2], [2, 2, 2], [4, 2, 2]]);
    assert.equal(segs[0].data.toString('latin1', 4, 8), 'moof');
    sp.flush();
    assert.equal(segs.length, 4);
    assert.equal(segs[3].fragments, 1);
});

test('fmp4: a long GOP makes one segment per fragment; 1 s target cuts every fragment', () => {
    const segs: Segment[] = [];
    const sp = new Fmp4Splitter(1, { onInit: () => { }, onSegment: s => segs.push(s) });
    sp.push(Buffer.concat([initSegment(), fragment(1, 0, 0.95), fragment(2, 0.95, 1.05), fragment(3, 2, 4)]));
    assert.equal(segs.length, 3);
});

test('fmp4: malformed boxes throw instead of looping', () => {
    const sp = new Fmp4Splitter(2, { onInit: () => { }, onSegment: () => { } });
    const bad = Buffer.alloc(16);
    bad.writeUInt32BE(4, 0);
    bad.write('moof', 4, 'latin1');
    assert.throws(() => sp.push(bad));
});

test('store: 425 before a segment is cut, 200 when cut, 404 after ack, TTL or the end of the segmenter', () => {
    const clock = new FakeClock();
    const st = new SegmentStore({ ttlMs: 600_000, maxBytes: 1e9, maxEvents: 10 }, clock);
    st.open('e1', 'cam');
    assert.equal(st.getInit('e1').status, 425);
    assert.equal(st.getSegment('e1', 0).status, 425);
    st.setInit('e1', buf(10));
    st.addSegment('e1', buf(100), 0, 2);
    st.addSegment('e1', buf(100), 2, 2);
    assert.equal(st.getInit('e1').status, 200);
    assert.equal(st.getSegment('e1', 1).status, 200);
    assert.equal(st.getSegment('e1', 2).status, 425);
    assert.equal(st.ack('e1', 0), 1);
    assert.equal(st.getSegment('e1', 0).status, 404);
    assert.equal(st.bytes, 110);
    st.finish('e1');
    st.closeThumbnail('e1');
    assert.equal(st.getSegment('e1', 2).status, 404, 'segmenter ended: never coming');
    assert.equal(st.getSegment('nope', 0).status, 404);
    clock.advance(599_999);
    st.sweep();
    assert.equal(st.getSegment('e1', 1).status, 200);
    clock.advance(1);
    st.sweep();
    assert.equal(st.getSegment('e1', 1).status, 404);
    assert.equal(st.has('e1'), false, 'event dropped ttl after its segmenter finished and the segments went');
    assert.equal(st.bytes, 0);
});

test('store: the final ack releases everything once the event is closed', () => {
    const st = new SegmentStore(undefined, new FakeClock());
    st.open('e1', 'cam');
    st.setInit('e1', buf(10));
    st.addSegment('e1', buf(100), 0, 2);
    st.setThumbnail('e1', buf(5), 'start');
    st.finish('e1');
    st.ack('e1', 0);
    assert.equal(st.has('e1'), true, 'the thumbnail may still come while the event is open');
    st.closeThumbnail('e1');
    st.ack('e1', 0);
    assert.equal(st.has('e1'), false);
    assert.equal(st.bytes, 0);
});

test('store: the memory bound evicts the oldest segments first and reports them', () => {
    const clock = new FakeClock();
    const st = new SegmentStore({ ttlMs: 600_000, maxBytes: 1000, maxEvents: 10 }, clock);
    const evicted: [string, number[]][] = [];
    st.onEvict = (id, ns) => evicted.push([id, ns]);
    st.open('a', 'c1');
    st.open('b', 'c2');
    st.setInit('a', Buffer.alloc(0));
    st.setInit('b', Buffer.alloc(0));
    for (let i = 0; i < 4; i++) { st.addSegment('a', buf(200), i * 2, 2); clock.advance(10); }
    for (let i = 0; i < 3; i++) { st.addSegment('b', buf(200), i * 2, 2); clock.advance(10); }
    assert.ok(st.bytes <= 1000);
    assert.deepEqual(evicted, [['a', [0]], ['a', [1]]]);
    assert.equal(st.getSegment('a', 0).status, 404);
    assert.equal(st.getSegment('b', 2).status, 200);
    assert.equal(st.info('a')!.evicted, 2);
});

test('store: thumbnails: confirmation replaces start, not the reverse; 425 while one may come', () => {
    const st = new SegmentStore(undefined, new FakeClock());
    st.open('e', 'c');
    assert.equal(st.getThumbnail('e').status, 425);
    st.setThumbnail('e', buf(5, 1), 'start');
    st.setThumbnail('e', buf(6, 2), 'confirmation');
    assert.equal(st.setThumbnail('e', buf(7, 3), 'start'), false);
    const t = st.getThumbnail('e');
    assert.equal(t.status, 200);
    assert.equal(t.thumb!.kind, 'confirmation');
    assert.equal(st.bytes, 6);
    st.open('f', 'c');
    st.closeThumbnail('f');
    assert.equal(st.getThumbnail('f').status, 404);
});

test('store: the number of events is bounded, finished ones go first', () => {
    const st = new SegmentStore({ ttlMs: 1e9, maxBytes: 1e9, maxEvents: 3 }, new FakeClock());
    st.open('a', 'c'); st.open('b', 'c'); st.finish('b'); st.open('c', 'c'); st.open('d', 'c');
    assert.equal(st.count, 3);
    assert.equal(st.has('b'), false);
    assert.equal(st.has('a'), true);
});

test('store: the event-count bound announces segments it drops; an open event survives the TTL sweep', () => {
    const clock = new FakeClock();
    const st = new SegmentStore({ ttlMs: 1000, maxBytes: 1e9, maxEvents: 2 }, clock);
    const ev: [string, number[]][] = [];
    st.onEvict = (id, ns) => ev.push([id, ns]);
    st.open('a', 'c'); st.setInit('a', buf(1)); st.addSegment('a', buf(5), 0, 2); st.addSegment('a', buf(5), 2, 2);
    st.open('b', 'c'); st.finish('b'); st.closeThumbnail('b');
    st.open('c', 'c');
    assert.equal(st.has('b'), false, 'the finished one without segments goes first');
    st.open('d', 'c');
    assert.deepEqual(ev, [['a', [0, 1]]]);
    // TTL: an open event (thumbnail pending) keeps its entry
    st.finish('c');
    clock.advance(5000);
    st.sweep();
    assert.equal(st.has('c'), true);
    st.closeThumbnail('c');
    st.sweep();
    assert.equal(st.has('c'), false);
});
