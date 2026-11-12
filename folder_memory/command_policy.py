"""Conservative command parsing shared by corpus ingestion and typed adapters.

Parsing is not authorization. Launch adapters must also enforce their own narrow
executable, arguments, paths and working-directory policies. Never use a shell.
"""
import os
import re
import shlex

_FORBIDDEN = frozenset('rm mv cp chmod chown chgrp sudo su doas dd mkfs diskutil kill killall pkill shutdown reboot halt poweroff eval exec source xargs env bash sh zsh fish dash ksh csh tcsh ash busybox perl ruby php awk sed make find'.split())
# Deliberately smaller than the set of commands that *could* be read-only.
_READ_OPTIONS = {
    'ls': {'-a','-A','-l','-h','-1','-t','-r','-S','-R'},
    'pwd': {'-L','-P'},
    'cat': {'-n','-b','-s','-E','-T','-v'},
    'head': {'-n','-c'}, 'tail': {'-n','-c'},
    'wc': {'-l','-w','-c','-m'},
    'grep': {'-i','-n','-l','-c','-v','-w','-x','-F','-E','-r','-R','-e'},
    'rg': {'-i','-n','-l','-c','-v','-w','-F','-e','--files','--hidden'},
    'du': {'-s','-h','-a'}, 'df': {'-h','-k','-P'},
    'file': {'-b','-i'}, 'stat': set(),
}
_META = re.compile(r'[\x00-\x1f\x7f;&|<>`$(){}\[\]*?!\\~]')
_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def parse_simple_command(command):
    """Parse one literal command, refusing shell syntax, expansions and globs."""
    if not isinstance(command, str) or not command.strip() or len(command) > 4096 or _META.search(command):
        raise ValueError('Expected one literal command without shell syntax')
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise ValueError('Invalid command quoting') from exc
    if not argv or len(argv) > 64 or any(not a or len(a) > 1024 for a in argv) or _ASSIGNMENT.match(argv[0]):
        raise ValueError('Invalid command arguments')
    return argv


def is_forbidden_argv(argv):
    """Absolute denylist only; False does not imply a command is authorized."""
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(a, str) or not a or _META.search(a) for a in argv):
        return True
    name = os.path.basename(argv[0]).lower()
    if name in _FORBIDDEN or name.startswith('mkfs.') or _ASSIGNMENT.match(argv[0]):
        return True
    if re.fullmatch(r'python(?:[0-9]+(?:\.[0-9]+)*)?', name):
        return len(argv) < 3 or argv[1:3] != ['-m', 'http.server']
    if name in {'node', 'nodejs'} and any(a in {'-e', '--eval', '-p', '--print', '-r', '--require', '--import'} or a.startswith(('--eval=', '--print=', '--require=', '--import=')) for a in argv[1:]):
        return True
    return False


def validate_read_command(command):
    """Return argv only for an explicitly allowlisted literal read-only command."""
    argv = parse_simple_command(command)
    if is_forbidden_argv(argv) or argv[0] not in _READ_OPTIONS:
        raise ValueError('Command not in read-only allowlist')
    options = _READ_OPTIONS[argv[0]]
    end = False
    for arg in argv[1:]:
        if arg == '--' and not end:
            end = True
        elif not end and arg.startswith('-') and arg != '-' and arg not in options:
            raise ValueError('Option not in read-only allowlist')
    return argv

# These additional rules are for stored, non-executable reference recipes only.
# They do not expand the runtime command or managed-project launch policies above.
_RECIPE_OPTIONS = {
    **_READ_OPTIONS,
    'ls': {'-a','-A','-l','-h','-1','-t','-r','-S','-R','-d','-i','-s','-F'},
    'grep': {'-i','-n','-l','-c','-v','-w','-x','-F','-E','-r','-R','-e','-H','-h','-s','-o'},
    'sort': {'-n','-r','-u','-f','-b','-h','-M','-V'},
    'uniq': {'-c','-d','-u','-i'},
    'cut': {'-c','-b','-f','-d','-s'},
    'ps': {'-a','-A','-d','-e','-f','-l','-w','-x','-u'},
    'echo': {'-n'}, 'uname': {'-a','-s','-n','-r','-v','-m'},
}
_VALUE_OPTIONS = {'head': {'-n','-c'}, 'tail': {'-n','-c'}, 'grep': {'-e'}, 'rg': {'-e'},
                  'cut': {'-c','-b','-f','-d'}}
