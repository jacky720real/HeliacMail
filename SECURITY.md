# Security Policy for HeliacMail

## Reporting a Vulnerability

If you discover a security vulnerability, please open a **private advisory** or contact the maintainer directly. Do **not** open a public issue for security problems.

Please include:
- The affected version
- Steps to reproduce
- Impact description

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| v2.x    | :white_check_mark: |
| < 2.0   | :x:                |

## Security Notes

- Passwords / API keys are stored **in plain text** in `config.yaml` next to the program. Keep the file private; do not commit it (it is in `.gitignore`).
- The web UI binds to `127.0.0.1` by default. If you change `server.host`, set a strong `server.auth_token`.
- IMAP credentials are sent to your mail provider's server over TLS.
