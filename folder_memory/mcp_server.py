"""Expose Handy artifacts and scheduled Pi chats through the MCP stdio transport."""

import argparse
from datetime import datetime
import json
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, WithJsonSchema, with_config
from typing_extensions import TypedDict

from .artifact_tool import create_artifact
from .artifacts import project_root
from .file_access import ARTIFACT_NAMESPACE
from .runtime import runtime_dir


ArtifactKind = Literal['timer', 'pomodoro', 'clock', 'dice', 'password', 'chart', 'table', 'list']


@with_config(extra='forbid')
class ArtifactOptions(TypedDict, total=False):
    title: str
    time_zone: Annotated[str, Field(description='clock: IANA zone, e.g. America/New_York.')]
    seconds: Annotated[int, Field(description='timer: countdown duration in seconds.')]
    focus_minutes: int
    short_break_minutes: int
    long_break_minutes: int
    rounds: int
    count: int
    sides: int
    length: int
    groups: list[Literal['lowercase', 'uppercase', 'digits', 'symbols']]
    path: str
    data: Annotated[str, Field(description='Inline CSV, TSV, JSON or list text; use instead of path.')]
    format: Literal['csv', 'tsv', 'json', 'text', 'xlsx']
    x: str
    y: str
    chart_type: Literal['line', 'bar', 'donut']
    sheet: str
    where: list[dict[str, Any]]
    density: Literal['comfortable', 'compact']


# Keep option names inline for small models; canonical per-kind validation below
# remains authoritative and receives the original values without coercion.
ArtifactOptionsInput = Annotated[dict[str, Any], WithJsonSchema(TypeAdapter(ArtifactOptions).json_schema())]


