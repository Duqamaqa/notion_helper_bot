# notion_helper_bot

Telegram bot that:
- lets each user connect their own Notion integration token and task table
- shows PRD shortcut buttons for task suggestions
- opens task detail screens with inline actions
- reads/writes tasks from a Notion database
- works in manual mode without Google Calendar

## Setup

1. Create a Telegram bot in BotFather and copy `BOT_TOKEN`.
2. Duplicate the supported Simple Task template:
   https://www.notion.so/marketplace/templates/simple-task?cr=pro%253Aheyiammarco
3. Create a Notion integration and share the duplicated task database with it.
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
   ALLOWED_USERNAME=*
   ALLOWED_USER_ID=
   NOTION_TITLE_PROPERTY=Name
   NOTION_DESCRIPTION_PROPERTY=Text
   NOTION_DUE_DATE_PROPERTY=Due Date
   NOTION_STATUS_PROPERTY=Status
   NOTION_ESTIMATE_PROPERTY=Estimated time
   NOTION_INBOX_STATUS=inbox
   NOTION_DOING_STATUS=progress
   NOTION_DONE_STATUS=done
   APP_TIMEZONE=Asia/Jerusalem
   BOT_STATE_DB=bot_state.sqlite3
   REMIND_LATER_MINUTES=150
   ```
7. Start the bot:
   ```bash
   .venv/bin/python bot.py
   ```
8. In Telegram, send `/start`. The bot asks for:
   - your Notion integration token
   - the Notion task database/table link

The token and table ID are stored locally in `bot_state.sqlite3`, which is ignored by git.

You can inspect server-level setup with:

```bash
.venv/bin/python bot.py --check
```

## Required Notion template

The bot intentionally works only with the supported Simple Task template shape. The connected database must contain:

- title property named `Name`, `Task`, or similar
- `Status`: select or status with options like `inbox`/`not started`, `progress`/`in progress`, and `done`

Optional supported properties:

- `Text`: rich text description
- `Due Date`: date
- `Estimated time`: number of minutes

The bot also supports `Estimated time` as rich text and parses values like `20`, `20 min`, or `1h 30m`.

If you need to create a compatible database manually, set the admin-only Notion values in `.env` and run:

```bash
.venv/bin/python create_task_database.py
```

Admin-only `.env` values:

```ini
NOTION_TOKEN=YOUR_NOTION_INTEGRATION_TOKEN
NOTION_DATABASE_ID=
NOTION_DATABASE_NAME=To do list
NOTION_PARENT_PAGE_ID=PAGE_ID_TO_CREATE_DATABASE_UNDER
```

## Run

Start the bot:

```bash
.venv/bin/python bot.py
```

On first `/start`, each user connects their own Notion token and table link. To reconnect later:

   ```bash
   /connect
   ```

## Telegram frontend

- `What should I do now?`: shows ranked open task suggestions
- `Show me quick tasks`: shows tasks with `Estimated Time <= 30`
- `What can I finish in 20 minutes?`: shows tasks with `Estimated Time <= 20`
- `All tasks`: lists all tasks and offers inline task buttons
- `New task`: asks for a title and creates a new Notion page
- `/connect`: connect or replace the user's Notion token/table
- `/setup`: checks Telegram and Notion configuration

Each suggested task opens a detail screen with title, due date, description, status, and estimate. Inline actions are available for `Done`, `Doing`, and `Remind later`.

## First-version behavior

This version does not use Google Calendar yet. `Remind later` stores a local snooze in SQLite and hides that task from manual suggestions until the snooze expires.
