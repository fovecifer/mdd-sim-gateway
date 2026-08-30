# Privacy

MDD Sim Gateway is self-hosted and has no product analytics or telemetry. Operational data is stored locally under the configured data directory. Network requests occur only for configured carrier/IMS operation, subscriptions, notification channels, eSIM provisioning, dependency installation and explicit update checks.

Local data may contain SIM identifiers, phone numbers, SMS/call metadata, notification credentials, proxy subscription URLs and a SIM PIN. Runtime directories and credential-bearing files are owner-only, but host storage and backups still require protection. Support bundles redact known identities, credentials, URLs, activation codes and cryptographic material; always review a bundle before sharing it. Removing the data directory during `uninstall --purge` is irreversible unless you have a backup.

If the self-use Telegram SMS extension is enabled, SMS text, sender/recipient numbers and
command confirmations pass through Telegram cloud chats, not Secret Chats/end-to-end
encryption. Only the configured owner's private chat receives the extension's messages.
The local SMS database also stores queued Telegram content, reply mappings and draft bodies.
Those extension rows are logically pruned after 30 days while the manager is running; this
does not erase Telegram history, normal SMS history, database free pages or backups. Disabling
SMS control cancels old pending work but does not delete existing history. In-flight network
requests cannot be recalled. Do not share the SQLite database as a diagnostic artifact.
