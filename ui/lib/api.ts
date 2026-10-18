// The page's three calls: ask for an upload target, send the file straight to it, and poll the
// batch's progress. /config.js decides where they go: `make ui`'s local stand-ins, or the
// deployed Function URLs and the S3 bucket. Browser-only.

import type { Status } from './status.ts';

export interface Config { presignUrl: string; statusUrl: string; uploadOrigin: string; target?: 'local' | 'aws'; region?: string }
export interface Target { url: string; fields: Record<string, string> }

declare global {
  interface Window { APP_CONFIG?: Partial<Config> }
}

export function config(): Config {
  return { presignUrl: '/api/presign', statusUrl: '/api/status', uploadOrigin: '', ...window.APP_CONFIG };
}

/** Open the TLS connections early: from India each new one to Sydney costs two round trips. */
/** Browsers drop an unused preconnection after about 10 s, so call this again on intent
 *  (a file dragged over the page, the button pressed): the handshake then overlaps the
 *  file dialog instead of the upload. */
export function preconnect(): void {
  const c = config();
  for (const url of [c.presignUrl, c.statusUrl, c.uploadOrigin]) {
    if (!url) continue;
    const origin = new URL(url, window.location.href).origin;
    if (origin === window.location.origin) continue;
    document.querySelector(`link[rel="preconnect"][href="${origin}"]`)?.remove();
    const link = document.createElement('link');
    link.rel = 'preconnect';
    link.href = origin;
    link.crossOrigin = 'anonymous';
    document.head.append(link);
  }
}

export class UploadError extends Error {}

/** Gzip the CSV in the browser: it shrinks about 4x, and the upload to Sydney is the slowest step. */
export async function compress(file: File): Promise<Blob | null> {
  if (typeof CompressionStream === 'undefined') return null;
  try {
    return await new Response(file.stream().pipeThrough(new CompressionStream('gzip'))).blob();
  } catch {
    return null;
  }
}

export const canCompress = (): boolean => typeof CompressionStream !== 'undefined';

// An upload link fetched ahead of time, so dropping a file starts the upload at once instead
// of after a round trip to Sydney. Links last 5 minutes; one older than 4 is fetched again.
// An unused link costs nothing: no object exists until a file is posted with it.
let ready: { gzip: boolean; at: number; target: Promise<Target> } | null = null;

export function prefetchTarget(gzip: boolean): void {
  if (ready && ready.gzip === gzip && performance.now() - ready.at < 240_000) return;
  const target = presign(new File([''], 'pending.csv'), gzip, undefined, 1);
  target.catch(() => { ready = null; });
  ready = { gzip, at: performance.now(), target };
}

/** The prefetched link if it is still fresh (each is used once), otherwise a new one. */
export function takeTarget(file: File, gzip: boolean, signal?: AbortSignal): Promise<Target> {
  const fresh = ready && ready.gzip === gzip && performance.now() - ready.at < 240_000 ? ready.target : null;
  ready = null;
  return fresh ? fresh.catch(() => presign(file, gzip, signal)) : presign(file, gzip, signal);
}

/** A presigned POST for one new key. text/plain keeps it a CORS simple request (no preflight). */
export async function presign(file: File, gzip: boolean, signal?: AbortSignal, size = file.size): Promise<Target> {
  let response: Response;
  try {
    response = await fetch(config().presignUrl, {
      method: 'POST',
      headers: { 'Content-Type': 'text/plain' },
      body: JSON.stringify({ filename: file.name, size, gzip }),
      signal,
    });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new UploadError("Couldn't reach the upload service. Check your connection and try again.");
  }
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as { error?: string };
    throw new UploadError(body.error ?? `The upload service answered ${response.status}. Try again in a moment.`);
  }
  return (await response.json()) as Target;
}

/** POST the file with the target's fields (file last, as S3 requires), reporting progress. */
export function upload(target: Target, body: Blob, filename: string, onProgress: (sent: number) => void, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    for (const [name, value] of Object.entries(target.fields)) form.append(name, value);
    form.append('file', body, filename);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', target.url);
    xhr.upload.onprogress = (event) => onProgress(event.lengthComputable ? event.loaded : 0);
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) return resolve();
      const message = /<Message>([^<]+)<\/Message>/.exec(xhr.responseText)?.[1];
      reject(new UploadError(message ? `The upload was refused: ${message}` : `The upload failed (HTTP ${xhr.status}). Try again.`));
    };
    xhr.onerror = () => reject(new UploadError('The upload stopped. Check your connection and try again.'));
    xhr.onabort = () => reject(new DOMException('Upload cancelled', 'AbortError'));
    signal.addEventListener('abort', () => xhr.abort(), { once: true });
    xhr.send(form);
  });
}

export async function fetchStatus(key: string, signal: AbortSignal): Promise<Status> {
  const response = await fetch(`${config().statusUrl}?key=${encodeURIComponent(key)}`, { cache: 'no-store', signal });
  if (!response.ok) throw new Error(`status ${response.status}`);
  return (await response.json()) as Status;
}
