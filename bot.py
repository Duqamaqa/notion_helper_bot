import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_DATABASE_NAME = os.getenv("NOTION_DATABASE_NAME", "To do list")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
ALLOWED_USERNAME = (os.getenv("ALLOWED_USERNAME") or "").lstrip("@").lower()
BUTTON_NOW = "What should I do now?"
BUTTON_QUICK = "Show me quick tasks"
BUTTON_TWENTY = "What can I finish in 20 minutes?"
BUTTON_ALL = "All tasks"
BUTTON_NEW = "New task"
MENU = ReplyKeyboardMarkup(
    [
        [BUTTON_NOW],
        [BUTTON_QUICK, BUTTON_TWENTY],
        [BUTTON_ALL, BUTTON_NEW],
    ],
    resize_keyboard=True,
)
LEGACY_BUTTON_TEXT = {
    "All": BUTTON_ALL,
    "Next": BUTTON_NOW,
    "New": BUTTON_NEW,
}
SUGGESTION_LIMIT = 5
QUICK_TASK_MINUTES = 30
TWENTY_MINUTES = 20
DONE_WORDS = {
    "done",
    "complete",
    "completed",
    "closed",
    "resolved",
    "cancelled",
    "canceled",
}
DEFAULT_STATUS_EMOJI = "📌"
STATUS_EMOJI_BY_KEYWORD = {
    "inbox": "📥",
    "to do": "📝",
    "todo": "📝",
    "not started": "📝",
    "backlog": "📝",
    "in progress": "🔄",
    "doing": "🔄",
    "working": "🔄",
    "review": "👀",
    "blocked": "⛔",
    "stuck": "⛔",
    "on hold": "⏸️",
    "waiting": "⏳",
    "done": "✅",
    "complete": "✅",
    "completed": "✅",
    "closed": "✅",
    "resolved": "✅",
    "cancelled": "🚫",
    "canceled": "🚫",
}
RESOLVED_DATABASE_ID: Optional[str] = None


def is_allowed_user(update: Update) -> bool:
    user = update.effective_user
    username = (user.username or "").lower() if user else ""
    if not ALLOWED_USERNAME:
        return True
    return username == ALLOWED_USERNAME


def notion_ready_error() -> Optional[str]:
    if not NOTION_TOKEN:
        return "Missing NOTION_TOKEN."
    return None


