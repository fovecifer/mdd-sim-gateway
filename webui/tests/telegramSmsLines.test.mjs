import test from 'node:test'
import assert from 'node:assert/strict'
import { telegramSmsLines } from '../src/telegramSmsLines.js'

const line = { id: '1', name: 'Test SIM', iccid: '8900000000000000001' }

test('uses the real GET /api/instances envelope when SMS control is enabled', () => {
  assert.deepEqual(telegramSmsLines({ instances: [line] }), [line])
})

test('also accepts a legacy bare array', () => {
  assert.deepEqual(telegramSmsLines([line]), [line])
})

test('empty instances are a valid empty list', () => {
  assert.deepEqual(telegramSmsLines({ instances: [] }), [])
})

test('unexpected response types become a caught load error, not render-time filter errors', () => {
  for (const value of [null, undefined, {}, { instances: null }, { instances: {} },
    { instances: 'bad' }, { detail: 'authentication required' }, 'html']) {
    assert.throws(() => telegramSmsLines(value), /Could not load SIM lines/)
  }
})

test('ineligible or malformed entries cannot crash option rendering', () => {
  assert.deepEqual(telegramSmsLines({ instances: [null, [], {}, false, 'bad',
    { id: '2', name: 'No card' }, { id: {}, iccid: 'test' },
    { id: Infinity, iccid: 'test' }, { id: '3', iccid: '' }, line] }), [line])
})

test('numeric IDs and non-string names normalize to renderable values', () => {
  assert.deepEqual(telegramSmsLines({ instances: [{ id: 1, name: {}, iccid: line.iccid }] }),
    [{ id: '1', name: '', iccid: line.iccid }])
})
