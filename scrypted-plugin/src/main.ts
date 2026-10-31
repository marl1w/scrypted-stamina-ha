// Scrypted shell: the provider (settings, mixin provider, HTTP handler) and the camera mixin. All logic is in engine.ts and
// camera.ts; this file only adapts the SDK to their Host/CameraEnv interfaces.

import sdk, { FFmpegInput, HttpRequest, HttpRequestHandler, HttpResponse, MixinProvider, ScryptedDeviceBase, ScryptedDeviceType, ScryptedInterface, ScryptedMimeTypes, Setting, Settings, SettingValue, WritableDeviceState } from '@scrypted/sdk';
import { SettingsMixinDeviceBase, SettingsMixinDeviceOptions } from '@scrypted/sdk/settings-mixin';
import child_process from 'child_process';
import { apiPath, ClipInfo, clipInfo } from './api';
import { CameraCore, CameraEnv, DeviceState, Iface, isMirror } from './camera';
import { NUMBER_SETTINGS, PLUGIN_ID, readNumber, NumberKey } from './config';
import { Engine, Host, StreamOptionsInfo } from './engine';
import { applySourceSetting, sourceSettings } from './settings';
import { buildInputArgs, StreamInput } from './segmenter';
import { cleanLabel, errText, realClock, tokenFingerprint, withTimeout } from './util';

const { systemManager, mediaManager, endpointManager } = sdk;

const STREAM_TIMEOUT_MS = 10_000;

function deviceState(id: string): DeviceState | undefined {
    const d: any = systemManager.getDeviceById(id);
    if (!d || !Array.isArray(d.interfaces)) return undefined;
    return {
        name: d.name, pluginId: d.pluginId, nativeId: d.nativeId, providerId: d.providerId, interfaces: d.interfaces, mixins: d.mixins,
        motionDetected: d.motionDetected, occupied: d.occupied, audioDetected: d.audioDetected, binaryState: d.binaryState, online: d.online,
    };
}

async function toInput(mo: any): Promise<StreamInput> {
    const input = await withTimeout(mediaManager.convertMediaObjectToJSON<FFmpegInput>(mo, ScryptedMimeTypes.FFmpegInput), STREAM_TIMEOUT_MS, 'stream conversion');
    if (!input?.url && !(input?.inputArguments || []).includes('-i')) throw new Error('the stream has no url');
    const mso: any = input.mediaStreamOptions || {};
    return {
        args: buildInputArgs(input.inputArguments, input.url),
        ffmpegPath: input.ffmpegPath,
        env: input.env,
        prebufferMs: typeof mso.prebuffer === 'number' ? mso.prebuffer : undefined,
        audioCodec: mso.audio?.codec,
        name: cleanLabel(mso.name || mso.id),
    };
}

export class StaminaMixin extends SettingsMixinDeviceBase<any> {
    /** undefined for a mirror camera: it is never observed (its source camera is), so no duplicate events */
    readonly core?: CameraCore;

    constructor(options: SettingsMixinDeviceOptions<any>, private provider: StaminaProvider) {
        super(options);
        const id = this.id;
        const env: CameraEnv = {
            id,
            name: () => this.name || id,
            clock: realClock,
            roles: provider.engine.roles,
            console: this.console,
            pluginOf: x => { try { return (systemManager.getDeviceById(x) as any)?.pluginId; } catch { return undefined; } },
            state: deviceState,
            children: () => {
                const st = systemManager.getSystemState();
                return Object.keys(st).filter(k => k !== id && (st[k] as any)?.providerId?.value === id);
            },
            listen: (dev, iface: Iface, cb, layer) => {
                const reg = systemManager.listenDevice(dev, layer ? { event: iface, mixinId: layer } : { event: iface }, (_s, details, data) => cb(details || {}, data));
                return () => reg.removeListener();
            },
            storageGet: k => this.storage.getItem(k),
            storageSet: (k, v) => this.storage.setItem(k, v),
            episode: () => provider.engine.episodeConfig(),
            selfPlugin: PLUGIN_ID,
        };
        // a mirror view repeats a crop for another Home: observing it would duplicate that crop's events
        if (isMirror(deviceState(id))) {
            this.console.log(`${this.name || id}: a mirror view, not observed (its source camera is)`);
            return;
        }
        // a rebuilt mixin chain re-creates the mixin: the camera's core (tracker, open event) carries over
        this.core = provider.engine.attach(env);
    }