def notion_request(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN is not configured.")

    response = requests.request(
        method=method,
        url=f"https://api.notion.com/v1{path}",
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    if response.status_code >= 400:
        try:
            body = response.json()
            message = body.get("message", "Unknown Notion API error.")
        except ValueError:
            message = response.text
        raise RuntimeError(f"Notion API {response.status_code}: {message}")
    return response.json()


def rich_text_to_plain(chunks: list[dict[str, Any]]) -> str:
    return "".join(chunk.get("plain_text", "") for chunk in chunks).strip()


def resolve_database_id() -> str:
    global RESOLVED_DATABASE_ID

    if NOTION_DATABASE_ID:
        return NOTION_DATABASE_ID
    if RESOLVED_DATABASE_ID:
        return RESOLVED_DATABASE_ID

    if not NOTION_DATABASE_NAME:
        raise RuntimeError("Missing NOTION_DATABASE_ID or NOTION_DATABASE_NAME.")

    search_body = {
        "query": NOTION_DATABASE_NAME,
        "page_size": 20,
        "filter": {"value": "database", "property": "object"},
    }
    data = notion_request("POST", "/search", payload=search_body)
    results = data.get("results", [])
    if not results:
        raise RuntimeError(
            f'Could not find database "{NOTION_DATABASE_NAME}". '
            "Share it with the integration or set NOTION_DATABASE_ID."
        )

    exact_matches = []
    for item in results:
        title = rich_text_to_plain(item.get("title", []))
        if title.casefold() == NOTION_DATABASE_NAME.casefold():
            exact_matches.append(item)

    chosen = exact_matches[0] if exact_matches else results[0]
    database_id = chosen.get("id")
    if not database_id:
        raise RuntimeError("Found a database but could not read its ID.")

    RESOLVED_DATABASE_ID = database_id
    return database_id


def query_tasks_page(page_size: int = 25, start_cursor: Optional[str] = None) -> dict[str, Any]:
    database_id = resolve_database_id()
    body = {"page_size": page_size}
    if start_cursor:
        body["start_cursor"] = start_cursor
    data = notion_request("POST", f"/databases/{database_id}/query", payload=body)
    return data


def query_tasks(page_size: int = 25) -> list[dict[str, Any]]:
    data = query_tasks_page(page_size=page_size)
    return data.get("results", [])


def query_all_tasks() -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    next_cursor: Optional[str] = None

    while True:
        data = query_tasks_page(page_size=100, start_cursor=next_cursor)
        tasks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        next_cursor = data.get("next_cursor")
        if not next_cursor:
            break

    return tasks


def get_database_schema() -> dict[str, Any]:
    database_id = resolve_database_id()
    return notion_request("GET", f"/databases/{database_id}")


def get_title_property_name(database: dict[str, Any]) -> Optional[str]:
    properties = database.get("properties", {})
    for prop_name, prop in properties.items():
        if prop.get("type") == "title":
            return prop_name
    return None


def extract_title(page: dict[str, Any]) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            chunks = prop.get("title", [])
            title = "".join(chunk.get("plain_text", "") for chunk in chunks).strip()
            return title or "Untitled"
    return "Untitled"


def extract_status(page: dict[str, Any]) -> Optional[str]:
    for prop in page.get("properties", {}).values():
        prop_type = prop.get("type")
        if prop_type == "status" and prop.get("status"):
            return (prop["status"].get("name") or "").strip() or None
        if prop_type == "select" and prop.get("select"):
            return (prop["select"].get("name") or "").strip() or None
    return None


def extract_due_date(page: dict[str, Any]) -> Optional[str]:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "date" and prop.get("date"):
            return prop["date"].get("start")
    return None


def extract_description(page: dict[str, Any]) -> Optional[str]:
    rich_text_properties: list[tuple[int, str]] = []
    for prop_name, prop in page.get("properties", {}).items():
        if prop.get("type") != "rich_text":
            continue
        text = rich_text_to_plain(prop.get("rich_text", []))
        if not text:
            continue
        normalized = prop_name.strip().lower()
        score = 0 if any(word in normalized for word in ("description", "text", "notes", "details")) else 1
        rich_text_properties.append((score, text))

    if not rich_text_properties:
        return None

    rich_text_properties.sort(key=lambda item: item[0])
    return rich_text_properties[0][1]


def extract_estimated_minutes(page: dict[str, Any]) -> Optional[int]:
    number_properties: list[tuple[int, float]] = []
    for prop_name, prop in page.get("properties", {}).items():
        if prop.get("type") != "number" or prop.get("number") is None:
            continue
        normalized = prop_name.strip().lower()
        score = 0 if any(word in normalized for word in ("estimated", "duration", "time", "minute")) else 1
        number_properties.append((score, float(prop["number"])))

    if not number_properties:
        return None

    number_properties.sort(key=lambda item: item[0])
    minutes = number_properties[0][1]
    return int(minutes) if minutes.is_integer() else round(minutes)


def parse_notion_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        normalized = date_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def is_done_task(page: dict[str, Any]) -> bool:
    status = (extract_status(page) or "").strip().lower()
    if status and any(word in status for word in DONE_WORDS):
        return True

    for prop_name, prop in page.get("properties", {}).items():
        if prop.get("type") == "checkbox":
            name = prop_name.lower()
            if prop.get("checkbox") and any(word in name for word in DONE_WORDS):
                return True
    return False


def format_task_line(page: dict[str, Any], index: Optional[int] = None) -> str:
    prefix = f"{index}. " if index is not None else "- "
    title = extract_title(page)
    status = extract_status(page) or "No status"
    due = extract_due_date(page) or "-"
    estimate = extract_estimated_minutes(page)
    estimate_text = f"{estimate} min" if estimate is not None else "no estimate"
    emoji = status_to_emoji(status)
    return f"{prefix}{emoji} {title} | {status} | {due} | {estimate_text}"


def status_to_emoji(status: Optional[str]) -> str:
    if not status:
        return DEFAULT_STATUS_EMOJI
    normalized = status.strip().lower()
    for keyword, emoji in STATUS_EMOJI_BY_KEYWORD.items():
        if keyword in normalized:
            return emoji
    return DEFAULT_STATUS_EMOJI


def normalize_menu_text(text: str) -> str:
    return LEGACY_BUTTON_TEXT.get(text, text)


def truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 3:
        return text[:max_len]
    return text[: max_len - 3].rstrip() + "..."


def due_label(page: dict[str, Any]) -> str:
    raw_due = extract_due_date(page)
    if not raw_due:
        return "No due date"

    parsed = parse_notion_date(raw_due)
    if not parsed:
        return raw_due

    now = datetime.now(parsed.tzinfo or timezone.utc)
    due_date = parsed.date()
    if due_date == now.date():
        return f"Today ({raw_due})"
    if due_date == now.date() + timedelta(days=1):
        return f"Tomorrow ({raw_due})"
    return raw_due


def due_sort_key(page: dict[str, Any]) -> float:
    due = parse_notion_date(extract_due_date(page))
    return due.timestamp() if due else float("inf")


def duration_sort_key(page: dict[str, Any]) -> float:
    estimate = extract_estimated_minutes(page)
    return float(estimate) if estimate is not None else float("inf")


def rank_tasks(tasks: list[dict[str, Any]], slot_minutes: Optional[int] = None) -> list[dict[str, Any]]:
    def rank(task: dict[str, Any]) -> tuple[int, float, float, str]:
        estimate = extract_estimated_minutes(task)
        if slot_minutes is None:
            fit_rank = 0
        elif estimate is None:
            fit_rank = 1
        elif estimate <= slot_minutes:
            fit_rank = 0
        else:
            fit_rank = 2
        return (fit_rank, due_sort_key(task), duration_sort_key(task), extract_title(task).casefold())

    return sorted(tasks, key=rank)


def filter_tasks_for_slot(tasks: list[dict[str, Any]], slot_minutes: int) -> list[dict[str, Any]]:
    return [
        task
        for task in tasks
        if (extract_estimated_minutes(task) is not None and extract_estimated_minutes(task) <= slot_minutes)
    ]


def task_callback_id(page: dict[str, Any]) -> Optional[str]:
    page_id = page.get("id")
    return page_id if isinstance(page_id, str) and page_id else None


def format_task_button_label(page: dict[str, Any]) -> str:
    title = truncate(extract_title(page), 38)
    estimate = extract_estimated_minutes(page)
    estimate_text = f"{estimate}m" if estimate is not None else "?m"
    return truncate(f"{title} | {estimate_text} | {due_label(page)}", 58)


def build_task_list_keyboard(tasks: list[dict[str, Any]], refresh_mode: Optional[str] = None) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for task in tasks:
        page_id = task_callback_id(task)
        if not page_id:
            continue
        rows.append([InlineKeyboardButton(format_task_button_label(task), callback_data=f"task:{page_id}")])

    if refresh_mode:
        rows.append([InlineKeyboardButton("Refresh", callback_data=f"nav:{refresh_mode}")])

    return InlineKeyboardMarkup(rows)


def build_task_detail_keyboard(page_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Done", callback_data=f"action:done:{page_id}"),
                InlineKeyboardButton("Doing", callback_data=f"action:doing:{page_id}"),
            ],
            [InlineKeyboardButton("Remind later", callback_data=f"action:later:{page_id}")],
            [InlineKeyboardButton("Back to suggestions", callback_data="nav:now")],
        ]
    )


