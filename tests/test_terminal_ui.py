"""System failures have a terminal-only style, never interpreted message markup."""

import io
import unittest

from rich.console import Console

from folder_memory.terminal_ui import print_system_error


class SystemErrorDisplayTests(unittest.TestCase):
    def test_truecolor_terminal_uses_red_text_dark_background_and_literal_copy(self):
        output = io.StringIO()
        console = Console(file=output, force_terminal=True, no_color=False, color_system="truecolor", width=160)
        message = "HTTP 401 [bold]literal[/bold] [link=https://example.test]URL[/link]"
        print_system_error(console, message)
        rendered = output.getvalue()
        self.assertIn("38;2;255;107;107", rendered)
        self.assertIn("48;2;28;11;11", rendered)
        self.assertIn(message, rendered)
        self.assertNotIn("\x1b]8;", rendered)
        self.assertEqual(rendered.count("\n"), 1)

    def test_redirected_output_remains_plain_and_complete(self):
        output = io.StringIO()
        message = "Не удалось получить ответ: HTTP 401 [bold]literal[/bold]"
        print_system_error(Console(file=output, force_terminal=False, width=160), message)
        self.assertEqual(output.getvalue(), message + "\n")

    def test_no_color_mode_does_not_force_ansi_even_for_a_terminal(self):
        output = io.StringIO()
        message = "Не удалось сохранить настройку."
        print_system_error(Console(file=output, force_terminal=True, no_color=True, width=160), message)
        self.assertEqual(output.getvalue(), message + "\n")


if __name__ == "__main__":
    unittest.main()
