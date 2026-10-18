// Runs an n8n Code-node file outside n8n, the way n8n does: the file body becomes an async
// function receiving $input, $ and require. Tests and the bench use this, so they execute
// exactly the JavaScript that build_workflows.py inlines into the workflows.
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const require = createRequire(import.meta.url);
export const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');

export async function runCodeNode(file, items, { nodes = {}, execution = { id: 'test' }, runIndex = 0 } = {}) {
  const code = readFileSync(path.join(REPO_ROOT, 'n8n', 'code', file), 'utf8');
  const wrap = (json) => ({ json });
  const $input = { all: () => items.map(wrap), first: () => wrap(items[0]) };
  // A node is given either as its latest output (an object or a list of items), or as
  // { runs: [[output 0 items, output 1 items, ...], ...] } for code that reads a specific run and
  // branch with $('Node').all(branch, run), as n8n allows. A missing run throws, as in n8n.
  const $ = (name) => {
    if (!(name in nodes)) throw new Error(`node ${name} not provided to harness`);
    const given = nodes[name];
    if (given && Array.isArray(given.runs)) {
      const output = (branch = 0, run = given.runs.length - 1) => {
        if (run < 0 || run >= given.runs.length) throw new Error(`run ${run} of node "${name}" not found`);
        return (given.runs[run][branch] ?? []).map(wrap);
      };
      return { first: () => output()[0], all: output, item: output()[0] };
    }
    const list = Array.isArray(given) ? given : [given];
    return { first: () => wrap(list[0]), all: () => list.map(wrap), item: wrap(list[0]) };
  };
  // $runIndex is the number of times this node already ran in the execution (retry loops).
  const fn = new AsyncFunction('$input', '$', 'require', '$execution', '$runIndex', code);
  const out = await fn($input, $, require, execution, runIndex);
  return out.map((item) => item.json);
}
