"""Integration test — runs INSIDE iTerm2's Python environment.

To run:
  1. Copy this file to ~/Library/Application Support/iTerm2/Scripts/
  2. Run it from iTerm2 → Scripts menu
  3. Watch for tab color changes in a new test tab
"""

import asyncio
import json
import os
import time
from pathlib import Path

import iterm2

FLASH_DIR = os.environ.get("CLAUDE_ITERM2_TAB_STATUS_DIR", "/tmp/claude-tab-status")


async def main(connection):
    app = await iterm2.async_get_app(connection)
    window = app.current_terminal_window

    if not window:
        print("ERROR: No current window")
        return

    # Create a test tab
    tab = await window.async_create_tab()
    session = tab.current_session
    tty = await session.async_get_variable("tty")
    print(f"Test session TTY: {tty}")

    # Capture original state
    profile = await session.async_get_profile()
    orig_color = profile.tab_color
    print(f"Original tab color: R={orig_color.red} G={orig_color.green} B={orig_color.blue}")

    # Write a signal file pointing to this TTY
    signal_dir = Path(FLASH_DIR)
    signal_dir.mkdir(exist_ok=True)
    signal = {
        "session_id": "integration-test",
        "type": "attention",
        "message": "Integration test",
        "project": "test",
        "cwd": "/tmp",
        "tty": tty,
        "pid": "0",
        "ts": str(int(time.time())),
    }
    signal_file = signal_dir / "integration-test.json"
    signal_file.write_text(json.dumps(signal))
    print("Signal written — tab should start flashing within 1-2 seconds")

    # Wait for flashing to be visible
    await asyncio.sleep(5)

    # Remove signal
    signal_file.unlink(missing_ok=True)
    print("Signal removed — tab should stop flashing within 1-2 seconds")

    await asyncio.sleep(3)

    # Verify restoration
    profile2 = await session.async_get_profile()
    restored_color = profile2.tab_color
    rc = restored_color
    print(f"Restored tab color: R={rc.red} G={rc.green} B={rc.blue}")

    if (
        abs(restored_color.red - orig_color.red) < 0.01
        and abs(restored_color.green - orig_color.green) < 0.01
        and abs(restored_color.blue - orig_color.blue) < 0.01
    ):
        print("✓ PASS: Tab color restored correctly")
    else:
        print("✗ FAIL: Tab color not restored")

    print("Integration test complete.")


iterm2.run_until_complete(main)
