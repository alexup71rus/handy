"""Launch the pi terminal agent with Handy's app-managed profile."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

from .runtime import runtime_dir
from . import pi_runtime as pi


def open_menu(state):
    if sys.platform != 'darwin':
        return
    bundle = Path(state) / 'Handy Menu.app'
    if not (bundle / 'Contents/MacOS/HandyMenu').is_file():
        subprocess.run([sys.executable, str(pi.PROJECT / 'integration/macos/build.py'),
                        '--state-dir', str(state), '--output', str(bundle)], check=True)
    subprocess.run(['/usr/bin/open', '-g', str(bundle)], check=True, timeout=10)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Management owns its options; forward them before parsing chat flags.
    index = 0
    while index < len(argv):
        if argv[index] in {'models', 'providers'}:
            from .model_cli import main as models
            return models((['providers'] if argv[index] == 'providers' else []) + argv[:index] + argv[index + 1:])
        if argv[index] == '--state-dir':
            index += 2
        elif argv[index].startswith('--state-dir=') or argv[index] in {'--json', '--ids'}:
            index += 1
        else:
            break
    parser = argparse.ArgumentParser(prog='handy', add_help=False,
                                     description='Handy · pi. Настройки моделей и MCP — в Handy.app.')
    parser.add_argument('--state-dir', type=Path, default=runtime_dir())
    parser.add_argument('-C', '--directory', type=Path, default=Path.cwd())
    parser.add_argument('--no-app', action='store_true')
    parser.add_argument('--settings', action='store_true')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--new', action='store_true')
    args, rest = parser.parse_known_args(argv)
    state = args.state_dir.expanduser().resolve()
    os.umask(0o077)
    try:
        if args.settings:
            from .pi_control import open_settings
            open_settings(state)
            return 0
        cwd = args.directory.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            raise ValueError('Рабочий путь должен быть папкой.')
        if '--help' in rest or '-h' in rest:
            print('Handy · pi\n  -C PATH       Рабочая папка\n  --settings    Открыть настройки приложения\n  --no-app      Не открывать приложение\n  --once        Один ответ\nОстальные параметры передаются в pi:\n', flush=True)
            return subprocess.run([pi.executable(), '--help']).returncode
        if '--version' in rest or '-v' in rest:
            return subprocess.run([pi.executable(), '--version']).returncode
        if not args.no_app:
            open_menu(state)
        pi.prepare(state, allow_unconfigured=sys.stdin.isatty() and not args.once and '--print' not in rest and '-p' not in rest)
        if pi.load_settings(state)['scheduler_enabled']:
            from .pi_scheduler import start_worker
            start_worker(state)
        if args.once:
            rest.insert(0, '--print')
        os.chdir(cwd)
        os.execve(pi.executable(), pi.command(state, rest), pi.environment(state))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print('Handy: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
