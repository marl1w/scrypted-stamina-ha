// The "Detection sources" and "Class map" settings: one row per source plugin, rendered and parsed here (pure, tested).

import type { Setting, SettingValue } from '@scrypted/sdk';
import { classMapRows, parseClassMapRows, Role, RoleRow, ROLES, RolesTable } from './roles';

export const SOURCES_GROUP = 'Detection sources';
export const CLASSMAP_GROUP = 'Class map';

export const ROLE_CHOICES: Record<Role, string> = {
    trigger: 'Trigger: starts an event (e.g. the camera\'s own AI)',
    verifier: 'Verifier: confirms or vetoes what triggers saw (e.g. Coral Guard)',
    enricher: 'Enricher: adds detail (faces, plates)',
    ignore: 'Ignore',
};

/** What the chosen role does, in one line. */
export const ROLE_HELP: Record<Role, string> = {
    trigger: 'Its detections, motion and sound start events; subjects wait for a verifier when the camera has one.',
    verifier: 'A subject it sees is confirmed; one it does not see within the verify window is vetoed. Its motion and sound only start events.',
    enricher: 'Its faces, plates and labels are attached to the subjects of an open event; it never starts one.',
    ignore: 'Everything it sends is ignored, on every camera.',
};

/** A role from a dropdown value: the label, or a bare role name. */
export function roleFromChoice(v: SettingValue): Role | undefined {
    const s = String(v ?? '').trim();
    if ((ROLES as string[]).includes(s)) return s as Role;
    return ROLES.find(r => ROLE_CHOICES[r] === s);
}

/** The settings of every source: its role (one dropdown), "gated" when it is a verifier, and its class map. */
export function sourceSettings(rows: RoleRow[], nameOf: (source: string) => string | undefined, invalid: Map<string, string[]> = new Map()): Setting[] {
    const out: Setting[] = [];
    const title = (r: RoleRow) => {
        const n = nameOf(r.source);
        return n && n !== r.source ? n : r.source;
    };
    for (const r of rows) {
        const n = title(r);
        out.push({
            group: SOURCES_GROUP, key: `role:${r.source}`, title: n, choices: ROLES.map(x => ROLE_CHOICES[x]), value: ROLE_CHOICES[r.role],
            description: `${ROLE_HELP[r.role]}${!r.roleSet ? ' (No detection seen yet.)' : r.inferred ? ' (Default.)' : ''}${n !== r.source ? ` Plugin: ${r.source}.` : ''}`,
        });
        if (r.role === 'verifier')
            out.push({
                group: SOURCES_GROUP, key: `gated:${r.source}`, title: `${n}: its motion counts as confirmation`, type: 'boolean', value: r.gated,
                description: 'On when this plugin raises motion only after it has verified something itself (Coral Guard in Gate mode). Its motion then confirms the subject waiting for it.',
            });
    }
    for (const r of rows) {
        const bad = invalid.get(r.source);
        out.push({
            group: CLASSMAP_GROUP, key: `classmap:${r.source}`, title: title(r), type: 'string', multiple: true, value: classMapRows(r.classMap),
            placeholder: 'people → person',
            description: `One row per class name: what this plugin sends → the class Stamina uses (people → person). Classes not listed pass through.${bad?.length ? ` Dropped (not "name → class"): ${bad.join(', ')}.` : ''}`,
        });
    }
    if (!rows.length)
        out.push({ group: SOURCES_GROUP, key: 'sourcesEmpty', title: 'Sources', readonly: true, value: 'A row appears for each plugin the first time it sends a detection or motion on a camera with the Stamina extension.' });
    return out;
}

/**
 * Applies a `role:`, `gated:` or `classmap:` setting. Returns false for any other key. Invalid class map rows are dropped
 * and recorded in `invalid` (shown in the setting's description until the next save).
 */
export function applySourceSetting(roles: RolesTable, key: string, value: SettingValue, invalid: Map<string, string[]> = new Map()): boolean {
    if (key.startsWith('role:')) {
        const role = roleFromChoice(value);
        if (role) roles.setRole(key.slice(5), role);
        return true;
    }
    if (key.startsWith('gated:')) {
        roles.setGated(key.slice(6), value === true || value === 'true');
        return true;
    }
    if (key.startsWith('classmap:')) {
        const src = key.slice(9);
        const { map, invalid: bad } = parseClassMapRows(value);
        roles.setClassMap(src, map);
        if (bad.length) invalid.set(src, bad);
        else invalid.delete(src);
        return true;
    }
    return false;
}
