"""JSON bridge used by Handy.app and the pi integration."""
import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from .runtime import get_token, runtime_dir
from . import pi_runtime as pi


def open_settings(state):
    if sys.platform != 'darwin':
        raise ValueError('Окно настроек доступно в Handy.app для macOS.')
    bundle = Path(state) / 'Handy Menu.app'
    if not (bundle / 'Contents/MacOS/HandyMenu').is_file():
        subprocess.run([sys.executable, str(pi.PROJECT / 'integration/macos/build.py'),
                        '--state-dir', str(state), '--output', str(bundle)], check=True)
    subprocess.run(['/usr/bin/open', '-a', str(bundle), 'handy://settings'], check=True, timeout=10)
    return {'opened': True}


def open_chat(state, payload):
    session = Path(payload['session_path']).expanduser().resolve(strict=True)
    cwd = Path(payload['cwd']).expanduser().resolve(strict=True)
    if not session.is_file() or not cwd.is_dir():
        raise ValueError('Чат или рабочая папка не найдены.')
    args = [str(pi.PROJECT / 'handy'), '--state-dir', str(state), '-C', str(cwd), '--session', str(session)]
    if sys.platform != 'darwin':
        raise ValueError('Автоматическое открытие терминала доступно в macOS.')
    # Pass data through argv to AppleScript, and quote every shell argument.
    script = 'on run argv\ntell application "Terminal"\nactivate\ndo script (item 1 of argv)\nend tell\nend run'
    result = subprocess.run(['/usr/bin/osascript', '-e', script, '--', shlex.join(args)],
                            capture_output=True, text=True, timeout=10)
    if result.returncode:
        raise ValueError((result.stderr.strip() or 'Не удалось открыть чат в Terminal.')[:600])
    return {'opened': True}


def credential(state, identifier):
    from .model_registry import ModelRegistry
    profile = ModelRegistry(state).get(identifier)
    if profile.get('backend', 'local') == 'local':
        return get_token(Path(state))
    from .pi_models import api_endpoint
    from .remote_model import OpenAIModel
    api_endpoint(state, profile)
    model = OpenAIModel(state, config=profile)
    try:
        return model._key() or 'handy-no-auth'
    finally:
        model.close()


