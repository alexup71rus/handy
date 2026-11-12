"""Dispatch only the typed operations supplied by Handy's native tools."""

from pathlib import Path

from .artifact_tool import create_artifact
from .file_access import is_private, require_user_path
from .schedule_tool import finish_change, list_schedules, prepare_change
from .memory_tool import finish_memory, prepare_memory, recall_memory


def execute_tool(app, body):
    if set(body) != {"tool", "root", "session", "arguments"}:
        raise ValueError("Expected tool, root, session and arguments")
    tool, directory, session, args = (body[key] for key in ("tool", "root", "session", "arguments"))
    if not isinstance(directory, str) or len(directory) > 4096 or not Path(directory).is_absolute():
        raise ValueError("Tool requires an absolute working directory")
    root = require_user_path(directory)
    if not root.is_dir() or is_private(root):
        raise ValueError("Tool requires an existing workspace outside private directories")
    if (not isinstance(session, str) or not 1 <= len(session) <= 128
            or any(ord(c) < 32 or ord(c) == 127 or 0xD800 <= ord(c) <= 0xDFFF for c in session)):
        raise ValueError("Invalid tool session ID")
    if not isinstance(args, dict):
        raise ValueError("Tool arguments must be an object")

    with app._session_lock(session):
        app._ensure_running()
        try:
            if tool == "artifact" and set(args) <= {"kind", "options"}:
                return create_artifact(app, root, session, args.get("kind"), args.get("options", {}))
            if tool == "schedule_list" and set(args) <= {"artifact"}:
                return list_schedules(app, root, session, artifact=args.get("artifact", False))
            if tool == "schedule_prepare":
                return prepare_change(app, root, session, args)
            if tool == "schedule_finish" and set(args) == {"plan_id", "approved"}:
                return finish_change(app, root, session, args["plan_id"], approved=args["approved"])
            if tool == "memory_prepare":
                return prepare_memory(app, root, session, args)
            if tool == "memory_recall":
                return recall_memory(app, root, session, args)
            if tool == "memory_finish" and set(args) == {"plan_id", "approved"}:
                return finish_memory(app, root, session, args["plan_id"], approved=args["approved"])
            if tool == "browser":
                return app.browser.execute(args)
            raise ValueError("Неизвестный инструмент или набор параметров.")
        except PermissionError as error:
            return {"status": "error", "text": str(error), "data": {"code": "tool_scope"}}
        except ValueError as error:
            return {"status": "needs_input", "text": str(error), "data": {"code": "invalid_tool_arguments"}}
