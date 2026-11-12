#!/bin/bash
set -e
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
cd "$(dirname "$0")"

fail() {
    printf '\n%s\n' "$1" >&2
    if [[ -t 0 ]]; then read -r -p 'Нажми Enter, чтобы закрыть окно.'; fi
    exit 1
}

[[ "$(uname -s)" == Darwin ]] || fail 'Установщик рассчитан на macOS.'
command -v python3 >/dev/null && python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fail 'Нужен Python 3.11 или новее.'
command -v node >/dev/null && node -e 'const [a,b] = process.versions.node.split(".").map(Number); process.exit(a > 22 || (a === 22 && b >= 19) ? 0 : 1)' || fail 'Нужен Node.js 22.19 или новее.'
command -v npm >/dev/null || fail 'Нужен npm.'
xcrun --find swift >/dev/null 2>&1 || fail 'Нужны Xcode Command Line Tools: выполни xcode-select --install.'

python3 scripts/install_cli.py "$@" || fail 'Установка не завершена. Проверь сообщение об ошибке выше.'
printf '\nHandy установлен. Запусти команду handy в терминале.\n'
if [[ -t 0 ]]; then read -r -p 'Нажми Enter, чтобы закрыть окно.'; fi
