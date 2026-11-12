"""Validate tool arguments and derive repair hints from the model-facing schema.

This checks arguments, never intent, permissions or effects. jsonschema is
an explicit dependency of Handy's MCP tools.
"""

from functools import lru_cache

from .tool_catalog import TOOL_SCHEMAS


_PARAMETERS = {tool['function']['name']: tool['function']['parameters'] for tool in TOOL_SCHEMAS}
_CONSTRAINTS = ('type', 'const', 'enum', 'minimum', 'maximum', 'minLength', 'maxLength',
                'minItems', 'maxItems', 'uniqueItems', 'pattern', 'default')


@lru_cache(maxsize=16)
def _validator(name):
    from jsonschema import Draft202012Validator
    return Draft202012Validator(_PARAMETERS[name])


def _tags(schema):
    return {name: field['const'] for name, field in schema.get('properties', {}).items()
            if isinstance(field, dict) and 'const' in field}


def selected_contract(name, arguments):
    """Return only a uniquely identified variant; never guess an operation."""
    schema = _PARAMETERS.get(name)
    if schema is None:
        return None
    variants = schema.get('oneOf', schema.get('anyOf', ()))
    if isinstance(arguments, dict) and variants:
        matches = [variant for variant in variants
                   if _tags(variant) and all(arguments.get(key) == value for key, value in _tags(variant).items())]
        if len(matches) == 1:
            return matches[0]
    return schema


def _describe(schema, value, path, repair):
    properties = schema.get('properties', {})
    if properties:
        repair['allowed_fields'][path] = sorted(properties)
        if value is None or isinstance(value, dict):
            repair['missing_fields'].extend(path + '.' + key for key in schema.get('required', ())
                                            if value is None or key not in value)
        if isinstance(value, dict):
            if schema.get('additionalProperties') is False:
                repair['unexpected_fields'].extend(path + '.' + key for key in value if key not in properties)
        for key, field in properties.items():
            child = path + '.' + key
            repair['fields'][child] = {name: field[name] for name in _CONSTRAINTS if name in field}
            if isinstance(field.get('items'), dict):
                repair['fields'][child]['items'] = {name: field['items'][name] for name in _CONSTRAINTS
                                                   if name in field['items']}
            _describe(field, value.get(key) if isinstance(value, dict) else None, child, repair)


def _issue(error):
    expected = error.validator_value
    if error.validator in {'oneOf', 'anyOf'}:
        expected = [{'selected': _tags(branch), 'required': branch.get('required', [])} for branch in expected]
    return {'path': '$' + ''.join('.' + str(part) for part in error.absolute_path),
            'rule': error.validator, 'expected': expected}


def argument_feedback(name, arguments):
    """Return None or a non-executing needs_input result, without echoing values.

    Unknown extension tools keep their own adapters and validation. No arguments
    are removed or changed; a repair must be a new, valid model call.
    """
    if name not in _PARAMETERS:
        return None
    if not next(_validator(name).iter_errors(arguments), None):
        return None
    schema = selected_contract(name, arguments)
    repair = {'tool': name, 'selected': _tags(schema), 'allowed_fields': {},
              'unexpected_fields': [], 'missing_fields': [], 'fields': {}}
    variants = schema.get('oneOf', schema.get('anyOf', ()))
    if variants and not _tags(schema):
        repair['variants'] = [_tags(variant) for variant in variants]
        # An unknown discriminator has no selected option contract yet. Show
        # how to select one, rather than every utility's fields at once.
        discriminators = set.intersection(*(set(_tags(variant)) for variant in variants))
        description = {**schema, 'properties': {key: field for key, field in schema.get('properties', {}).items()
                                               if key in discriminators}, 'additionalProperties': True}
    else:
        description = schema
        if variants:
            repair['alternatives'] = [variant.get('required', []) for variant in variants]
    _describe(description, arguments, '$', repair)
    from jsonschema import Draft202012Validator
    repair['issues'] = [_issue(error) for error in list(Draft202012Validator(schema).iter_errors(arguments))[:12]]
    return {'status': 'needs_input',
            'text': 'Параметры не подошли выбранному варианту. Исправь указанные поля и повтори вызов.',
            'data': {'code': 'invalid_tool_arguments', 'repair': repair}}
