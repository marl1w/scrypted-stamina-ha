// Normalisation: a raw Scrypted signal (ObjectsDetected, a sensor edge) -> Observations with a source, role and mapped class.

import { ObsKind, Role, RolesTable, signalRole } from './roles';
import { clampNum, cleanLabel } from './util';

/** Normalised box: [x1, y1, x2, y2] as fractions of the frame (0..1). */
export type Box = [number, number, number, number];

export interface Observation {
    /** event time (ms): the detection's own timestamp when sane, else the receive time */
    t: number;
    /** plugin receive time (ms) */
    recv: number;
    source: string;
    role: Role;
    kind: ObsKind;
    class: string;
    /** the class as the source sent it */
    raw: string;
    score: number | null;
    box: Box | null;
    track: string | null;
    label: string | null;
    zones: string[] | null;
    /** the source is flagged gated: a classless verifier signal from it may confirm */
    gated?: boolean;
    /** a box-less detection of a split camera's own AI (whole stitched frame): evidence for a view's subject only */
    ambiguous?: boolean;
}

/** A detection timestamp further than this from the receive time is not trusted (clock skew, replays). */
export const MAX_SKEW_MS = 60_000;
/** At most this many detections are taken from one ObjectsDetected payload. */
export const MAX_DETECTIONS = 32;

export interface Attribution {
    /** EventDetails.mixinId */
    mixinId?: string;
    /** ObjectsDetected.sourceId */
    sourceId?: string;
}

/** Resolves a Scrypted device id to the plugin that owns it. */
export type PluginOf = (deviceId: string) => string | undefined;

/**
 * Source plugin of a signal: the mixin that emitted it, else the declared sourceId, else the camera's own plugin.
 * A sourceId that is not a device id (some plugins put a name there) is taken as the name when it looks like a plugin id.
 */
export function attribute(a: Attribution, pluginOf: PluginOf, cameraPlugin: string): string {
    if (a.mixinId) {
        const p = pluginOf(a.mixinId);
        if (p) return p;
    }
    if (a.sourceId) {
        const p = pluginOf(a.sourceId);
        if (p) return p;
        const s = cleanLabel(a.sourceId, 100);
        if (s && /^[A-Za-z0-9@._/-]+$/.test(s)) return s;
    }
    return cameraPlugin;
}

function sane(ts: unknown, recv: number): number {
    return typeof ts === 'number' && Number.isFinite(ts) && Math.abs(ts - recv) <= MAX_SKEW_MS ? ts : recv;
}

/**
 * Scrypted boxes are [x, y, width, height] in the pixels of `inputDimensions`. Without dimensions a box is used only when it
 * is already fractional (every value within 0..1). Degenerate boxes are dropped.
 */
export function normBox(b: unknown, dims: unknown): Box | null {
    if (!Array.isArray(b) || b.length !== 4 || !b.every(v => typeof v === 'number' && Number.isFinite(v)))
        return null;
    let [x, y, w, h] = b as number[];
    if (Array.isArray(dims) && dims.length === 2 && dims.every(v => typeof v === 'number' && v > 0)) {
        const [W, H] = dims as number[];
        x /= W; y /= H; w /= W; h /= H;
    }
    else if (![x, y, w, h].every(v => v >= 0 && v <= 1))
        return null;
    const x1 = clampNum(x, 0, 1, 0), y1 = clampNum(y, 0, 1, 0);
    const x2 = clampNum(x + w, 0, 1, 0), y2 = clampNum(y + h, 0, 1, 0);
    if (x2 - x1 <= 0.001 || y2 - y1 <= 0.001)
        return null;
    const r = (v: number) => Math.round(v * 10000) / 10000;
    return [r(x1), r(y1), r(x2), r(y2)];
}

export interface NormalizeContext {
    roles: RolesTable;
    cameraPlugin: string;
    now: number;
}

/** Observations from one ObjectsDetected payload; sources with role `ignore` yield nothing. */
export function fromDetections(data: any, source: string, eventTime: number | undefined, ctx: NormalizeContext): Observation[] {
    const dets: any[] = Array.isArray(data?.detections) ? data.detections.slice(0, MAX_DETECTIONS) : [];
    if (!dets.length)
        return [];
    const t = sane(data?.timestamp ?? eventTime, ctx.now);
    const out: Observation[] = [];
    for (const d of dets) {
        const raw = typeof d?.className === 'string' ? cleanLabel(d.className, 48) : undefined;
        if (!raw) continue;
        const label = cleanLabel(d?.label, 64) ?? null;
        const row = ctx.roles.ensure({ source, kind: 'object', className: raw, label: label ?? undefined, cameraPlugin: ctx.cameraPlugin }, ctx.now);
        if (!row || row.role === 'ignore') continue;
        const cls = ctx.roles.mapClass(source, raw);
        const kind: ObsKind = cls === 'motion' ? 'motion' : 'object';
        const score = typeof d?.score === 'number' && Number.isFinite(d.score) ? Math.round(clampNum(d.score, 0, 1, 0) * 1000) / 1000 : null;
        const zones = Array.isArray(d?.zones) ? d.zones.slice(0, 8).map((z: unknown) => cleanLabel(z, 48)).filter(Boolean) as string[] : null;
        out.push({
            t, recv: ctx.now, source, role: row.role, kind, class: cls, raw, score, gated: row.gated,
            box: normBox(d?.boundingBox, data?.inputDimensions),
            track: d?.id !== undefined && d?.id !== null ? cleanLabel(d.id, 48) ?? null : null,
            label, zones: zones?.length ? zones : null,
        });
    }
    return out;
}

const KIND_CLASS: Record<Exclude<ObsKind, 'object'>, string> = { motion: 'motion', occupancy: 'presence', audio: 'audio' };

/** One observation for the rising edge of a binary sensor (motion, occupancy, audio); nothing for a falling edge or `ignore`. */
export function fromBinary(kind: Exclude<ObsKind, 'object'>, on: boolean, source: string, eventTime: number | undefined, ctx: NormalizeContext): Observation | undefined {
    const row = ctx.roles.ensure({ source, kind, cameraPlugin: ctx.cameraPlugin }, ctx.now);
    // classless signals use the row's signal role: never a verifier unless the source is gated
    const role: Role | undefined = row ? signalRole(row) : undefined;
    if (!role || role === 'ignore' || !on)
        return undefined;
    const cls = KIND_CLASS[kind];
    return { t: sane(eventTime, ctx.now), recv: ctx.now, source, role, kind, class: cls, raw: cls, score: null, box: null, track: null, label: null, zones: null, gated: row!.gated };
}
