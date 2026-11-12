"""Cancellation and editing bindings for the optional model-settings console."""


class TurnCancelled(KeyboardInterrupt):
    pass


def install_prompt_cancel(session):
    """Install form cancellation and terminal editing keys."""
    if getattr(session, '_handy_cancel_installed', None) is True:
        return
    from prompt_toolkit.document import Document
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
    from prompt_toolkit.filters import emacs_insert_mode, vi_insert_mode
    binding = KeyBindings()

    @binding.add('escape')
    def cancel(event):
        if getattr(session, '_handy_cancel_turn', False):
            event.app.exit(exception=TurnCancelled())
        elif event.current_buffer.complete_state is not None:
            event.current_buffer.cancel_completion()
            event.app.invalidate()
        else:
            event.current_buffer.reset()
            event.app.invalidate()

    @binding.add('c-u', filter=emacs_insert_mode | vi_insert_mode)
    def clear_line(event):
        # Cmd+Backspace is often sent as Ctrl+U. Clear the logical line at
        # any cursor column, preserving neighboring lines in pasted input.
        buffer = event.current_buffer
        document = buffer.document
        start = document.cursor_position + document.get_start_of_line_position()
        end = start + len(document.current_line)
        if start != end:
            event.app.clipboard.set_text(document.text[start:end])
            buffer.document = Document(document.text[:start] + document.text[end:], start)

    previous = session.key_bindings
    session.key_bindings = merge_key_bindings([previous, binding]) if previous else binding
    _install_reported_keys()
    session.app.ttimeoutlen = .1
    session.app.timeoutlen = .1
    session._handy_cancel_installed = True


def _install_reported_keys():
    """Recognize optional terminal reports without requesting a new protocol.

    A terminal may consume Command shortcuts itself. Only bytes actually sent
    to stdin can be handled here; ordinary readline editing stays with the
    prompt_toolkit defaults.
    """
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    # Preserve distinct Shift+Enter where terminals report it. Ordinary CR
    # remains Enter; terminals that send CR for both can use Alt+Enter.
    sequences = {'\x1b[13;2u': Keys.ControlJ, '\x1b[27;2;13~': Keys.ControlJ,
                 '\x1b[1;9D': Keys.Home, '\x1b[1;9C': Keys.End}
    for code in (8, 127):
        for modifier, key in ((3, (Keys.Escape, Keys.ControlH)),
                              (5, Keys.ControlW), (9, Keys.ControlU)):
            for event in ('', ':1', ':2'):  # Unspecified, press, repeat.
                sequences[f'\x1b[{code};{modifier}{event}u'] = key
            sequences[f'\x1b[{code};{modifier}:3u'] = Keys.Ignore
    changed = any(ANSI_SEQUENCES.get(sequence) != key for sequence, key in sequences.items())
    ANSI_SEQUENCES.update(sequences)
    if changed:
        # Other prompt sessions may already have cached these as unknown
        # prefixes before Handy's bindings were installed.
        from prompt_toolkit.input.vt100_parser import _IS_PREFIX_OF_LONGER_MATCH_CACHE
        _IS_PREFIX_OF_LONGER_MATCH_CACHE.clear()
