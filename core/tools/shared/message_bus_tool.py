"""MessageBus tool - cross-session message passing."""

from __future__ import annotations

from core.tools.shared.base import Tool, ToolResult
from core.message_bus import get_message_bus


class MessageBusTool(Tool):
    name = "message_bus"
    description = (
        "**Cross-session message passing.**\n"
        "Send and receive messages between different chat sessions.\n\n"
        "## Actions:\n"
        "- **send**: Send a message to another session\n"
        "- **receive**: Receive all pending messages for current session\n"
        "- **check**: Check if there are unread messages (non-consuming)\n"
        "- **list_sessions**: List all registered sessions\n"
        "- **clear**: Clear all messages for current session\n\n"
        "## Use cases:\n"
        "- Background SubAgent reports results to main session\n"
        "- Cross-session coordination (one session needs data from another)\n"
        "- Status notifications between sessions\n\n"
        "## Note:\n"
        "- Messages are ephemeral (in-memory only, not persisted)\n"
        "- Messages survive within the same server session\n"
        "- Use `receive` to consume messages, `check` to peek without consuming"
    )

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["send", "receive", "check", "list_sessions", "clear"],
                    "description": "Action to perform.",
                },
                "to_session": {
                    "type": "string",
                    "description": "Target session ID (required for 'send' action).",
                },
                "message": {
                    "type": "string",
                    "description": "Message content (required for 'send' action).",
                },
                "message_type": {
                    "type": "string",
                    "enum": ["text", "command", "status"],
                    "description": "Message type (default: 'text').",
                    "default": "text",
                },
            },
            "required": ["action"],
        }

    def execute(
        self,
        action: str = "receive",
        to_session: str | None = None,
        message: str | None = None,
        message_type: str = "text",
    ) -> ToolResult:
        """Execute message_bus action."""
        bus = get_message_bus()

        # Determine current session ID
        current_session_id = ""
        if self.session_manager:
            current_session_id = self.session_manager.session_id

        if action == "send":
            if not to_session:
                return ToolResult("Error: 'to_session' is required for 'send' action", error=True)
            if not message:
                return ToolResult("Error: 'message' is required for 'send' action", error=True)
            bus.send(current_session_id, to_session, message, message_type)
            return ToolResult(f"Message sent to session '{to_session}'")

        elif action == "receive":
            messages = bus.receive(current_session_id, mark_read=True)
            if not messages:
                return ToolResult("No pending messages")
            lines = [f"Received {len(messages)} message(s):"]
            for msg in messages:
                sender = msg["sender_session_id"] or "unknown"
                content = msg["content"]
                mtype = msg.get("message_type", "text")
                lines.append(f"  [{mtype}] From {sender}: {content}")
            return ToolResult("\n".join(lines))

        elif action == "check":
            count = bus.unread_count(current_session_id)
            if count == 0:
                return ToolResult("No unread messages")
            return ToolResult(f"{count} unread message(s)")

        elif action == "list_sessions":
            sessions = bus.list_sessions()
            if not sessions:
                return ToolResult("No registered sessions")
            lines = [f"Registered sessions ({len(sessions)}):"]
            for s in sessions:
                name = s["name"] or "(unnamed)"
                sid = s["session_id"]
                unread = s["unread_count"]
                marker = f" [{unread} unread]" if unread > 0 else ""
                lines.append(f"  - {sid} ({name}){marker}")
            return ToolResult("\n".join(lines))

        elif action == "clear":
            count = bus.clear(current_session_id)
            return ToolResult(f"Cleared {count} message(s)")

        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)
