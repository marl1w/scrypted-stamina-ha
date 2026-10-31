// Fakes for ffmpeg (a child process that streams synthetic fMP4) and the Scrypted host.
/* eslint-disable @typescript-eslint/no-explicit-any */
import { EventEmitter } from 'events';
import { PassThrough } from 'stream';
import type { ChildProcess } from 'child_process';
import { CameraEnv, DeviceState, Details, Iface } from '../src/camera';
import { Host } from '../src/engine';
import { RolesTable } from '../src/roles';
import { FakeClock, fragment, initSegment } from './helpers';

export class FakeProc extends EventEmitter {
    stdout = new PassThrough();
    stderr = new PassThrough();
    stdin = new PassThrough();
    exitCode: number | null = null;
    killed: string[] = [];
    constructor(public cmd: string, public args: string[]) { super(); }
    kill(sig: string) {
        this.killed.push(sig);
        if (this.exitCode === null) setImmediate(() => this.exit(sig === 'SIGINT' ? 255 : 137));
        return true;
    }
    exit(code: number) {
        if (this.exitCode !== null) return;
        this.exitCode = code;
        this.stdout.end();
        setImmediate(() => this.emit('close', code));
    }
    /** writes an init and `n` 1 s fragments (a prebuffer burst) */
    burst(n: number, from = 0) {
        if (from === 0) this.stdout.write(initSegment());
        for (let i = 0; i < n; i++) this.stdout.write(fragment(from + i + 1, from + i, 1));
    }
}

export function fakeSpawn() {
    const procs: FakeProc[] = [];
    const spawn = (cmd: string, args: string[]) => { const p = new FakeProc(cmd, args); procs.push(p); return p as unknown as ChildProcess; };
    return { procs, spawn };
}

export interface FakeDevice extends DeviceState { id: string; providerId?: string }

/** An in-memory Scrypted: devices, listeners (with optional layer), storage. */
export class FakeScrypted {
    devices = new Map<string, FakeDevice>();
    listeners: { dev: string; iface: Iface; layer?: string; cb: (d: Details, data: any) => void }[] = [];
    storage = new Map<string, string>();
    pluginIds = new Map<string, string>();

    add(d: FakeDevice) { this.devices.set(d.id, d); return d; }

    emit(dev: string, iface: Iface, data: any, details: Details = {}, layer?: string) {
        for (const l of this.listeners)
            if (l.dev === dev && l.iface === iface && l.layer === layer) l.cb({ eventTime: Date.now(), ...details }, data);
    }

    env(id: string, clock: FakeClock, roles: RolesTable, ep = { marginMs: 10_000, capMs: 600_000, verifyWindowMs: 5000 }): CameraEnv {
        return {
            id,
            name: () => this.devices.get(id)?.name ?? id,
            clock, roles,
            console: { log: () => { }, warn: () => { } },
            pluginOf: x => this.pluginIds.get(x) ?? this.devices.get(x)?.pluginId,
            state: x => this.devices.get(x),
            children: () => [...this.devices.values()].filter(d => d.providerId === id && d.id !== id).map(d => d.id),
            listen: (dev, iface, cb, layer) => {
                const l = { dev, iface, layer, cb };
                this.listeners.push(l);
                return () => { this.listeners = this.listeners.filter(x => x !== l); };
            },
            storageGet: k => this.storage.get(`${id}:${k}`) ?? null,
            storageSet: (k, v) => { this.storage.set(`${id}:${k}`, v); },
            episode: () => ep,
        };
    }
}

export function fakeHost(clock: FakeClock, spawn: ReturnType<typeof fakeSpawn>['spawn'], over: Partial<Host> = {}): Host & { store: Map<string, string>; warns: string[] } {
    const store = new Map<string, string>();
    const warns: string[] = [];
    return {
        store, warns,
        clock,
        console: { log: () => { }, warn: (...a: any[]) => { warns.push(a.join(' ')); } },
        storageGet: k => store.get(k) ?? null,
        storageSet: (k, v) => { store.set(k, v); },
        streamInput: async () => ({ args: ['-i', 'rtsp://user:secret@192.0.2.10:554/stream?token=abc'], prebufferMs: 10000, audioCodec: 'aac' }),
        streamOptions: async () => ({ prebufferMs: 10000, audio: true }),
        recordingInput: async () => ({ args: ['-i', 'rtsp://192.0.2.10/rec'] }),
        clips: async () => [],
        picture: async () => undefined,
        interfaces: () => ['VideoCamera', 'MotionSensor', 'ObjectDetector'],
        ffmpegPath: async () => 'ffmpeg',
        spawn: spawn as any,
        ...over,
    };
}
