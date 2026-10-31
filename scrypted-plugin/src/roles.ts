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
    /** a verifier whose motion/occupancy turn on only after its own verification (Coral Guard): those signals confirm */
    gated: boolean;
    /** raw class name -> normalised class (IGNORE_CLASS: dropped) */
    classMap: Record<string, string>;
    /** raw classes seen from this source (bounded), shown in the class map */
    classes: string[];
    /** 0 while the source was only discovered (it has not sent anything yet) */
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

/** Common aliases a source without its own defaults starts with. */
export const COMMON_CLASS_MAP: Record<string, string> = {
    people: 'person',
    dog_cat: 'animal',
};

/** Coral Guard's published classes: they belong to Coral Guard only (0.1.1 put them on every source). */
const CORAL_CLASSES = ['verified_person', 'verified_vehicle', 'verified_animal'];

/** A class mapped to this is dropped (the user chose "ignore" for it). */
export const IGNORE_CLASS = '!ignore';

/** Raw classes a known source declares, shown in the class map before it is seen sending them. */
export const KNOWN_CLASSES: Record<string, string[]> = {
    '@marl1w/scrypted-reolink': ['people', 'vehicle', 'dog_cat'],
    'scrypted-coral-guard': CORAL_CLASSES,
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
    return { ...(SOURCE_CLASS_MAPS[source] ?? COMMON_CLASS_MAP) };
}

/** Facts from the system state: a plugin that provides detection or sensor interfaces on a camera with Stamina. */
export interface Discovered {
    source: string;
    /** it is the camera's own plugin */
    cameraPlugin: boolean;
    /** which of ObjectDetector, MotionSensor, OccupancySensor, AudioSensor it provides */
    ifaces: string[];
}

