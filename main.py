"""Android entry point: python-for-android requires main.py in the app dir.

Runs at module level on purpose -- p4a executes main.py via runpy, where
__name__ is not "__main__".
"""

from kivy_client import sHadesClientApp

sHadesClientApp().run()
