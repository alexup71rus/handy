"""Console model management using the app's global settings operations."""

import argparse
from copy import deepcopy
import getpass
import json
from pathlib import Path
import sys
import time

from .model_settings import ModelSettings
from .model_setup import _binary
from .model_references import filter_profiles, matches_query, provider_name, resolve_model, resolve_provider
from .model_visibility import set_model_visibility, set_provider_visibility
from .neural import NeuralError
from .runtime import runtime_dir


_CONFIG_LIMIT = 1024 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # An unsupported argument could be a mistakenly pasted credential.
        # Usage contains declared flags only, never arbitrary argv values.
        self.print_usage(sys.stderr)
        self.exit(2, "Неверные параметры. Доступные команды: handy models --help.\n")


def _common(parser):
    parser.add_argument("--state-dir", type=Path, default=runtime_dir(), help="Каталог состояния сервиса Handy.")
    parser.add_argument("--json", action="store_true", help="Вывести результат как JSON.")
    parser.add_argument("--ids", action="store_true", help="Показать внутренние ID для скриптов.")


def _configuration(parser):
    parser.add_argument("--config", metavar="FILE|-", help="JSON-файл с настройками; - читает JSON из stdin.")


def _visibility(parser):
    visibility = parser.add_mutually_exclusive_group()
    visibility.add_argument("--visible", dest="visible", action="store_const", const=True, default=None,
                            help="Показывать в списке выбора моделей чата.")
    visibility.add_argument("--hidden", dest="visible", action="store_const", const=False,
                            help="Скрыть из списка выбора моделей чата, сохранив настройки и текущий выбор.")


def _model_fields(parser):
    _configuration(parser)
    _visibility(parser)
    parser.add_argument("--name", help="Название профиля.")
    parser.add_argument("--kind", choices=("chat", "embedding"), help="Назначение модели.")
    parser.add_argument("--backend", choices=("local", "openai"), help="Локальный GGUF или OpenAI-совместимый API.")
    parser.add_argument("--provider", metavar="NAME|ID", help="Название или ID сохранённого провайдера; пустая строка отсоединяет профиль.")
    parser.add_argument("--model", help="Путь GGUF или ID модели API, в зависимости от backend.")
    parser.add_argument("--context", type=int, help="Окно контекста, токенов.")
    parser.add_argument("--binary", help="Абсолютный путь к llama-server.")
    parser.add_argument("--mmproj", help="Путь к GGUF vision-проектору; пустая строка убирает проектор.")
    parser.add_argument("--base-url", help="Корень API для профиля без сохранённого провайдера.")
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument("--api-key-env", help="Имя переменной окружения с ключом.")
    keys.add_argument("--api-key-file", help="Абсолютный путь к приватному файлу ключа.")
    parser.add_argument("--dimensions", type=int, help="Размерность эмбеддингов.")
    parser.add_argument("--pooling", choices=("mean", "last", "cls"), help="Pooling локального энкодера.")
    parser.add_argument("--query-prefix", help="Префикс поискового запроса; пробелы сохраняются.")
    parser.add_argument("--passage-prefix", help="Префикс документа; пробелы сохраняются.")


def _provider_fields(parser):
    _configuration(parser)
    _visibility(parser)
    parser.add_argument("--name", help="Название подключения.")
    parser.add_argument("--base-url", help="Корень OpenAI-совместимого API, например https://host/v1.")
    parser.add_argument("--auth", choices=("none", "key", "env", "file"), help="Способ авторизации.")
    keys = parser.add_mutually_exclusive_group()
    keys.add_argument("--api-key-env", help="Имя переменной окружения с ключом.")
    keys.add_argument("--api-key-file", help="Абсолютный путь к приватному файлу ключа.")
    keys.add_argument("--ask-key", action="store_true", help="Ввести новый API-ключ скрыто, без аргумента командной строки.")


