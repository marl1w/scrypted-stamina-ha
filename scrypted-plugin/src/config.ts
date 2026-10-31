// Provider settings: definitions, defaults and clamping (pure).

import { clampInt } from './util';

export const PLUGIN_ID = '@marl1w/scrypted-stamina';
export const VERSION = '0.1.0';

export interface NumberSetting { key: string; title: string; group: string; description: string; def: number; min: number; max: number }

export const NUMBER_SETTINGS: NumberSetting[] = [
    { key: 'marginS', title: 'Event margin (s)', group: 'Events', description: 'An event ends this long after its last observation (or after the last held signal, such as motion, turns off).', def: 10, min: 2, max: 120 },
    { key: 'capMin', title: 'Event cap (min)', group: 'Events', description: 'An event is closed after this long; if activity continues a new event starts at once.', def: 10, min: 1, max: 30 },
    { key: 'verifyWindowS', title: 'Verify window (s)', group: 'Events', description: 'A subject a verifier has not seen within this long of its first observation is vetoed.', def: 5, min: 1, max: 30 },
    { key: 'segmentS', title: 'Segment length (s)', group: 'Video', description: 'Target length of each fMP4 segment. Segments are cut on key frames (about 1 s apart on most cameras).', def: 2, min: 1, max: 6 },
    { key: 'prerollS', title: 'Pre-roll (s)', group: 'Video', description: 'Video before the first detection, taken from the Rebroadcast prebuffer (limited by its length).', def: 10, min: 0, max: 30 },
    { key: 'memoryMB', title: 'Segment memory (MB)', group: 'Video', description: 'All events\' segments together; the oldest are dropped first when full.', def: 64, min: 16, max: 512 },
];

export type NumberKey = 'marginS' | 'capMin' | 'verifyWindowS' | 'segmentS' | 'prerollS' | 'memoryMB';

export function readNumber(key: NumberKey, raw: string | null | undefined): number {
    const d = NUMBER_SETTINGS.find(s => s.key === key)!;
    return clampInt(raw ?? undefined, d.min, d.max, d.def);
}