def format_task_detail(page: dict[str, Any]) -> str:
    title = extract_title(page)
    status = extract_status(page) or "No status"
    estimate = extract_estimated_minutes(page)
    estimate_text = f"{estimate} minutes" if estimate is not None else "No estimate"
    description = extract_description(page) or "No description."

    return "\n".join(
        [
            "Task detail",
            "",
            title,
            "",
            f"Status: {status}",
            f"Due: {due_label(page)}",
            f"Estimate: {estimate_text}",
            "",
            "Description:",
            description,
        ]
    )


async def reply_long_text(update: Update, text: str, add_menu: bool = True) -> None:
    if not update.message:
        return

    max_chunk_len = 3900
    lines = text.split("\n")
    chunks: list[str] = []
    current = ""

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > max_chunk_len and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        reply_markup = MENU if add_menu and is_last else None
        await update.message.reply_text(chunk, reply_markup=reply_markup)


def create_task(title: str) -> str:
    db = get_database_schema()
    title_property = get_title_property_name(db)
    if not title_property:
        raise RuntimeError("Could not find a title property in the Notion database.")

    database_id = resolve_database_id()

    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            title_property: {
                "title": [{"text": {"content": title}}],
            }
        },
    }
    created = notion_request("POST", "/pages", payload=payload)
    return created.get("url", "")