def _parser():
    parser = _Parser(prog="handy models", description="Глобальные модели и провайдеры Handy. Чат не запускается.",
                     allow_abbrev=False,
                     formatter_class=argparse.RawDescriptionHelpFormatter,
                     epilog="Без команды открывается меню со стрелками и поиском.\n"
                            "Примеры:\n  handy models search gpt --provider OpenAI\n"
                            "  handy models use gpt-4.1-mini --provider OpenAI\n"
                            "  handy providers add home --base-url http://127.0.0.1:11434/v1\n"
                            "  handy providers edit OpenAI --ask-key\n"
                            "  handy models remove MODEL\n"
                            "add/edit сохраняют настройки; apply применяет их к сервису. use сразу применяет выбор.")
    _common(parser)
    commands = parser.add_subparsers(dest="command", parser_class=_Parser)
    listing = commands.add_parser("list", help="Модели, выбор и готовность файлов/ключей.", allow_abbrev=False)
    listing.add_argument("--kind", choices=("chat", "embedding"))
    listing.add_argument("--provider", metavar="NAME|ID", help="Только модели этого провайдера.")
    listing.add_argument("--search", default="", metavar="QUERY", help="Поиск по названию, ID и провайдеру.")
    search = commands.add_parser("search", help="Найти модели по нескольким словам.", allow_abbrev=False)
    search.add_argument("query", nargs="+", help="Слова поиска.")
    search.add_argument("--provider", metavar="NAME|ID")
    search.add_argument("--kind", choices=("chat", "embedding"))
    for command in ("add", "edit"):
        child = commands.add_parser(command, help="Добавить модель." if command == "add" else "Изменить поля модели.", allow_abbrev=False)
        child.add_argument("id", nargs="?", help="Новый ID для add; название или ID для edit. Без аргумента открывается форма.")
        _model_fields(child)
    for command, help_text in (("remove", "Удалить профиль, сохранив веса."), ("download", "Скачать и проверить веса."),
                               ("test", "Проверить локальные файлы или отправить пробный запрос API.")):
        child = commands.add_parser(command, help=help_text, allow_abbrev=False)
        child.add_argument("id", nargs="?" if command == "remove" else None,
                           help="Название, ID модели API или ID профиля.")
        child.add_argument("--provider", metavar="NAME|ID", help="Уточнить провайдера модели.")
    selection = commands.add_parser("select", aliases=["use"], help="Выбрать и применить модель по ID или названию.", allow_abbrev=False)
    selection.add_argument("id", help="Название, ID модели API или ID профиля; none --kind embedding отключает энкодер.")
    selection.add_argument("--kind", choices=("chat", "embedding"))
    selection.add_argument("--provider", metavar="NAME|ID")
    commands.add_parser("apply", help="Применить сохранённые настройки; работающий сервис перезапустится.", allow_abbrev=False)
    commands.add_parser("restore", help="Вернуть отсутствующие стандартные профили.", allow_abbrev=False)
    providers = commands.add_parser("providers", help="Подключения OpenAI-совместимых API.", allow_abbrev=False,
                                    formatter_class=argparse.RawDescriptionHelpFormatter,
                                    epilog="Без команды открывается список провайдеров со стрелками и поиском.\n"
                                           "Примеры:\n  handy providers add\n  handy providers edit OpenAI --ask-key\n"
                                           "  handy providers list --search local\n"
                                           "  handy providers models OpenAI --search mini\n"
                                           "  handy providers remove OpenAI\n"
                                           "add/edit/remove без аргумента открывают форму в терминале.")
    provider_commands = providers.add_subparsers(dest="provider_command", parser_class=_Parser)
    provider_listing = provider_commands.add_parser("list", help="Сохранённые подключения.", allow_abbrev=False)
    provider_listing.add_argument("--search", default="", metavar="QUERY")
    for command in ("add", "edit"):
        child = provider_commands.add_parser(command, help="Добавить подключение." if command == "add" else "Изменить подключение.", allow_abbrev=False)
        child.add_argument("id", nargs="?", help="ID или название; без аргумента открывается форма.")
        _provider_fields(child)
    for command, help_text in (("remove", "Удалить провайдера и его модели."), ("test", "Проверить доступ к /models."),
                               ("models", "Получить модели провайдера; ID можно добавить вручную.")):
        child = provider_commands.add_parser(command, help=help_text, allow_abbrev=False)
        child.add_argument("id", nargs="?" if command == "remove" else None,
                           help="Название или ID провайдера.")
        if command == "models":
            child.add_argument("--search", default="", metavar="QUERY", help="Найти ID модели в ответе провайдера.")
    return parser


