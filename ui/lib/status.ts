// Turns the status API's answer (services/status/core.py) into the four steps the page shows.
// A batch moves uploaded -> ingested -> matching -> matched -> notifying -> notified, and any
// step can end in failed. Pure functions, so `node --test` checks every state without a browser.

import { count, duration, plural } from './format.ts';

export type BatchStatus =
  | 'uploaded' | 'ingested' | 'matching' | 'matched' | 'notifying' | 'notified' | 'failed';

export interface Status {
  status: BatchStatus;
  done: boolean;
  error?: string | null;
  email_mode?: string | null;
  rows?: { total: number | null; valid: number | null; rejected: number | null };
  matching?: {
    pairs: number | null;
    by_status: Partial<Record<'matched' | 'llm_matched' | 'llm_rejected' | 'pending_llm', number>>;
    llm_calls: number;
    customers_with_offers?: number | null;
    products?: number | null;
  };
  emails?: Partial<Record<'sent' | 'suppressed' | 'failed' | 'pending', number>> & { to_send?: number | null };
  ms?: Partial<Record<'ingest' | 'wait_for_match' | 'match' | 'notify' | 'total', number | null>>;
}

export type StepId = 'upload' | 'check' | 'match' | 'notify';
export type StepState = 'waiting' | 'active' | 'done' | 'failed';
export interface Step { id: StepId; title: string; detail: string; state: StepState; ms: number | null }

const RANK: Record<Exclude<BatchStatus, 'failed'>, number> = {
  uploaded: 0, ingested: 1, matching: 2, matched: 3, notifying: 4, notified: 5,
};

/** Where a failed batch stopped: the first step whose end time is missing. */
function failedAt(s: Status): StepId {
  if (s.ms?.ingest == null) return 'check';
  if (s.ms?.match == null) return 'match';
  return 'notify';
}

