// Scrypted shell: the provider (settings, mixin provider, HTTP handler) and the camera mixin. All logic is in engine.ts and
// camera.ts; this file only adapts the SDK to their Host/CameraEnv interfaces.

import sdk, { FFmpegInput, HttpRequest, HttpRequestHandler, HttpResponse, MixinProvider, ScryptedDeviceBase, ScryptedDeviceType, ScryptedInterface, ScryptedMimeTypes, Setting, Settings, SettingValue, WritableDeviceState } from '@scrypted/sdk';
import { SettingsMixinDeviceBase, SettingsMixinDeviceOptions } from '@scrypted/sdk/settings-mixin';
import child_process from 'child_process';
import { apiPath, ClipInfo, clipInfo } from './api';
import { CameraCore, CameraEnv, DeviceState, Iface } from './camera';
import { NUMBER_SETTINGS, PLUGIN_ID, readNumber, NumberKey } from './config';
import { Engine, Host, StreamOptionsInfo } from './engine';
import { applySourceSetting, CameraStatus, cameraStatusLines, sourceSettings } from './settings';
import { Discovered } from './roles';
import { buildInputArgs, StreamInput } from './segmenter';
import { cleanLabel, errText, realClock, tokenFingerprint, withTimeout } from './util';

const { systemManager, mediaManager, endpointManager } = sdk;

const STREAM_TIMEOUT_MS = 10_000;

