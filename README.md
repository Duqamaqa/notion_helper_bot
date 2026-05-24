# notion_helper_bot

Telegram bot that:
- responds only to `@aaronikuka`
- shows PRD shortcut buttons for task suggestions
- opens task detail screens with inline actions
- reads/writes tasks from a Notion database

## Setup

1. Create a Telegram bot in BotFather and copy `BOT_TOKEN`.
2. Share your target Notion database with the integration.
3. Copy the Notion database ID from its URL.
4. Install dependencies:
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```
5. Create `.env`:
   ```bash
   cp .env.example .env
   ```
6. Fill `.env`:
   ```ini
   BOT_TOKEN=YOUR_TELEGRAM_BOT_TOKEN
   NOTION_TOKEN=YOUR_NOTION_INTEGRATION_TOKEN
   NOTION_DATABASE_ID=YOUR_NOTION_DATABASE_ID   # optional if using NOTION_DATABASE_NAME
   NOTION_DATABASE_NAME=To do list
   ALLOWED_USERNAME=aaronikuka
   GOOGLE_CALENDAR_ID=primary
   GOOGLE_OAUTH_CLIENT_SECRETS_FILE=/path/to/google_client_secret.json
   TELEGRAM_CHAT_ID=YOUR_TELEGRAM_CHAT_ID
   ```
7. Run:
   ```bash
   .venv/bin/python bot.py
   ```

## Telegram frontend

- `What should I do now?`: shows ranked open task suggestions
- `Show me quick tasks`: shows tasks with `Estimated Time <= 30`
- `What can I finish in 20 minutes?`: shows tasks with `Estimated Time <= 20`
- `All tasks`: lists all tasks and offers inline task buttons
- `New task`: asks for a title and creates a new Notion page

Each suggested task opens a detail screen with title, due date, description, status, and estimate. Inline actions are available for `Done`, `Doing`, and `Remind later`.

The current pass implements the Telegram frontend. Google Calendar free-time detection, proactive reminders, and scheduler persistence still need the Google Calendar credentials and chat ID listed above.
