import argparse
import asyncio
import contextvars
import logging
import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

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


def read_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid integer for %s=%r. Using %s.", name, value, default)
        return default


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

BOT_TOKEN = os.getenv("BOT_TOKEN")
NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_DATABASE_NAME = os.getenv("NOTION_DATABASE_NAME", "To do list")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
ALLOWED_USERNAME = (os.getenv("ALLOWED_USERNAME") or "*").lstrip("@").lower()
ALLOWED_USER_ID = os.getenv("ALLOWED_USER_ID", "").strip()
NOTION_TITLE_PROPERTY = os.getenv("NOTION_TITLE_PROPERTY", "Name")
NOTION_DESCRIPTION_PROPERTY = os.getenv("NOTION_DESCRIPTION_PROPERTY", "Text")
NOTION_DUE_DATE_PROPERTY = os.getenv("NOTION_DUE_DATE_PROPERTY", "Due Date")
NOTION_STATUS_PROPERTY = os.getenv("NOTION_STATUS_PROPERTY", "Status")
NOTION_ESTIMATE_PROPERTY = os.getenv("NOTION_ESTIMATE_PROPERTY", "Estimated time")
NOTION_INBOX_STATUS = os.getenv("NOTION_INBOX_STATUS", "inbox")
NOTION_DOING_STATUS = os.getenv("NOTION_DOING_STATUS", "progress")
NOTION_DONE_STATUS = os.getenv("NOTION_DONE_STATUS", "done")
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "Asia/Jerusalem")
BOT_STATE_DB = Path(os.getenv("BOT_STATE_DB", "bot_state.sqlite3"))
REMIND_LATER_MINUTES = read_int_env("REMIND_LATER_MINUTES", 150)
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
    "progress": "🔄",
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
STATUS_CANDIDATES = {
    "inbox": [NOTION_INBOX_STATUS, "Inbox", "inbox", "Not started", "not started", "To do", "To Do", "todo", "Backlog"],
    "doing": [NOTION_DOING_STATUS, "In Progress", "in progress", "Progress", "progress", "Doing", "doing"],
    "done": [NOTION_DONE_STATUS, "Done", "done", "Complete", "complete", "Completed", "completed"],
}
RESOLVED_DATABASE_ID: Optional[str] = None
ACTIVE_NOTION_TOKEN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("ACTIVE_NOTION_TOKEN", default=None)
ACTIVE_DATABASE_ID: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("ACTIVE_DATABASE_ID", default=None)
ONBOARDING_TEMPLATE_URL = "https://www.notion.so/marketplace/templates/simple-task?cr=pro%253Aheyiammarco"
REQUIRED_TEMPLATE_PROPERTIES = [
    (NOTION_TITLE_PROPERTY, {"title"}, "title"),
    (NOTION_STATUS_PROPERTY, {"status", "select"}, "status"),
]
OPTIONAL_TEMPLATE_PROPERTIES = [
    (NOTION_DESCRIPTION_PROPERTY, {"rich_text"}, "description"),
    (NOTION_DUE_DATE_PROPERTY, {"date"}, "due date"),
    (NOTION_ESTIMATE_PROPERTY, {"number", "rich_text"}, "estimated minutes"),
]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(APP_TIMEZONE)
    except Exception:
        logger.warning("Invalid APP_TIMEZONE=%r. Falling back to UTC.", APP_TIMEZONE)
        return ZoneInfo("UTC")