function deviceState(id: string): DeviceState | undefined {
    const d: any = systemManager.getDeviceById(id);
    if (!d || !Array.isArray(d.interfaces)) return undefined;
    return {
        name: d.name, pluginId: d.pluginId, nativeId: d.nativeId, providerId: d.providerId, interfaces: d.interfaces,
        providedInterfaces: d.providedInterfaces, mixins: d.mixins,
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
    /** the camera's tracker and open event (undefined only if attaching failed) */
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
            listenAll: (iface, cb) => provider.listenAll(iface, cb),
        };
        // a mirror view (a crop repeated for another Home) is observed too, so it is listed in that Home's area;
        // its events repeat the crop's
        // a rebuilt mixin chain re-creates the mixin: the camera's core (tracker, open event) carries over
        this.core = provider.engine.attach(env);
    }

    async getMixinSettings(): Promise<Setting[]> {
        if (!this.core)
            return [{ key: 'stamina:status', title: 'Stamina', readonly: true, value: 'Starting…' }];
        const e = this.provider.engine;
        const info = e.camerasInfo().find(c => c.id === this.id);
        const caps = info?.capabilities;
        if (!info || !caps) return [{ key: 'stamina:status', title: 'Stamina', readonly: true, value: 'Starting…' }];
        // the sources on this camera: its own plugin, its mixins, and any seen here
        const st = deviceState(this.id);
        const here = new Set<string>([...(st?.pluginId ? [st.pluginId] : []), ...info.sources.map(x => x.source)]);
        for (const m of st?.mixins ?? []) { const p = (systemManager.getDeviceById(m) as any)?.pluginId; if (p && p !== PLUGIN_ID) here.add(p); }
        const status: CameraStatus = {
            triggers: [...here].filter(x => e.roles.get(x)?.role === 'trigger').sort(),
            // "not responding" only without activity or a liveness beat on this camera within 3 min, also for a verifier mixin
            // that has not detected anything here yet (it does not count for verdicts until it does)
            verifiers: [...caps.verifiers, ...caps.silent_verifiers].map(v => ({ source: v, live: this.core!.verifierLiveOf(v) })),
            silent: [],
            prebufferMs: caps.prebuffer_ms,
            recordings: caps.video_recorder,
            clips: caps.video_clips,
            split: caps.split,
            parent: caps.parent,
            detections: caps.detections,
        };
        return [
            { key: 'stamina:status', title: 'Stamina', readonly: true, type: 'textarea', value: cameraStatusLines(status, x => this.provider.pluginName(x)).join('\n'), description: 'What each plugin does is set in the Scrypted Stamina plugin, for all cameras.' },
            { key: 'stamina:event', title: 'Event now', readonly: true, value: info.open_event ? 'yes' : 'no' },
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
            discover: ids => this.discover(ids),
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
    pluginName(source: string): string | undefined {
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
        const { sources, classes, advanced } = sourceSettings(e.roles.list(), src => this.pluginName(src), { declared: e.declared });
        return [
            { group: 'Integration', key: 'integrationUrl', title: 'URL', readonly: true, value: await this.integrationUrl(), description: 'Copy this and the token into Home Assistant.' },
            shown
                ? { group: 'Integration', key: 'token', title: 'Token', readonly: true, value: token }
                : { group: 'Integration', key: 'token', title: 'Token', readonly: true, value: 'Hidden. Regenerate to get a new one.' },
            shown
                ? { group: 'Integration', key: 'hideToken', title: 'Hide token', type: 'button' }
                : { group: 'Integration', key: 'regenerateToken', title: 'Regenerate token', type: 'button', description: 'Home Assistant disconnects until it has the new token.' },
            ...sources,
            ...classes,
            { group: 'Advanced', key: 'segments', title: 'Save video clips', type: 'boolean', value: e.segmentsEnabled(), description: 'Record each event from a few seconds before it starts. Off: events only.' },
            ...NUMBER_SETTINGS.map((s): Setting => ({ group: 'Advanced', key: s.key, title: s.title, type: 'integer', range: [s.min, s.max], description: `${s.description} ${s.min} to ${s.max}, default ${s.def}.`, value: readNumber(s.key as NumberKey, this.storage.getItem(s.key)) })),
            ...advanced,
        ];
    }

    async putSetting(key: string, value: SettingValue): Promise<void> {
        const e = this.engine;
        if (key === 'hideToken') this.storage.setItem('tokenShown', 'false');
        else if (key === 'regenerateToken') { e.regenerateToken(); this.console.log('API token regenerated.'); }
        else if (key === 'segments') this.storage.setItem('segments', String(value === true || value === 'true'));
        else if (NUMBER_SETTINGS.some(s => s.key === key)) this.storage.setItem(key, String(readNumber(key as NumberKey, String(value))));
        else if (applySourceSetting(e.roles, key, value)) { /* roles and classes */ }
        else return;
        e.applySettings();
        this.onDeviceEvent(ScryptedInterface.Settings, undefined);
    }

    /** One system-wide listener for all cameras, dispatched by interface (a delivery path for detections). */
    private allListeners = new Map<string, Set<(dev: string, d: any, data: any) => void>>();
    private systemListener?: { removeListener(): void };

    listenAll(iface: string, cb: (dev: string, d: any, data: any) => void): () => void {
        let set = this.allListeners.get(iface);
        if (!set) { set = new Set(); this.allListeners.set(iface, set); }
        set.add(cb);
        if (!this.systemListener)
            this.systemListener = systemManager.listen((src, details, data) => {
                const subs = details?.eventInterface ? this.allListeners.get(details.eventInterface) : undefined;
                if (!subs?.size || !src?.id) return;
                for (const f of [...subs]) { try { f(src.id, details, data); } catch { /* the subscriber's own */ } }
            });
        return () => { set!.delete(cb); };
    }

    /**
     * Sources on these cameras: the camera's own plugin when the camera has a detection or sensor interface, and each mixin
     * (but Stamina) whose canMixin adds ObjectDetector, MotionSensor, OccupancySensor or AudioSensor; plus the classes the
     * cameras declare (getObjectTypes), attributed to the camera plugin.
     */
    private async discover(cameraIds: string[]): Promise<{ found: Discovered[]; declared: Record<string, string[]> }> {
        const RELEVANT = ['ObjectDetector', 'MotionSensor', 'OccupancySensor', 'AudioSensor'];
        const found = new Map<string, Discovered>();
        const declared: Record<string, string[]> = {};
        const asked = new Map<string, Promise<string[]>>();
        for (const id of cameraIds) {
            const cam: any = systemManager.getDeviceById(id);
            if (!cam?.interfaces) continue;
            const ifs = (cam.interfaces as string[]).filter(i => RELEVANT.includes(i));
            if (cam.pluginId && ifs.length) {
                const prev = found.get(cam.pluginId);
                found.set(cam.pluginId, { source: cam.pluginId, cameraPlugin: true, ifaces: [...new Set([...(prev?.ifaces ?? []), ...ifs])] });
                if (ifs.includes('ObjectDetector')) {
                    try {
                        const t: any = await withTimeout(cam.getObjectTypes(), 5000, 'object types');
                        const list = declared[cam.pluginId] ??= [];
                        for (const c of (t?.classes ?? []).slice(0, 48)) { const x = cleanLabel(c, 48); if (x && !list.includes(x)) list.push(x); }
                    }
                    catch { /* no object types */ }
                }
            }
            for (const m of cam.mixins ?? []) {
                const prov: any = systemManager.getDeviceById(m);
                const plugin = prov?.pluginId;
                if (!plugin || plugin === PLUGIN_ID || typeof prov.canMixin !== 'function') continue;
                const k = `${m}|${cam.type}`;
                if (!asked.has(k)) asked.set(k, withTimeout(Promise.resolve(prov.canMixin(cam.type, cam.interfaces)), 5000, 'canMixin').then(r => Array.isArray(r) ? r : []).catch(() => []));
                const adds = (await asked.get(k)!).filter((i: string) => RELEVANT.includes(i));
                if (!adds.length) continue;
                const prev = found.get(plugin);
                found.set(plugin, { source: plugin, cameraPlugin: prev?.cameraPlugin ?? false, ifaces: [...new Set([...(prev?.ifaces ?? []), ...adds])] });
            }
        }
        return { found: [...found.values()], declared };
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
