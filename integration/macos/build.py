"""Build the local macOS menu app with Xcode Command Line Tools."""

import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
from xml.parsers.expat import ExpatError


HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parents[1]


def require_closed(destination):
    """Replacing a live binary breaks macOS privacy attribution for its children."""
    executable = str(destination / 'Contents/MacOS/HandyMenu')
    processes = subprocess.run(['/bin/ps', '-ww', '-axo', 'comm='], capture_output=True,
                               text=True, check=True, timeout=10)
    if executable in (line.strip() for line in processes.stdout.splitlines()):
        raise ValueError('Закрой приложение Handy перед пересборкой, затем повтори команду.')


def build(destination, python, state_directory):
    if sys.platform != 'darwin':
        raise ValueError('Handy Menu requires macOS 13 or later.')
    if destination.suffix != '.app':
        raise ValueError('The output path must end in .app.')
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f'Python is not executable: {python}')
    if subprocess.run([str(python), '-c', 'import sys; raise SystemExit(sys.version_info < (3, 11))'], check=False).returncode:
        raise ValueError('Handy requires Python 3.11 or later.')

    identity = hashlib.sha256(os.fsencode(f'{CHECKOUT}\0{state_directory}')).hexdigest()[:12]
    bundle_id = f'local.handy.menu.{identity}'
    replacing = destination.exists() or destination.is_symlink()
    if replacing:
        plist = destination / 'Contents/Info.plist'
        try:
            previous = plistlib.loads(plist.read_bytes()) if not destination.is_symlink() else {}
        except (OSError, plistlib.InvalidFileException, ExpatError):
            previous = {}
        if not isinstance(previous, dict) or previous.get('CFBundleIdentifier') != bundle_id:
            raise ValueError(f'Output already exists and is not this Handy Menu bundle: {destination}')
        require_closed(destination)

    search_path = [str(python.parent), *(part for part in os.get_exec_path() if os.path.isabs(part))]
    info = {
        'CFBundleDevelopmentRegion': 'ru',
        'CFBundleDisplayName': 'Handy',
        'CFBundleExecutable': 'HandyMenu',
        'CFBundleIdentifier': bundle_id,
        'CFBundleInfoDictionaryVersion': '6.0',
        'CFBundleName': 'Handy Menu',
        'CFBundlePackageType': 'APPL',
        'CFBundleShortVersionString': '1.0',
        'CFBundleVersion': '1',
        'CFBundleIconFile': 'Handy',
        'LSMinimumSystemVersion': '13.0',
        'LSUIElement': True,
        'NSDocumentsFolderUsageDescription': 'Handy использует папку проекта в Документах для настроек, моделей и работы терминального помощника.',
        'CFBundleURLTypes': [{'CFBundleURLName': 'Handy settings', 'CFBundleURLSchemes': ['handy']}],
        'HandyCheckout': str(CHECKOUT),
        'HandyPython': str(python),
        'HandyStateDirectory': str(state_directory),
        'HandyToolPath': os.pathsep.join(dict.fromkeys(search_path)),
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.handy-menu-', dir=destination.parent) as staging:
        temporary = Path(staging)
        bundle = temporary / destination.name
        binary = bundle / 'Contents/MacOS/HandyMenu'
        binary.parent.mkdir(parents=True)
        icon = CHECKOUT / 'integration/brand/Handy.icns'
        if icon.is_file():
            resources = bundle / 'Contents/Resources'
            resources.mkdir()
            shutil.copyfile(icon, resources / 'Handy.icns')
        else:
            info.pop('CFBundleIconFile', None)
        (bundle / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        arch = os.uname().machine
        sources = sorted(str(path) for path in HERE.glob('*.swift'))
        subprocess.run(['xcrun', 'swiftc', '-target', f'{arch}-apple-macosx13.0', '-parse-as-library', '-O',
                        *sources, '-o', str(binary)], check=True)
        subprocess.run(['/usr/bin/codesign', '--sign', '-', str(bundle)], check=True)
        backup = temporary / 'previous.app'
        if replacing:
            require_closed(destination)
            destination.rename(backup)
        try:
            bundle.rename(destination)
        except BaseException:
            if replacing and not destination.exists():
                backup.rename(destination)
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=CHECKOUT / '.runtime/Handy Menu.app')
    parser.add_argument('--python', type=Path, default=Path(sys.executable), help='Python 3.11+ interpreter used by Handy')
    parser.add_argument('--state-dir', type=Path, default=CHECKOUT / '.runtime', help='Handy state directory')
    args = parser.parse_args()
    destination = args.output.expanduser().absolute()
    try:
        build(destination, args.python.expanduser().absolute(), args.state_dir.expanduser().resolve())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'{error}\n')
    print(destination)


if __name__ == '__main__':
    main()
