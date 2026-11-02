#!/usr/bin/env python3
"""Android entry point for sHades client.

p4a executes this module via PyRun_SimpleFile with __name__ == "__main__".
stdout/stderr are redirected to Android logcat (tag: python/stderr).
"""

import sys
import traceback
import time

from kivy_client import sHadesClientApp


def main():
    """Entry point called from Android activity."""
    try:
        app = sHadesClientApp()
        app.run()
    except Exception:
        traceback.print_exc()
        # Keep process alive briefly so we can read the traceback in logcat
        time.sleep(30)
        sys.exit(1)


if __name__ == "__main__":
    main()