"""Markdown formatting shared by deterministic data analysis."""

import re


def inline_literal(value):
    fence = '`' * (1 + max((len(run) for run in re.findall(r'`+', value)), default=0))
    padding = ' ' if value.startswith('`') or value.endswith('`') else ''
    return fence + padding + value + padding + fence
