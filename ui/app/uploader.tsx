'use client';

// The upload card: choose or drop a CSV, check it in the browser, send it straight to S3, then
// follow the batch through checking, matching and notifying until it is done.
//
// Speed matters because the API is in Sydney (about 390 ms a round trip from India): the
// presign request starts while the file is still being checked, connections are opened when the
// page loads, and the status is polled quickly at first and less often as a batch runs longer.

import { useCallback, useEffect, useRef, useState, type ChangeEvent, type DragEvent } from 'react';
import { canCompress, compress, fetchStatus, preconnect, prefetchTarget, presign, takeTarget, upload, UploadError } from '../lib/api.ts';
import { checkHead, HEAD_BYTES, REQUIRED } from '../lib/csv.ts';
import { bytes, count, duration, percent } from '../lib/format.ts';
import { explain, funnel, steps, stepTime, summary, type Status, type Step, type StepId } from '../lib/status.ts';
import { IconAlert, IconCheck, IconFile, IconUpload } from './icons';

interface Picked { file: File; rows: number; exact: boolean }

type View =
  | { phase: 'idle'; error?: string; retry?: File }
  | { phase: 'uploading'; picked: Picked; sent: number; total: number; zipped: boolean; startedAt: number }
  | { phase: 'tracking'; picked: Picked; key: string; status: Status | null; startedAt: number; uploadMs: number; sentBytes: number; reconnecting: boolean }
  | { phase: 'finished'; picked: Picked; key: string; status: Status; startedAt: number; uploadMs: number; sentBytes: number; finishedAt: number };

export default function Uploader() {
  const [view, setView] = useState<View>({ phase: 'idle' });
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const inFlight = useRef<AbortController | null>(null);

  useEffect(() => {
    preconnect();
    prefetchTarget(canCompress());
    const refresh = window.setInterval(() => prefetchTarget(canCompress()), 60_000);
    return () => window.clearInterval(refresh);
  }, []);
  const warm = () => { preconnect(); prefetchTarget(canCompress()); };

  // A file dropped beside the drop zone must not make the browser open it and leave the page.
  useEffect(() => {
    const keep = (event: globalThis.DragEvent) => {
      if (event.dataTransfer?.types.includes('Files')) event.preventDefault();
    };
    window.addEventListener('dragover', keep);
    window.addEventListener('drop', keep);
    return () => {
      window.removeEventListener('dragover', keep);
      window.removeEventListener('drop', keep);
    };
  }, []);

  const start = useCallback(async (file: File) => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;
    // Presign and compression overlap the header check: from India each round trip to Sydney
    // costs ~0.4 s, and a gzipped CSV uploads about 4x faster.
    const gzip = canCompress();
    const target = takeTarget(file, gzip, controller.signal);
    const packed = gzip ? compress(file) : Promise.resolve(null);
    target.catch(() => undefined); // awaited below; this only silences an early, unused failure
    const check = checkHead(file.name, file.size, await file.slice(0, HEAD_BYTES).text());
    if (!check.ok) {
      controller.abort();
      setView({ phase: 'idle', error: check.error });
      return;
    }
    const picked: Picked = { file, rows: check.rows, exact: check.exact };
    const startedAt = performance.now();
    setView({ phase: 'uploading', picked, sent: 0, total: file.size, zipped: false, startedAt });
    try {
      let destination = await target;
      const zipped = await packed;
      if (gzip && !zipped) destination = await presign(file, false, controller.signal); // plain after all
      const body: Blob = zipped ?? file;
      setView((v) => (v.phase === 'uploading' && v.picked === picked ? { ...v, total: body.size, zipped: Boolean(zipped) } : v));
      await upload(destination, body, file.name, (sent) => {
        setView((v) => (v.phase === 'uploading' && v.picked === picked ? { ...v, sent } : v));
      }, controller.signal);
      setView({
        phase: 'tracking', picked, key: destination.fields.key ?? '', status: null, startedAt,
        uploadMs: performance.now() - startedAt, sentBytes: body.size, reconnecting: false,
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      const message = error instanceof UploadError ? error.message : 'The upload failed. Try again.';
      setView({ phase: 'idle', error: message, retry: file });
    }
  }, []);

  const trackingKey = view.phase === 'tracking' ? view.key : null;
  useEffect(() => {
    if (!trackingKey) return;
    const controller = new AbortController();
    const began = performance.now();
    let timer = 0;
    let misses = 0;
    const later = () => {
      const age = performance.now() - began;
      const wait = age < 5_000 ? 250 : age < 30_000 ? 700 : 2_000;
      timer = window.setTimeout(poll, wait * Math.min(4, 1 + misses));
    };
    const poll = async () => {
      try {
        const status = await fetchStatus(trackingKey, controller.signal);
        misses = 0;
        setView((v) => {
          if (v.phase !== 'tracking' || v.key !== trackingKey) return v;
          if (!status.done) return { ...v, status, reconnecting: false };
          return { phase: 'finished', picked: v.picked, key: v.key, status, startedAt: v.startedAt,
                   uploadMs: v.uploadMs, sentBytes: v.sentBytes, finishedAt: performance.now() };
        });
        if (!status.done) later();
      } catch {
        if (controller.signal.aborted) return;
        misses += 1;
        if (misses >= 3) {
          setView((v) => (v.phase === 'tracking' && v.key === trackingKey ? { ...v, reconnecting: true } : v));
        }
        later();
      }
    };
    void poll();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [trackingKey]);

  const reset = () => {
    inFlight.current?.abort();
    setView({ phase: 'idle' });
  };

  const choose = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = ''; // so choosing the same file again still fires a change
    if (file) void start(file);
  };

  const drop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    const file = event.dataTransfer.files[0];
    if (file) void start(file);
  };

  return (
    <section className="card" aria-label="Upload a customer list">
      {view.phase === 'idle' ? (
        <div className="idle">
          <div
            className={`drop${dragging ? ' is-over' : ''}`}
            onDragEnter={(e) => { e.preventDefault(); if (!dragging) warm(); setDragging(true); }}
            onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; }}
            onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false); }}
            onDrop={drop}
          >
            <span className="drop-icon"><IconUpload /></span>
            <p className="drop-title">{dragging ? 'Drop to check eligibility' : 'Drop your customer list here'}</p>
            <p className="drop-sub">A CSV file with one row per customer, up to 50 MB</p>
            <button type="button" className="btn btn-primary" onPointerDown={warm} onClick={() => input.current?.click()}>
              Choose a CSV file
            </button>
            <input ref={input} id="csv-file" type="file" accept=".csv,text/csv" hidden onChange={choose} />
            <p className="drop-sample">No file handy? <a href="/sample-users.csv" download>Download a sample CSV</a></p>
          </div>
          {view.error ? (
            <div className="notice is-error" role="alert">
              <IconAlert />
              <span>{view.error}</span>
              {view.retry ? (
                <button type="button" className="text-btn" onClick={() => view.retry && void start(view.retry)}>Try again</button>
              ) : null}
            </div>
          ) : null}
          <div className="columns" aria-label="Required columns">
            <span className="columns-label">Required columns</span>
            {REQUIRED.map((c) => <code key={c}>{c}</code>)}
          </div>
        </div>
      ) : (
        <Run view={view} onCancel={reset} onAgain={reset} />
      )}
    </section>
  );
}