def artifact_inventory(root: Path, limit: int = 100) -> list[dict[str, Any]]:
    """Read only complete, non-symlink artifacts inside the workspace namespace."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('limit должен быть целым числом от 1 до 100.')
    root = project_root(root)
    directory = root / ARTIFACT_NAMESPACE / 'artifacts'
    if not directory.exists():
        return []
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError('Каталог артефактов не должен быть символической ссылкой.')
    artifacts = []
    for entry in directory.iterdir():
        if entry.name.startswith('.') or entry.is_symlink() or not entry.is_dir():
            continue
        html, data = entry / 'index.html', entry / 'data.json'
        if any(path.is_symlink() or not path.is_file() for path in (html, data)):
            continue
        try:
            if data.stat().st_size > 768 * 1024:
                continue
            payload = json.loads(data.read_text(encoding='utf-8'))
            if not isinstance(payload, dict) or not isinstance(payload.get('title'), str):
                continue
            artifacts.append({'id': entry.name, 'kind': entry.name.rsplit('-', 1)[0],
                              'title': payload['title'], 'path': str(html), 'uri': html.as_uri(),
                              'data_path': str(data), 'created_at': html.stat().st_mtime})
        except (OSError, UnicodeError, ValueError):
            continue
    return sorted(artifacts, key=lambda artifact: artifact['created_at'], reverse=True)[:limit]


def _timestamp(value: float | str | None) -> float | None:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('В run_at укажи часовой пояс, например 2026-11-12T09:00:00+03:00.')
        return parsed.timestamp()
    return value


def build_server(state: Path, root: Path, *, artifacts: bool = True, scheduler: bool = True):
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.exceptions import ToolError

    state = state.expanduser().resolve()
    root = root.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Рабочий путь должен быть папкой.')
    server = FastMCP('Handy', instructions=(
        'Handy creates offline HTML artifacts in the current workspace and schedules prompts '
        'for fresh Pi chats. Only report an artifact or a schedule as created after a successful receipt. '
        'Open returned artifact paths with the operating system or browser. Scheduling runs model tools '
        'with the same local permissions as an interactive Pi session.'))

    def require_enabled(feature):
        # A pi session can keep this stdio server alive after the app disables it.
        from .pi_runtime import load_settings
        if not load_settings(state)[feature + '_enabled']:
            raise ToolError('Этот инструмент выключен в настройках Handy.')

    from .pi_knowledge import register_knowledge
    register_knowledge(server, state, root)

    if artifacts:
        @server.tool()
        def artifact_create(kind: ArtifactKind, options: ArtifactOptionsInput) -> dict[str, Any]:
            """Create a standalone HTML artifact; return published file paths.

            A live clock uses kind="clock", options={"time_zone":"America/New_York"};
            a countdown uses kind="timer", options={"seconds":60}. Use only fields
            relevant to that kind. Chart/table/list need path OR data+format.
            """
            require_enabled('artifacts')
            result = create_artifact(None, root, 'pi-mcp', kind, options)
            if result['status'] != 'ok':
                raise ToolError(result['text'])
            receipt = result['data']['artifact']
            return {**result, 'data': {**result['data'], 'artifact': {
                **receipt, 'uri': Path(receipt['path']).as_uri()}}}

        @server.tool()
        def artifact_list(limit: int = 30) -> dict[str, Any]:
            """List the latest complete HTML artifacts saved in this workspace (maximum 100)."""
            require_enabled('artifacts')
            return {'artifacts': artifact_inventory(root, limit)}

    if scheduler:
        from . import pi_scheduler as jobs

        def workspace_jobs():
            return [job for job in jobs.list_jobs(state) if job.get('cwd') == str(root)]

        def require_job(job_id):
            if not any(job['id'] == job_id for job in workspace_jobs()):
                raise ToolError('В этой рабочей папке нет задачи с таким ID. Вызови schedule_list.')

        def start(job):
            try:
                require_enabled('scheduler')
                worker = jobs.start_worker(state)
            except (OSError, ValueError, RuntimeError, ToolError) as error:
                return {'job': job, 'worker_error': str(error),
                        'message': 'Задача сохранена, но исполнитель не запущен. Запусти Handy в приложении.'}
            return {'job': job, 'worker': worker}

        @server.tool()
        def schedule_create(title: str, prompt: str, run_at: float | str | None = None,
                            interval_seconds: int | None = None) -> dict[str, Any]:
            """Schedule a prompt in a NEW Pi chat using this workspace.

            run_at is a Unix timestamp or an ISO 8601 timestamp with timezone.
            Use run_at for a one-time task; interval_seconds for a repeating task.
            The background chat uses the configured model and MCP tools. Results remain
            in Pi's native session, and Handy.app can notify and open the new chat.
            """
            require_enabled('scheduler')
            return start(jobs.create_job(state, title=title, prompt=prompt, cwd=str(root),
                                         run_at=_timestamp(run_at), interval_seconds=interval_seconds))

        @server.tool()
        def schedule_list() -> dict[str, Any]:
            """List scheduled prompts belonging to this workspace and worker status."""
            require_enabled('scheduler')
            return {'jobs': workspace_jobs(), 'worker': jobs.worker_status(state)}

        @server.tool()
        def schedule_update(job_id: str, title: str | None = None, prompt: str | None = None,
                            enabled: bool | None = None, run_at: float | str | None = None,
                            interval_seconds: int | None = None) -> dict[str, Any]:
            """Edit or pause an existing workspace task. Omitted fields stay unchanged."""
            require_enabled('scheduler')
            require_job(job_id)
            changes = {key: value for key, value in {
                'title': title, 'prompt': prompt, 'enabled': enabled, 'run_at': _timestamp(run_at),
                'interval_seconds': interval_seconds}.items() if value is not None}
            if not changes:
                raise ToolError('Укажи хотя бы одно изменение.')
            return start(jobs.update_job(state, job_id, expected_cwd=root, **changes))

        @server.tool()
        def schedule_delete(job_id: str) -> dict[str, Any]:
            """Delete a scheduled prompt in this workspace; existing run chats are retained."""
            require_enabled('scheduler')
            require_job(job_id)
            result = jobs.delete_job(state, job_id, expected_cwd=root)
            return {'deleted': job_id, 'result': result}

        @server.tool()
        def schedule_runs() -> dict[str, Any]:
            """List completed/failed background chats for this workspace with session paths and answers."""
            require_enabled('scheduler')
            return {'runs': jobs.list_runs(state, cwd=root)}

    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=runtime_dir())
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--no-artifacts', action='store_true')
    parser.add_argument('--no-scheduler', action='store_true')
    args = parser.parse_args(argv)
    server = build_server(args.state_dir, args.root, artifacts=not args.no_artifacts,
                          scheduler=not args.no_scheduler)
    server.run(transport='stdio')


if __name__ == '__main__':
    main()
