"""Session-local TODO tool state."""

from __future__ import annotations


CURRENT_TODOS: list[dict] = []


def run_todo_write(todos: list) -> str:
    """Validate and display the current structured task list."""
    global CURRENT_TODOS

    valid_statuses = {"pending", "in_progress", "completed"}
    normalized = []
    for index, todo in enumerate(todos, start=1):
        if not isinstance(todo, dict):
            return f"Error: todo #{index} must be an object"

        content = str(todo.get("content", "")).strip()
        status = todo.get("status", "")
        if not content:
            return f"Error: todo #{index} content must not be empty"
        if status not in valid_statuses:
            return f"Error: todo #{index} has invalid status: {status}"

        normalized.append({"content": content, "status": status})

    CURRENT_TODOS = normalized

    icons = {
        "pending": " ",
        "in_progress": ">",
        "completed": "x",
    }
    lines = ["\n## Current Tasks"]
    for todo in CURRENT_TODOS:
        lines.append(f"  [{icons[todo['status']]}] {todo['content']}")
    print("\n".join(lines))

    return f"Updated {len(CURRENT_TODOS)} tasks"

