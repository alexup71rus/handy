"""Canonical contracts shared by Handy's server tools and MCP validators."""

import json
from pathlib import Path


TOOL_SCHEMAS = json.loads((Path(__file__).with_name('resources') / 'tool-schemas.json').read_text())