    async getMixinSettings(): Promise<Setting[]> {
        if (!this.core)
            return [{ key: 'stamina:status', title: 'Stamina', readonly: true, value: 'Mirror view: not observed. Its source camera is, so its events are not duplicated.' }];
        const info = this.provider.engine.camerasInfo().find(c => c.id === this.id);
        const caps = info?.capabilities;
        const yes = (v: unknown) => v === null || v === undefined ? 'unknown' : v ? 'yes' : 'no';
        return [
            { key: 'stamina:status', title: 'Capabilities', readonly: true, type: 'textarea', value: caps ? [
                `Verifier: ${caps.verifiers.length ? caps.verifiers.join(', ') : 'none (subjects are unverified)'}`,
                ...(caps.silent_verifiers.length ? [`Silent verifier mixins (not counted): ${caps.silent_verifiers.join(', ')}`] : []),
                `Prebuffer: ${yes(caps.prebuffer)}${caps.prebuffer_ms ? ` (${Math.round(caps.prebuffer_ms / 1000)} s)` : ''}`,
                `Recordings (VideoRecorder): ${yes(caps.video_recorder)}; clips (VideoClips): ${yes(caps.video_clips)}`,
                `Audio: ${yes(caps.audio)}`,
            ].join('\n') : 'starting' },
            { key: 'stamina:sources', title: 'Sources seen', readonly: true, type: 'textarea', description: 'Roles are set in the Scrypted Stamina plugin settings, for all cameras.', value: (info?.sources ?? []).map(s => `${s.source}: ${s.role ?? '?'}`).join('\n') || 'none yet' },
            { key: 'stamina:event', title: 'Open event', readonly: true, value: info?.open_event ?? 'none' },
        ];
    }

    async putMixinSetting(_key: string, _value: SettingValue): Promise<boolean> {
        return false;
    }

    /** set when a newer mixin of the same camera took over: releasing this one must not detach the shared core */
    replaced = false;

    async release() {
        if (this.provider.live.get(this.id) === this) this.provider.live.delete(this.id);
        if (!this.replaced && this.core) this.provider.engine.detach(this.id);
    }
}

export class StaminaProvider extends ScryptedDeviceBase implements MixinProvider, Settings, HttpRequestHandler {
    readonly engine: Engine;
    readonly live = new Map<string, StaminaMixin>();
    private ffmpegPromise?: Promise<string>;
    /** class map rows dropped at the last save, per source (shown in its description) */
    private classMapInvalid = new Map<string, string[]>();
    private endpoint?: string;

    constructor(nativeId?: string) {
        super(nativeId);
        const host: Host = {
            clock: realClock,
            console: this.console,
            storageGet: k => this.storage.getItem(k),
            storageSet: (k, v) => this.storage.setItem(k, v),
            streamInput: async (id, prerollMs, lowRes) => {
                const d: any = systemManager.getDeviceById(id);
                const mo = await withTimeout(d.getVideoStream({ ...(lowRes ? { destination: 'low-resolution' } : {}), ...(prerollMs > 0 ? { prebuffer: prerollMs } : {}) }), STREAM_TIMEOUT_MS, 'getVideoStream');
                return toInput(mo);
            },
            streamOptions: async (id): Promise<StreamOptionsInfo> => {
                const d: any = systemManager.getDeviceById(id);
                const opts: any[] = (await d.getVideoStreamOptions?.()) || [];
                return {
                    prebufferMs: Math.max(0, ...opts.map(o => Number(o?.prebuffer) || 0)),
                    audio: opts.some(o => o?.audio && o.audio !== null),
                };
            },
            recordingInput: async (id, startTime, durationMs) => {
                const d: any = systemManager.getDeviceById(id);
                const mo = await d.getRecordingStream({ startTime, ...(durationMs ? { duration: durationMs } : {}) });
                return toInput(mo);
            },
            clips: async (id, start, end): Promise<ClipInfo[]> => {
                const d: any = systemManager.getDeviceById(id);
                const clips: any[] = (await d.getVideoClips({ startTime: start, endTime: end })) || [];
                return clips.slice(0, 500).map(clipInfo);
            },
            picture: async (id, width) => {
                const d: any = systemManager.getDeviceById(id);
                if (!d?.interfaces?.includes(ScryptedInterface.Camera)) return undefined;
                const mo = await d.takePicture({ picture: { width }, reason: 'event' });
                return mediaManager.convertMediaObjectToBuffer(mo, 'image/jpeg');
            },
            interfaces: id => (systemManager.getDeviceById(id) as any)?.interfaces ?? [],
            ffmpegPath: () => this.ffmpeg(),
            spawn: (c, a, o) => child_process.spawn(c, a, o),
        };
        this.engine = new Engine(host);
        if (this.storage.getItem('tokenShown') === 'true')
            this.console.log('API token generated; it is shown in the plugin settings until you hide it.');
    }

    private ffmpeg(): Promise<string> {
        if (!this.ffmpegPromise) {
            const p = withTimeout(mediaManager.getFFmpegPath(), 10_000, 'ffmpeg path');
            p.catch(() => { if (this.ffmpegPromise === p) this.ffmpegPromise = undefined; });
            this.ffmpegPromise = p;
        }
        return this.ffmpegPromise;
    }

    /** A plugin's display name (its root device's name), when Scrypted knows it. */
    private pluginName(source: string): string | undefined {
        try { return cleanLabel((systemManager.getDeviceById(source) as any)?.name, 64); }
        catch { return undefined; }
    }

