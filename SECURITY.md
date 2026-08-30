# Security policy

Security fixes are provided for the latest release line. Do not open a public issue for a vulnerability that exposes SIM identities, credentials, message content, host access, or remote code execution.

Until a dedicated security mailbox is published, use GitHub private vulnerability reporting on the repository. Include the affected version, deployment mode, reproduction steps, impact and a redacted diagnostic bundle. Never include a real PIN, IMSI, ICCID, EID, phone number, subscription URL or notification token.

Deploy behind a trusted LAN or VPN, use a trusted TLS certificate, keep the host patched, and do not expose Docker, ModemManager, pcscd, SIP, AMI or the management port directly to the public Internet.

This self-use fork adds a separately opt-in Telegram SMS control boundary. Only a configured
numeric owner ID in that same private chat can create and confirm SMS drafts. Grants bind to
the selected SIM identity, expire individual confirmations after 120 seconds, and are checked
again before submission. A compromised owner account or bot token is still a serious risk:
use Telegram two-step verification, protect the token, and disable SMS control immediately
if either is compromised. No bot registration, first-message ownership claim, public callback,
admin-session bypass, remote calls, shell or generic management commands are provided.

See [Telegram SMS](docs/TELEGRAM_SMS.md) for limitations, retention and safe deployment.
