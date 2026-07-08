"""Thread-safe agent activity tracking."""
import threading

# Thread-safe activity tracker: {session_id: {"text": str, "tools_run": int}}
_agent_activity: dict[str, dict] = {}
_activity_lock = threading.Lock()


def get_agent_activity(session_id: str) -> dict:
    """Get current agent activity for a session."""
    with _activity_lock:
        return dict(_agent_activity.get(session_id, {"text": "", "tools_run": 0}))


def set_agent_activity(session_id: str, text: str, tools_run: int = 0):
    """Update agent activity for a session."""
    with _activity_lock:
        _agent_activity[session_id] = {"text": text, "tools_run": tools_run}


def clear_agent_activity(session_id: str):
    """Clear agent activity for a session."""
    with _activity_lock:
        _agent_activity.pop(session_id, None)
