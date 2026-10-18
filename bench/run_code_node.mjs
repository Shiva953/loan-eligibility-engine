// Run one n8n Code-node file outside n8n, the way n8n runs it, for the Python bench.
// Usage: node bench/run_code_node.mjs <file.js> <input.json>
// input.json: { "items": [...], "nodes": { "<node name>": {...} | [...] }, "runIndex": 0 }
// Prints the node's output items (their json) as a JSON array.
import { readFileSync } from 'node:fs';
import { runCodeNode } from '../tests/js/harness.mjs';

const [file, input] = process.argv.slice(2);
const { items = [], nodes = {}, runIndex = 0 } = JSON.parse(readFileSync(input, 'utf8'));
const out = await runCodeNode(file, items, { nodes, runIndex });
process.stdout.write(JSON.stringify(out));