def get_task_page(page_id: str) -> dict[str, Any]:
    return notion_request("GET", f"/pages/{page_id}")


def get_status_property(database: dict[str, Any]) -> Optional[tuple[str, str]]:
    candidates: list[tuple[int, str, str]] = []
    for prop_name, prop in database.get("properties", {}).items():
        prop_type = prop.get("type")
        if prop_type not in {"status", "select"}:
            continue
        normalized = prop_name.strip().lower()
        score = 0 if "status" in normalized else 1
        candidates.append((score, prop_name, prop_type))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, prop_name, prop_type = candidates[0]
    return prop_name, prop_type


def set_task_status(page_id: str, status_name: str) -> dict[str, Any]:
    db = get_database_schema()
    status_property = get_status_property(db)
    if not status_property:
        raise RuntimeError("Could not find a Status or select property in the Notion database.")

    prop_name, prop_type = status_property
    if prop_type == "status":
        value = {"status": {"name": status_name}}
    else:
        value = {"select": {"name": status_name}}

    return notion_request("PATCH", f"/pages/{page_id}", payload={"properties": {prop_name: value}})


def get_active_tasks() -> list[dict[str, Any]]:
    return [task for task in query_all_tasks() if not is_done_task(task)]


def build_suggestions_payload(mode: str) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    notion_error = notion_ready_error()
    if notion_error:
        return notion_error, None

    active_tasks = get_active_tasks()
    if not active_tasks:
        return "No open tasks found.", None

    heading = "Suggested tasks"
    helper = "Tap a task to open details and actions."
    slot_minutes: Optional[int] = None
    filtered_tasks = active_tasks

    if mode == "quick":
        heading = f"Quick tasks ({QUICK_TASK_MINUTES} minutes or less)"
        helper = "These tasks have an Estimated Time that fits a short free slot."
        slot_minutes = QUICK_TASK_MINUTES
        filtered_tasks = filter_tasks_for_slot(active_tasks, QUICK_TASK_MINUTES)
    elif mode == "twenty":
        heading = f"Tasks you can finish in {TWENTY_MINUTES} minutes"
        helper = "These tasks have an Estimated Time of 20 minutes or less."
        slot_minutes = TWENTY_MINUTES
        filtered_tasks = filter_tasks_for_slot(active_tasks, TWENTY_MINUTES)
    elif mode == "now":
        helper = "Here are the best open tasks based on due date and estimate."

    if not filtered_tasks:
        return (
            f"No tasks found with an Estimated Time of {slot_minutes} minutes or less.",
            None,
        )

    ranked = rank_tasks(filtered_tasks, slot_minutes=slot_minutes)[:SUGGESTION_LIMIT]
    lines = [heading, "", helper, ""]
    lines.extend(format_task_line(task, index=i + 1) for i, task in enumerate(ranked))
    return "\n".join(lines), build_task_list_keyboard(ranked, refresh_mode=mode)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not update.message:
        return
    await update.message.reply_text("Choose a shortcut:", reply_markup=MENU)


async def send_suggestions(update: Update, mode: str) -> None:
    if not update.message:
        return
    try:
        text, keyboard = build_suggestions_payload(mode)
    except RuntimeError as err:
        await update.message.reply_text(str(err), reply_markup=MENU)
        return

    await update.message.reply_text(text, reply_markup=keyboard or MENU)


async def send_all_tasks(update: Update) -> None:
    if not update.message:
        return

    notion_error = notion_ready_error()
    if notion_error:
        await update.message.reply_text(notion_error, reply_markup=MENU)
        return

    try:
        tasks = query_all_tasks()
    except RuntimeError as err:
        await update.message.reply_text(str(err), reply_markup=MENU)
        return

    if not tasks:
        await update.message.reply_text("No tasks found.", reply_markup=MENU)
        return

    lines = [f'Tasks from "{NOTION_DATABASE_NAME}":']
    lines.extend(format_task_line(task, index=i + 1) for i, task in enumerate(tasks))
    await reply_long_text(update, "\n".join(lines), add_menu=True)

    openable_tasks = [task for task in tasks if task_callback_id(task)][:10]
    if openable_tasks:
        await update.message.reply_text(
            "Open a task:",
            reply_markup=build_task_list_keyboard(openable_tasks),
        )


