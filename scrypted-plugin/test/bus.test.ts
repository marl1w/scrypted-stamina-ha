import assert from 'node:assert/strict';
import { test } from 'node:test';
import { Bus, parseResume, sseStream } from '../src/bus';

async function take(gen: AsyncGenerator<Buffer, void>, n: number): Promise<string> {
    let s = '';
    for (let i = 0; i < n; i++) {
        const r = await gen.next();
        if (r.done) break;
        s += r.value.toString();
    }
    return s;
}

function frames(text: string) {
    return text.split('\n\n').filter(Boolean).map(f => {
        const ev = /^event: (.+)$/m.exec(f)?.[1];
        const data = /^data: (.+)$/m.exec(f)?.[1];
        return { ev, data: data ? JSON.parse(data) : undefined, id: /^id: (.+)$/m.exec(f)?.[1] };
    });
}

test('seq increases monotonically; the ring keeps the last N', () => {
    const b = new Bus('ep_test', 5);
    for (let i = 0; i < 8; i++) b.publish('event.update', { i });
    assert.equal(b.lastSeq, 8);
    assert.equal(b.oldestSeq, 4);
    const r = b.replay(5, 'ep_test');
    assert.deepEqual(r.messages.map(m => m.seq), [6, 7, 8]);
    assert.equal(r.gap, false);
});

test('resume: gap when since is older than the ring, reset on another epoch or an impossible seq', () => {
    const b = new Bus('ep_test', 5);
    for (let i = 0; i < 8; i++) b.publish('event.update', {});
    assert.equal(b.replay(3, 'ep_test').gap, false, 'since=3: 4.. are all there');
    const g = b.replay(1, 'ep_test');
    assert.equal(g.gap, true);
    assert.deepEqual(g.messages.map(m => m.seq), [4, 5, 6, 7, 8]);
    const r = b.replay(8, 'ep_other');
    assert.equal(r.reset, true);
    assert.equal(r.messages.length, 5);
    assert.equal(b.replay(99, 'ep_test').reset, true);
    assert.equal(b.replay(undefined, undefined).messages.length, 0, 'no since: live only');
});

test('resume parameters: Last-Event-ID wins, invalid values are rejected', () => {
    assert.deepEqual(parseResume('ep_abc:12', '3', 'ep_x'), { epoch: 'ep_abc', since: 12 });
    assert.deepEqual(parseResume(undefined, '3', 'ep_x'), { since: 3, epoch: 'ep_x' });
    assert.equal(parseResume('garbage', null, null).invalid, true);
    assert.equal(parseResume(undefined, '-1', null).invalid, true);
    assert.equal(parseResume(undefined, '3', '../x').invalid, true);
    assert.deepEqual(parseResume(undefined, null, null), {});
});

test('SSE: hello, replay after since, then live messages; ids carry epoch:seq', async () => {
    const b = new Bus('ep_test');
    b.publish('event.start', { a: 1 });
    b.publish('event.update', { a: 2 });
    const s = sseStream(b, { since: 1, epoch: 'ep_test' }, { keepaliveMs: 1000, maxLifetimeMs: 60_000, maxQueue: 100 });
    const first = frames(await take(s.stream, 1));
    assert.equal(first[0].ev, 'hello');
    assert.equal(first[0].data.resumed, true);
    assert.equal(first[0].data.last_seq, 2);
    assert.equal(first[1].id, 'ep_test:2');
    assert.equal(first[1].data.a, 2);
    b.publish('event.end', { a: 3 });
    const live = frames(await take(s.stream, 1));
    assert.equal(live[0].ev, 'event.end');
    assert.equal(live[0].data.seq, 3);
    s.close();
    assert.equal((await s.stream.next()).done, true);
    assert.equal(b.subscribers, 0);
});

test('SSE: keepalive comment when idle', async () => {
    const b = new Bus('ep_test');
    const s = sseStream(b, {}, { keepaliveMs: 20, maxLifetimeMs: 60_000, maxQueue: 100 });
    await take(s.stream, 1);
    assert.equal(await take(s.stream, 1), ': keepalive\n\n');
    s.close();
});

test('SSE: a client that falls too far behind is cut off', async () => {
    const b = new Bus('ep_test');
    const s = sseStream(b, {}, { keepaliveMs: 1000, maxLifetimeMs: 60_000, maxQueue: 3 });
    for (let i = 0; i < 5; i++) b.publish('event.update', {});
    assert.equal(s.closed, true);
    assert.equal(b.subscribers, 0);
});

test('SSE: the stream ends after its lifetime', async () => {
    const b = new Bus('ep_test');
    const s = sseStream(b, {}, { keepaliveMs: 1000, maxLifetimeMs: 30, maxQueue: 10 });
    await take(s.stream, 1);
    assert.equal((await s.stream.next()).done, true);
});

test('messages are serialised at publish: later changes to the objects do not change what was sent', () => {
    const b = new Bus('ep_test');
    const ev = { state: 'open', subjects: [{ verdict: 'pending' }] };
    b.publish('event.start', { event: ev });
    ev.state = 'closed';
    ev.subjects[0].verdict = 'confirmed';
    const m = JSON.parse(b.replay(0, 'ep_test').messages[0].json);
    assert.equal(m.event.state, 'open');
    assert.equal(m.event.subjects[0].verdict, 'pending');
});

test('the ring is bounded in bytes as well as in count', () => {
    const b = new Bus('ep_test', 1000, Date.now, 10_000);
    for (let i = 0; i < 50; i++) b.publish('event.update', { pad: 'x'.repeat(500) });
    assert.ok(b.bytes <= 10_000);
    assert.ok(b.oldestSeq > 1);
    assert.equal(b.lastSeq, 50);
});

test('the ring keeps frames only, counts UTF-8 bytes, and holds up to 5000 messages', () => {
    const b = new Bus('ep_test');
    const e = b.publish('event.update', { name: 'Café è qui' });
    assert.equal(JSON.parse(e.json).name, 'Café è qui');
    assert.equal(e.frame.endsWith(`${e.json}\n\n`), true);
    assert.equal(e.bytes, Buffer.byteLength(e.frame));
    for (let i = 0; i < 6000; i++) b.publish('event.update', {});
    assert.equal(b.lastSeq - b.oldestSeq + 1, 5000);
});
