"""Install pi, Handy's MCP services and the handy terminal command."""

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROJECT = Path(__file__).resolve().parents[1]


def install(bin_dir):
    from_path = str(PROJECT)
    if from_path not in sys.path:
        sys.path.insert(0, from_path)
    from folder_memory.pi_runtime import PI_VERSION, BROWSER_VERSION
    npm = shutil.which('npm')
    if not npm:
        raise ValueError('Для pi нужен Node.js 22.19+ и npm.')
    destination = bin_dir.expanduser().absolute() / 'handy'
    source = PROJECT / 'handy'
    if destination.exists() or destination.is_symlink():
        if not destination.is_symlink() or destination.resolve() != source:
            raise ValueError(f'Команда уже существует: {destination}')
    interpreter = PROJECT / '.venv/bin/python'
    bundle = PROJECT / '.runtime/Handy Menu.app'
    if sys.platform == 'darwin':
        from integration.macos.build import require_closed
        require_closed(bundle)
    uv = shutil.which('uv')
    if not interpreter.is_file():
        subprocess.run([uv, 'venv', str(PROJECT / '.venv')] if uv else
                       [sys.executable, '-m', 'venv', str(PROJECT / '.venv')], check=True)
    subprocess.run([uv, 'pip', 'install', '--python', str(interpreter), '-e', str(PROJECT)] if uv else
                   [str(interpreter), '-m', 'pip', 'install', '-e', str(PROJECT)], check=True)
    source.chmod(source.stat().st_mode | 0o111)
    subprocess.run([npm, 'install', '--prefix', str(PROJECT / '.runtime/pi'), '--save-exact',
                    f'@earendil-works/pi-coding-agent@{PI_VERSION}',
                    f'chrome-devtools-mcp@{BROWSER_VERSION}'], check=True)
    from folder_memory.pi_localization import localized_cli
    localized_cli(PROJECT / '.runtime/pi', PI_VERSION)
    if sys.platform == 'darwin':
        subprocess.run([str(interpreter), str(PROJECT / 'integration/macos/build.py'),
                        '--output', str(bundle)], check=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.is_symlink():
        destination.symlink_to(source)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bin-dir', type=Path, default=Path.home() / '.local/bin')
    args = parser.parse_args()
    try:
        destination = install(args.bin_dir)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, str(error) + '\n')
    print(destination)
    if str(destination.parent) not in os.get_exec_path():
        print(f'Добавь {destination.parent} в PATH.')


if __name__ == '__main__':
    main()
