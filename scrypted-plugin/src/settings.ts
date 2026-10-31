// The plugin's settings for sources and classes, and the camera status lines: rendered and parsed here (pure, tested).

import type { Setting, SettingValue } from '@scrypted/sdk';
import { CLASS_NAME_RE, defaultClassMap, IGNORE_CLASS, KNOWN_CLASSES, Role, RoleRow, ROLES, RolesTable } from './roles';

export const SOURCES_GROUP = 'Detection sources';
export const CLASSMAP_GROUP = 'Class map';
export const ADVANCED_GROUP = 'Advanced';

/** The question's answers, in plain words. */
export const ROLE_CHOICES: Record<Role, string> = {
    trigger: 'Start events',
    verifier: 'Double-check other detections',
    enricher: 'Add names / plates',
    ignore: 'Ignore',
};

/** "<Name> starts events" in the summary. */
const ROLE_VERB: Record<Role, string> = {
    trigger: 'starts events',
    verifier: 'double-checks',
    enricher: 'adds names / plates',
    ignore: 'is ignored',
};

/** One short line on what the answer does. */
export const ROLE_HELP: Record<Role, string> = {
    trigger: 'What it detects starts an event.',
    verifier: 'Confirms what started an event, or rejects it when it sees nothing.',
    enricher: 'Adds names and plates to what is already in an event.',
    ignore: 'Nothing it sends is used.',
};

const ROLE_ORDER: Role[] = ['trigger', 'verifier', 'enricher', 'ignore'];

/** The classes a raw class can be mapped to, then the two special answers. */
export const CLASS_CHOICES = ['person', 'vehicle', 'animal', 'package', 'face', 'plate', 'line_crossing', 'intrusion', 'loitering', 'forgotten_item', 'taken_item'];
export const KEEP = 'keep as is';
export const IGNORE = 'ignore';

/** A role from a dropdown value: the answer, or a bare role name. */
export function roleFromChoice(v: SettingValue): Role | undefined {
    const s = String(v ?? '').trim();
    if ((ROLES as string[]).includes(s)) return s as Role;
    return ROLES.find(r => ROLE_CHOICES[r] === s);
}

export type NameOf = (source: string) => string | undefined;

function display(nameOf: NameOf, source: string): string {
    const n = nameOf(source);
    return n && n !== source ? n : source;
}

function byRole(rows: RoleRow[], nameOf: NameOf): RoleRow[] {
    return [...rows].sort((a, b) => ROLE_ORDER.indexOf(a.role) - ROLE_ORDER.indexOf(b.role) || display(nameOf, a.source).localeCompare(display(nameOf, b.source)));
}

/** "Reolink starts events · Coral Guard double-checks · …" */
export function sourcesSummary(rows: RoleRow[], nameOf: NameOf): string {
    return byRole(rows, nameOf).map(r => `${display(nameOf, r.source)} ${ROLE_VERB[r.role]}`).join(' · ');
}

/**
 * The raw classes shown for a source: classes it was seen sending, classes it declares (the camera's own plugin: the cameras'
 * getObjectTypes), and the known ones of a known plugin. A class known to belong to another plugin (Coral Guard's verified_*,
 * which also appear in the cameras' object types) is shown only under that plugin, unless this one was seen sending it.
 */
export function classesOf(r: RoleRow, declared: string[] = []): string[] {
    const elsewhere = new Set(Object.entries(KNOWN_CLASSES).filter(([s]) => s !== r.source).flatMap(([, c]) => c));
    const out: string[] = [];
    for (const c of [...(KNOWN_CLASSES[r.source] ?? []), ...declared, ...r.classes]) {
        if (!CLASS_NAME_RE.test(c) || out.includes(c)) continue;
        if (elsewhere.has(c) && !r.classes.includes(c)) continue;
        out.push(c);
    }
    return out;
}

/** The select's value for a raw class: its class, "keep as is", or "ignore". */
export function classChoice(r: RoleRow, raw: string): string {
    const to = r.classMap[raw];
    if (to === IGNORE_CLASS) return IGNORE;
    if (to === undefined || to === raw) return CLASS_CHOICES.includes(raw) ? raw : KEEP;
    return to;
}

/** A class Stamina already handles: mapped by default, already one of the classes it uses, or chosen by the user. */
function handled(r: RoleRow, raw: string): boolean {
    return raw in defaultClassMap(r.source) || CLASS_CHOICES.includes(raw) || r.classMap[raw] !== undefined;
}

export interface SourceSettingsOptions {
    /** classes each source declares (the camera plugin: getObjectTypes of the cameras) */
    declared?: Map<string, string[]>;
}

/**
 * The "Detection sources" group (a summary, then one question per source, ordered by role) and the class map: one select
 * per (source, raw class). Classes Stamina does not handle yet are in "Class map"; those it already maps are in Advanced.
 */