_FIND_VALUE_OPTIONS = {'-name','-iname','-type','-maxdepth','-mindepth','-size','-mtime','-path','-regex'}
_FIND_NO_VALUE = {'-print','-print0'}


def _recipe_parts(command):
    """Split literal pipelines; only quoted glob/regex punctuation is accepted."""
    if not isinstance(command,str) or not command.strip() or len(command)>4096:
        raise ValueError('Invalid recipe')
    parts=[]; start=0; quote=None; escaped=False
    for i,ch in enumerate(command):
        if ord(ch)<32 or ord(ch)==127 or ch == '`':
            raise ValueError('Control characters and substitutions are forbidden')
        if ch == '$' and not (quote == "'" and i+1 < len(command) and command[i+1] in "'|)"):
            raise ValueError('Shell substitutions are forbidden')
        if escaped:
            escaped=False
            continue
        if quote == "'":
            if ch == "'": quote=None
            continue
        if ch == '\\':
            if quote is None: raise ValueError('Unquoted escapes are forbidden')
            escaped=True
            continue
        if quote == '"':
            if ch == '"': quote=None
            continue
        if ch in "'\"": quote=ch
        elif ch == '|':
            parts.append(command[start:i]); start=i+1
        elif ch in '&;<>*?[](){}!~':
            raise ValueError('Unquoted shell syntax is forbidden')
    if quote or escaped: raise ValueError('Invalid quoting')
    parts.append(command[start:])
    if len(parts)>8 or any(not part.strip() for part in parts): raise ValueError('Invalid pipeline')
    return parts


def _check_find(args):
    expression=False; i=0
    while i<len(args):
        arg=args[i]
        if arg in _FIND_NO_VALUE:
            expression=True; i+=1; continue
        if arg in _FIND_VALUE_OPTIONS:
            expression=True; i+=1
            if i>=len(args): raise ValueError('Missing find argument')
            value=args[i]
            if arg in {'-maxdepth','-mindepth'} and not re.fullmatch(r'\d+',value): raise ValueError('Invalid depth')
            if arg=='-type' and value not in {'b','c','d','f','l','p','s'}: raise ValueError('Invalid find type')
            if arg=='-size' and not re.fullmatch(r'[+-]?\d+[bckMG]?',value): raise ValueError('Invalid size')
            if arg=='-mtime' and not re.fullmatch(r'[+-]?\d+',value): raise ValueError('Invalid mtime')
            i+=1; continue
        if expression or arg.startswith('-') or arg in {'!', '(', ')'}:
            raise ValueError('Unsupported find expression')
        i+=1


def _check_recipe_stage(argv):
    if not argv or len(argv)>64 or any(not a or len(a)>1024 for a in argv): raise ValueError('Invalid recipe arguments')
    name=argv[0]
    if name=='find':
        _check_find(argv[1:]); return
    if name not in _RECIPE_OPTIONS or is_forbidden_argv([name]): raise ValueError('Unsupported recipe command')
    opts=_RECIPE_OPTIONS[name]; valued=_VALUE_OPTIONS.get(name,set()); end=False; pos=0; i=1
    while i<len(argv):
        arg=argv[i]
        if not end and arg=='--': end=True; i+=1; continue
        if not end and arg.startswith('-') and arg!='-':
            if arg in opts:
                if arg in valued:
                    i+=1
                    if i>=len(argv): raise ValueError('Missing option value')
            elif arg.startswith('--'):
                raise ValueError('Unsupported long option')
            elif name in {'head','tail'} and re.fullmatch(r'-\d+',arg):
                pass
            elif len(arg)>2 and all(('-'+ch) in opts and ('-'+ch) not in valued for ch in arg[1:]):
                pass
            else:
                raise ValueError('Unsupported option')
        else:
            pos+=1
        i+=1
    # uniq's second positional parameter is an output file.
    if name=='uniq' and pos>1: raise ValueError('Output file not allowed')


def validate_read_recipe(command):
    """Return literal argv stages for non-executed corpus reference only.

    Even validated recipes are untrusted data, not permission to run programs.
    Runtime must use validate_read_command and its own path/argument controls.
    """
    stages=[]
    for part in _recipe_parts(command):
        try: argv=shlex.split(part, posix=True)
        except ValueError as exc: raise ValueError('Invalid recipe quoting') from exc
        _check_recipe_stage(argv); stages.append(argv)
    return stages
