"""Shared literal error display for the optional model-management console."""
from rich.style import Style
from rich.text import Text

_SYSTEM_ERROR_STYLE = Style(color='#ff6b6b', bgcolor='#1c0b0b')


def print_system_error(console, message):
    console.print(Text(str(message), style=_SYSTEM_ERROR_STYLE), markup=False, highlight=False)
