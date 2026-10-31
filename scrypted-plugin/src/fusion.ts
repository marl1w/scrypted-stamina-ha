// Fusion: which subject of an event an observation belongs to.
//   1. the same track id (per source: track ids of two plugins are unrelated);
//   2. box overlap, IoU >= 0.3, compatible class family;
//   3. a compatible class family within +-2 s, when one side has no box.
// Enrichers attach to the subject whose box contains theirs (else the latest compatible subject within +-2 s).

import { Box, Observation } from './normalize';
import { compatible, Family, familyOf, Role } from './roles';

export type Verdict = 'pending' | 'confirmed' | 'vetoed' | 'unverified';

export interface SourceStat {
    role: Role;
    /** the source's observations were ambiguous (a split camera's whole-frame AI) */
    ambiguous?: boolean;
    count: number;
    max_score: number | null;
    first: number;
    last: number;
}

export interface Enrichment {
    source: string;
    class: string;
    label: string | null;
    score: number | null;
    t: number;
}

export interface Subject {
    id: string;
    class: string;
    family: Family;
    /** classes of family `any` seen on it (motion, presence, audio, smart events) */
    tags: string[];
    first: number;
    last: number;
    box: Box | null;
    /** sources' track ids, as "source#track" */
    tracks: string[];
    sources: Record<string, SourceStat>;
    max_score: number | null;
    verdict: Verdict;
    verdict_at: number | null;
    verdict_source: string | null;
    enrichments: Enrichment[];
    /** internal: receive time of the first and last observation (timers run on these) */
    firstRecv: number;
    lastRecv: number;
    boxRecv: number;
}

export interface FusionConfig {
    iou: number;
    classTimeMs: number;
    /** a subject's box is matched by overlap only this long after it was seen */
    boxStaleMs: number;
    /** enricher box containment (intersection / enricher area) */
    containment: number;
}

export const DEFAULT_FUSION: FusionConfig = { iou: 0.3, classTimeMs: 2000, boxStaleMs: 10_000, containment: 0.8 };

export function area(b: Box): number {
    return Math.max(0, b[2] - b[0]) * Math.max(0, b[3] - b[1]);
}

export function intersection(a: Box, b: Box): number {
    const w = Math.min(a[2], b[2]) - Math.max(a[0], b[0]);
    const h = Math.min(a[3], b[3]) - Math.max(a[1], b[1]);
    return w > 0 && h > 0 ? w * h : 0;
}

export function iou(a: Box, b: Box): number {
    const i = intersection(a, b);
    const u = area(a) + area(b) - i;
    return u > 0 ? i / u : 0;
}

export function trackKey(o: Pick<Observation, 'source' | 'track'>): string | undefined {
    return o.track ? `${o.source}#${o.track}` : undefined;
}

/** The subject an observation (trigger or verifier) joins, or undefined for a new subject. */
export function match(subjects: Subject[], o: Observation, cfg: FusionConfig = DEFAULT_FUSION): Subject | undefined {
    const fam = familyOf(o.class);
    // 1. track id
    const tk = trackKey(o);
    if (tk) {
        const s = subjects.find(x => x.tracks.includes(tk));
        if (s) return s;
    }
    // 2. box overlap
    if (o.box) {
        let best: Subject | undefined, bestIou = 0;
        for (const s of subjects) {
            if (!s.box || !compatible(s.family, fam) || Math.abs(o.recv - s.boxRecv) > cfg.boxStaleMs) continue;
            const v = iou(o.box, s.box);
            if (v >= cfg.iou && v > bestIou) { best = s; bestIou = v; }
        }
        if (best) return best;
    }
    // 3. class family and time, when one side has no box
    let best: Subject | undefined;
    for (const s of subjects) {
        if (o.box && s.box) continue;
        if (!compatible(s.family, fam) || Math.abs(o.recv - s.lastRecv) > cfg.classTimeMs) continue;
        if (!best) { best = s; continue; }
        const exact = (x: Subject) => x.family === fam ? 1 : 0;
        if (exact(s) > exact(best) || (exact(s) === exact(best) && s.lastRecv > best.lastRecv)) best = s;
    }
    return best;
}

/** The subject an enricher observation attaches to, or undefined (event-level). */
export function matchEnricher(subjects: Subject[], o: Observation, cfg: FusionConfig = DEFAULT_FUSION): Subject | undefined {
    if (o.box) {
        let best: Subject | undefined, bestC = 0;
        const a = area(o.box);
        for (const s of subjects) {
            if (!s.box || a <= 0) continue;
            const c = intersection(o.box, s.box) / a;
            if (c >= cfg.containment && (c > bestC || (c === bestC && best && area(s.box) < area(best.box!)))) { best = s; bestC = c; }
        }
        if (best) return best;
    }
    const fam = familyOf(o.class);
    let best: Subject | undefined;
    for (const s of subjects) {
        if (!compatible(s.family, fam) || Math.abs(o.recv - s.lastRecv) > cfg.classTimeMs) continue;
        if (!best || s.lastRecv >= best.lastRecv) best = s;
    }
    return best;
}

/** A new subject from its first observation. The verdict is set by the caller. */
export function newSubject(id: string, o: Observation): Subject {
    const s: Subject = {
        id, class: o.class, family: familyOf(o.class), tags: [], first: o.t, last: o.t, box: null, tracks: [], sources: {},
        max_score: null, verdict: 'unverified', verdict_at: null, verdict_source: null, enrichments: [],
        firstRecv: o.recv, lastRecv: o.recv, boxRecv: o.recv,
    };
    addTo(s, o);
    return s;
}

/** Adds an observation to a subject: time span, box, track, per-source stats, class upgrade from `any` to a concrete family. */
export function addTo(s: Subject, o: Observation) {
    const fam = familyOf(o.class);
    if (fam === 'any') {
        if (!s.tags.includes(o.class) && s.tags.length < 16) s.tags.push(o.class);
    }
    else if (s.family === 'any') {
        if (!s.tags.includes(s.class) && s.tags.length < 16) s.tags.push(s.class);
        s.class = o.class;
        s.family = fam;
    }
    s.first = Math.min(s.first, o.t);
    s.last = Math.max(s.last, o.t);
    s.lastRecv = Math.max(s.lastRecv, o.recv);
    if (o.box) { s.box = o.box; s.boxRecv = o.recv; }
    const tk = trackKey(o);
    if (tk && !s.tracks.includes(tk) && s.tracks.length < 16) s.tracks.push(tk);
    const st = s.sources[o.source] ??= { role: o.role, count: 0, max_score: null, first: o.t, last: o.t };
    st.count++;
    st.role = o.role;
    if (o.ambiguous) st.ambiguous = true;
    st.first = Math.min(st.first, o.t);
    st.last = Math.max(st.last, o.t);
    if (o.score !== null) {
        st.max_score = Math.max(st.max_score ?? 0, o.score);
        s.max_score = Math.max(s.max_score ?? 0, o.score);
    }
}

export function enrich(s: Subject | undefined, o: Observation): Enrichment {
    const e: Enrichment = { source: o.source, class: o.class, label: o.label, score: o.score, t: o.t };
    if (s && s.enrichments.length < 16) s.enrichments.push(e);
    return e;
}

/** The subject as sent over the API (internal fields dropped). */
export function publicSubject(s: Subject) {
    const { firstRecv, lastRecv, boxRecv, ...rest } = s;
    return { ...rest, tags: [...s.tags], tracks: [...s.tracks], sources: { ...s.sources }, enrichments: [...s.enrichments] };
}
