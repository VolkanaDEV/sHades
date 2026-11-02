#!/usr/bin/env python3
"""Android entry point for sHades client.

p4a executes this module via PyRun_SimpleFile with __name__ == "__main__".
stdout/stderr are redirected to Android logcat (tags: python / stderr).
"""

import sys
import time
import traceback


def main():
    """Entry point called from Android activity. All Python execution
    is wrapped so any import/runtime crash prints a traceback to logcat."""
    try:
        # Deferred import so import errors are caught below
        from kivy_client import sHadesClientApp
        app = sHadesClientApp()
        app.run()
    except Exception:
        traceback.print_exc()
        # Keep process alive briefly so we can read the traceback in logcat
        time.sleep(30)
        sys.exit(1)


if __name__ == "__main__":
    main()