export function sourceSettings(rows: RoleRow[], nameOf: NameOf, opts: SourceSettingsOptions = {}): { sources: Setting[]; classes: Setting[]; advanced: Setting[] } {
    const sources: Setting[] = [];
    const classes: Setting[] = [];
    const advanced: Setting[] = [];
    if (!rows.length) {
        sources.push({ group: SOURCES_GROUP, key: 'sourcesEmpty', title: 'Sources', readonly: true, value: 'None yet. Enable the Scrypted Stamina extension on a camera.' });
        return { sources, classes, advanced };
    }
    sources.push({ group: SOURCES_GROUP, key: 'sourcesSummary', title: 'Summary', readonly: true, value: `${sourcesSummary(rows, nameOf)}. Change only if this is wrong.` });
    for (const r of byRole(rows, nameOf)) {
        sources.push({
            group: SOURCES_GROUP, key: `role:${r.source}`, title: `What should Stamina do with ${display(nameOf, r.source)}?`,
            choices: ROLE_ORDER.map(x => ROLE_CHOICES[x]), value: ROLE_CHOICES[r.role],
            description: `${ROLE_HELP[r.role]}${r.firstSeen ? '' : ' Not seen yet.'}`,
        });
    }
    for (const r of byRole(rows, nameOf)) {
        const n = display(nameOf, r.source);
        let firstUnknown = true, firstKnown = true;
        for (const raw of classesOf(r, opts.declared?.get(r.source))) {
            const value = classChoice(r, raw);
            const choices = [...CLASS_CHOICES, ...(CLASS_CHOICES.includes(value) || value === KEEP || value === IGNORE ? [] : [value]), KEEP, IGNORE];
            const s: Setting = { key: `class:${r.source}:${raw}`, title: raw, choices, value };
            if (handled(r, raw)) {
                advanced.push({ ...s, group: ADVANCED_GROUP, subgroup: `Classes from ${n}`, ...(firstKnown ? { description: `How ${n}'s classes are read. Already set; change only if one is wrong.` } : {}) });
                firstKnown = false;
            }
            else {
                classes.push({ ...s, group: CLASSMAP_GROUP, subgroup: `Unknown classes from ${n}`, ...(firstUnknown ? { description: `What each of ${n}'s classes means. "keep as is" passes the name through.` } : {}) });
                firstUnknown = false;
            }
        }
    }
    return { sources, classes, advanced };
}

/** Applies a `role:` or `class:` setting; false for any other key. */
export function applySourceSetting(roles: RolesTable, key: string, value: SettingValue): boolean {
    if (key.startsWith('role:')) {
        const role = roleFromChoice(value);
        if (role) roles.setRole(key.slice(5), role);
        return true;
    }
    if (key.startsWith('class:')) {
        const rest = key.slice(6);
        const i = rest.indexOf(':');
        if (i <= 0) return true;
        const source = rest.slice(0, i), raw = rest.slice(i + 1);
        const v = String(value ?? '').trim();
        if (v === KEEP) roles.setClass(source, raw, raw);
        else if (v === IGNORE) roles.setClass(source, raw, IGNORE_CLASS);
        else if (CLASS_NAME_RE.test(v)) roles.setClass(source, raw, v);
        return true;
    }
    return false;
}

/** A camera's capabilities in plain words, for its Stamina page. */
export interface CameraStatus {
    triggers: string[];
    verifiers: { source: string; live: boolean }[];
    silent: string[];
    prebufferMs: number | null;
    recordings: boolean;
    clips: boolean;
    split: boolean;
    parent: string | null;
    detections: number;
}

export function cameraStatusLines(st: CameraStatus, nameOf: NameOf): string[] {
    const n = (s: string) => display(nameOf, s);
    const yes = (b: boolean) => b ? 'yes' : 'no';
    const checks = [...st.verifiers.map(v => `${n(v.source)}${v.live ? '' : ' (not responding)'}`), ...st.silent.map(s => `${n(s)} (not responding)`)];
    return [
        `Starts events: ${st.triggers.length ? st.triggers.map(n).join(', ') : 'nothing yet'}`,
        `Double-check: ${checks.length ? checks.join(', ') : 'none'}`,
        `Video before the event: ${st.prebufferMs === null ? 'unknown' : st.prebufferMs > 0 ? `${Math.round(st.prebufferMs / 1000)} s` : 'none'}`,
        `Recordings: ${yes(st.recordings)} · Clips: ${yes(st.clips)}`,
        ...(st.split ? [st.parent ? 'This camera is a view of another camera' : 'This camera is split into views'] : []),
        `Detections received: ${st.detections}`,
    ];
}
