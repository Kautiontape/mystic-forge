import os
import sys

# Make the repo-root modules (server.py) importable from tests/.
sys.path.insert(0, os.path.dirname(__file__))
