import assert from 'node:assert/strict';
import { test } from 'node:test';
import { clipInfo } from '../src/api';
import { maskSecrets, maskUrls, safeField, tokenFingerprint } from '../src/util';

test('maskSecrets: userinfo up to the last @ (passwords with / or @), queries', () => {
    assert.equal(maskSecrets('rtsp://user:pa/ss@192.0.2.1:554/x'), 'rtsp://***@192.0.2.1:554/x');
    assert.equal(maskSecrets('rtsp://user:p@ss@192.0.2.1/x?token=abc'), 'rtsp://***@192.0.2.1/x?***');
    assert.equal(maskUrls('open rtsp://u:a/b@192.0.2.1:554/path/z?t=1 failed'), 'open rtsp://192.0.2.1:554 failed');
    assert.ok(!maskUrls('rtsp://admin:hunter2@192.0.2.5/x').includes('hunter2'));
});

test('safeField drops values carrying a URL; fingerprints reveal nothing of the token', () => {
    assert.equal(safeField('rec-123'), 'rec-123');
    assert.equal(safeField('http://192.0.2.1/clip?token=x'), undefined);
    assert.equal(safeField({}), undefined);
    const t = 'abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG';
    const fp = tokenFingerprint(t);
    assert.match(fp, /^[0-9a-f]{8}$/);
    assert.ok(!t.includes(fp));
});

test('clips from another plugin: fields carrying URLs are dropped', () => {
    const c = clipInfo({ id: 'c1', startTime: 5, duration: 3, videoId: 'rtsp://u:p@192.0.2.1/v', thumbnailId: 't1', description: 'see https://192.0.2.1/x?token=1', detectionClasses: ['person', 'http://x'] });
    assert.deepEqual(c, { id: 'c1', start: 5, duration: 3, event: null, description: null, classes: ['person'], thumbnail_id: 't1', video_id: null });
});
