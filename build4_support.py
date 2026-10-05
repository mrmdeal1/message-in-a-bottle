import sys

# Preserve the proven Build 4 implementation under build4_base, then expose it
# as build4_support so the existing usercustomize startup hook keeps working.
import build4_base as _base
sys.modules[__name__] = _base

# Build 5 patches the Build 4 moderation functions in place and adds the Apple
# Guideline 1.2 safety layer before server.py creates the FastAPI app.
import build5_support  # noqa: F401,E402
