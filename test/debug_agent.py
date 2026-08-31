"""Quick debug script to test RootAgent with DGX local endpoint."""

import os
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# 使用 DGX 本地端点（不消耗真实 API 配额）
from test.conftest import make_dgx_config
from core.root_agent import RootAgent
from core.session import SessionManager


def get_default_workspace_uuid() -> str:
    """Find the default workspace UUID."""
    from core.config import DATA_DIR
    workspace_dir = DATA_DIR / "workspace"
    if not workspace_dir.exists():
        return ""
    for item in workspace_dir.iterdir():
        if item.is_dir():
            import json
            config_file = item / "setting.json"
            if config_file.exists():
                with open(config_file, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("workspace_name", "").lower() == "default":
                    return item.name
    return ""


def main():
    print("Using DGX local endpoint...")
    config = make_dgx_config("anthropic")
    print(f"  Model: {config.model.name}")
    print(f"  API URL: {config.model.base_url}")
    print(f"  Interface: {config.model.interface_type}")

    workspace_uuid = get_default_workspace_uuid()
    if not workspace_uuid:
        print("  ERROR: No default workspace found")
        return
    print(f"  Workspace: {workspace_uuid}")

    # Use a temp directory for testing
    import tempfile
    with tempfile.TemporaryDirectory() as tmpdir:
        test_dir = Path(tmpdir)
        # Create a test file
        (test_dir / "test.txt").write_text("Hello, World!", encoding="utf-8")
        print(f"  Test dir: {test_dir}")

        print("\nCreating RootAgent...")
        agent = RootAgent(config, cwd=str(test_dir), workspace_uuid=workspace_uuid)

        # Create a NEW session
        new_session = SessionManager.create_new_session(agent.sessions_dir, "Debug Test")
        agent.switch_session(new_session.session_id)

        print(f"  NEW Session ID: {agent.current_session_id}")
        print(f"  Session dir: {agent.session_dir}")
        print(f"  Initial messages: {len(agent.messages)}")
        print(f"  Tools: {len(agent.tools)}")

        print("\n=== Test 1: Simple conversation ===")
        outputs = []
        print("  Sending: 'What is 2+2? Reply with just the number.'")
        agent.run(
            "What is 2+2? Reply with just the number.",
            on_text=lambda t: outputs.append(t),
        )

        full_output = "".join(outputs)
        print(f"  Response: {full_output}")
        print(f"  Messages after: {len(agent.messages)}")

        # Verify
        assert len(agent.messages) == 2, f"Expected 2 messages, got {len(agent.messages)}"
        assert "4" in full_output, f"Expected '4' in output"
        print("  [PASS] Test 1 passed!")

        print("\n=== Test 2: Tool execution (bash) ===")
        tool_calls = []
        tool_results = []
        outputs2 = []

        print("  Sending: 'Run echo Hello Test and show output.'")
        agent.run(
            "Run 'echo Hello Test' and show me the output.",
            on_text=lambda t: outputs2.append(t),
            on_tool_call=lambda name, inp, tid: tool_calls.append((name, inp, tid)),
            on_tool_result=lambda name, out, err, tid: tool_results.append((name, out, err, tid)),
        )

        print(f"  Tool calls: {len(tool_calls)}")
        print(f"  Tool results: {len(tool_results)}")

        if tool_calls:
            tool_names = [tc[0] for tc in tool_calls]
            print(f"  Tools used: {tool_names}")
            assert "bash" in tool_names, f"Expected 'bash' tool"
            print("  [PASS] Test 2 passed!")

        # Check output files
        output_files = list(agent.session_dir.glob("*.txt"))
        print(f"  Output files: {len(output_files)}")
        if output_files:
            print(f"  First file: {output_files[0].name}")

        print("\n=== Test 3: Session persistence ===")
        # Save current state
        agent._sync_to_session_manager()
        agent.session_manager.save()
        saved_msg_count = len(agent.messages)
        saved_session_id = agent.current_session_id
        print(f"  Saved {saved_msg_count} messages to session {saved_session_id}")

        # Create a new agent and load the session
        agent2 = RootAgent(config, cwd=str(test_dir), workspace_uuid=workspace_uuid)
        agent2.switch_session(saved_session_id)

        print(f"  Loaded session: {agent2.current_session_id}")
        print(f"  Loaded messages: {len(agent2.messages)}")

        assert len(agent2.messages) == saved_msg_count, \
            f"Expected {saved_msg_count} messages, got {len(agent2.messages)}"
        print("  [PASS] Test 3 passed!")

        agent2.cleanup()
        agent.cleanup()

        print("\n=== All tests passed! ===")


if __name__ == "__main__":
    main()