function Run({ view, onCancel, onAgain }: { view: Exclude<View, { phase: 'idle' }>; onCancel: () => void; onAgain: () => void }) {
  const { file, rows, exact } = view.picked;
  const status = view.phase === 'uploading' ? null : view.status;
  const uploading = view.phase === 'uploading';
  const list = steps({
    done: !uploading,
    detail: uploading
      ? view.sent === 0
        ? view.zipped ? `Compressed to ${bytes(view.total)}, connecting` : 'Connecting'
        : `${percent(view.sent, view.total)} · ${bytes(view.sent)} of ${bytes(view.total)}${view.zipped ? ' (compressed)' : ''}`
      : view.sentBytes < file.size ? `${bytes(file.size)} sent as ${bytes(view.sentBytes)}` : `${bytes(file.size)} sent`,
    ms: uploading ? null : view.uploadMs,
  }, status);
  const active = list.find((s) => s.state === 'active');
  // When each step started waiting, so the active one can show its own running time.
  const since = useRef<Partial<Record<StepId, number>>>({});
  for (const step of list) {
    if (step.state === 'active' && since.current[step.id] === undefined) since.current[step.id] = performance.now();
  }

  return (
    <div className="run">
      <div className="file-row">
        <span className="file-icon"><IconFile /></span>
        <div className="file-meta">
          <p className="file-name" title={file.name}>{file.name}</p>
          <p className="file-sub">{bytes(file.size)} · {exact ? '' : 'about '}{count(rows)} rows</p>
        </div>
        {view.phase === 'finished' ? (
          <button type="button" className="btn btn-secondary" onClick={onAgain}>Upload another file</button>
        ) : view.phase === 'uploading' ? (
          <button type="button" className="btn btn-quiet" onClick={onCancel}>Cancel</button>
        ) : (
          <Elapsed since={view.startedAt} />
        )}
      </div>

      {uploading ? (
        <div className={`bar${view.sent === 0 ? ' is-waiting' : ''}`} role="progressbar" aria-label="Upload progress"
             aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round((view.sent / Math.max(1, view.total)) * 100)}>
          <span style={view.sent === 0 ? undefined : { width: `${(view.sent / Math.max(1, view.total)) * 100}%` }} />
        </div>
      ) : null}

      {view.phase === 'finished' ? <Result status={view.status} totalMs={view.finishedAt - view.startedAt} /> : null}

      <ol className="steps">
        {list.map((step) => <StepRow key={step.id} step={step} since={since.current[step.id]} />)}
      </ol>

      {view.phase === 'tracking' && view.reconnecting ? (
        <p className="notice is-warn" role="status">The connection dropped. Reconnecting…</p>
      ) : null}
      <p className="sr-only" aria-live="polite">
        {view.phase === 'finished' ? 'Finished.' : active ? `${active.title}: ${active.detail}` : ''}
      </p>
    </div>
  );
}