/** The role a discovered (not yet seen) source starts with. */
export function inferDiscovered(d: Discovered): Role {
    if (d.cameraPlugin) return 'trigger';
    if (KNOWN_VERIFIERS.includes(d.source)) return 'verifier';
    return d.ifaces.includes('ObjectDetector') ? 'verifier' : 'trigger';
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

/**
 * Role of a source's classless signals (motion, occupancy, audio), derived from its one role: ignored with it, a
 * verifier only for a gated verifier, otherwise always a trigger (a signal never verifies by itself).
 */
export function signalRole(r: Pick<RoleRow, 'role' | 'gated'>): Role {
    if (r.role === 'ignore') return 'ignore';
    return r.role === 'verifier' && r.gated ? 'verifier' : 'trigger';
}

const SOURCE_RE = /^[A-Za-z0-9@._/-]{1,100}$/;
export const CLASS_NAME_RE = /^[\w.:-]{1,48}$/;

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

/** The class map as list rows for the settings page: "people → person". */
export function classMapRows(m: Record<string, string>): string[] {
    return Object.entries(m).map(([a, b]) => `${a} → ${b}`);
}

const CLASS_RE = /^[\w.:-]{1,48}$/;

/** List rows ("raw → class" or "raw = class", one each) to a map; rows that do not parse are returned as invalid. */
export function parseClassMapRows(rows: unknown): { map: Record<string, string>; invalid: string[] } {
    const list = Array.isArray(rows) ? rows : typeof rows === 'string' ? rows.split(/[\n,;]/) : [];
    const map: Record<string, string> = {};
    const invalid: string[] = [];
    for (const raw of list.slice(0, 128)) {
        const text = String(raw ?? '').trim();
        if (!text) continue;
        const m = /^(.+?)\s*(?:→|->|=)\s*(.+)$/.exec(text);
        const from = m?.[1].trim(), to = m?.[2].trim();
        if (from && to && CLASS_RE.test(from) && CLASS_RE.test(to) && Object.keys(map).length < 64) map[from] = to;
        else invalid.push(text.slice(0, 60));
    }
    return { map, invalid };
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
                // rows of 0.1.0 had a separate signal role: it is dropped (signals now follow the one role)
                const gated = typeof r.gated === 'boolean' ? r.gated : KNOWN_GATED.includes(r.source);
                const classMap: Record<string, string> = {};
                if (r.classMap && typeof r.classMap === 'object')
                    for (const [k, v] of Object.entries(r.classMap).slice(0, 64))
                        if (typeof v === 'string' && cleanLabel(k) && cleanLabel(v)) classMap[cleanLabel(k)!] = cleanLabel(v)!;
                // 0.1.1 gave every source Coral Guard's verified_* aliases: they are removed where they are only that default
                if (r.source !== 'scrypted-coral-guard')
                    for (const c of CORAL_CLASSES) if (classMap[c] && classMap[c] === c.slice(9)) delete classMap[c];
                const classes = Array.isArray(r.classes) ? r.classes.filter((c: unknown) => typeof c === 'string' && CLASS_NAME_RE.test(c)).slice(0, 48) : [];
                this.rows.set(r.source, {
                    source: r.source, role, inferred: !!r.inferred, roleSet: typeof r.roleSet === 'boolean' ? r.roleSet : true,
                    gated, classMap, classes, firstSeen: Number(r.firstSeen) || 0, lastSeen: Number(r.lastSeen) || 0,
                });
            }
        }
        catch { /* corrupt: start empty */ }
    }

    toJSON(): string {
        return JSON.stringify([...this.rows.values()]);
    }

    /** Rows of 0.1.0 still carry a separate signal role: rewritten once in the current shape. */
    static needsMigration(json: string | null | undefined): boolean {
        try {
            const arr = JSON.parse(json || '[]');
            return Array.isArray(arr) && arr.some((r: any) => r && ('signalRole' in r || !('classes' in r)
                || (r.source !== 'scrypted-coral-guard' && r.classMap && CORAL_CLASSES.some(c => c in r.classMap))));
        }
        catch { return false; }
    }

    /**
     * A source found in the system state (a mixin or the camera's plugin providing detections or sensors): a row with its
     * inferred role, before it sends anything (firstSeen 0 = not seen yet). Existing rows are left alone.
     */
    preseed(d: Discovered): RoleRow | undefined {
        if (this.rows.has(d.source) || !validSource(d.source) || this.rows.size >= RolesTable.MAX_ROWS) return this.rows.get(d.source);
        const r: RoleRow = {
            source: d.source, role: inferDiscovered(d), inferred: true, roleSet: false, gated: KNOWN_GATED.includes(d.source),
            classMap: defaultClassMap(d.source), classes: [], firstSeen: 0, lastSeen: 0,
        };
        this.rows.set(d.source, r);
        this.onChange?.();
        return r;
    }

    /** A raw class seen from a source: remembered (bounded) for the class map. */
    noteClass(source: string, raw: string) {
        const r = this.rows.get(source);
        if (!r || r.classes.includes(raw) || r.classes.length >= 48 || !CLASS_NAME_RE.test(raw)) return;
        r.classes.push(raw);
        this.onChange?.();
    }

    /** One class map entry: a class, `raw` itself (keep as is), or IGNORE_CLASS. */
    setClass(source: string, raw: string, to: string) {
        const r = this.rows.get(source);
        if (!r || !CLASS_NAME_RE.test(raw) || !(to === IGNORE_CLASS || CLASS_NAME_RE.test(to))) return;
        r.classMap = { ...r.classMap, [raw]: to };
        this.onChange?.();
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
                // a row first seen through a signal is a trigger until its first detection decides (a known verifier plugin is
                // a verifier by name, not because of the signal)
                source: f.source, role: isObject ? inferRole(f) : KNOWN_VERIFIERS.includes(f.source) ? 'verifier' : 'trigger', inferred: true, roleSet: isObject, gated, classMap: defaultClassMap(f.source), classes: [], firstSeen: now, lastSeen: now,
            };
            this.rows.set(f.source, r);
            this.persistedAt.set(f.source, now);
            this.onChange?.();
            return r;
        }
        let dirty = false;
        if (!r.firstSeen) { r.firstSeen = now; dirty = true; }
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
