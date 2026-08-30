// GET /api/instances returns { instances: [...] }, not a bare array. Keep the
// response boundary explicit so an unexpected payload cannot crash the entire page.
export function telegramSmsLines(response) {
  const rows = Array.isArray(response) ? response : response?.instances
  if (!Array.isArray(rows)) throw new TypeError('Could not load SIM lines')
  return rows.filter(row => row && !Array.isArray(row) &&
    (typeof row.id === 'string' || (typeof row.id === 'number' && Number.isFinite(row.id))) &&
    String(row.id).trim() && typeof row.iccid === 'string' && row.iccid.trim()
  ).map(row => ({
    id: String(row.id),
    name: typeof row.name === 'string' ? row.name : '',
    iccid: row.iccid,
  }))
}