def _merge(original, patch):
    result = deepcopy(original)
    for key, value in patch.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def _read_config(source):
    if source is None:
        return {}
    try:
        if source == "-":
            raw = sys.stdin.read(_CONFIG_LIMIT + 1)
        else:
            with Path(source).expanduser().open("rb") as stream:
                raw = stream.read(_CONFIG_LIMIT + 1)
    except (OSError, ValueError):
        raise ValueError("Не удалось открыть JSON-конфигурацию.") from None
    if len(raw.encode("utf-8") if isinstance(raw, str) else raw) > _CONFIG_LIMIT:
        raise ValueError("JSON-конфигурация больше 1 МиБ.")

    def invalid_constant(_):
        raise ValueError

    try:
        value = json.loads(raw, parse_constant=invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Не удалось прочитать JSON-конфигурацию.") from None
    if not isinstance(value, dict):
        raise ValueError("JSON-конфигурация должна содержать объект.")
    return value


def _item_config(source, collection, identifier, singular):
    value = _read_config(source)
    if collection in value:
        values = value[collection]
        if not isinstance(values, dict) or not isinstance(values.get(identifier), dict):
            raise ValueError("В JSON-конфигурации нет нужного ID.")
        value = values[identifier]
    elif singular in value:
        if not isinstance(value[singular], dict):
            raise ValueError("В JSON ожидается объект настроек.")
        value = {**value[singular], **({"api_key": value["api_key"]} if "api_key" in value else {})}
    return value


def _model_request(args, manager):
    if args.provider:
        args.provider = resolve_provider(manager.registry, args.provider)
    if args.command == "edit":
        args.id = (args.id if args.id in manager.registry.profiles()
                   else resolve_model(manager.registry, args.id, provider=args.provider))
    original = manager.registry.get(args.id) if args.command == "edit" else {}
    patch = _item_config(args.config, "profiles", args.id, "profile")
    value = _merge(original, patch)
    for field, opposite in (("api_key_env", "api_key_file"), ("api_key_file", "api_key_env")):
        if field in patch and opposite not in patch:
            value.pop(opposite, None)
    for field in ("name", "kind", "backend", "context", "binary", "mmproj", "base_url", "dimensions", "pooling", "query_prefix", "passage_prefix", "visible"):
        argument = getattr(args, field, None)
        if argument is not None:
            value[field] = argument
    if args.provider is not None:
        if args.provider:
            value["provider_id"] = args.provider
        else:
            value.pop("provider_id", None)
    if "backend" not in value:
        value["backend"] = "openai" if value.get("provider_id") or value.get("base_url") else "local"
    if args.model is not None:
        value["model" if value["backend"] == "local" else "model_id"] = args.model
    for field, opposite in (("api_key_env", "api_key_file"), ("api_key_file", "api_key_env")):
        argument = getattr(args, field)
        if argument is not None:
            value.pop(opposite, None)
            value[field] = argument
    if args.command == "add":
        value.setdefault("name", args.id)
        value.setdefault("kind", "chat")
        value.setdefault("enabled", True)
        local = value["backend"] == "local"
        value.setdefault("context", 65536 if value["kind"] == "chat" else 512 if local else 8192)
        if local:
            value.setdefault("binary", _binary(manager.registry))
        if value["kind"] == "embedding":
            value.setdefault("dimensions", 1024)
            if local:
                value.setdefault("pooling", "mean")
    result = {"operation": "save_profile", "id": args.id, "profile": value}
    if args.command == "edit":
        result["original_id"] = args.id
    return result


def _provider_request(args, manager):
    if args.provider_command == "edit":
        args.id = resolve_provider(manager.registry, args.id)
    original = manager.registry.providers().get(args.id) if args.provider_command == "edit" else {}
    if original is None:
        raise ValueError("Провайдер не найден.")
    patch = _item_config(args.config, "providers", args.id, "provider")
    value = _merge(original, patch)
    for field in ("name", "base_url", "auth", "api_key_env", "api_key_file", "visible"):
        argument = getattr(args, field)
        if argument is not None:
            value[field] = argument
    if args.auth is None:
        if args.api_key_env is not None:
            value["auth"] = "env"
        elif args.api_key_file is not None:
            value["auth"] = "file"
        elif "auth" not in patch:
            if "api_key_env" in patch and "api_key_file" not in patch:
                value["auth"] = "env"
            elif "api_key_file" in patch and "api_key_env" not in patch:
                value["auth"] = "file"
    key = value.pop("api_key", None)
    if args.ask_key:
        if args.auth is not None and args.auth != "key":
            raise ValueError("--ask-key используется с --auth key.")
        if not sys.stdin.isatty():
            raise ValueError("Для скрытого ввода ключа нужен терминал. В скрипте используй --api-key-env или --api-key-file.")
        key = getpass.getpass("API-ключ: ")
        value["auth"] = "key"
    if key is not None and args.auth is None and not args.api_key_env and not args.api_key_file:
        value["auth"] = "key"
    value.setdefault("name", args.id)
    value.setdefault("auth", "env" if value.get("api_key_env") else "file" if value.get("api_key_file") else "none")
    request = {"operation": "save_provider", "id": args.id, "provider": value}
    if key is not None:
        request["api_key"] = key
    if args.provider_command == "edit":
        request["original_id"] = args.id
    return request


def _show(result, command, args):
    if args.json:
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text
    console = Console(highlight=False)
    if command == "list":
        table = Table(box=None, pad_edge=False)
        for title in ("Модель", "Провайдер", "Тип", "Состояние"):
            table.add_column(title)
        if args.ids:
            table.add_column("ID профиля")
        providers = {item["id"]: item for item in result.get("providers", [])}
        for item in result["profiles"]:
            if getattr(args, "kind", None) and item["kind"] != args.kind:
                continue
            ready = ("API настроен" if item["backend"] == "openai" else "На диске") if item["ready"] else "Не готова"
            provider = providers.get(item.get("provider_id"), {})
            local_hidden = item["backend"] == "local" and not result.get("standard_provider_visible", True)
            if not item.get("visible", True) or not provider.get("visible", True) or local_hidden:
                ready += " · скрыта"
            label = Text(("● " if item["selected"] else "") + item["name"])
            identity = item.get("model_id") or item["id"]
            if identity != item["name"]:
                label.append("\n" + identity, style="dim")
            row = [label, Text(provider_name({"providers": providers}, item)),
                   "Чат" if item["kind"] == "chat" else "Эмбеддинги", ready]
            if args.ids:
                row.append(Text(item["id"]))
            table.add_row(*row)
        console.print(table if result["profiles"] else Text("Совпадений нет."))
        console.print("Выбрать: handy models use ID --provider НАЗВАНИЕ", style="dim", markup=False)
    elif command == "providers list":
        table = Table(box=None, pad_edge=False)
        for title in ("Провайдер", "Адрес API", "Авторизация", "В списке"):
            table.add_column(title)
        if args.ids:
            table.add_column("ID")
        for item in result["providers"]:
            row = [*(Text(str(item.get(field, ""))) for field in ("name", "base_url", "auth")),
                   "Показан" if item.get("visible", True) else "Скрыт"]
            if args.ids:
                row.append(Text(item["id"]))
            table.add_row(*row)
        console.print(table if result["providers"] else Text("Провайдеры пока не добавлены."))
    elif command == "providers models":
        table = Table(box=None, pad_edge=False)
        table.add_column("ID модели")
        table.add_column("Контекст")
        for item in result["models"]:
            table.add_row(Text(item["id"]), str(item.get("context", "—")))
        console.print(table if result["models"] else Text("Провайдер вернул пустой список."))
    elif "message" in result:
        console.print(result["message"], markup=False)
    else:
        message = {"add": "Модель добавлена.", "edit": "Модель обновлена.", "remove": "Профиль удалён; веса сохранены.",
                   "select": "Выбор применён.", "download": "Веса скачаны и проверены.", "apply": "Настройки применены.",
                   "restore": "Отсутствующие стандартные профили восстановлены.", "providers add": "Провайдер добавлен.",
                   "providers edit": "Провайдер обновлён.", "providers remove": "Провайдер удалён."}.get(command, "Готово.")
        console.print(message, markup=False)
        if command in {"add", "edit", "providers add", "providers edit"} and not result.get("applied", False):
            console.print("Для применения к сервису: handy models apply", style="dim", markup=False)


def _interactive_tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


def _interactive_menu(state, *, management=False, action=None):
    from rich.console import Console
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import DummyHistory
    from .model_console import model_menu
    from .terminal_cancel import install_prompt_cancel
    normal = PromptSession()

    def prompt(label, **kwargs):
        cancel_on_escape = kwargs.pop("cancel_on_escape", False)
        session = PromptSession(history=DummyHistory()) if kwargs.get("is_password") else normal
        install_prompt_cancel(session)
        session._handy_cancel_turn = cancel_on_escape
        try:
            return session.prompt(label, multiline=False, completer=None, **kwargs)
        finally:
            session._handy_cancel_turn = False

    model_menu(state, Console(highlight=False), prompt, management=management, **({'action': action} if action else {}))
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Global output/state options also work after nested provider commands.
    global_parser = _Parser(add_help=False, allow_abbrev=False)
    _common(global_parser)
    common, remaining = global_parser.parse_known_args(argv)
    args = _parser().parse_args(remaining)
    args.state_dir, args.json, args.ids = common.state_dir, common.json, common.ids
    missing_action = None
    if getattr(args, 'id', 'present') is None:
        entity = 'provider' if args.command == 'providers' else 'model'
        verb = args.provider_command if entity == 'provider' else args.command
        expected = ['providers', verb] if entity == 'provider' else [verb]
        if verb in {'add', 'edit', 'remove'} and remaining == expected and not args.json and _interactive_tty():
            missing_action = verb + '_' + entity
        else:
            _parser().error('Укажи ID или открой интерактивное меню.')
    last_progress = [0.0, ""]

    def progress(file, done, total):
        now = time.monotonic()
        if file != last_progress[1] or now - last_progress[0] >= 1 or done == total:
            print(f"{file}: {done:,}" + (f" / {total:,} байт" if total else " байт"), file=sys.stderr, flush=True)
            last_progress[:] = [now, file]

    command = args.command or "list"
    if command == "use":
        command = args.command = "select"
    try:
        if missing_action:
            return _interactive_menu(args.state_dir.expanduser().resolve(), management=True, action=missing_action)
        if args.command is None and not args.json and _interactive_tty():
            return _interactive_menu(args.state_dir.expanduser().resolve())
        if args.command == "providers" and args.provider_command is None and not args.json and _interactive_tty():
            return _interactive_menu(args.state_dir.expanduser().resolve(), management=True)
        manager = ModelSettings(args.state_dir.expanduser().resolve(), progress=progress)
        provider = None
        if getattr(args, "provider", None) and command not in {"add", "edit"}:
            provider = resolve_provider(manager.registry, args.provider, allow_standard=True)
        if command == "providers":
            action = args.provider_command or "list"
            command += " " + action
            if action in {"add", "edit"}:
                request = _provider_request(args, manager)
            elif action == "list":
                request = {"operation": "snapshot"}
            else:
                args.id = resolve_provider(manager.registry, args.id)
                request = {"operation": {"remove": "delete_provider", "test": "test_provider", "models": "refresh_models"}[action],
                           "provider_id" if action != "remove" else "id": args.id}
        elif command in {"add", "edit"}:
            request = _model_request(args, manager)
        elif command == "select":
            if args.id == "none" and args.kind == "embedding":
                identifier, kind = None, "embedding"
            else:
                identifier = resolve_model(manager.registry, args.id, provider=provider, kind=args.kind)
                kind = args.kind or manager.registry.get(identifier)["kind"]
            request = {"operation": "select", "id": identifier, "kind": kind}
        else:
            operation = {"list": "snapshot", "search": "snapshot", "remove": "delete_profile", "test": "test_profile", "restore": "restore_catalogue"}.get(command, command)
            request = {"operation": operation}
            if hasattr(args, "id"):
                request["id"] = resolve_model(manager.registry, args.id, provider=provider)
        result = manager.perform(request)
        # Explicit CLI checkboxes have the same cascading behavior as the app.
        # Raw JSON remains backwards-compatible with parent visibility masks.
        visibility = getattr(args, 'visible', None)
        if visibility is not None and command in {'add', 'edit', 'providers add', 'providers edit'}:
            data = manager.registry.load()
            revision = manager.registry.settings_revision(data)
            if command.startswith('providers '):
                set_provider_visibility(data, args.id, visibility)
            else:
                set_model_visibility(data, args.id, visibility)
            manager.registry.replace_settings(data, expected_revision=revision)
            result = manager.snapshot()
        if command in {"list", "search"}:
            query = " ".join(args.query) if command == "search" else getattr(args, "search", "")
            result["profiles"] = filter_profiles(result, query=query, provider=provider, kind=getattr(args, "kind", None))
            command = "list"
        elif command == "providers list":
            result["providers"] = [item for item in result["providers"] if matches_query(getattr(args, "search", ""), item["id"], item["name"], item["base_url"])]
        elif command == "providers models":
            result["models"] = [item for item in result["models"] if matches_query(getattr(args, "search", ""), item["id"], item.get("name"))]
            result["count"] = len(result["models"])
        _show(result, command, args)
        return 0
    except KeyboardInterrupt:
        message = "Операция отменена."
        status = 130
    except (ValueError, NeuralError) as error:
        message = str(error)
        status = 1
    except OSError:
        message = "Не удалось выполнить операцию. Проверь файлы и доступность сервиса."
        status = 1
    except Exception:
        message = "Не удалось обработать настройки. Проверь конфигурацию и попробуй ещё раз."
        status = 1
    if args.json:
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    elif status == 130:
        print(message, file=sys.stderr)
    else:
        from rich.console import Console
        from .terminal_ui import print_system_error
        print_system_error(Console(stderr=True, highlight=False), message)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
