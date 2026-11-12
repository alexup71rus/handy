"""One terminal browser for provider management and live model selection."""

from pathlib import Path

from rich.text import Text

from .model_activation import activate_models
from .model_references import STANDARD_PROVIDER, matches_query, provider_name, resolve_provider
from .model_registry import ModelRegistry
from .model_settings import ModelSettings
from .model_visibility import provider_visibility_state, set_model_visibility, set_provider_visibility
from .terminal_picker import PickerAction, PickerItem, pick
from .terminal_ui import print_system_error


def _confirmation(prompt, message):
    return prompt(message + ' [да/Нет] › ', cancel_on_escape=True).strip().casefold() in {'да', 'yes', 'y'}


class ModelConsole:
    def __init__(self, state, console, prompt, *, kind='chat', provider=None, query='', management=False, picker=None):
        self.manager = ModelSettings(state)
        self.registry = self.manager.registry
        self.console, self.prompt, self.picker = console, prompt, picker or pick
        self.kind = kind
        self.provider = resolve_provider(self.registry, provider, allow_standard=True) if provider else None
        self.query = query
        self.show_hidden = management
        self.focus = None
        self.edited = False
        self.pending_apply = False
        self.chosen = False
        self.refreshed = set()

    def _data(self):
        return self.registry.load()

    def _current(self, data):
        profile = data['profiles'].get(data.get('selected_' + self.kind), {})
        return profile.get('name', 'Без эмбеддингов' if self.kind == 'embedding' else 'Не выбрана')

    def _owner(self, identifier, profile):
        if profile.get('backend', 'local') == 'local':
            return STANDARD_PROVIDER
        return profile.get('provider_id') or 'legacy:' + identifier

    def _visible(self, data, identifier, profile):
        owner = self._owner(identifier, profile)
        return (profile.get('visible', True)
                and (owner != STANDARD_PROVIDER or data.get('standard_provider_visible', True))
                and data.get('providers', {}).get(owner, {}).get('visible', True))

    def _provider_records(self, data):
        result = {STANDARD_PROVIDER: {'name': 'Стандартные · GGUF', 'base_url': 'llama.cpp',
                                     'visible': data.get('standard_provider_visible', True)}}
        result.update(data.get('providers', {}))
        for identifier, profile in data['profiles'].items():
            if profile.get('kind') == self.kind and profile.get('backend') == 'openai' and not profile.get('provider_id'):
                result['legacy:' + identifier] = {'name': profile.get('name', 'API'), 'base_url': profile.get('base_url', ''),
                                                  'visible': profile.get('visible', True)}
        return result

    def _model_items(self, data, query='', provider=None, *, include_hidden=None):
        include_hidden = self.show_hidden if include_hidden is None else include_hidden
        items = []
        for identifier, profile in data['profiles'].items():
            if profile['kind'] != self.kind or provider is not None and self._owner(identifier, profile) != provider:
                continue
            visible = self._visible(data, identifier, profile)
            if not visible and not include_hidden:
                continue
            owner = provider_name(data, profile)
            model_id = profile.get('model_id') if profile.get('backend') == 'openai' else Path(profile.get('model', '')).name
            if not matches_query(query, profile['name'], model_id or '', identifier, owner):
                continue
            state = self._readiness(identifier, profile)
            details = [model_id] if model_id and model_id != profile['name'] else []
            details += [owner, state]
            if not visible:
                details.append('скрыта')
            items.append(PickerItem('m:' + identifier, profile['name'], ' · '.join(details),
                                    selected=data.get('selected_' + self.kind) == identifier))
        return sorted(items, key=lambda item: item.title.casefold())

    def _root_items(self, data, query):
        active = data['profiles'].get(data.get('selected_' + self.kind), {})
        active_owner = self._owner(data.get('selected_' + self.kind, ''), active) if active else None
        items = []
        for identifier, provider in self._provider_records(data).items():
            if not provider.get('visible', True) and not self.show_hidden:
                continue
            if not matches_query(query, identifier, provider['name'], provider.get('base_url', '')):
                continue
            count = sum(p['kind'] == self.kind and self._owner(key, p) == identifier
                        and (self.show_hidden or self._visible(data, key, p)) for key, p in data['profiles'].items())
            details = [provider.get('base_url', ''), f'моделей: {count}']
            if not provider.get('visible', True):
                details.append('скрыт')
            items.append(PickerItem('p:' + identifier, provider['name'], ' · '.join(details), selected=active_owner == identifier))
        return items + self._model_items(data, query) if query.strip() else items

    def _readiness(self, identifier, profile):
        if self.registry.profile_ready(profile):
            return 'API' if profile.get('backend') == 'openai' else 'на диске'
        if profile.get('backend') == 'openai':
            return 'проверь ключ'
        binary = profile.get('binary')
        if not binary or not Path(binary).is_file():
            return 'нужен llama-server'
        return 'скачать' if profile.get('download') else 'файл не найден'

    def _actions(self):
        actions = [PickerAction('add', 'Добавить модель' if self.provider else 'Добавить провайдера'), PickerAction('edit', 'Изменить'),
                PickerAction('remove', 'Удалить'), PickerAction('visibility', 'Видимость'),
                PickerAction('refresh', 'Обновить'), PickerAction('test', 'Проверить'),
                PickerAction('hidden', 'Только видимые' if self.show_hidden else 'Скрытые'),
                PickerAction('kind', 'Для чата' if self.kind == 'embedding' else 'Эмбеддинги'),
                PickerAction('help', 'Справка')]
        if self.kind == 'embedding':
            actions.append(PickerAction('index', 'Индекс'))
        return actions

    def _refresh(self, provider=None):
        providers = self._data().get('providers', {})
        targets = [provider] if provider in providers else list(providers) if provider is None else []
        for identifier in targets:
            with self.console.status('Получаю список моделей…'):
                self.manager.perform({'operation': 'refresh_models', 'provider_id': identifier})
            self.refreshed.add(identifier)

    def _select(self, identifier):
        from .model_setup import _index_ready, _offer_index
        profile = self.registry.get(identifier)
        if not self.registry.ready(identifier):
            if profile.get('download'):
                if not _confirmation(self.prompt, f'Скачать {profile["name"]}?'):
                    return False
                with self.console.status('Скачиваю и проверяю веса…'):
                    self.manager.perform({'operation': 'download', 'id': identifier})
            if not self.registry.ready(identifier):
                raise ValueError('Модель не готова. Проверь файлы, llama-server или ключ провайдера.')
        previous = self._data().get('selected_' + self.kind)
        with self.console.status('Переключаю модель…'):
            self.manager.perform({'operation': 'select', 'id': identifier, 'kind': self.kind})
            if previous == identifier and profile.get('api_key_env'):
                self.manager.perform({'operation': 'apply'})
        self.chosen = True
        if self.kind == 'embedding' and not _index_ready(self.registry.get(identifier)):
            _offer_index(self.registry, identifier, self.console, self.prompt)
            self.edited = True
        self.console.print(Text('Выбрана ' + profile['name'] + '.'))
        return True

    def _target_provider(self, key):
        if key and key.startswith('p:'):
            return key[2:]
        if key and key.startswith('m:'):
            identifier = key[2:]
            return self._owner(identifier, self.registry.get(identifier))
        return self.provider

    def _add(self, key=None):
        from .terminal_model_forms import model_form, provider_form
        if self.provider is None:
            identifier = provider_form(self.manager, self.console, self.prompt)
            if identifier:
                self.edited = True
                self.pending_apply = True
                self.provider = identifier
                self.query = ''
                self._refresh(identifier)
            return
        provider = self._target_provider(key)
        identifier = model_form(self.manager, self.console, self.prompt, provider_id=provider if provider in self._data().get('providers', {}) else None,
                                kind=self.kind, backend='local' if provider == STANDARD_PROVIDER else 'openai')
        if identifier:
            self.edited = True
            self.pending_apply = True
            self.focus = 'm:' + identifier
            self.query = ''

    def _edit(self, key):
        from .terminal_model_forms import model_form, provider_form
        if key and key.startswith('m:'):
            identifier = model_form(self.manager, self.console, self.prompt, identifier=key[2:])
        elif key and key.startswith('p:') and key[2:] in self._data().get('providers', {}):
            identifier = provider_form(self.manager, self.console, self.prompt, identifier=key[2:])
        elif key and key.startswith('p:legacy:'):
            identifier = model_form(self.manager, self.console, self.prompt, identifier=key[len('p:legacy:'):])
        elif key == 'p:' + STANDARD_PROVIDER:
            self.provider = STANDARD_PROVIDER
            self.query = ''
            return
        else:
            raise ValueError('Сначала выбери строку провайдера или модели.')
        if identifier:
            self.edited = True
            self.pending_apply = True

    def _remove(self, key):
        data = self._data()
        if key and key.startswith('m:'):
            identifier = key[2:]
            name = data['profiles'][identifier]['name']
            if _confirmation(self.prompt, f'Удалить модель «{name}» из настроек?'):
                self.manager.perform({'operation': 'delete_profile', 'id': identifier})
                self.edited = True
        elif key and key.startswith('p:') and key[2:] in data.get('providers', {}):
            identifier = key[2:]
            provider = data['providers'][identifier]
            count = sum(p.get('provider_id') == identifier for p in data['profiles'].values())
            if _confirmation(self.prompt, f'Удалить «{provider["name"]}» и связанные модели ({count})?'):
                self.manager.perform({'operation': 'delete_provider', 'id': identifier})
                self.edited = True
                self.provider = None
        elif key and key.startswith('p:legacy:'):
            self._remove('m:' + key[len('p:legacy:'):])
        else:
            raise ValueError('Стандартный каталог можно скрыть действием «Видимость».')

    def _visibility(self, key):
        data = self._data()
        revision = self.registry.settings_revision(data)
        if key and key.startswith('m:') and key[2:] in data['profiles']:
            identifier = key[2:]
            set_model_visibility(data, identifier, not self._visible(data, identifier, data['profiles'][identifier]))
        elif key and key.startswith('p:'):
            identifier = key[2:]
            visible = provider_visibility_state(data, identifier) != 'on'
            set_provider_visibility(data, identifier, visible, self.registry.catalogue(data))
        else:
            raise ValueError('Сначала выбери строку провайдера или модели.')
        # Visibility never changes the active backend or pending runtime config.
        self.registry.replace_settings(data, expected_revision=revision)
        self.edited = True

    def _test(self, key):
        if key and key.startswith('m:'):
            request = {'operation': 'test_profile', 'id': key[2:]}
        else:
            provider = self._target_provider(key)
            if provider not in self._data().get('providers', {}):
                raise ValueError('Для локальной модели выбери её строку и нажми «Проверить».')
            request = {'operation': 'test_provider', 'provider_id': provider}
        with self.console.status('Проверяю…'):
            result = self.manager.perform(request)
        self.console.print(Text(result['message']))

    def _help(self):
        self.console.print(Text('Введи название модели, её ID или провайдера в строку поиска. '
                                'В общем списке поиск идёт по всем известным моделям. '
                                'Enter открывает провайдера или выбирает модель; Tab переводит к действиям, Esc возвращает назад.'))
        self.console.print(Text('В терминале: handy models · handy providers · handy models search QUERY', style='cyan'))

    def _finish(self):
        if self.pending_apply:
            activate_models(self.registry.state, self.registry.resolved(), start=False)
        return self.chosen or self.edited

    def run(self, *, action=None):
        if action:
            return self._one_action(action)
        while True:
            try:
                data = self._data()
                if self.provider in data.get('providers', {}) and self.provider not in self.refreshed:
                    try:
                        self._refresh(self.provider)
                        data = self._data()
                    except (OSError, ValueError) as error:
                        self.refreshed.add(self.provider)
                        print_system_error(self.console, str(error))
                title = ('Эмбеддинги' if self.kind == 'embedding' else 'Модели') + ' · ' + self._current(data)
                if self.provider:
                    title += ' · ' + self._provider_records(data).get(self.provider, {}).get('name', 'API')
                    source = lambda query: self._model_items(data, query, self.provider)
                else:
                    source = lambda query: self._root_items(data, query)
                actions = self._actions()
                if self.kind == 'embedding':
                    actions = [PickerAction('disable', 'Без эмбеддингов'), *actions]
                result = self.picker(title, source, query=self.query, actions=actions, selected_id=self.focus)
                self.query, self.focus = result.query, result.id
                if result.action == 'cancel':
                    break
                if result.action == 'back':
                    if self.provider is None:
                        break
                    self.provider, self.query, self.focus = None, '', None
                elif result.action == 'select' and result.id:
                    if result.id.startswith('p:'):
                        self.provider, self.query, self.focus = result.id[2:], '', None
                    elif result.id.startswith('m:') and self._select(result.id[2:]):
                        break
                elif result.action == 'add':
                    self._add(result.id)
                elif result.action == 'edit':
                    self._edit(result.id)
                elif result.action == 'remove':
                    self._remove(result.id)
                elif result.action == 'visibility':
                    self._visibility(result.id)
                elif result.action == 'refresh':
                    self._refresh(self._target_provider(result.id))
                elif result.action == 'test':
                    self._test(result.id)
                elif result.action == 'hidden':
                    self.show_hidden = not self.show_hidden
                elif result.action == 'kind':
                    self.kind = 'embedding' if self.kind == 'chat' else 'chat'
                    self.query, self.focus = '', None
                elif result.action == 'help':
                    self._help()
                elif result.action == 'index' and self.kind == 'embedding':
                    from .model_setup import _offer_index
                    identifier = result.id[2:] if result.id and result.id.startswith('m:') else self._data().get('selected_embedding')
                    if not identifier or not self.registry.ready(identifier):
                        raise ValueError('Сначала выбери готовый энкодер.')
                    _offer_index(self.registry, identifier, self.console, self.prompt)
                    self.edited = True
                elif result.action == 'disable' and self.kind == 'embedding':
                    self.manager.perform({'operation': 'select', 'id': None, 'kind': 'embedding'})
                    self.chosen = True
                    break
            except (KeyboardInterrupt, EOFError):
                break
            except (OSError, ValueError) as error:
                print_system_error(self.console, str(error))
        return self._finish()

    def _one_action(self, action):
        from .terminal_model_forms import model_form, provider_form
        if action == 'add_provider':
            self.edited = bool(provider_form(self.manager, self.console, self.prompt))
            self.pending_apply = self.edited
        elif action == 'add_model':
            if self.provider is None:
                data = self._data()
                result = self.picker('Где добавить модель?', self._root_items(data, ''), actions=[PickerAction('add_provider', 'Добавить провайдера')])
                if result.action == 'add_provider':
                    self.provider = provider_form(self.manager, self.console, self.prompt)
                    self.edited = bool(self.provider)
                    self.pending_apply = self.edited
                elif result.action == 'select' and result.id:
                    self.provider = result.id[2:]
                else:
                    return False
            if self.provider:
                identifier = model_form(self.manager, self.console, self.prompt, kind=self.kind,
                                        provider_id=self.provider if self.provider in self._data().get('providers', {}) else None,
                                        backend='local' if self.provider == STANDARD_PROVIDER else 'openai')
                self.edited = self.edited or bool(identifier)
                self.pending_apply = self.pending_apply or bool(identifier)
        else:
            data = self._data()
            is_provider = action.endswith('_provider')
            items = self._root_items(data, '') if is_provider else lambda query: self._model_items(data, query, self.provider, include_hidden=True)
            result = self.picker('Выбери провайдера' if is_provider else 'Выбери модель', items, query=self.query)
            if result.action != 'select':
                return False
            (self._edit if action.startswith('edit_') else self._remove)(result.id)
        return self._finish()


def model_menu(state, console, prompt, *, kind='chat', provider=None, query='', management=False, action=None):
    return ModelConsole(state, console, prompt, kind=kind, provider=provider, query=query, management=management).run(action=action)


def choose_profile(registry, kind, console, prompt):
    return model_menu(registry.state, console, prompt, kind=kind)
