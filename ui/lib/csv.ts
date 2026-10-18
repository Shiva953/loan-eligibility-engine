// Checks a CSV in the browser before it is uploaded, by the same rules the ingest Lambda uses
// (services/ingest/core.py): the first row names the seven required columns, in any order,
// ignoring case, spaces, quotes and a byte-order mark; other columns are allowed. Only the first
// 64 KB is read, so a 50 MB file is checked instantly. Rows are counted exactly when the whole
// file fits in that slice, and estimated from the average line length otherwise.

import { bytes } from './format.ts';

export const REQUIRED = [
  'user_id', 'name', 'email', 'monthly_income', 'credit_score', 'employment_status', 'age',
] as const;
export const MAX_BYTES = 50 * 1024 * 1024;
export const HEAD_BYTES = 64 * 1024;

export type HeadCheck =
  | { ok: true; rows: number; exact: boolean }
  | { ok: false; error: string };

/** The names in a header line, as ingest reads them. */
export function headerNames(line: string): string[] {
  const names: string[] = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"' && line[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { names.push(field); field = ''; }
    else field += c;
  }
  names.push(field);
  return names.map((n) => n.replace(/^﻿/, '').trim().toLowerCase());
}

function list(items: readonly string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** Whether a file can be uploaded, from its name, size and first HEAD_BYTES as text. */
export function checkHead(name: string, size: number, head: string): HeadCheck {
  if (!/\.csv$/i.test(name)) return { ok: false, error: 'Choose a .csv file.' };
  if (size === 0) return { ok: false, error: 'This file is empty.' };
  if (size > MAX_BYTES) {
    return { ok: false, error: `This file is ${bytes(size)}. Files can be up to 50 MB.` };
  }
  const end = head.search(/\r?\n/);
  const whole = size <= HEAD_BYTES;
  if (end === -1 && !whole) return { ok: false, error: "The first row is too long to be a header." };
  const names = headerNames(end === -1 ? head : head.slice(0, end));
  const missing = REQUIRED.filter((c) => !names.includes(c));
  if (missing.length > 0) {
    const noun = missing.length === 1 ? 'column' : 'columns';
    return { ok: false, error: `This file has no ${list(missing)} ${noun}. The first row needs all seven columns listed below.` };
  }
  const lines = head.split(/\r?\n/).filter((l) => l.trim() !== '').length;  // ingest skips blank rows
  if (whole) return { ok: true, rows: Math.max(0, lines - 1), exact: true };
  const complete = head.slice(0, head.lastIndexOf('\n') + 1);
  const perLine = new TextEncoder().encode(complete).length / Math.max(1, complete.split('\n').length - 1);
  return { ok: true, rows: Math.max(1, Math.round(size / perLine) - 1), exact: false };
}
