"""MessageBus tool - cross-session message passing."""

from __future__ import annotations

from core.tools.base import Tool, ToolResult, UNTRUSTED_DATA_BEGIN, UNTRUSTED_DATA_END
from core.message_bus import get_message_bus


class MessageBusTool(Tool):
    name = "message_bus"
    description = (
        "**Cross-session and cross-agent message passing.**\n"
        "Send and receive messages between different chat sessions or agents by name.\n\n"
        "## Actions:\n"
        "- **send**: Send a message to another session (by session_id)\n"
        "- **send_to_agent**: Send a message to an agent by name (agent_name/exec_id/label)\n"
        "- **receive**: Receive all pending messages for current session\n"
        "- **check**: Check if there are unread messages (non-consuming)\n"
        "- **list_sessions**: List all registered sessions\n"
        "- **list_agents**: List all registered agents (by name, with unread counts)\n"
        "- **clear**: Clear all messages for current session\n\n"
        "## Use cases:\n"
        "- Background Worker/Lite sub-agent reports results to main session\n"
        "- Cross-session coordination (one session needs data from another)\n"
        "- Agent-to-agent messaging (master sends instructions to worker by label/exec_id)\n"
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
                    "enum": ["send", "send_to_agent", "receive", "check",
                             "list_sessions", "list_agents", "clear"],
                    "description": "Action to perform.",
                },
                "to_session": {
                    "type": "string",
                    "description": "Target session ID (required for 'send' action).",
                },
                "to_agent": {
                    "type": "string",
                    "description": (
                        "Target agent name (required for 'send_to_agent' action). "
                        "Can be an exec_id (e.g. 'agent-1') or a label (e.g. 'translate-doc')."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "Message content (required for 'send' and 'send_to_agent' actions).",
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
        to_agent: str | None = None,
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
            sent = bus.send(current_session_id, to_session, message, message_type)
            if not sent:
                return ToolResult(
                    f"Error: target session '{to_session}' is not registered. "
                    "Check the session ID (list_sessions action) — the message was not sent.",
                    error=True,
                )
            return ToolResult(f"Message sent to session '{to_session}'")

        elif action == "send_to_agent":
            if not to_agent:
                return ToolResult(
                    "Error: 'to_agent' is required for 'send_to_agent' action",
                    error=True,
                )
            if not message:
                return ToolResult(
                    "Error: 'message' is required for 'send_to_agent' action",
                    error=True,
                )
            # 获取当前 agent 名字（优先用注册的 agent name，回退 session_id）
            from_agent = bus.get_session_agent(current_session_id) or current_session_id
            sent = bus.send_to_agent(from_agent, to_agent, message, message_type)
            if not sent:
                return ToolResult(
                    f"Error: target agent '{to_agent}' is not registered. "
                    "Check available agents (list_agents action) — the message was not sent.",
                    error=True,
                )
            return ToolResult(f"Message sent to agent '{to_agent}'")

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
            return ToolResult(
                UNTRUSTED_DATA_BEGIN + "\n".join(lines) + UNTRUSTED_DATA_END
            )

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

        elif action == "list_agents":
            agents = bus.list_agents()
            if not agents:
                return ToolResult("No registered agents")
            lines = [f"Registered agents ({len(agents)}):"]
            for a in agents:
                name = a["agent_name"]
                sid = a["session_id"]
                unread = a["unread_count"]
                marker = f" [{unread} unread]" if unread > 0 else ""
                lines.append(f"  - {name} (session: {sid}){marker}")
            return ToolResult("\n".join(lines))

        elif action == "clear":
            count = bus.clear(current_session_id)
            return ToolResult(f"Cleared {count} message(s)")

        else:
            return ToolResult(f"Error: unknown action '{action}'", error=True)
