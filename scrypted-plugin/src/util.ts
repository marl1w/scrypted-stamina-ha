// Small pure helpers: clocks, secrets masking, ids, validation and clamping.

import crypto from 'crypto';

/** Remove credentials and query strings (tokens) from text that may contain stream URLs. */
export function maskSecrets(text: string): string {
    return String(text)
        // userinfo up to the LAST '@' of the URL: a password may contain '/' or '@'
        .replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s'"]*@/gi, '$1***@')
        .replace(/\?[^\s'"]*/g, '?***');
}

/** maskSecrets, and every URL reduced to scheme://host:port (no path, credentials or query): safe for any log line or JSON. */
export function maskUrls(text: string): string {
    return maskSecrets(text).replace(/([a-z][a-z0-9+.-]*:\/\/)(?:[^/\s]*@)?([^/\s?]*)\/[^\s'"]*/gi, '$1$2');
}

/** An error's message, masked: what may go into a log line or an API reply. */
export function errText(e: unknown): string {
    return maskUrls(String((e as Error)?.message ?? e)).slice(0, 300);
}

/** A string field from another plugin: cleaned, and dropped (undefined) when it carries a URL. */
export function safeField(v: unknown, max = 64): string | undefined {
    if (typeof v !== 'string' && typeof v !== 'number') return undefined;
    const s = cleanLabel(v, max);
    return s && !/:\/\//.test(s) ? s : undefined;
}

/** A short fingerprint of a secret (8 hex characters of its SHA-256): identifies it, reveals none of it. */
export function tokenFingerprint(token: string): string {
    return token ? crypto.createHash('sha256').update(token).digest('hex').slice(0, 8) : 'none';
}

/** Opaque random id: a prefix and 16 random bytes, base64url (22 characters). */
export function randomId(prefix = ''): string {
    return prefix + crypto.randomBytes(16).toString('base64url');
}

/** A new API token: 32 random bytes, base64url (43 characters). */
export function newToken(): string {
    return crypto.randomBytes(32).toString('base64url');
}

/** Constant-time comparison of two strings (hashing first so the length does not leak either). */
export function safeEqual(a: string, b: string): boolean {
    if (typeof a !== 'string' || typeof b !== 'string' || !b)
        return false;
    const ha = crypto.createHash('sha256').update(a).digest();
    const hb = crypto.createHash('sha256').update(b).digest();
    return crypto.timingSafeEqual(ha, hb) && a.length === b.length;
}

/** The value as a finite number clamped to [min, max], or the fallback when it is not a number. */
export function clampNum(v: unknown, min: number, max: number, fallback: number): number {
    const n = typeof v === 'number' ? v : typeof v === 'string' && v.trim() !== '' ? Number(v) : NaN;
    if (!Number.isFinite(n))
        return fallback;
    return Math.min(max, Math.max(min, n));
}

/** An integer clamped to [min, max], or the fallback. */
export function clampInt(v: unknown, min: number, max: number, fallback: number): number {
    return Math.round(clampNum(v, min, max, fallback));
}

/** Event ids are opaque base64url strings with a prefix. */
export const EVENT_ID_RE = /^ev_[A-Za-z0-9_-]{22}$/;
/** Scrypted device ids are short numeric strings; accept a slightly wider, still path-safe, set. */
export const DEVICE_ID_RE = /^[A-Za-z0-9_-]{1,32}$/;
/** Playback session ids. */
export const SESSION_ID_RE = /^pb_[A-Za-z0-9_-]{22}$/;

export function validEventId(id: string): boolean {
    return EVENT_ID_RE.test(id);
}

export function validDeviceId(id: string): boolean {
    return DEVICE_ID_RE.test(id);
}

/** A short printable label for a class or label from another plugin: trimmed, control characters removed, at most 64 characters. */
export function cleanLabel(v: unknown, max = 64): string | undefined {
    if (v === undefined || v === null)
        return undefined;
    const s = String(v).replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, max);
    return s || undefined;
}

export function withTimeout<T>(p: Promise<T>, ms: number, what: string): Promise<T> {
    return new Promise<T>((resolve, reject) => {
        const t = setTimeout(() => reject(new Error(`${what} timed out after ${ms} ms`)), ms);
        p.then(v => { clearTimeout(t); resolve(v); }, e => { clearTimeout(t); reject(e); });
    });
}

/** Clock used by every component, injectable in tests. */
export interface Clock {
    now(): number;
    setTimeout(fn: () => void, ms: number): any;
    clearTimeout(t: any): void;
}

export const realClock: Clock = {
    now: () => Date.now(),
    setTimeout: (fn, ms) => { const t = setTimeout(fn, ms); (t as any).unref?.(); return t; },
    clearTimeout: t => clearTimeout(t),
};
