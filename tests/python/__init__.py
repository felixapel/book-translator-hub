"""Python test suite."""

import os
import tempfile

# Modules that import server open the cache at import time; keep that
# runtime database (and keys beside it) out of the checkout.
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(prefix="bt-tests-"), "translations.db"))
