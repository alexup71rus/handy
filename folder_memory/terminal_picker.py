"""A compact inline list picker; terminal history stays on the main screen."""

from dataclasses import dataclass
from functools import partial
from typing import Callable, Sequence

from prompt_toolkit import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import DummyHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Dimension, DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import HorizontalAlign, ScrollOffsets
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Button, TextArea


@dataclass(frozen=True)
class PickerItem:
    id: str
    title: str
    detail: str = ""
    search: str = ""
    selected: bool = False
    disabled: bool = False


@dataclass(frozen=True)
class PickerAction:
    id: str
    label: str
    enabled: bool = True


@dataclass(frozen=True)
class PickerResult:
    action: str
    id: str | None = None
    query: str = ""


Items = Sequence[PickerItem] | Callable[[str], Sequence[PickerItem]]


def _literal(value):
    return "".join(char if char.isprintable() else " " for char in str(value))


def _matching(items, query):
    words = query.casefold().split()
    return [item for item in items if all(word in " ".join(
        (item.title, item.detail, item.search, item.id)).casefold() for word in words)]


class _Picker:
    """State for one invocation; injectable input/output exercise real key parsing."""

    def __init__(self, title, items: Items, *, query="", actions=(), selected_id=None,
                 input=None, output=None):
        self.source = items
        self.items = []
        self.index = 0
        self._initial_id = selected_id
        self.search = TextArea(text=query, multiline=False, height=1,
                               prompt="Поиск › ", history=DummyHistory(),
                               wrap_lines=False, style="class:search", name="picker-search")
        self.search.buffer.on_text_changed += self._filter
        self.list_control = FormattedTextControl(self._rows, show_cursor=False,
                                                get_cursor_position=lambda: Point(x=0, y=self.index))
        self.list_window = Window(self.list_control, height=Dimension(min=1, preferred=10, max=12),
                                  wrap_lines=False, dont_extend_height=True,
                                  scroll_offsets=ScrollOffsets(top=1, bottom=1),
                                  allow_scroll_beyond_bottom=False)
        self.actions = tuple(actions)
        self.buttons = {action.id: Button(_literal(action.label), handler=partial(self._finish, action.id),
                                         width=get_cwidth(_literal(action.label)) + 4)
                        for action in self.actions if action.enabled}
        self._filter()
        keys = self._bindings()
        content = HSplit([
            Window(FormattedTextControl([("class:title", _literal(title))]), height=1),
            self.search,
            self.list_window,
            Window(FormattedTextControl(self._position), height=1),
            DynamicContainer(self._action_bar),
            Window(FormattedTextControl(self._help), height=1),
        ])
        self.app = Application(layout=Layout(content, focused_element=self.search),
                               key_bindings=keys, full_screen=False, erase_when_done=True,
                               mouse_support=True, input=input, output=output,
                               style=Style.from_dict({
                                   "title": "bold",
                                   "search": "",
                                   "row": "",
                                   "row.focused": "bg:#17364b bold",
                                   "row.disabled": "#888888",
                                   "active": "#44bb88",
                                   "detail": "#888888",
                                   "button": "#aaaaaa",
                                   "button.focused": "bg:#17364b #ffffff bold",
                                   "help": "#888888",
                               }))
        self.app.ttimeoutlen = .1
        self.app.timeoutlen = .2

    @property
    def current(self):
        return self.items[self.index] if self.items else None

    def _filter(self, *_):
        previous = self.current.id if self.current is not None else self._initial_id
        query = self.search.text
        self.items = list(self.source(query)) if callable(self.source) else _matching(self.source, query)
        preferred = previous or next((item.id for item in self.items if item.selected), None)
        self.index = next((index for index, item in enumerate(self.items) if item.id == preferred), 0)
        self._initial_id = None

    def _rows(self):
        if not self.items:
            return [("class:detail", "  Нет совпадений")]
        fragments = []
        for index, item in enumerate(self.items):
            focused = index == self.index
            style = "class:row.focused" if focused else "class:row"
            if item.disabled:
                style += " class:row.disabled"

            def clicked(event, identifier=item.id):
                if event.event_type == MouseEventType.MOUSE_UP:
                    index = next((i for i, current in enumerate(self.items) if current.id == identifier), None)
                    if index is not None:
                        self.index = index
                        get_app().invalidate()

            fragments.append((style, "› " if focused else "  ", clicked))
            fragments.append((style + (" class:active" if item.selected else ""), "● " if item.selected else "  ", clicked))
            fragments.append((style, _literal(item.title), clicked))
            if item.detail:
                fragments.append((style + " class:detail", "  " + _literal(item.detail), clicked))
            if index < len(self.items) - 1:
                fragments.append(("", "\n"))
        return fragments

    def _position(self):
        return [("class:help", f"  {self.index + 1} / {len(self.items)}" if self.items else "")]

    def _help(self):
        if get_app().output.get_size().columns < 56:
            return [("class:help", "↑↓ · Tab · Enter · Esc" if self.buttons else "↑↓ · Enter · Esc")]
        text = "↑↓ выбор · Enter открыть · Esc назад"
        if self.buttons:
            text = "↑↓ выбор · Tab действия · Enter · Esc назад"
        return [("class:help", text)]

    def _action_bar(self):
        width = max(20, get_app().output.get_size().columns)
        rows, row, used = [], [], 0
        for action in self.actions:
            size = get_cwidth(_literal(action.label)) + 4
            if row and used + size + 1 > width:
                rows.append(VSplit(row, padding=1, align=HorizontalAlign.LEFT))
                row, used = [], 0
            if action.enabled:
                row.append(self.buttons[action.id])
            else:
                row.append(Window(FormattedTextControl([("class:detail", f"  {_literal(action.label)}  ")]),
                                  width=size, height=1, dont_extend_width=True))
            used += size + 1
        if row:
            rows.append(VSplit(row, padding=1, align=HorizontalAlign.LEFT))
        return HSplit(rows) if rows else Window(height=0)

    def _finish(self, action):
        item = self.current
        if action == "select" and (item is None or item.disabled):
            return
        self.app.exit(result=PickerResult(action, item.id if item else None, self.search.text))

    def _move(self, amount):
        self.index = min(max(0, self.index + amount), max(0, len(self.items) - 1))

    def _page(self):
        info = self.list_window.render_info
        return max(1, info.window_height - 1) if info is not None else 9

    def _focus(self, backwards=False):
        controls = [self.search.control, *(button.control for button in self.buttons.values())]
        current = self.app.layout.current_control
        index = controls.index(current) if current in controls else 0
        self.app.layout.focus(controls[(index + (-1 if backwards else 1)) % len(controls)])

    def _bindings(self):
        keys = KeyBindings()

        @keys.add("up", eager=True)
        def _(event): self._move(-1)

        @keys.add("down", eager=True)
        def _(event): self._move(1)

        @keys.add("pageup", eager=True)
        def _(event): self._move(-self._page())

        @keys.add("pagedown", eager=True)
        def _(event): self._move(self._page())

        @keys.add("c-home", eager=True)
        def _(event): self.index = 0

        @keys.add("c-end", eager=True)
        def _(event): self.index = max(0, len(self.items) - 1)

        @keys.add("home", filter=Condition(lambda: self.app.layout.current_control is not self.search.control), eager=True)
        def _(event): self.index = 0

        @keys.add("end", filter=Condition(lambda: self.app.layout.current_control is not self.search.control), eager=True)
        def _(event): self.index = max(0, len(self.items) - 1)

        @keys.add("enter", filter=Condition(lambda: self.app.layout.current_control is self.search.control), eager=True)
        def _(event): self._finish("select")

        @keys.add("tab", eager=True)
        def _(event): self._focus()

        @keys.add("s-tab", eager=True)
        def _(event): self._focus(backwards=True)

        @keys.add("escape", eager=True)
        def _(event): self._finish("back")

        @keys.add("c-c", eager=True)
        @keys.add("c-d", eager=True)
        def _(event): self._finish("cancel")

        @keys.add("c-u", filter=Condition(lambda: self.app.layout.current_control is self.search.control), eager=True)
        def _(event):
            self.app.clipboard.set_text(self.search.text)
            self.search.buffer.text = ""

        return keys


def pick(title, items: Items, *, query="", actions=(), selected_id=None) -> PickerResult:
    """Choose a row or action using arrows, incremental search, Tab and Enter.

    A callable ``items(query)`` owns its filtering and may switch provider rows
    to model results. A sequence uses case-insensitive AND matching on words.
    Esc returns ``back``; Ctrl+C/EOF return ``cancel`` without raising.
    """
    picker = _Picker(title, items, query=query, actions=actions, selected_id=selected_id)
    try:
        return picker.app.run()
    except (EOFError, KeyboardInterrupt):
        return PickerResult("cancel", picker.current.id if picker.current else None, picker.search.text)
