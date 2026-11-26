"""Loaded by every Python process the sandbox starts (PYTHONPATH points here).

Puts the workspace at the end of sys.path. Modules saved there can still be
imported, but they cannot shadow the standard library or the installed
packages (a saved email.py would otherwise break urllib), and because this
file is found first, a sitecustomize.py saved in the workspace never runs.
This directory is part of the image, so snippets cannot change it.
"""

import os
import sys

_workspace = os.environ.get("SANDBOX_WORKSPACE", "")
if _workspace and _workspace not in sys.path:
    sys.path.append(_workspace)
del _workspace
