#!/usr/bin/env python3
"""Android entry point for sHades client.

p4a executes this module via runpy where __name__ is not "__main__".
The app must be started from the Android activity's onCreate callback.
"""

from kivy_client import sHadesClientApp


def main():
    """Entry point called from Android activity."""
    app = sHadesClientApp()
    app.run()


if __name__ == "__main__":
    main()