/** The ingest error as a sentence a person can act on. */
export function explain(error: string | null | undefined): string {
  if (!error) return 'Something went wrong while processing this file. Try uploading it again.';
  const missing = /^missing column\(s\): (.+)$/i.exec(error);
  if (missing) return `The file has no ${missing[1]} column. Add it to the first row and upload again.`;
  const text = error.charAt(0).toUpperCase() + error.slice(1);
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

export function eligible(s: Status): number {
  const by = s.matching?.by_status ?? {};
  return (by.matched ?? 0) + (by.llm_matched ?? 0);
}

export function emailLine(s: Status): string {
  const e = s.emails ?? {};
  const sent = e.sent ?? 0;
  const held = (e.suppressed ?? 0) + (e.pending ?? 0);
  const failed = e.failed ?? 0;
  const parts: string[] = [];
  if (sent > 0) parts.push(`${plural(sent, 'email', 'emails')} sent`);
  if (held > 0) {
    parts.push(sent > 0 || s.email_mode === 'test_aliases'
      ? `${count(held)} held back in test mode`
      : `${plural(held, 'email', 'emails')} ready, sending is switched off`);
  }
  if (failed > 0) parts.push(`${count(failed)} failed`);
  return parts.length > 0 ? parts.join(' · ') : 'No customer had an offer to send';
}

export interface UploadProgress { done: boolean; detail: string; ms: number | null }

export function steps(upload: UploadProgress, s: Status | null): Step[] {
  const rank = s && s.status !== 'failed' ? RANK[s.status] : -1;
  const failed = s?.status === 'failed' ? failedAt(s) : null;
  const ms = s?.ms ?? {};
  const order: StepId[] = ['upload', 'check', 'match', 'notify'];
  const state = (id: StepId, doneAt: number, activeFrom: number): StepState => {
    if (failed === id) return 'failed';
    if (failed && order.indexOf(id) > order.indexOf(failed)) return 'waiting';
    if (rank >= doneAt || (failed && order.indexOf(id) < order.indexOf(failed))) return 'done';
    return upload.done && rank >= activeFrom ? 'active' : 'waiting';
  };
  const rows = s?.rows;
  const offers = s?.matching?.customers_with_offers;
  const products = s?.matching?.products;

  const check = state('check', 1, 0);
  const match = state('match', 3, 1);
  const notify = state('notify', 5, 3);
  const matchMs = ms.match == null ? null : (ms.wait_for_match ?? 0) + ms.match;
  return [
    { id: 'upload', title: 'Upload', detail: upload.detail, state: upload.done ? 'done' : 'active', ms: upload.ms },
    {
      id: 'check', title: 'Check every row', state: check, ms: check === 'done' ? ms.ingest ?? null : null,
      detail: check === 'failed' ? explain(s?.error)
        : check === 'done' && rows?.valid != null
          ? `${plural(rows.valid, 'customer', 'customers')} ready${rows.rejected ? ` · ${plural(rows.rejected, 'row', 'rows')} skipped` : ''}`
          : check === 'active' ? 'Reading the file and checking each value' : 'Waiting for the upload',
    },
    {
      id: 'match', title: 'Match loans', state: match, ms: match === 'done' ? matchMs : null,
      detail: match === 'failed' ? explain(s?.error)
        : match === 'done' && s
          ? [offers != null ? `${count(offers)} meet a lender's criteria` : null,
             `${plural(eligible(s), 'loan match', 'loan matches')}${products ? ` across ${products} products` : ''}`]
              .filter(Boolean).join(' · ')
          : match === 'active' ? "Comparing each customer with every lender's criteria" : 'Next, after the check',
    },
    {
      id: 'notify', title: 'Notify customers', state: notify, ms: notify === 'done' ? ms.notify ?? null : null,
      detail: notify === 'failed' ? explain(s?.error)
        : notify === 'done' && s ? emailLine(s)
          : notify === 'active' ? 'Writing each customer an email with their offers' : 'Last, an email per customer',
    },
  ];
}

export interface Summary { customers: number; skipped: number; withOffers: number | null; matches: number; products: number | null }

export function summary(s: Status): Summary {
  return {
    customers: s.rows?.valid ?? 0,
    skipped: s.rows?.rejected ?? 0,
    withOffers: s.matching?.customers_with_offers ?? null,
    matches: eligible(s),
    products: s.matching?.products ?? null,
  };
}

export function stepTime(step: Step): string {
  return step.ms == null ? '' : duration(step.ms);
}

export interface FunnelStage { label: string; value: number; note: string }

/** The optimisation funnel for one batch, from numbers the status call already returns.
 *  An AI-only design would judge every (customer, product) pair; the SQL filter keeps the pairs
 *  that pass the published thresholds; only pairs with a vague criterion need judgement, and
 *  those are judged per cohort and cached, so the batch itself usually makes no AI call. */
export function funnel(s: Status): FunnelStage[] | null {
  const customers = s.rows?.valid;
  const products = s.matching?.products;
  const pairs = s.matching?.pairs;
  if (customers == null || products == null || pairs == null) return null;
  const by = s.matching?.by_status ?? {};
  const judged = (by.llm_matched ?? 0) + (by.llm_rejected ?? 0) + (by.pending_llm ?? 0);
  const calls = s.matching?.llm_calls ?? 0;
  return [
    { label: 'Possible checks', value: customers * products,
      note: `${count(customers)} customers × ${products} products: what an AI-only design would judge` },
    { label: 'Pass the published criteria', value: pairs,
      note: 'Income, credit score, age and employment, compared in one database query' },
    { label: 'Need a judgement call', value: judged,
      note: judged ? 'Products with a vague rule, grouped into a few customer cohorts' : 'No product had a vague rule for these customers' },
    { label: 'AI calls for this upload', value: calls,
      note: calls ? 'Each call judges many cohorts at once, then the answers are cached' : 'Every cohort was judged after the last crawl and cached' },
  ];
}
