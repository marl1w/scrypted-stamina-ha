// Roles table: one row per source plugin, added the first time it emits, with an inferred default role and a class map.
// No per-camera override: a row applies to every camera.

import { cleanLabel } from './util';

export type Role = 'trigger' | 'verifier' | 'enricher' | 'ignore';
export const ROLES: Role[] = ['trigger', 'verifier', 'enricher', 'ignore'];

/** What kind of signal an observation came from. */
export type ObsKind = 'object' | 'motion' | 'occupancy' | 'audio';

export interface RoleRow {
    source: string;
    /** role of the source's detections (ObjectDetector) */
    role: Role;
    /** the detection role was inferred, not chosen */
    inferred: boolean;
    /** the detection role is decided (from a detection or by the user); false while only signals were seen */
    roleSet: boolean;
    /** role of the source's classless signals (motion, occupancy, audio edges): never `verifier` unless `gated` */
    signalRole: Role;
    /** the source gates the camera's signals on its own verification (Coral Guard): its classless signals may confirm */
    gated: boolean;
    /** raw class name -> normalised class */
    classMap: Record<string, string>;
    firstSeen: number;
    lastSeen: number;
}

/** Plugins that analyse frames and confirm what a trigger claimed. */
export const KNOWN_VERIFIERS = [
    'scrypted-coral-guard',
    '@scrypted/objectdetector',
    '@scrypted/openvino',
    '@scrypted/coreml',
    '@scrypted/onnx',
    '@scrypted/tensorflow-lite',
    '@scrypted/ncnn',
];

/** Plugins whose motion/occupancy only turn on after their own verification: their classless signals may confirm. */
export const KNOWN_GATED = ['scrypted-coral-guard'];

/** Classes that identify rather than detect: their source is an enricher by default. */
export const ENRICHER_CLASSES = ['face', 'plate', 'license_plate'];

/** Smart-event classes a camera may send as detections (role trigger, no box). */
export const SMART_EVENT_CLASSES = ['line_crossing', 'intrusion', 'loitering', 'forgotten_item', 'taken_item'];

/** Common aliases every new row starts with. */
export const COMMON_CLASS_MAP: Record<string, string> = {
    people: 'person',
    dog_cat: 'animal',
    verified_person: 'person',
    verified_vehicle: 'vehicle',
    verified_animal: 'animal',
};

/** Defaults per known source plugin (merged over the common aliases). */
export const SOURCE_CLASS_MAPS: Record<string, Record<string, string>> = {
    '@marl1w/scrypted-reolink': {
        people: 'person', vehicle: 'vehicle', dog_cat: 'animal',
        ...Object.fromEntries(SMART_EVENT_CLASSES.map(c => [c, c])),
    },
    'scrypted-coral-guard': {
        verified_person: 'person', verified_vehicle: 'vehicle', verified_animal: 'animal',
    },
};

export function defaultClassMap(source: string): Record<string, string> {
    return { ...COMMON_CLASS_MAP, ...(SOURCE_CLASS_MAPS[source] ?? {}) };
}

/** Facts about the first observation of a new source, used to infer its role. */
export interface FirstSeen {
    source: string;
    kind: ObsKind;
    className?: string;
    label?: string;
    /** the plugin of the camera the observation came from */
    cameraPlugin?: string;
}

/** Inferred role of a source's detections. */
export function inferRole(f: FirstSeen): Role {
    if (f.cameraPlugin && f.source === f.cameraPlugin)
        return 'trigger';
    if (KNOWN_VERIFIERS.includes(f.source))
        return 'verifier';
    const cls = (f.className ?? '').toLowerCase();
    if (ENRICHER_CLASSES.includes(cls) || f.label)
        return 'enricher';
    // another plugin publishing detections on the camera: a mixin that analyses frames
    return 'verifier';
}

/** Inferred role of a source's classless signals: a trigger, or a verifier only when the source is gated. */
export function inferSignalRole(gated: boolean): Role {
    return gated ? 'verifier' : 'trigger';
}

const SOURCE_RE = /^[A-Za-z0-9@._/-]{1,100}$/;

export function validSource(s: string): boolean {
    return SOURCE_RE.test(s);
}

/** "people=person, dog_cat=animal" -> map; invalid pairs are skipped. */
export function parseClassMap(text: string): Record<string, string> {
    const out: Record<string, string> = {};
    for (const part of String(text ?? '').split(/[,\n;]/)) {
        const m = /^\s*([^=\s]+)\s*=\s*([^=\s]+)\s*$/.exec(part);
        if (!m) continue;
        const from = cleanLabel(m[1], 48), to = cleanLabel(m[2], 48);
        if (from && to && /^[\w.:-]+$/.test(from) && /^[\w.:-]+$/.test(to))
            out[from] = to;
        if (Object.keys(out).length >= 64) break;
    }
    return out;
}

export function formatClassMap(m: Record<string, string>): string {
    return Object.entries(m).map(([a, b]) => `${a}=${b}`).join(', ');
}

/** The table, persisted as JSON by the provider. Bounded: at most 64 sources. */
export class RolesTable {
    private rows = new Map<string, RoleRow>();
    /** when each row's lastSeen was last persisted (in memory only) */
    private persistedAt = new Map<string, number>();
    static readonly MAX_ROWS = 64;

    constructor(json?: string | null, private onChange?: () => void) {
        if (json) this.load(json);
    }

