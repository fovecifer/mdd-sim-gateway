import test from 'node:test'
import assert from 'node:assert/strict'
import { telegramSmsLines, telegramSmsSelection, toggleTelegramSmsLine } from '../src/telegramSmsLines.js'

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

test('blank-card drafts cannot be selected as real SIMs', () => {
  assert.deepEqual(telegramSmsLines({instances: [line, {...line, id: '2', provisioning_state: 'draft'}]}), [line])
})

test('legacy selection is displayed and explicit multi-selection wins', () => {
  assert.deepEqual(telegramSmsSelection({instance_id: '1'}), ['1'])
  assert.deepEqual(telegramSmsSelection({instance_id: '1', instance_ids: ['3']}), ['3'])
  assert.deepEqual(telegramSmsSelection({instance_id: '1', instance_ids: []}), [])
  assert.deepEqual(telegramSmsSelection({instance_id: '1', instance_ids: 'bad'}), [])
})

test('multi-checkbox updates preserve other selected cards', () => {
  assert.deepEqual(toggleTelegramSmsLine(['1'], '3', true), ['1', '3'])
  assert.deepEqual(toggleTelegramSmsLine(['1', '3'], '1', false), ['3'])
  assert.deepEqual(toggleTelegramSmsLine(['1', '3'], '3', true), ['1', '3'])
})
