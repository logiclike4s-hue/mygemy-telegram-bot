import test from 'node:test';
import assert from 'node:assert/strict';
import {
  openAppCommands,
  runCommandActions
} from '../src/windows.js';

test('application allowlist contains only fixed executable names', () => {
  assert.equal(openAppCommands.calculator[0], 'calc.exe');
  assert.equal(openAppCommands.browser[0], 'explorer.exe');
  assert.equal(openAppCommands.unknown, undefined);
});

test('run allowlist contains fixed read-only commands', () => {
  assert.deepEqual(Object.keys(runCommandActions).sort(), ['ipconfig', 'systeminfo', 'tasklist']);
});