    load(json: string) {
        this.rows.clear();
        try {
            const arr = JSON.parse(json);
            if (!Array.isArray(arr)) return;
            for (const r of arr.slice(0, RolesTable.MAX_ROWS)) {
                if (!r || typeof r.source !== 'string' || !validSource(r.source)) continue;
                const role: Role = ROLES.includes(r.role) ? r.role : 'trigger';
                const gated = typeof r.gated === 'boolean' ? r.gated : KNOWN_GATED.includes(r.source);
                const signalRole: Role = ROLES.includes(r.signalRole) ? r.signalRole : role === 'ignore' ? 'ignore' : inferSignalRole(gated);
                const classMap: Record<string, string> = {};
                if (r.classMap && typeof r.classMap === 'object')
                    for (const [k, v] of Object.entries(r.classMap).slice(0, 64))
                        if (typeof v === 'string' && cleanLabel(k) && cleanLabel(v)) classMap[cleanLabel(k)!] = cleanLabel(v)!;
                this.rows.set(r.source, {
                    source: r.source, role, inferred: !!r.inferred, roleSet: typeof r.roleSet === 'boolean' ? r.roleSet : true,
                    signalRole, gated, classMap, firstSeen: Number(r.firstSeen) || 0, lastSeen: Number(r.lastSeen) || 0,
                });
            }
        }
        catch { /* corrupt: start empty */ }
    }

    toJSON(): string {
        return JSON.stringify([...this.rows.values()]);
    }

    list(): RoleRow[] {
        return [...this.rows.values()].sort((a, b) => a.source.localeCompare(b.source));
    }

    get(source: string): RoleRow | undefined {
        return this.rows.get(source);
    }

    /**
     * The row for a source, created when it is new. A detection decides the detection role (inferred once); a classless
     * signal only creates the row (its detection role stays undecided) and never makes it a verifier unless it is gated.
     * `undefined` when the table is full or the name is invalid.
     */
    ensure(f: FirstSeen, now: number): RoleRow | undefined {
        let r = this.rows.get(f.source);
        if (!r) {
            if (!validSource(f.source) || this.rows.size >= RolesTable.MAX_ROWS)
                return undefined;
            const gated = KNOWN_GATED.includes(f.source);
            const isObject = f.kind === 'object';
            r = {
                source: f.source, role: isObject ? inferRole(f) : (f.cameraPlugin === f.source ? 'trigger' : 'verifier'), inferred: true, roleSet: isObject,
                signalRole: inferSignalRole(gated), gated, classMap: defaultClassMap(f.source), firstSeen: now, lastSeen: now,
            };
            this.rows.set(f.source, r);
            this.persistedAt.set(f.source, now);
            this.onChange?.();
            return r;
        }
        let dirty = false;
        if (f.kind === 'object' && !r.roleSet) {
            if (r.inferred) r.role = inferRole(f);
            r.roleSet = true;
            dirty = true;
        }
        // lastSeen is exact in memory (verifier liveness) and persisted at most once a minute per source
        r.lastSeen = Math.max(r.lastSeen, now);
        if (now - (this.persistedAt.get(r.source) ?? -Infinity) >= 60_000) { this.persistedAt.set(r.source, now); dirty = true; }
        if (dirty) this.onChange?.();
        return r;
    }

    setRole(source: string, role: Role) {
        const r = this.rows.get(source);
        if (!r || !ROLES.includes(role)) return;
        r.role = role;
        r.inferred = false;
        r.roleSet = true;
        this.onChange?.();
    }

    setSignalRole(source: string, role: Role) {
        const r = this.rows.get(source);
        if (!r || !ROLES.includes(role)) return;
        r.signalRole = role;
        this.onChange?.();
    }

    setGated(source: string, gated: boolean) {
        const r = this.rows.get(source);
        if (!r) return;
        r.gated = gated;
        this.onChange?.();
    }

    setClassMap(source: string, m: Record<string, string>) {
        const r = this.rows.get(source);
        if (!r) return;
        r.classMap = m;
        this.onChange?.();
    }

    remove(source: string) {
        if (this.rows.delete(source)) this.onChange?.();
    }

    /** Normalised class for a raw class of a source: its map, else the common aliases, else unchanged (lower-cased). */
    mapClass(source: string, raw: string): string {
        const r = this.rows.get(source);
        const m = r?.classMap ?? defaultClassMap(source);
        return m[raw] ?? m[raw.toLowerCase()] ?? raw.toLowerCase();
    }
}

/** Class family: what may be fused with what. `any` (motion, presence, audio, smart events) fuses with every family. */
export type Family = string;

const FAMILY: Record<string, string> = {
    person: 'person', face: 'person',
    vehicle: 'vehicle', car: 'vehicle', truck: 'vehicle', bus: 'vehicle', motorcycle: 'vehicle', bicycle: 'vehicle', plate: 'vehicle', license_plate: 'vehicle',
    animal: 'animal', dog: 'animal', cat: 'animal', bird: 'animal', horse: 'animal', sheep: 'animal', cow: 'animal', bear: 'animal', deer: 'animal',
    package: 'package',
    motion: 'any', presence: 'any', audio: 'any',
    ...Object.fromEntries(SMART_EVENT_CLASSES.map(c => [c, 'any'])),
};

export function familyOf(cls: string): Family {
    return FAMILY[cls] ?? cls;
}

export function compatible(a: Family, b: Family): boolean {
    return a === b || a === 'any' || b === 'any';
}