function Result({ status, totalMs }: { status: Status; totalMs: number }) {
  if (status.status === 'failed') {
    return (
      <div className="result is-failed">
        <span className="result-mark"><IconAlert /></span>
        <div>
          <h2>This file couldn't be processed</h2>
          <p>{explain(status.error)}</p>
        </div>
      </div>
    );
  }
  const s = summary(status);
  const offers = s.withOffers ?? s.customers;
  return (
    <div className="result">
      <div className="result-head">
        <span className="result-mark"><IconCheck /></span>
        <div>
          <h2>{offers === s.customers ? `All ${count(offers)}` : `${count(offers)} of ${count(s.customers)}`} customers meet at least one lender&apos;s published criteria</h2>
          <p>Checked against the published criteria of {s.products ?? 'every'} loan products in {duration(totalMs)}. A match means a customer may qualify; it is not a loan approval.</p>
        </div>
      </div>
      <dl className="stats">
        <div><dt>Customers checked</dt><dd>{count(s.customers)}</dd><span>{s.skipped ? `${count(s.skipped)} rows skipped` : 'no rows skipped'}</span></div>
        <div><dt>Have a match</dt><dd>{count(offers)}</dd><span>{percent(offers, s.customers)} of customers</span></div>
        <div><dt>Loan matches</dt><dd>{count(s.matches)}</dd><span>{offers ? `${(s.matches / offers).toFixed(1)} products each` : '—'}</span></div>
        <div><dt>Total time</dt><dd>{duration(totalMs)}</dd><span>from upload to finish</span></div>
      </dl>
      <Funnel status={status} />
    </div>
  );
}

// How the checks were narrowed down. Bars are to scale; a stage that isn't zero keeps a sliver.
function Funnel({ status }: { status: Status }) {
  const stages = funnel(status);
  if (!stages) return null;
  const top = Math.max(1, stages[0]?.value ?? 1);
  const width = (v: number) => (v <= 0 ? 0 : Math.max(1.2, (v / top) * 100));
  return (
    <section className="funnel" aria-labelledby="funnel-title">
      <h3 id="funnel-title">How {count(stages[0]?.value ?? 0)} possible checks were narrowed down</h3>
      <ol>
        {stages.map((stage, i) => (
          <li key={stage.label} className={i === stages.length - 1 ? 'is-last' : undefined}>
            <div className="funnel-row">
              <span className="funnel-label">{stage.label}</span>
              <span className="funnel-value">{count(stage.value)}</span>
            </div>
            <span className="funnel-bar" aria-hidden="true"><span style={{ width: `${width(stage.value)}%` }} /></span>
            <span className="funnel-note">{stage.note}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function StepRow({ step, since }: { step: Step; since: number | undefined }) {
  const waiting = step.state === 'active';
  return (
    <li className={`step is-${step.state}`}>
      <span className="step-mark" aria-hidden="true">
        {step.state === 'done' ? <IconCheck /> : step.state === 'failed' ? '!' : null}
      </span>
      <div className="step-text">
        <p className="step-title">{step.title}</p>
        <p className="step-detail">{step.detail}</p>
        {waiting && step.id !== 'upload' ? <span className="step-wait" aria-hidden="true"><span /></span> : null}
      </div>
      <span className="step-time">{waiting && since !== undefined ? <Elapsed since={since} /> : stepTime(step)}</span>
    </li>
  );
}

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(id);
  }, []);
  return <span className="elapsed" aria-hidden="true">{duration(now - since)}</span>;
}
