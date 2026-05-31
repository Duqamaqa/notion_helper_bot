import os
from datetime import date
from typing import Any, Optional

import requests
from dotenv import load_dotenv


load_dotenv()

NOTION_TOKEN = os.getenv("NOTION_TOKEN")
SOURCE_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
NOTION_VERSION = os.getenv("NOTION_VERSION", "2022-06-28")
TRACKER_NAME = "Simple Habit Tracker"


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


def find_existing_tracker() -> Optional[dict[str, Any]]:
    data = notion_request(
        "POST",
        "/search",
        {
            "query": TRACKER_NAME,
            "page_size": 10,
            "filter": {"value": "database", "property": "object"},
        },
    )
    for result in data.get("results", []):
        if plain_text(result.get("title", [])) == TRACKER_NAME:
            return result
    return None


def get_parent_page_id() -> str:
    if not SOURCE_DATABASE_ID:
        raise RuntimeError("NOTION_DATABASE_ID is missing from .env")

    database = notion_request("GET", f"/databases/{SOURCE_DATABASE_ID}")
    parent = database.get("parent", {})
    if parent.get("type") == "page_id" and parent.get("page_id"):
        return parent["page_id"]
    return create_host_page(database)


def create_host_page(database: dict[str, Any]) -> str:
    title_property = None
    for property_name, property_value in database.get("properties", {}).items():
        if property_value.get("type") == "title":
            title_property = property_name
            break

    if title_property is None:
        raise RuntimeError("Could not find a title property in the existing database.")

    page = notion_request(
        "POST",
        "/pages",
        {
            "parent": {"database_id": SOURCE_DATABASE_ID},
            "properties": {
                title_property: {
                    "title": [{"text": {"content": "Habit Tracker Home"}}],
                }
            },
            "children": [
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [
                            {
                                "type": "text",
                                "text": {
                                    "content": "Simple Habit Tracker lives inside this page.",
                                },
                            }
                        ]
                    },
                }
            ],
        },
    )
    return page["id"]


def create_tracker_database(parent_page_id: str) -> dict[str, Any]:
    return notion_request(
        "POST",
        "/databases",
        {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": TRACKER_NAME}}],
            "properties": {
                "Habit": {"title": {}},
                "Date": {"date": {}},
                "Done": {"checkbox": {}},
                "Category": {
                    "select": {
                        "options": [
                            {"name": "Health", "color": "green"},
                            {"name": "Mind", "color": "blue"},
                            {"name": "Work", "color": "yellow"},
                            {"name": "Home", "color": "orange"},
                        ]
                    }
                },
                "Frequency": {
                    "select": {
                        "options": [
                            {"name": "Daily", "color": "green"},
                            {"name": "Weekdays", "color": "blue"},
                            {"name": "Weekly", "color": "purple"},
                        ]
                    }
                },
                "Streak": {"number": {"format": "number"}},
                "Notes": {"rich_text": {}},
            },
        },
    )


def query_tracker_pages(database_id: str) -> list[dict[str, Any]]:
    data = notion_request("POST", f"/databases/{database_id}/query", {"page_size": 10})
    return data.get("results", [])


def create_habit(database_id: str, habit: str, category: str, notes: str) -> None:
    notion_request(
        "POST",
        "/pages",
        {
            "parent": {"database_id": database_id},
            "properties": {
                "Habit": {"title": [{"text": {"content": habit}}]},
                "Date": {"date": {"start": date.today().isoformat()}},
                "Done": {"checkbox": False},
                "Category": {"select": {"name": category}},
                "Frequency": {"select": {"name": "Daily"}},
                "Streak": {"number": 0},
                "Notes": {"rich_text": [{"text": {"content": notes}}]},
            },
        },
    )


def seed_habits(database_id: str) -> None:
    if query_tracker_pages(database_id):
        return

    habits = [
        ("Drink water", "Health", "Drink enough water today."),
        ("Move for 20 minutes", "Health", "Walk, stretch, gym, or any movement counts."),
        ("Read 10 pages", "Mind", "Keep it small and easy to finish."),
        ("Plan tomorrow", "Work", "Write the top 3 tasks for tomorrow."),
        ("Sleep on time", "Health", "Start winding down before bed."),
    ]
    for habit, category, notes in habits:
        create_habit(database_id, habit, category, notes)


def main() -> None:
    existing = find_existing_tracker()
    if existing:
        database = existing
        created = False
    else:
        database = create_tracker_database(get_parent_page_id())
        created = True

    database_id = database["id"]
    seed_habits(database_id)
    print(f"{'CREATED' if created else 'EXISTING'} {database_id} {database.get('url', '')}")


if __name__ == "__main__":
    main()
