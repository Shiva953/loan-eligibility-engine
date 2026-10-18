// Numbers as an Indian reader writes them: 1,00,000 rather than 100,000.
const grouped = new Intl.NumberFormat('en-IN');

export function count(n: number): string {
  return grouped.format(n);
}

export function plural(n: number, one: string, many: string): string {
  return `${count(n)} ${n === 1 ? one : many}`;
}

export function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10 * 1024 ? 1 : 0)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function duration(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  return `${minutes} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function percent(part: number, whole: number): string {
  if (whole <= 0) return '0%';
  const value = (part / whole) * 100;
  return `${value >= 99.5 || value < 1 ? Math.round(value) : value.toFixed(value < 10 ? 1 : 0)}%`;
}