def to_utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_utc_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def state_connection() -> sqlite3.Connection:
    BOT_STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(BOT_STATE_DB)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snoozes (
            page_id TEXT PRIMARY KEY,
            until_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_configs (
            telegram_user_id TEXT PRIMARY KEY,
            telegram_username TEXT,
            notion_token TEXT NOT NULL,
            notion_database_id TEXT NOT NULL,
            created_utc TEXT NOT NULL,
            updated_utc TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def get_user_key(update: Update) -> Optional[str]:
    user = update.effective_user
    return str(user.id) if user and user.id else None


def load_user_config(user_id: str) -> Optional[dict[str, str]]:
    conn = state_connection()
    try:
        row = conn.execute(
            """
            SELECT telegram_user_id, telegram_username, notion_token, notion_database_id
            FROM user_configs
            WHERE telegram_user_id = ?
            """,
            (user_id,),
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return None
    return {
        "telegram_user_id": row[0],
        "telegram_username": row[1] or "",
        "notion_token": row[2],
        "notion_database_id": row[3],
    }


def save_user_config(user_id: str, username: str, notion_token: str, database_id: str) -> None:
    now = to_utc_iso(utc_now())
    conn = state_connection()
    try:
        conn.execute(
            """
            INSERT INTO user_configs(
                telegram_user_id,
                telegram_username,
                notion_token,
                notion_database_id,
                created_utc,
                updated_utc
            )
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                telegram_username=excluded.telegram_username,
                notion_token=excluded.notion_token,
                notion_database_id=excluded.notion_database_id,
                updated_utc=excluded.updated_utc
            """,
            (user_id, username, notion_token, database_id, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_config(user_id: str) -> None:
    conn = state_connection()
    try:
        conn.execute("DELETE FROM user_configs WHERE telegram_user_id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


def bind_user_config(config: dict[str, str]) -> None:
    ACTIVE_NOTION_TOKEN.set(config["notion_token"])
    ACTIVE_DATABASE_ID.set(config["notion_database_id"])


def snooze_task(page_id: str, until: datetime) -> None:
    conn = state_connection()
    try:
        conn.execute(
            "INSERT INTO snoozes(page_id, until_utc) VALUES(?, ?) ON CONFLICT(page_id) DO UPDATE SET until_utc=excluded.until_utc",
            (page_id, to_utc_iso(until)),
        )
        conn.commit()
    finally:
        conn.close()


def clear_snooze(page_id: str) -> None:
    conn = state_connection()
    try:
        conn.execute("DELETE FROM snoozes WHERE page_id = ?", (page_id,))
        conn.commit()
    finally:
        conn.close()


def is_task_snoozed(page_id: str, now: Optional[datetime] = None) -> bool:
    now = now or utc_now()
    conn = state_connection()
    try:
        row = conn.execute("SELECT until_utc FROM snoozes WHERE page_id = ?", (page_id,)).fetchone()
        if not row:
            return False
        until = parse_utc_iso(row[0])
        if until and until > now:
            return True
        conn.execute("DELETE FROM snoozes WHERE page_id = ?", (page_id,))
        conn.commit()
    finally:
        conn.close()
    return False


def is_allowed_user(update: Update) -> bool:
    user = update.effective_user
    username = (user.username or "").lower() if user else ""
    user_id = str(user.id) if user and user.id else ""
    if ALLOWED_USER_ID and user_id == ALLOWED_USER_ID:
        return True
    if ALLOWED_USERNAME == "*":
        return True
    return username == ALLOWED_USERNAME


def notion_ready_error() -> Optional[str]:
    if not (ACTIVE_NOTION_TOKEN.get() or NOTION_TOKEN):
        return "Missing NOTION_TOKEN."
    return None


def notion_request(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    token = ACTIVE_NOTION_TOKEN.get() or NOTION_TOKEN
    if not token:
        raise RuntimeError("NOTION_TOKEN is not configured.")

    response = requests.request(
        method=method,
        url=f"https://api.notion.com/v1{path}",
        headers={
            "Authorization": f"Bearer {token}",
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

    active_database_id = ACTIVE_DATABASE_ID.get()
    if active_database_id:
        return active_database_id
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
    configured = find_schema_property(database, NOTION_TITLE_PROPERTY, {"title"}, ["name", "title", "task"])
    if configured:
        return configured[0]
    return None


def normalize_property_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.casefold())


def extract_database_id(raw: str) -> Optional[str]:
    text = raw.strip()
    uuid_match = re.search(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        text,
    )
    if uuid_match:
        return uuid_match.group(0).lower()

    compact_match = re.search(r"(?<![0-9a-fA-F])([0-9a-fA-F]{32})(?![0-9a-fA-F])", text)
    if compact_match:
        value = compact_match.group(1).lower()
        return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:]}"
    return None


def validate_template_database(database: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for name, allowed_types, label in REQUIRED_TEMPLATE_PROPERTIES:
        keywords = [label, "name", "task"] if label == "title" else [label]
        found = find_schema_property(database, name, allowed_types, keywords)
        if not found:
            type_list = "/".join(sorted(allowed_types))
            errors.append(f"Missing {label} property `{name}` ({type_list}).")

    status_prop = find_schema_property(database, NOTION_STATUS_PROPERTY, {"status", "select"}, ["status"])
    if status_prop:
        prop_name, prop = status_prop
        prop_type = prop.get("type")
        options = prop.get(prop_type, {}).get("options", []) if prop_type else []
        option_names = {option.get("name", "").casefold() for option in options if option.get("name")}
        for status_key in ("inbox", "doing", "done"):
            if not any(candidate.casefold() in option_names for candidate in STATUS_CANDIDATES[status_key]):
                expected = " or ".join(STATUS_CANDIDATES[status_key][:3])
                errors.append(f"Status `{prop_name}` is missing an option like {expected}.")

    return errors


def validate_notion_connection(notion_token: str, database_id: str) -> dict[str, Any]:
    token = ACTIVE_NOTION_TOKEN.set(notion_token)
    database = ACTIVE_DATABASE_ID.set(database_id)
    try:
        data = get_database_schema()
        errors = validate_template_database(data)
        if errors:
            raise RuntimeError(
                "This table does not match the supported Simple Task template:\n"
                + "\n".join(f"- {error}" for error in errors)
            )
        query_tasks(page_size=1)
        return data
    finally:
        ACTIVE_NOTION_TOKEN.reset(token)
        ACTIVE_DATABASE_ID.reset(database)


def find_page_property(
    page: dict[str, Any],
    preferred_name: Optional[str],
    allowed_types: set[str],
    keywords: list[str],
) -> Optional[tuple[str, dict[str, Any]]]:
    properties = page.get("properties", {})
    if preferred_name:
        preferred_key = normalize_property_name(preferred_name)
        for prop_name, prop in properties.items():
            if normalize_property_name(prop_name) == preferred_key and prop.get("type") in allowed_types:
                return prop_name, prop

    keyword_keys = [normalize_property_name(keyword) for keyword in keywords]
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for prop_name, prop in properties.items():
        if prop.get("type") not in allowed_types:
            continue
        normalized = normalize_property_name(prop_name)
        score = 0 if any(keyword in normalized for keyword in keyword_keys) else 1
        candidates.append((score, prop_name, prop))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1].casefold()))
    _, prop_name, prop = candidates[0]
    return prop_name, prop


def find_schema_property(
    database: dict[str, Any],
    preferred_name: Optional[str],
    allowed_types: set[str],
    keywords: list[str],
) -> Optional[tuple[str, dict[str, Any]]]:
    properties = database.get("properties", {})
    if preferred_name:
        preferred_key = normalize_property_name(preferred_name)
        for prop_name, prop in properties.items():
            if normalize_property_name(prop_name) == preferred_key and prop.get("type") in allowed_types:
                return prop_name, prop

    keyword_keys = [normalize_property_name(keyword) for keyword in keywords]
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for prop_name, prop in properties.items():
        if prop.get("type") not in allowed_types:
            continue
        normalized = normalize_property_name(prop_name)
        score = 0 if any(keyword in normalized for keyword in keyword_keys) else 1
        candidates.append((score, prop_name, prop))

    if not candidates:
        return None

    candidates.sort(key=lambda item: (item[0], item[1].casefold()))
    _, prop_name, prop = candidates[0]
    return prop_name, prop


def extract_title(page: dict[str, Any]) -> str:
    found = find_page_property(page, NOTION_TITLE_PROPERTY, {"title"}, ["name", "title", "task"])
    if found:
        _, prop = found
        chunks = prop.get("title", [])
        title = "".join(chunk.get("plain_text", "") for chunk in chunks).strip()
        return title or "Untitled"
    return "Untitled"


def extract_status(page: dict[str, Any]) -> Optional[str]:
    found = find_page_property(page, NOTION_STATUS_PROPERTY, {"status", "select"}, ["status"])
    if not found:
        return None
    _, prop = found
    prop_type = prop.get("type")
    if prop_type == "status" and prop.get("status"):
        return (prop["status"].get("name") or "").strip() or None
    if prop_type == "select" and prop.get("select"):
        return (prop["select"].get("name") or "").strip() or None
    return None


def extract_due_date(page: dict[str, Any]) -> Optional[str]:
    found = find_page_property(page, NOTION_DUE_DATE_PROPERTY, {"date"}, ["due", "date"])
    if found:
        _, prop = found
        if prop.get("date"):
            return prop["date"].get("start")
    return None


def extract_description(page: dict[str, Any]) -> Optional[str]:
    found = find_page_property(
        page,
        NOTION_DESCRIPTION_PROPERTY,
        {"rich_text"},
        ["description", "text", "notes", "details"],
    )
    if not found:
        return None
    _, prop = found
    text = rich_text_to_plain(prop.get("rich_text", []))
    return text or None


def parse_minutes_from_text(text: str) -> Optional[int]:
    normalized = text.strip().lower()
    if not normalized:
        return None

    hour_match = re.search(r"(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours)\b", normalized)
    minute_match = re.search(r"(\d+(?:\.\d+)?)\s*(m|min|mins|minute|minutes)\b", normalized)
    if hour_match:
        minutes = float(hour_match.group(1)) * 60
        if minute_match:
            minutes += float(minute_match.group(1))
        return round(minutes)
    if minute_match:
        return round(float(minute_match.group(1)))

    bare_number = re.search(r"\d+(?:\.\d+)?", normalized)
    if bare_number:
        return round(float(bare_number.group(0)))
    return None


def extract_estimated_minutes(page: dict[str, Any]) -> Optional[int]:
    found = find_page_property(
        page,
        NOTION_ESTIMATE_PROPERTY,
        {"number", "rich_text"},
        ["estimated", "estimate", "duration", "time", "minute"],
    )
    if not found:
        return None

    _, prop = found
    if prop.get("type") == "number" and prop.get("number") is not None:
        minutes = float(prop["number"])
        return int(minutes) if minutes.is_integer() else round(minutes)
    if prop.get("type") == "rich_text":
        return parse_minutes_from_text(rich_text_to_plain(prop.get("rich_text", [])))
    return None


def parse_notion_date(date_str: Optional[str]) -> Optional[datetime]:
    if not date_str:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
            return datetime.combine(date.fromisoformat(date_str), time.min, tzinfo=local_timezone())
        normalized = date_str.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=local_timezone())
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

    now = datetime.now(parsed.tzinfo or local_timezone())
    due_date = parsed.date()
    if due_date == now.date():
        return f"Today ({raw_due})"
    if due_date == now.date() + timedelta(days=1):
        return f"Due tomorrow ({raw_due})"
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
    status_property = get_status_property(db)
    if status_property:
        prop_name, prop_type = status_property
        status_name = resolve_status_option_name(db, "inbox")
        if prop_type == "status":
            payload["properties"][prop_name] = {"status": {"name": status_name}}
        elif prop_type == "select":
            payload["properties"][prop_name] = {"select": {"name": status_name}}
    created = notion_request("POST", "/pages", payload=payload)
    return created.get("url", "")


def get_task_page(page_id: str) -> dict[str, Any]:
    return notion_request("GET", f"/pages/{page_id}")


def get_status_property(database: dict[str, Any]) -> Optional[tuple[str, str]]:
    found = find_schema_property(database, NOTION_STATUS_PROPERTY, {"status", "select"}, ["status"])
    if not found:
        return None
    prop_name, prop = found
    return prop_name, prop.get("type", "")


def get_done_checkbox_property(database: dict[str, Any]) -> Optional[str]:
    found = find_schema_property(database, "Done", {"checkbox"}, ["done", "complete"])
    return found[0] if found else None


def resolve_status_option_name(database: dict[str, Any], status_key: str) -> str:
    status_property = get_status_property(database)
    if not status_property:
        fallback = STATUS_CANDIDATES[status_key][0]
        return fallback

    prop_name, prop_type = status_property
    prop = database.get("properties", {}).get(prop_name, {})
    options = prop.get(prop_type, {}).get("options", [])
    option_names = [option.get("name", "") for option in options if option.get("name")]
    for candidate in STATUS_CANDIDATES[status_key]:
        for option_name in option_names:
            if option_name.casefold() == candidate.casefold():
                return option_name
    return STATUS_CANDIDATES[status_key][0]


def set_task_status(page_id: str, status_key: str) -> dict[str, Any]:
    db = get_database_schema()
    status_property = get_status_property(db)
    if not status_property:
        if status_key == "done":
            done_checkbox = get_done_checkbox_property(db)
            if done_checkbox:
                return notion_request(
                    "PATCH",
                    f"/pages/{page_id}",
                    payload={"properties": {done_checkbox: {"checkbox": True}}},
                )
        raise RuntimeError("Could not find a Status/select property in the Notion database.")

    prop_name, prop_type = status_property
    status_name = resolve_status_option_name(db, status_key)
    if prop_type == "status":
        value = {"status": {"name": status_name}}
    else:
        value = {"select": {"name": status_name}}

    return notion_request("PATCH", f"/pages/{page_id}", payload={"properties": {prop_name: value}})


def get_active_tasks(include_snoozed: bool = False) -> list[dict[str, Any]]:
    active: list[dict[str, Any]] = []
    for task in query_all_tasks():
        if is_done_task(task):
            continue
        page_id = task_callback_id(task)
        if page_id and not include_snoozed and is_task_snoozed(page_id):
            continue
        active.append(task)
    return active


def build_suggestions_payload(
    mode: str,
    slot_minutes_override: Optional[int] = None,
    include_snoozed: bool = False,
) -> tuple[str, Optional[InlineKeyboardMarkup], list[dict[str, Any]]]:
    notion_error = notion_ready_error()
    if notion_error:
        return notion_error, None, []

    active_tasks = get_active_tasks(include_snoozed=include_snoozed)
    if not active_tasks:
        return "No open tasks found.", None, []

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
            [],
        )

    ranked = rank_tasks(filtered_tasks, slot_minutes=slot_minutes)[:SUGGESTION_LIMIT]
    lines = [heading, "", helper, ""]
    lines.extend(format_task_line(task, index=i + 1) for i, task in enumerate(ranked))
    return "\n".join(lines), build_task_list_keyboard(ranked, refresh_mode=mode), ranked


def onboarding_intro() -> str:
    return "\n".join(
        [
            "Connect your Notion task table.",
            "",
            "This bot works only with the Simple Task template:",
            ONBOARDING_TEMPLATE_URL,
            "",
            "First, send your Notion integration token.",
            "It starts with `secret_` or `ntn_`.",
        ]
    )


def ensure_user_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict[str, str]]:
    user_id = get_user_key(update)
    if not user_id:
        return None
    config = load_user_config(user_id)
    if config:
        bind_user_config(config)
        return config
    ACTIVE_NOTION_TOKEN.set(None)
    ACTIVE_DATABASE_ID.set(None)
    context.user_data["onboarding_step"] = "token"
    return None


async def require_config(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[dict[str, str]]:
    config = ensure_user_config(update, context)
    if config:
        return config
    if update.message:
        await update.message.reply_text(onboarding_intro())
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(onboarding_intro())
    return None


def describe_setup(include_env_fallback: bool = True) -> str:
    lines = ["Setup check"]
    lines.append(f"Telegram token: {'set' if BOT_TOKEN else 'missing'}")
    lines.append(f"Allowed username: @{ALLOWED_USERNAME}" if ALLOWED_USERNAME != "*" else "Allowed username: anyone")
    lines.append(f"Allowed user ID: {'set' if ALLOWED_USER_ID else 'not set'}")
    active_token = ACTIVE_NOTION_TOKEN.get()
    active_database = ACTIVE_DATABASE_ID.get()
    notion_token = active_token or (NOTION_TOKEN if include_env_fallback else None)
    notion_database = active_database or (NOTION_DATABASE_ID if include_env_fallback else None) or (
        NOTION_DATABASE_NAME if include_env_fallback else None
    )
    lines.append(f"Notion token: {'set' if notion_token else 'not connected'}")
    lines.append(f"Notion database: {notion_database or 'not connected'}")
    lines.append("Mode: manual Telegram + Notion")

    if notion_token:
        try:
            db = get_database_schema()
            props = db.get("properties", {})
            lines.append("")
            lines.append(f"Notion database title: {rich_text_to_plain(db.get('title', [])) or '<untitled>'}")
            for name, types, label in REQUIRED_TEMPLATE_PROPERTIES + OPTIONAL_TEMPLATE_PROPERTIES:
                keywords = [label, "name", "task"] if label == "title" else [label]
                found = find_schema_property(db, name, types, keywords)
                if found:
                    prop_name, prop = found
                    lines.append(f"{label}: {prop_name} ({prop.get('type')})")
                else:
                    required = any(item[2] == label for item in REQUIRED_TEMPLATE_PROPERTIES)
                    prefix = "missing required" if required else "missing optional"
                    lines.append(f"{label}: {prefix} property {name}")
            status_prop = get_status_property(db)
            if status_prop:
                prop_name, prop_type = status_prop
                options = props.get(prop_name, {}).get(prop_type, {}).get("options", [])
                option_names = ", ".join(option.get("name", "") for option in options if option.get("name"))
                lines.append(f"status options: {option_names or '<none>'}")
            template_errors = validate_template_database(db)
            lines.append("template: ok" if not template_errors else "template: invalid")
        except Exception as exc:
            lines.append(f"Notion check failed: {exc}")

    lines.append("Reminders: manual snooze only in this first version")
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not update.message:
        return
    config = ensure_user_config(update, context)
    if not config:
        await update.message.reply_text(onboarding_intro())
        return
    await update.message.reply_text("Choose a shortcut:", reply_markup=MENU)


async def send_suggestions(update: Update, mode: str) -> None:
    if not update.message:
        return
    try:
        text, keyboard, _ = build_suggestions_payload(mode)
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
    if not await require_config(update, context):
        return
    await send_suggestions(update, "now")


async def quick_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not await require_config(update, context):
        return
    await send_suggestions(update, "quick")


async def twenty_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not await require_config(update, context):
        return
    await send_suggestions(update, "twenty")


async def all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not await require_config(update, context):
        return
    await send_all_tasks(update)


async def new_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not await require_config(update, context):
        return
    await begin_new_task(update, context)


async def connect_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not update.message:
        return
    user_id = get_user_key(update)
    if user_id:
        delete_user_config(user_id)
    context.user_data.clear()
    context.user_data["onboarding_step"] = "token"
    await update.message.reply_text(onboarding_intro())


async def setup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not update.message:
        return
    user_id = get_user_key(update)
    config = load_user_config(user_id) if user_id else None
    if config:
        bind_user_config(config)
    else:
        ACTIVE_NOTION_TOKEN.set(None)
        ACTIVE_DATABASE_ID.set(None)
    await reply_long_text(update, describe_setup(include_env_fallback=False), add_menu=True)


async def handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> bool:
    if not update.message:
        return False

    step = context.user_data.get("onboarding_step")
    if not step:
        return False

    user_id = get_user_key(update)
    if not user_id:
        await update.message.reply_text("Could not identify your Telegram user. Try /start again.")
        return True

    if step == "token":
        notion_token = text.strip()
        if not (notion_token.startswith("secret_") or notion_token.startswith("ntn_")):
            await update.message.reply_text("That does not look like a Notion integration token. Send the token value.")
            return True
        context.user_data["pending_notion_token"] = notion_token
        context.user_data["onboarding_step"] = "database"
        await update.message.reply_text(
            "Now send the Notion table link from your duplicated Simple Task template.\n"
            "Make sure the database is shared with your Notion integration first."
        )
        return True

    if step == "database":
        notion_token = context.user_data.get("pending_notion_token")
        if not notion_token:
            context.user_data["onboarding_step"] = "token"
            await update.message.reply_text("Send your Notion integration token again.")
            return True

        database_id = extract_database_id(text)
        if not database_id:
            await update.message.reply_text("I could not find a Notion database ID in that link. Send the full database/table URL.")
            return True

        try:
            database = validate_notion_connection(notion_token, database_id)
        except RuntimeError as err:
            await update.message.reply_text(str(err))
            await update.message.reply_text(
                "Duplicate the Simple Task template, share that database with your integration, then send the table link again."
            )
            return True

        username = (update.effective_user.username or "") if update.effective_user else ""
        save_user_config(user_id, username, notion_token, database_id)
        context.user_data.pop("pending_notion_token", None)
        context.user_data.pop("onboarding_step", None)
        config = load_user_config(user_id)
        if config:
            bind_user_config(config)
        title = rich_text_to_plain(database.get("title", [])) or "your task database"
        await update.message.reply_text(
            f"Connected to {title}. Choose a shortcut:",
            reply_markup=MENU,
        )
        return True

    return False


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed_user(update):
        return
    if not update.message:
        return

    text = normalize_menu_text((update.message.text or "").strip())

    if await handle_onboarding(update, context, text):
        return

    if not await require_config(update, context):
        return

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
    if not await require_config(update, context):
        return

    data = query.data or ""

    if data.startswith("nav:"):
        mode = data.split(":", 1)[1]
        await query.answer()
        try:
            text, keyboard, _ = build_suggestions_payload(mode)
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
            until = utc_now() + timedelta(minutes=REMIND_LATER_MINUTES)
            snooze_task(page_id, until)
            await query.answer("Snoozed.")
            try:
                page = get_task_page(page_id)
            except RuntimeError:
                return
            await query.edit_message_text(
                format_task_detail(page)
                + f"\n\nSnoozed until {until.astimezone(local_timezone()).strftime('%H:%M')}.",
                reply_markup=build_task_detail_keyboard(page_id),
            )
            return

        status_by_action = {"done": "done", "doing": "doing"}
        status_key = status_by_action.get(action)
        if not status_key:
            await query.answer("Unknown action.")
            return

        try:
            page = set_task_status(page_id, status_key)
            clear_snooze(page_id)
        except RuntimeError as err:
            await query.answer("Could not update task.", show_alert=True)
            await query.edit_message_text(str(err))
            return

        await query.answer("Updated task.")
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
            BotCommand("connect", "Connect or replace Notion table"),
            BotCommand("setup", "Check bot configuration"),
        ]
    )


def build_application() -> Application:
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
    app.add_handler(CommandHandler("connect", connect_command))
    app.add_handler(CommandHandler("setup", setup_command))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Telegram Notion task reminder bot")
    parser.add_argument("--check", action="store_true", help="Print setup diagnostics without starting Telegram polling")
    args = parser.parse_args()

    try:
        if args.check:
            print(describe_setup())
            return

        app = build_application()
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    allowed_label = "anyone" if ALLOWED_USERNAME == "*" else f"@{ALLOWED_USERNAME}"
    logger.info("Bot started. Allowed user: %s", allowed_label)
    app.run_polling()


if __name__ == "__main__":
    main()
