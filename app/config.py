"""Runtime configuration read from environment variables."""
from __future__ import annotations

import os

from .models import CompatibilityLevel

DEFAULT_COMPATIBILITY = CompatibilityLevel(
    os.environ.get("SCHEMA_REGISTRY_COMPATIBILITY", "BACKWARD")
)
