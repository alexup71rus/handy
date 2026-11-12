"""Drive the inline picker through terminal bytes, not binding callbacks."""

import asyncio
from pathlib import Path
import subprocess
import sys
import unittest

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from folder_memory.terminal_picker import PickerAction, PickerItem, _Picker


class TerminalPickerTests(unittest.IsolatedAsyncioTestCase):
    async def choose(self, items, keys, *, query="", selected_id=None, actions=()):
        with create_pipe_input() as pipe:
            picker = _Picker("Модели", items, query=query, selected_id=selected_id,
                             actions=actions, input=pipe, output=DummyOutput())
            pending = asyncio.create_task(picker.app.run_async())
            try:
                await asyncio.sleep(.01)
                pipe.send_text(keys)
                return await asyncio.wait_for(pending, 2), picker
            finally:
                if not pending.done():
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        pass

    async def test_search_matches_all_words_case_insensitively_across_model_and_provider(self):
        result, _ = await self.choose([
            PickerItem("gpt-big", "GPT full", detail="OpenAI"),
            PickerItem("gpt-mini", "GPT Mini", detail="OpenAI", search="technical"),
            PickerItem("other-mini", "Mini", detail="Other API"),
        ], "OPENAI mini\r")
        self.assertEqual((result.action, result.id, result.query), ("select", "gpt-mini", "OPENAI mini"))

    async def test_unicode_query_does_not_need_transliteration(self):
        result, _ = await self.choose([
            PickerItem("first", "Локальная модель", detail="Домашний сервер"),
            PickerItem("second", "Переводчик", detail="Облако"),
        ], "ДОМАШНИЙ модель\r")
        self.assertEqual(result.id, "first")

    async def test_callers_can_switch_to_model_results_while_searching(self):
        seen = []

        def items(query):
            seen.append(query)
            return [PickerItem("model:exact-id", "Matched model")] if query else [PickerItem("provider:api", "API")]

        result, _ = await self.choose(items, "gpt\r")
        self.assertEqual(result.id, "model:exact-id")
        self.assertEqual(result.query, "gpt")
        self.assertEqual(seen[-1], "gpt")

    async def test_selected_id_and_current_marker_choose_correct_initial_row(self):
        values = [PickerItem("a", "First"), PickerItem("b", "Second", selected=True), PickerItem("c", "Third")]
        result, _ = await self.choose(values, "\r")
        self.assertEqual(result.id, "b")
        result, _ = await self.choose(values, "\r", selected_id="c")
        self.assertEqual(result.id, "c")

    async def test_tab_actions_keep_the_highlighted_model(self):
        result, _ = await self.choose([PickerItem("a", "First"), PickerItem("b", "Second")],
                                      "\x1b[B\t\t\r", actions=[PickerAction("add", "Добавить"), PickerAction("edit", "Изменить")])
        self.assertEqual((result.action, result.id), ("edit", "b"))

    async def test_disabled_actions_are_visible_but_skipped_by_keyboard_focus(self):
        result, _ = await self.choose([PickerItem("a", "First")], "\t\r", actions=[
            PickerAction("remove", "Удалить", enabled=False), PickerAction("add", "Добавить")])
        self.assertEqual(result.action, "add")

    async def test_typing_letters_searches_instead_of_firing_action_shortcuts(self):
        result, _ = await self.choose([PickerItem("abc", "Alphabet"), PickerItem("other", "Other")],
                                      "alp\r", actions=[PickerAction("add", "Добавить")])
        self.assertEqual((result.action, result.id), ("select", "abc"))

    async def test_unavailable_model_cannot_be_selected_but_next_row_can(self):
        result, _ = await self.choose([PickerItem("disabled", "Missing", disabled=True), PickerItem("ready", "Ready")],
                                      "\r\x1b[B\r")
        self.assertEqual(result.id, "ready")

    async def test_empty_result_still_allows_add_action(self):
        result, _ = await self.choose([PickerItem("a", "First")], "missing\r\t\r",
                                      actions=[PickerAction("add", "Добавить")])
        self.assertEqual((result.action, result.id, result.query), ("add", None, "missing"))

    async def test_back_and_cancel_preserve_query_and_do_not_submit(self):
        for keys, action in (("\x1b", "back"), ("\x03", "cancel"), ("\x04", "cancel")):
            with self.subTest(action=action, keys=repr(keys)):
                result, _ = await self.choose([PickerItem("a", "First")], "abc" + keys)
                self.assertEqual((result.action, result.query), (action, "abc"))

    async def test_large_list_can_page_and_jump_to_end_without_dumping_rows(self):
        values = [PickerItem(str(index), f"Model {index}") for index in range(300)]
        result, picker = await self.choose(values, "\x1b[6~\x1b[1;5F\r")
        self.assertEqual(result.id, "299")
        self.assertLessEqual(picker.list_window.render_info.window_height, 12)

    async def test_search_cursor_editing_and_whole_line_clear_remain_normal(self):
        result, _ = await self.choose([PickerItem("one", "first"), PickerItem("two", "second")], "wrong\x15second\r")
        self.assertEqual((result.id, result.query), ("two", "second"))
        result, _ = await self.choose([PickerItem("model", "alpha beta")], "beta\x01alpha \x05\r")
        self.assertEqual((result.id, result.query), ("model", "alpha beta"))

    async def test_shift_tab_reaches_last_action_without_resetting_selection(self):
        result, _ = await self.choose([PickerItem("a", "First"), PickerItem("b", "Second")],
                                      "\x1b[B\x1b[Z\r", actions=[PickerAction("add", "Добавить"), PickerAction("edit", "Изменить")])
        self.assertEqual((result.action, result.id), ("edit", "b"))

    def test_provider_names_and_model_details_are_literal_single_line_text(self):
        with create_pipe_input() as pipe:
            picker = _Picker("Title\x1b[2J", [PickerItem("id", "Name\x1b[31m\nnext", detail="[link]value[/link]\tend")],
                             input=pipe, output=DummyOutput())
            content = picker.list_control.create_content(80, 10)
            text = fragment_list_to_text(content.get_line(0))
            self.assertNotIn("\x1b", text)
            self.assertNotIn("\t", text)
            self.assertNotIn("\n", text)
            self.assertIn("[link]value[/link]", text)
            self.assertEqual(content.line_count, 1)

    def test_a_closed_input_returns_cancel_instead_of_escaping_the_model_menu(self):
        result = subprocess.run([sys.executable, "-c",
            'from folder_memory.terminal_picker import pick, PickerItem; '
            'print("RESULT=" + pick("Models", [PickerItem("id", "Model")]).action)'],
            input="", text=True, capture_output=True, timeout=3, cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT=cancel", result.stdout)


if __name__ == "__main__":
    unittest.main()