async def begin_new_task(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    notion_error = notion_ready_error()
    if notion_error:
        await update.message.reply_text(notion_error, reply_markup=MENU)
        return
    context.user_data["awaiting_new_title"] = True
    await update.message.reply_text("Send the new task title.", reply_markup=MENU)


async def now_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    await send_suggestions(update, "now")


async def quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    await send_suggestions(update, "quick")


async def twenty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    await send_suggestions(update, "twenty")


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    await send_all_tasks(update)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    await begin_new_task(update, context)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not update.message:
        return

    text = normalize_menu_text((update.message.text or "").strip())

    if text == BUTTON_NOW:
        await send_suggestions(update, "now")
        return

    if text == BUTTON_QUICK:
        await send_suggestions(update, "quick")
        return

    if text == BUTTON_TWENTY:
        await send_suggestions(update, "twenty")
        return

    if text == BUTTON_ALL:
        await send_all_tasks(update)
        return

    if text == BUTTON_NEW:
        await begin_new_task(update, context)
        return

    if context.user_data.get("awaiting_new_title"):
        task_title = text.strip()
        if not task_title:
            await update.message.reply_text("Title cannot be empty. Send a title.", reply_markup=MENU)
            return
        try:
            page_url = create_task(task_title)
        except RuntimeError as err:
            await update.message.reply_text(str(err), reply_markup=MENU)
            return
        finally:
            context.user_data["awaiting_new_title"] = False

        if page_url:
            await update.message.reply_text(f"Created: {page_url}", reply_markup=MENU)
        else:
            await update.message.reply_text("Created new task.", reply_markup=MENU)
        return

    await update.message.reply_text("Use one of the shortcut buttons.", reply_markup=MENU)


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if not is_allowed_user(update):
        await query.answer()
        return

    data = query.data or ""

    if data.startswith("nav:"):
        mode = data.split(":", 1)[1]
        await query.answer()
        try:
            text, keyboard = build_suggestions_payload(mode)
        except RuntimeError as err:
            await query.edit_message_text(str(err))
            return
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data.startswith("task:"):
        page_id = data.split(":", 1)[1]
        await query.answer()
        try:
            page = get_task_page(page_id)
        except RuntimeError as err:
            await query.edit_message_text(str(err))
            return
        await query.edit_message_text(format_task_detail(page), reply_markup=build_task_detail_keyboard(page_id))
        return

    if data.startswith("action:"):
        parts = data.split(":", 2)
        if len(parts) != 3:
            await query.answer("Unknown action.")
            return
        _, action, page_id = parts

        if action == "later":
            await query.answer("Kept for later.")
            try:
                page = get_task_page(page_id)
            except RuntimeError:
                return
            await query.edit_message_text(
                format_task_detail(page) + "\n\nReminder choice: later.",
                reply_markup=build_task_detail_keyboard(page_id),
            )
            return

        status_by_action = {"done": "Done", "doing": "In Progress"}
        status_name = status_by_action.get(action)
        if not status_name:
            await query.answer("Unknown action.")
            return

        try:
            page = set_task_status(page_id, status_name)
        except RuntimeError as err:
            await query.answer("Could not update task.", show_alert=True)
            await query.edit_message_text(str(err))
            return

        await query.answer(f"Marked {status_name}.")
        await query.edit_message_text(format_task_detail(page), reply_markup=build_task_detail_keyboard(page_id))


async def setup_bot_commands(app: Application) -> None:
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Show shortcut buttons"),
            BotCommand("menu", "Show shortcut buttons"),
            BotCommand("now", "Suggest what to do now"),
            BotCommand("quick", "Show quick tasks"),
            BotCommand("twenty", "Show tasks for a 20 minute slot"),
            BotCommand("all", "List all tasks"),
            BotCommand("new", "Create a new task"),
        ]
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Set BOT_TOKEN env var before starting the bot.")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(setup_bot_commands).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", start))
    app.add_handler(CommandHandler("now", now_command))
    app.add_handler(CommandHandler("quick", quick_command))
    app.add_handler(CommandHandler("twenty", twenty_command))
    app.add_handler(CommandHandler("all", all_command))
    app.add_handler(CommandHandler("new", new_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("Bot started. Allowed user: @%s", ALLOWED_USERNAME)
    app.run_polling()


if __name__ == "__main__":
    main()
