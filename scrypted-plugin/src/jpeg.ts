// JPEG helpers: the picture's size from its SOF marker, and an ffmpeg downscale for thumbnails.

import type { ChildProcess, SpawnOptions } from 'child_process';

/** Width and height from the first SOFn marker, or undefined. */
export function jpegSize(b: Buffer): { w: number; h: number } | undefined {
    if (b.length < 4 || b[0] !== 0xff || b[1] !== 0xd8) return undefined;
    let i = 2;
    while (i + 9 < b.length) {
        if (b[i] !== 0xff) { i++; continue; }
        const m = b[i + 1];
        if (m === 0xd8 || m === 0x01 || (m >= 0xd0 && m <= 0xd7)) { i += 2; continue; }
        const len = b.readUInt16BE(i + 2);
        if ((m >= 0xc0 && m <= 0xc3) || (m >= 0xc5 && m <= 0xc7) || (m >= 0xc9 && m <= 0xcb) || (m >= 0xcd && m <= 0xcf))
            return { h: b.readUInt16BE(i + 5), w: b.readUInt16BE(i + 7) };
        if (m === 0xda) return undefined;
        i += 2 + len;
    }
    return undefined;
}

export const THUMB_WIDTH = 320;
export const MAX_THUMB_BYTES = 256 * 1024;

/** Scales a JPEG to `width` px wide with ffmpeg (stdin -> stdout). */
export function scaleJpeg(spawn: (cmd: string, args: string[], o: SpawnOptions) => ChildProcess, ffmpeg: string, jpeg: Buffer, width = THUMB_WIDTH, timeoutMs = 10_000): Promise<Buffer> {
    return new Promise((resolve, reject) => {
        const cp = spawn(ffmpeg, ['-hide_banner', '-loglevel', 'error', '-f', 'image2pipe', '-i', 'pipe:0', '-vf', `scale=${width}:-2`, '-q:v', '5', '-frames:v', '1', '-f', 'image2pipe', '-c:v', 'mjpeg', 'pipe:1'], { stdio: ['pipe', 'pipe', 'pipe'] });
        const out: Buffer[] = [];
        let size = 0;
        const t = setTimeout(() => { try { cp.kill('SIGKILL'); } catch { /* gone */ } reject(new Error('thumbnail scale timed out')); }, timeoutMs);
        cp.stdout?.on('data', (d: Buffer) => { size += d.length; if (size <= MAX_THUMB_BYTES * 4) out.push(d); });
        cp.on('error', e => { clearTimeout(t); reject(e); });
        cp.on('close', code => {
            clearTimeout(t);
            const b = Buffer.concat(out);
            if (code === 0 && b.length) resolve(b);
            else reject(new Error(`thumbnail scale failed (ffmpeg ${code})`));
        });
        cp.stdin?.on('error', () => { /* ffmpeg gone */ });
        cp.stdin?.end(jpeg);
    });
}
