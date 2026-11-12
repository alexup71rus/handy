"""The model-settings console keeps cancellation and reported-key regressions."""

import asyncio
import unittest

from prompt_toolkit import PromptSession
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from folder_memory.terminal_cancel import TurnCancelled, install_prompt_cancel


class TerminalEditingTests(unittest.IsolatedAsyncioTestCase):
    async def edit(self, text, cursor, keys):
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput(), multiline=False)
            install_prompt_cancel(session)
            pending = asyncio.create_task(session.prompt_async(default=Document(text, cursor)))
            try:
                pipe.send_text(keys + '\r')
                return await asyncio.wait_for(pending, 2), session
            finally:
                if not pending.done():
                    pending.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await pending

    async def test_ctrl_u_clears_the_whole_line_from_any_column(self):
        for cursor in (0, 5, len('первая строка')):
            with self.subTest(cursor=cursor):
                result, session = await self.edit('первая строка', cursor, '\x15новая')
                self.assertEqual(result, 'новая')
                self.assertEqual(session.clipboard.get_data().text, 'первая строка')

    async def test_reported_command_backspace_clears_current_line(self):
        for code in (8, 127):
            for event in ('', ':1', ':2'):
                with self.subTest(code=code, event=event):
                    result, session = await self.edit('удалить всё', 4, f'\x1b[{code};9{event}u|')
                    self.assertEqual(result, '|')
                    self.assertEqual(session.clipboard.get_data().text, 'удалить всё')

    async def test_reported_backspace_key_release_does_not_edit_or_insert_escape_text(self):
        for modifier in (3, 5, 9):
            with self.subTest(modifier=modifier):
                result, _ = await self.edit('keep this', 4, f'\x1b[127;{modifier}:3u')
                self.assertEqual(result, 'keep this')

    async def test_custom_line_deletion_can_be_yanked_and_undone(self):
        for keys in ('\x15\x19', '\x15\x1f', '\x15\x15\x19'):
            with self.subTest(keys=repr(keys)):
                result, _ = await self.edit('second line', 5, keys)
                self.assertEqual(result, 'second line')

    async def test_reported_control_alt_backspace_preserves_word_deletion(self):
        for keys, expected in (('\x1b[127;5u', 'alpha '), ('\x1b[8;5u', 'alpha '),
                               ('\x1b[127;3u', 'alpha beta.'), ('\x1b[8;3u', 'alpha beta.')):
            with self.subTest(keys=repr(keys)):
                result, _ = await self.edit('alpha beta.gamma', 16, keys)
                self.assertEqual(result, expected)

    async def test_reported_command_arrows_move_to_line_edges(self):
        for key, expected in (('\x1b[1;9D', '|second line'), ('\x1b[1;9C', 'second line|')):
            with self.subTest(key=repr(key)):
                result, _ = await self.edit('second line', 5, key + '|')
                self.assertEqual(result, expected)

    async def test_escape_cancels_a_form_without_submitting_its_draft(self):
        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput(), multiline=False)
            session._handy_cancel_turn = True
            install_prompt_cancel(session)
            async def form():
                try:
                    await session.prompt_async(default='unsubmitted value')
                except TurnCancelled:
                    return 'cancelled'
                return 'submitted'
            pending = asyncio.create_task(form())
            pipe.send_text('\x1b')
            self.assertEqual(await asyncio.wait_for(pending, 2), 'cancelled')
            session._handy_cancel_turn = False
            resumed = asyncio.create_task(session.prompt_async())
            pipe.send_text('next value\r')
            self.assertEqual(await asyncio.wait_for(resumed, 2), 'next value')


if __name__ == '__main__':
    unittest.main()
