import React, { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useI18n } from '../i18n.jsx'
import { telegramSmsLines, telegramSmsSelection, toggleTelegramSmsLine } from '../telegramSmsLines.js'

const STATES = {
  disabled: 'Disabled', connected: 'Connected', invalid_token: 'Invalid bot token',
  bot_blocked: 'Bot blocked', poll_conflict: 'Another poller or webhook is active',
  rate_limited: 'Telegram rate limit', connection_error: 'Connection failed',
  telegram_error: 'Telegram API error', internal_error: 'Local processing error',
  another_worker: 'Handled by another server worker',
}

export default function TelegramSmsSettings({ config, onChange }) {
  const { t } = useI18n()
  const [lines, setLines] = useState([])
  const [health, setHealth] = useState(null)
  const [loadError, setLoadError] = useState('')
  const c = config.sms_control || {}
  const selected = telegramSmsSelection(c)
  const choices = [...lines, ...selected.filter(id => !lines.some(line => line.id === id))
    .map(id => ({ id, name: t('Unavailable line') }))]
  const change = patch => onChange({ sms_control: { ...c, ...patch } })
  useEffect(() => {
    let alive = true
    api.instances().then(telegramSmsLines).then(x => { if (alive) setLines(x) }).catch(() => {
      if (alive) { setLines([]); setLoadError('Could not load SIM lines') }
    })
    const refresh = () => api.telegramStatus().then(x => {
      if (alive) setHealth(x)
    }).catch(() => { if (alive) setHealth(null) })
    refresh()
    const timer = setInterval(refresh, 5000)
    return () => { alive = false; clearInterval(timer) }
  }, [])
  return <section className="u-tg-sms" aria-label={t('Two-way SMS')}>
    <h3>{t('Two-way SMS')}</h3>
    <p>{t('Self-use fork feature. Private chat, one owner, up to five authorized SIMs, VoWiFi only. Every send requires confirmation.')}</p>
    <label><input type="checkbox" className="u-toggle" checked={!!c.enabled}
      onChange={e => change({ enabled: e.target.checked })} />{t('Allow Telegram SMS commands')}</label>
    {c.enabled && <>
      {!config.enabled && <p role="status">{t('Enable the Telegram channel above to start receiving commands.')}</p>}
      <label htmlFor="tg-sms-owner">{t('Authorized numeric user ID')}</label>
      <input id="tg-sms-owner" inputMode="numeric" autoComplete="off" value={c.owner_id || ''}
        onChange={e => change({ owner_id: e.target.value })} />
      <p>{t('Must equal the private Chat ID above. Usernames and group chats are not allowed.')}</p>
      <fieldset className="u-tg-sms-lines">
        <legend>{t('Authorized SIM lines')}</legend>
        {choices.map(x => <label key={x.id}>
          <input type="checkbox" checked={selected.includes(x.id)}
            onChange={e => change({ instance_ids: toggleTelegramSmsLine(selected, x.id, e.target.checked),
              bind_current_sim: true })} />
          {x.name || `SIM ${x.id}`} · {t('Line')} {x.id}
        </label>)}
        {!choices.length && <p>{t('No configured SIM lines are available.')}</p>}
      </fieldset>
      <p>{t('Replies use the receiving SIM. New messages let you choose a SIM. Authorization does not start a stopped line or switch cards.')}</p>
      {loadError && <p role="alert">{t(loadError)}</p>}
      <label><input type="checkbox" checked={!!c.bind_current_sim}
        onChange={e => change({ bind_current_sim: e.target.checked })} />{t('Bind all selected lines to their current SIM identities when saving')}</label>
      <label htmlFor="tg-sms-limit">{t('Submission limit per rolling 24 hours')}</label>
      <input id="tg-sms-limit" type="number" min="1" max="100" value={c.daily_limit ?? 20}
        onChange={e => change({ daily_limit: Number(e.target.value) })} />
      <p>{t('Confirmations expire after 120 seconds. At least 10 seconds between submissions. Unicode text may be billed as multiple SMS segments.')}</p>
      <p>{t('All authorized SIMs share this submission limit. Changing authorization invalidates previous confirmations and reply links.')}</p>
      <p>{t('Bot chats are not end-to-end encrypted. Message content is stored in Telegram and the local database. Do not expose this bot to other users.')}</p>
      <p>{t('Save below, then send /lines to your bot. Reply to a new SMS notification, use /sms +international-number text to choose a line, or /sms line-ID +international-number text.')}</p>
    </>}
    <p aria-live="polite">{t('Saved configuration status')}: {health ? t(STATES[health.state] || 'Telegram API error') : t('Unavailable')}
      {health && <> · {t('Pending notifications')}: {health.pending_notifications}</>}
    </p>
    {health?.push_error && <p role="status">{t('Last push error')}: {t(STATES[health.push_error] || 'Telegram API error')}</p>}
  </section>
}
