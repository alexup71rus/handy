"""Build a source release from a committed Git revision."""

import argparse
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def build(revision, output):
    commit = subprocess.check_output(
        ['git', 'rev-parse', '--verify', f'{revision}^{{commit}}'], cwd=ROOT, text=True
    ).strip()
    archive = subprocess.check_output(['git', 'archive', '--format=tar', commit], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        version_file = source.extractfile('pyproject.toml')
        version = tomllib.loads(version_file.read().decode())['project']['version']
        output.mkdir(parents=True, exist_ok=True)
        target = output / f'handy-{version}-source.zip'
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as result:
            for member in source:
                if not member.isfile():
                    continue
                info = zipfile.ZipInfo(f'handy-{version}/{member.name}')
                info.create_system = 3
                info.external_attr = (0o100000 | member.mode) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                result.writestr(info, source.extractfile(member).read())
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    (output / 'SHA256SUMS').write_text(f'{digest}  {target.name}\n')
    print(f'{target}\nRevision: {commit}\nSHA256: {digest}')
    return target


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('revision', help='committed Git revision to export')
    parser.add_argument('--output', type=Path, default=ROOT / 'build/releases')
    args = parser.parse_args()
    build(args.revision, args.output)


if __name__ == '__main__':
    main()