    private async integrationUrl(): Promise<string> {
        if (!this.endpoint) {
            try { this.endpoint = await withTimeout(endpointManager.getInsecurePublicLocalEndpoint(), 5000, 'endpoint'); }
            catch (e) { return `(unavailable: ${errText(e)})`; }
        }
        return this.endpoint!;
    }

    async getSettings(): Promise<Setting[]> {
        const e = this.engine;
        const token = e.token();
        const shown = this.storage.getItem('tokenShown') === 'true';
        const settings: Setting[] = [
            { group: 'Integration', key: 'integrationUrl', title: 'Integration URL', readonly: true, value: await this.integrationUrl(), description: 'Enter this URL in the Scrypted Stamina Home Assistant integration (or the https one on port 10443).' },
            { group: 'Integration', key: 'token', title: 'API token', readonly: true, value: shown ? token : `hidden (fingerprint ${tokenFingerprint(token)})`, description: shown ? 'Copy it into the Home Assistant integration, then press Hide token. It is not shown again.' : 'Hidden. The fingerprint identifies it without revealing any of it. Regenerate to get a new one (the old one stops working at once).' },
            ...(shown ? [{ group: 'Integration', key: 'hideToken', title: 'Hide token', type: 'button' as const }] : []),
            { group: 'Integration', key: 'regenerateToken', title: 'Regenerate token', type: 'button', description: 'Disconnects the integration until it has the new token.' },
            { group: 'Video', key: 'segments', title: 'Video segments', type: 'boolean', value: e.segmentsEnabled(), description: 'Cut fMP4 segments from the prebuffer at every event start. Off: events only (shadow mode).' },
            ...NUMBER_SETTINGS.map((s): Setting => ({ group: s.group, key: s.key, title: s.title, type: 'integer', range: [s.min, s.max], description: `${s.description} ${s.min} to ${s.max}, default ${s.def}.`, value: readNumber(s.key as NumberKey, this.storage.getItem(s.key)) })),
        ];
        settings.push(...sourceSettings(e.roles.list(), src => this.pluginName(src), this.classMapInvalid));
        return settings;
    }

    async putSetting(key: string, value: SettingValue): Promise<void> {
        const e = this.engine;
        if (key === 'hideToken') this.storage.setItem('tokenShown', 'false');
        else if (key === 'regenerateToken') { e.regenerateToken(); this.console.log('API token regenerated.'); }
        else if (key === 'segments') this.storage.setItem('segments', String(value === true || value === 'true'));
        else if (NUMBER_SETTINGS.some(s => s.key === key)) this.storage.setItem(key, String(readNumber(key as NumberKey, String(value))));
        else if (applySourceSetting(e.roles, key, value, this.classMapInvalid)) { /* roles and class maps */ }
        else return;
        e.applySettings();
        this.onDeviceEvent(ScryptedInterface.Settings, undefined);
    }

    async canMixin(type: ScryptedDeviceType, interfaces: string[]): Promise<string[] | null | undefined | void> {
        if (type !== ScryptedDeviceType.Camera && type !== ScryptedDeviceType.Doorbell) return;
        if (!interfaces.includes(ScryptedInterface.VideoCamera)) return;
        return [ScryptedInterface.Settings];
    }

    async getMixin(mixinDevice: any, mixinDeviceInterfaces: ScryptedInterface[], mixinDeviceState: WritableDeviceState): Promise<any> {
        const id = String((mixinDeviceState as any)?.id ?? mixinDevice?.id ?? '');
        const old = id ? this.live.get(id) : undefined;
        if (old) old.replaced = true;
        const m = new StaminaMixin({ group: 'Stamina', groupKey: 'stamina', mixinDevice, mixinDeviceInterfaces, mixinDeviceState, mixinProviderNativeId: this.nativeId }, this);
        this.live.set(String(m.id || id), m);
        return m;
    }

    async releaseMixin(_id: string, mixinDevice: StaminaMixin): Promise<void> {
        await mixinDevice?.release?.();
    }

    async onRequest(request: HttpRequest, response: HttpResponse): Promise<void> {
        const p = apiPath(request.url, request.rootPath, PLUGIN_ID);
        if (!p) {
            response.send(JSON.stringify({ error: 'not_found' }), { code: 404, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' } });
            return;
        }
        const headers: Record<string, string> = {};
        for (const [k, v] of Object.entries(request.headers || {})) headers[k.toLowerCase()] = Array.isArray(v) ? String(v[0]) : String(v);
        const res = await this.engine.handle({ method: request.method || 'GET', path: p.path, query: p.query, headers, body: typeof request.body === 'string' ? request.body.slice(0, 4096) : undefined });
        if (res.stream) response.sendStream(res.stream, { code: res.status, headers: res.headers });
        else response.send(res.body as any ?? '', { code: res.status, headers: res.headers });
    }
}

export default StaminaProvider;
