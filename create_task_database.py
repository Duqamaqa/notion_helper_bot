import os
from datetime import date, timedelta
from typing import Any, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_DATABASE_NAME = os.getenv("NOTION_DATABASE_NAME", "To do list")
NOTION_PARENT_PAGE_ID = os.getenv("NOTION_PARENT_PAGE_ID")

TITLE_PROPERTY = os.getenv("NOTION_TITLE_PROPERTY", "Name")
DESCRIPTION_PROPERTY = os.getenv("NOTION_DESCRIPTION_PROPERTY", "Text")
DUE_DATE_PROPERTY = os.getenv("NOTION_DUE_DATE_PROPERTY", "Due Date")
STATUS_PROPERTY = os.getenv("NOTION_STATUS_PROPERTY", "Status")
ESTIMATE_PROPERTY = os.getenv("NOTION_ESTIMATE_PROPERTY", "Estimated time")
INBOX_STATUS = os.getenv("NOTION_INBOX_STATUS", "inbox")
DOING_STATUS = os.getenv("NOTION_DOING_STATUS", "progress")
DONE_STATUS = os.getenv("NOTION_DONE_STATUS", "done")


def notion_request(method: str, path: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if not NOTION_TOKEN:
        raise RuntimeError("NOTION_TOKEN is missing from .env")

    response = requests.request(
        method,
        f"https://api.notion.com/v1{path}",
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
            message = response.json().get("message", response.text)
        except ValueError:
            message = response.text
        raise RuntimeError(f"Notion API {response.status_code}: {message}")
    return response.json()


def plain_text(chunks: list[dict[str, Any]]) -> str:
    return "".join(chunk.get("plain_text", "") for chunk in chunks).strip()


def find_existing_task_database() -> Optional[dict[str, Any]]:
    if NOTION_DATABASE_ID:
        return notion_request("GET", f"/databases/{NOTION_DATABASE_ID}")

    data = notion_request(
        "POST",
        "/search",
        {
            "query": NOTION_DATABASE_NAME,
            "page_size": 20,
            "filter": {"value": "database", "property": "object"},
        },
    )
    for result in data.get("results", []):
        if plain_text(result.get("title", [])).casefold() == NOTION_DATABASE_NAME.casefold():
            return result
    return None


def resolve_parent_page_id() -> str:
    if NOTION_PARENT_PAGE_ID:
        return NOTION_PARENT_PAGE_ID

    if NOTION_DATABASE_ID:
        database = notion_request("GET", f"/databases/{NOTION_DATABASE_ID}")
        parent = database.get("parent", {})
        if parent.get("type") == "page_id" and parent.get("page_id"):
            return parent["page_id"]

    raise RuntimeError(
        "Set NOTION_PARENT_PAGE_ID to create a new PRD task database, "
        "or set NOTION_DATABASE_ID to reuse an existing one."
    )


def create_task_database(parent_page_id: str) -> dict[str, Any]:
    return notion_request(
        "POST",
        "/databases",
        {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": NOTION_DATABASE_NAME}}],
            "properties": {
                TITLE_PROPERTY: {"title": {}},
                DESCRIPTION_PROPERTY: {"rich_text": {}},
                DUE_DATE_PROPERTY: {"date": {}},
                STATUS_PROPERTY: {
                    "select": {
                        "options": [
                            {"name": INBOX_STATUS, "color": "gray"},
                            {"name": DOING_STATUS, "color": "blue"},
                            {"name": DONE_STATUS, "color": "green"},
                        ]
                    }
                },
                ESTIMATE_PROPERTY: {"number": {"format": "number"}},
            },
        },
    )


def query_pages(database_id: str) -> list[dict[str, Any]]:
    data = notion_request("POST", f"/databases/{database_id}/query", {"page_size": 10})
    return data.get("results", [])


def create_task(database_id: str, title: str, estimate: int, days_until_due: int, description: str) -> None:
    notion_request(
        "POST",
        "/pages",
        {
            "parent": {"database_id": database_id},
            "properties": {
                TITLE_PROPERTY: {"title": [{"text": {"content": title}}]},
                DESCRIPTION_PROPERTY: {"rich_text": [{"text": {"content": description}}]},
                DUE_DATE_PROPERTY: {"date": {"start": (date.today() + timedelta(days=days_until_due)).isoformat()}},
                STATUS_PROPERTY: {"select": {"name": INBOX_STATUS}},
                ESTIMATE_PROPERTY: {"number": estimate},
            },
        },
    )


def seed_tasks(database_id: str) -> None:
    if query_pages(database_id):
        return

    tasks = [
        ("Reply to an important message", 10, 1, "Small task for a short free window."),
        ("Review weekly priorities", 20, 2, "Pick the next concrete actions."),
        ("Deep work block", 60, 4, "Use a longer free slot."),
    ]
    for title, estimate, days_until_due, description in tasks:
        create_task(database_id, title, estimate, days_until_due, description)


def main() -> None:
    database = find_existing_task_database()
    created = False
    if not database:
        database = create_task_database(resolve_parent_page_id())
        created = True

    database_id = database["id"]
    seed_tasks(database_id)
    print(f"{'CREATED' if created else 'EXISTING'} {database_id} {database.get('url', '')}")


if __name__ == "__main__":
    main()
