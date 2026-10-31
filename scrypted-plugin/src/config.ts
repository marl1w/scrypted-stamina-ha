// Provider settings: definitions, defaults and clamping (pure).

import { clampInt } from './util';

export const PLUGIN_ID = '@marl1w/scrypted-stamina';
export const VERSION = '0.1.2';

export interface NumberSetting { key: string; title: string; group: string; description: string; def: number; min: number; max: number }

export const NUMBER_SETTINGS: NumberSetting[] = [
    { key: 'segmentS', title: 'Clip piece length (s)', group: 'Advanced', description: 'Clips are sent in pieces of about this length.', def: 2, min: 1, max: 6 },
    { key: 'prerollS', title: 'Video before the event (s)', group: 'Advanced', description: 'How much video before the first detection each clip starts with (as much as the camera keeps).', def: 10, min: 0, max: 30 },
    { key: 'memoryMB', title: 'Memory for clips (MB)', group: 'Advanced', description: 'All clips waiting for Home Assistant together; the oldest pieces go first when it is full.', def: 64, min: 16, max: 512 },
    { key: 'marginS', title: 'Event end after quiet (s)', group: 'Advanced', description: 'An event ends this long after the last thing was seen.', def: 10, min: 2, max: 120 },
    { key: 'capMin', title: 'Longest event (min)', group: 'Advanced', description: 'A longer event is split; the next part continues it.', def: 10, min: 1, max: 30 },
    { key: 'verifyWindowS', title: 'Double-check wait (s)', group: 'Advanced', description: 'How long a double-checker has to confirm a detection before it counts as rejected.', def: 5, min: 1, max: 30 },
];

export type NumberKey = 'marginS' | 'capMin' | 'verifyWindowS' | 'segmentS' | 'prerollS' | 'memoryMB';

export function readNumber(key: NumberKey, raw: string | null | undefined): number {
    const d = NUMBER_SETTINGS.find(s => s.key === key)!;
    return clampInt(raw ?? undefined, d.min, d.max, d.def);
}
