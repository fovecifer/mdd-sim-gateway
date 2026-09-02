// GET /api/instances returns { instances: [...] }, not a bare array. Keep the
// response boundary explicit so an unexpected payload cannot crash the entire page.
export function telegramSmsLines(response) {
  const rows = Array.isArray(response) ? response : response?.instances
  if (!Array.isArray(rows)) throw new TypeError('Could not load SIM lines')
  return rows.filter(row => row && !Array.isArray(row) &&
    (typeof row.id === 'string' || (typeof row.id === 'number' && Number.isFinite(row.id))) &&
    String(row.id).trim() && typeof row.iccid === 'string' && row.iccid.trim() &&
    row.provisioning_state !== 'draft'
  ).map(row => ({
    id: String(row.id),
    name: typeof row.name === 'string' ? row.name : '',
    iccid: row.iccid,
  }))
}

// A present but empty multi-selection never falls back to a stale single-line field.
export function telegramSmsSelection(control = {}) {
  if (Object.hasOwn(control, 'instance_ids')) {
    return Array.isArray(control.instance_ids)
      ? [...new Set(control.instance_ids.filter(id => typeof id === 'string' && /^[1-9]\d{0,9}$/.test(id)))]
      : []
  }
  const id = String(control.instance_id || '')
  return /^[1-9]\d{0,9}$/.test(id) ? [id] : []
}

export function toggleTelegramSmsLine(selected, id, checked) {
  return [...new Set(checked ? [...selected, id] : selected.filter(value => value !== id))]
    .sort((a, b) => Number(a) - Number(b))
}