def check_mcp(state, *, timeout=30, max_bytes=1024 * 1024):
    """Probe only on request, with a deadline and owned subprocess cleanup."""
    from .pi_scheduler import _terminate

    state = Path(state).resolve()
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        child = subprocess.Popen([pi.executable(), 'mcp', 'list', '--json'],
                                 cwd=pi.PROJECT, env=pi.environment(state), stdin=subprocess.DEVNULL,
                                 stdout=output, stderr=errors, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                if output.tell() + errors.tell() > max_bytes:
                    raise ValueError('Ответ проверки MCP слишком большой.')
                if time.monotonic() >= deadline:
                    raise ValueError('Истекло время проверки MCP. Проверь адреса серверов.')
                time.sleep(0.05)
            if output.tell() + errors.tell() > max_bytes:
                raise ValueError('Ответ проверки MCP слишком большой.')
            output.seek(0)
            raw = output.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise ValueError('Ответ проверки MCP слишком большой.')
            try:
                report = json.loads(raw)
            except (ValueError, UnicodeError):
                raise ValueError('Проверка MCP не вернула корректный отчёт. Проверь журнал pi/mcp.log.') from None
            if not isinstance(report, dict) or not isinstance(report.get('servers'), list) or not isinstance(report.get('errors'), list):
                raise ValueError('Проверка MCP вернула неизвестный формат отчёта.')
            # Command arguments and headers can contain secrets. The UI needs
            # actual connection results and tool names, never launch credentials.
            allowed = {'name', 'state', 'tools', 'exposure', 'enabled', 'scope', 'error', 'resources'}
            if child.returncode and not report['errors']:
                report['errors'].append(f'Проверка MCP завершилась с кодом {child.returncode}.')
            return {'servers': [{key: value for key, value in server.items() if key in allowed}
                                for server in report['servers'] if isinstance(server, dict)],
                    'errors': report['errors']}
        finally:
            _terminate(child)


def dispatch(state, command, payload):
    from . import pi_scheduler as scheduler
    if command == 'status':
        installed = Path(pi.PROJECT / '.runtime/pi/node_modules/.bin/pi').is_file()
        return {'settings': pi.load_settings(state),
                'pi': {'installed': installed, 'version': pi.PI_VERSION if installed else '', 'agent_dir': str(pi.agent_dir(state))},
                'jobs': scheduler.list_jobs(state), 'runs': scheduler.list_runs(state), 'scheduler': scheduler.worker_status(state),
                'mcp_servers': pi.mcp_servers(state)}
    if command == 'settings':
        return pi.load_settings(state)
    if command == 'model-inventory':
        from .pi_models import inventory
        return inventory(state)
    if command == 'select-model':
        from .pi_models import select_model
        return select_model(state, payload['id'], download=payload.get('download', False))
    if command == 'save-settings':
        return pi.save_settings(state, payload)
    if command == 'set-mcp-enabled':
        return pi.set_mcp_enabled(state, payload['id'], payload['enabled'])
    if command == 'check-mcp':
        return check_mcp(state)
    if command == 'jobs':
        return scheduler.list_jobs(state)
    if command == 'runs':
        return scheduler.list_runs(state)
    if command == 'create-job':
        result = scheduler.create_job(state, **payload)
    elif command == 'update-job':
        values = dict(payload)
        result = scheduler.update_job(state, values.pop('job_id'), **values)
    elif command == 'delete-job':
        return scheduler.delete_job(state, payload['job_id'])
    elif command == 'delete-run':
        return scheduler.delete_run(state, payload['run_id'])
    elif command == 'acknowledge-run':
        return scheduler.mark_notified(state, payload['run_id'])
    elif command == 'start-scheduler':
        if not pi.load_settings(state)['scheduler_enabled']:
            raise ValueError('Включи планировщик в настройках Handy.')
        return scheduler.start_worker(state)
    elif command == 'stop-scheduler':
        return scheduler.stop_worker(state)
    elif command == 'open-chat':
        return open_chat(state, payload)
    elif command == 'open-settings':
        return open_settings(state)
    else:
        raise ValueError('Неизвестная команда приложения.')
    if pi.load_settings(state)['scheduler_enabled']:
        scheduler.start_worker(state)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=runtime_dir())
    parser.add_argument('command', choices=('status','settings','save-settings','jobs','create-job','update-job','delete-job','delete-run',
                                          'runs','acknowledge-run','open-chat','open-settings','start-scheduler','stop-scheduler','credential',
                                          'model-inventory','select-model','check-mcp','set-mcp-enabled'))
    parser.add_argument('--profile')
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args(argv)
    state = args.state_dir.expanduser().resolve()
    os.umask(0o077)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        if args.command == 'credential':
            if not args.profile:
                raise ValueError('Нужен профиль модели.')
            print(credential(state, args.profile))
            return 0
        mutations = {'save-settings','create-job','update-job','delete-job','delete-run','acknowledge-run','open-chat','set-mcp-enabled'}
        if args.command in ('select-model', 'check-mcp'):
            def cancel_operation(*_):
                raise KeyboardInterrupt()
            signal.signal(signal.SIGTERM, cancel_operation)
        if args.command == 'select-model':
            if not args.profile:
                raise ValueError('Нужен профиль модели.')
            payload = {'id': args.profile, 'download': args.download}
        else:
            payload = json.load(sys.stdin) if args.command in mutations else {}
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON-объект.')
        print(json.dumps(dispatch(state, args.command, payload), ensure_ascii=False))
        return 0
    except KeyboardInterrupt:
        print('Handy: проверка MCP отменена.' if args.command == 'check-mcp' else
              'Handy: выбор или загрузка отменены.', file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('Handy: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
