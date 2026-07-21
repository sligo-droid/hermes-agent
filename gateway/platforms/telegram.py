"""Compatibility alias for the bundled Telegram platform plugin."""

from __future__ import annotations

import sys

from plugins.platforms.telegram import adapter as _adapter

# Preserve module identity so monkeypatches and class comparisons through the
# legacy import path operate on the plugin implementation itself.
sys.modules[__name__] = _adapter
