# Root-level entry point for Streamlit Cloud
# Delegates execution to dispatch_skool_scraper/app.py
import sys
import os

_root    = os.path.dirname(os.path.abspath(__file__))
_app_dir = os.path.join(_root, "dispatch_skool_scraper")
_app_file = os.path.join(_app_dir, "app.py")

# Make 'from fmcsa_scraper import ...' work
if _app_dir not in sys.path:
    sys.path.insert(0, _app_dir)

# Change CWD so relative file references inside app.py work
os.chdir(_app_dir)

# Run the actual app with correct __file__ context
with open(_app_file, "r", encoding="utf-8") as _f:
    _code = _f.read()

exec(compile(_code, _app_file, "exec"), {"__file__": _app_file, "__name__": "__main__"})
