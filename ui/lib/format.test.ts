import assert from 'node:assert/strict';
import { test } from 'node:test';
import { bytes, count, duration, percent } from './format.ts';

test('formats', () => {
  assert.equal(count(100000), '1,00,000');
  assert.equal(bytes(9_929_324), '9.5 MB');
  assert.equal(bytes(2_300), '2.2 KB');
  assert.equal(duration(693), '693 ms');
  assert.equal(duration(1450), '1.4 s');
  assert.equal(duration(125_000), '2 min 5 s');
  assert.equal(percent(1905, 1905), '100%');
  assert.equal(percent(98255, 100000), '98%');
});
