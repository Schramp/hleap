import os
import sys

# plugin.py lives in the repository root, fake_trace.py next to the tests
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
