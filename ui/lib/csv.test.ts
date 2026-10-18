import assert from 'node:assert/strict';
import { test } from 'node:test';
import { checkHead, headerNames, HEAD_BYTES, MAX_BYTES } from './csv.ts';

const HEADER = 'user_id,name,email,monthly_income,credit_score,employment_status,age';
const ROW = '3f1c2b9e-0d4a-4e7b-9c1a-2b3c4d5e6f70,Asha Rao,asha@example.com,52000,742,Salaried,31';

test('header names are read as ingest reads them: BOM, case, spaces and quotes ignored', () => {
  assert.deepEqual(headerNames('﻿ User_ID ,"Name","e,mail"'), ['user_id', 'name', 'e,mail']);
});

test('a small file is counted exactly, blank lines and a trailing newline ignored', () => {
  const text = `${HEADER}\r\n${ROW}\r\n\r\n${ROW}\r\n`;
  assert.deepEqual(checkHead('users.csv', text.length, text), { ok: true, rows: 2, exact: true });
});

test('columns may come in any order, with extras', () => {
  const text = `age,notes,email,user_id,employment_status,credit_score,monthly_income,name\n${ROW}\n`;
  assert.equal(checkHead('u.CSV', text.length, text).ok, true);
});

test('a missing column is named, before anything is uploaded', () => {
  const text = 'user_id,name,monthly_income,credit_score,employment_status\n1,a,1,1,x\n';
  const result = checkHead('u.csv', text.length, text);
  assert.equal(result.ok, false);
  assert.match(!result.ok ? result.error : '', /no email and age columns/);
});

test('wrong type, empty and oversized files are refused with the reason', () => {
  assert.deepEqual(checkHead('users.xlsx', 10, 'x'), { ok: false, error: 'Choose a .csv file.' });
  assert.deepEqual(checkHead('users.csv', 0, ''), { ok: false, error: 'This file is empty.' });
  const big = checkHead('users.csv', MAX_BYTES + 1, HEADER);
  assert.match(!big.ok ? big.error : '', /up to 50 MB/);
});

test('a large file has its rows estimated from the average line length', () => {
  const lines = [HEADER, ...Array.from({ length: 800 }, () => ROW)];
  const head = `${lines.join('\n')}\n`.slice(0, HEAD_BYTES);
  const size = (ROW.length + 1) * 100_000 + HEADER.length + 1;
  const result = checkHead('big.csv', size, head);
  assert.equal(result.ok && !result.exact, true);
  const rows = result.ok ? result.rows : 0;
  assert.ok(Math.abs(rows - 100_000) / 100_000 < 0.02, `estimated ${rows}`);
});
