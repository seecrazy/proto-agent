"""Shared tool parameter descriptions and side-effect-free validation."""
from __future__ import annotations

import copy
import inspect
import math

from proto.textstyle import MAX_TEXT as MAX_STYLE_TEXT, PROFILES as STYLE_PROFILES


class ParameterError(ValueError):
    pass


def object_schema(properties=None, required=(), additional=False):
    return {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": additional}


_PATH = {"type": "string", "minLength": 1, "description": "工作区相对路径"}
_TEXT = {"type": "string"}
_STYLE = object_schema({
    "text": {"type": "string", "maxLength": MAX_STYLE_TEXT},
    "profile": {"type": "string", "enum": list(STYLE_PROFILES), "default": "dialogue"}}, ["text"])
_BUILTINS = {
    "clock": object_schema(),
    "read_file": object_schema({
        "path": _PATH, "offset": {"type": "integer", "minimum": 0, "default": 0},
        "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200}}, ["path"]),
    "read_files": object_schema({"paths": {
        "type": "array", "items": _PATH, "minItems": 1, "maxItems": 5}}, ["paths"]),
    "write_file": object_schema({"path": _PATH, "content": _TEXT}, ["path", "content"]),
    "edit_file": object_schema({
        "path": _PATH, "old": {"type": "string", "minLength": 1}, "new": _TEXT,
        "replace_all": {"type": "boolean", "default": False}}, ["path", "old", "new"]),
    "list_dir": object_schema({"path": {"type": "string", "default": ""}}),
    "grep": object_schema({"pattern": _TEXT, "glob": {"type": "string", "default": "*"}}, ["pattern"]),
    "run_python": object_schema({
        "code": {"type": "string", "minLength": 1},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 120, "default": 60}}, ["code"]),
    "run_command": object_schema({
        "argv": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 32},
        "cwd": {"type": "string", "default": ""},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300, "default": 120}}, ["argv"]),
    "inspect_build": object_schema({"cwd": {"type": "string", "maxLength": 512}}),
    "read_skill": object_schema({"name": {"type": "string", "minLength": 1, "maxLength": 64}}, ["name"]),
    "scan_style": _STYLE,
    "polish_style": _STYLE,
    "web_fetch": object_schema({"url": {"type": "string", "minLength": 1}}, ["url"]),
    "github_search": object_schema({
        "query": {"type": "string", "minLength": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": 3}}, ["query"]),
    "github_readme": object_schema({"repo": {"type": "string", "minLength": 1}}, ["repo"]),
    "blackboard_read": object_schema(),
    "blackboard_post": object_schema({
        "text": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["help", "share", "report"], "default": "report"}}, ["text"]),
    "fleet_status": object_schema(),
    "company_context": object_schema({
        "offset": {"type": "integer", "minimum": 0, "maximum": 39, "default": 0}}),
    "company_inbox": object_schema(),
    "company_send": object_schema({
        "position_id": {"type": "string", "minLength": 32, "maxLength": 32},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000}}, ["position_id", "text"]),
    "delegate_coding": object_schema({"task": {"type": "string", "minLength": 1}}, ["task"]),
}
_KEYWORDS = {"type", "description", "properties", "required", "additionalProperties",
             "items", "minItems", "maxItems", "minLength", "maxLength",
             "minimum", "maximum", "enum", "default"}
_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def builtin_parameters(name):
    return copy.deepcopy(_BUILTINS[name])


def check_schema(schema):
    if not isinstance(schema, dict) or set(schema) - _KEYWORDS:
        raise ValueError("unsupported_parameter_schema")
    types = schema.get("type", [])
    types = [types] if isinstance(types, str) else types
    if not isinstance(types, list) or any(kind not in _TYPES for kind in types):
        raise ValueError("invalid_schema_type")
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if (not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties)
            or not isinstance(required, list) or not all(isinstance(key, str) for key in required)
            or set(required) - set(properties)
            or not isinstance(schema.get("additionalProperties", False), bool)):
        raise ValueError("invalid_object_schema")
    for child in properties.values():
        check_schema(child)
    if "items" in schema:
        check_schema(schema["items"])
    for key in ("minItems", "maxItems", "minLength", "maxLength"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError("invalid_schema_bound")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float) or not math.isfinite(schema[key])):
            raise ValueError("invalid_schema_bound")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("invalid_schema_enum")


def infer_parameters(function):
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return object_schema(additional=True)
    properties, required, additional = {}, [], False
    type_names = {str: "string", int: "integer", float: "number", bool: "boolean",
                  "str": "string", "int": "integer", "float": "number", "bool": "boolean"}
    for parameter in signature.parameters.values():
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            additional = True
        elif parameter.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY):
            kind = (type_names.get(parameter.annotation)
                    if isinstance(parameter.annotation, (type, str)) else None)
            if kind is None and parameter.default is not inspect.Parameter.empty:
                kind = type_names.get(type(parameter.default))
            descriptor = {"type": kind} if kind else {}
            if parameter.default is None and kind:
                descriptor["type"] = [kind, "null"]
            properties[parameter.name] = descriptor
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)
    return object_schema(properties, required, additional)


def validate(value, schema, location="args"):
    kind = schema.get("type")
    kinds = [kind] if isinstance(kind, str) else kind
    matches = {"object": isinstance(value, dict), "array": isinstance(value, list),
               "string": isinstance(value, str), "integer": type(value) is int,
               "number": type(value) in (int, float), "boolean": type(value) is bool,
               "null": value is None}
    if kinds and not any(matches.get(candidate, False) for candidate in kinds):
        raise ParameterError(f"{location} 类型应为 {kinds}")
    if type(value) is float and not math.isfinite(value):
        raise ParameterError(f"{location} 必须是有限数值")
    if "enum" in schema and not any(type(value) is type(item) and value == item for item in schema["enum"]):
        raise ParameterError(f"{location} 取值应为 {schema['enum']}")
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ParameterError(f"{location} 对象键必须是字符串")
        properties = schema.get("properties", {})
        missing = set(schema.get("required", [])) - set(value)
        if missing:
            raise ParameterError(f"{location} 缺少必需参数 {sorted(missing)}")
        unknown = set(value) - set(properties)
        if unknown and schema.get("additionalProperties") is False:
            raise ParameterError(f"{location} 未知参数 {sorted(unknown)}")
        for key, item in value.items():
            validate(item, properties.get(key, {}), f"{location}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", math.inf):
            raise ParameterError(f"{location} 数组长度超出契约")
        for index, item in enumerate(value):
            validate(item, schema.get("items", {}), f"{location}[{index}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", math.inf):
            raise ParameterError(f"{location} 文本长度超出契约")
    elif type(value) in (int, float):
        if value < schema.get("minimum", -math.inf) or value > schema.get("maximum", math.inf):
            raise ParameterError(f"{location} 数值超出契约")


def checked_arguments(name, arguments, schema):
    normalized = copy.deepcopy(arguments)
    if name == "write_file" and isinstance(normalized, dict) and "path" not in normalized:
        for alias in ("file_path", "filename"):
            if alias in normalized:
                normalized["path"] = normalized.pop(alias)
                break
    validate(normalized, schema)
    return normalized
