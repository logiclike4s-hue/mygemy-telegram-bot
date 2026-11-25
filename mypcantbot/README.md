# mypcantbot

Telegram bot for basic Windows PC control.

## Features

- PC status and disk/memory information
- Screenshot capture
- Volume and media controls
- Lock and confirmation-based sleep/restart/shutdown
- Allowlisted application launch
- Allowlisted read-only system commands

## Security

- Only `TELEGRAM_ALLOWED_USER_ID` can use the bot.
- Application and command actions use fixed allowlists; arbitrary shell commands are not accepted.
- Unlocking Windows automatically is not implemented because it would require storing or bypassing credentials.
- Power actions require a confirmation button.

## Setup

1. Revoke the token that was shared in chat and create a new token with @BotFather.
2. Copy `.env.example` to `.env`.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_ALLOWED_USER_ID`.
4. Install and start:

```powershell
npm install
npm start
```

Keep the bot process running on the PC you want to control.
