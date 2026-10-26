from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from types import UnionType
from typing import (
    Annotated,
    Any,
    Literal,
    NotRequired,
    Required,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)


class OperationType(Enum):
    READ = "read"
    WRITE = "write"


class ToolAttributeName(Enum):
    APP = "_is_app_tool"
    ENV = "_is_env_tool"
    DATA = "_is_data_tool"
    USER = "_is_user_tool"


@dataclass
class AppToolArg:
    name: str
    arg_type: str
    json_schema: dict[str, Any] | None = None
    description: str | None = None
    has_default: bool = False
    default: Any | None = None


@dataclass
class AppTool:
    class_name: str
    app_name: str
    name: str
    function_description: str | None
    args: list[AppToolArg]
    function: Callable[..., Any]
    class_instance: Any
    write_operation: bool | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.function(self.class_instance, *args, **kwargs)

    def to_open_ai(self) -> dict[str, Any]:
        properties = {
            arg.name: {
                **(arg.json_schema or {"type": _json_type(arg.arg_type)}),
                "description": arg.description or "",
            }
            for arg in self.args
        }
        required = [arg.name for arg in self.args if not arg.has_default]
        return {
            "type": "function",
            "function": {
                "name": f"{self.app_name}__{self.name}",
                "description": self.function_description or "",
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    # Reject invented arguments early at the model/tool
                    # boundary instead of silently ignoring misspelled fields.
                    "additionalProperties": False,
                },
            },
        }


def _json_type(type_name: str) -> str:
    return {
        "str": "string",
        "int": "integer",
        "float": "number",
        "bool": "boolean",
    }.get(type_name, "string")


def _json_schema(type_obj: Any) -> dict[str, Any]:
    """Translate Python annotations into the subset used by Tool Calling.

    Generic aliases such as ``list[str]`` do not have one of the primitive
    names handled by ``_json_type``.  Treating them as strings makes models
    emit comma-separated text, which business code then iterates character by
    character.  Preserve container structure explicitly in the JSON Schema.
    """

    origin = get_origin(type_obj)
    args = get_args(type_obj)
    if type_obj is Any:
        return {}
    if origin in {Required, NotRequired}:
        return _json_schema(args[0])
    if is_typeddict(type_obj):
        hints = get_type_hints(type_obj, include_extras=True)
        # Resolve qualifiers explicitly: postponed annotations can make
        # __required_keys__ inaccurate until get_type_hints is evaluated.
        required = [
            name
            for name, hint in hints.items()
            if get_origin(hint) is Required
            or (
                get_origin(hint) is not NotRequired
                and name in type_obj.__required_keys__
            )
        ]
        return {
            "type": "object",
            "properties": {name: _json_schema(hint) for name, hint in hints.items()},
            "required": required,
            "additionalProperties": False,
        }
    if origin is Annotated:
        base_type, *metadata = args
        schema = _json_schema(base_type)
        # A small JSON-Schema mapping inside ``Annotated`` lets domain Apps
        # publish numeric bounds without coupling this generic module to them.
        for item in metadata:
            if isinstance(item, dict):
                schema.update(item)
        return schema
    if origin in {list, tuple, set, frozenset}:
        item_type = args[0] if args else str
        return {"type": "array", "items": _json_schema(item_type)}
    if origin is dict:
        return {
            "type": "object",
            "additionalProperties": _json_schema(args[1]) if len(args) > 1 else {},
        }
    if origin is Literal:
        values = list(args)
        primitive = type(values[0]) if values else str
        return {**_json_schema(primitive), "enum": values}
    if origin in {Union, UnionType}:
        non_none = [item for item in args if item is not type(None)]
        if len(non_none) == 1:
            return _json_schema(non_none[0])
        return {"anyOf": [_json_schema(item) for item in non_none]}
    if inspect.isclass(type_obj) and issubclass(type_obj, Enum):
        return {
            "type": "string",
            "enum": [item.value for item in type_obj],
        }
    primitive_types = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        dict: "object",
    }
    return {"type": primitive_types.get(type_obj, "string")}


def _mark_tool(attr: ToolAttributeName, func: Callable[..., Any] | None = None):
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        setattr(f, attr.value, True)
        return f

    return decorator if func is None else decorator(func)


def app_tool(func: Callable[..., Any] | None = None, **_: Any):
    return _mark_tool(ToolAttributeName.APP, func)


def data_tool(func: Callable[..., Any] | None = None, **_: Any):
    return _mark_tool(ToolAttributeName.DATA, func)


def env_tool(func: Callable[..., Any] | None = None, **_: Any):
    return _mark_tool(ToolAttributeName.ENV, func)


def user_tool(func: Callable[..., Any] | None = None, **_: Any):
    return _mark_tool(ToolAttributeName.USER, func)


def agent_tool(func: Callable[..., Any] | None = None, *, doc_enabled: bool = True):
    def decorator(f: Callable[..., Any]) -> Callable[..., Any]:
        f.is_agent_tool = True
        setattr(f, ToolAttributeName.APP.value, True)
        if not doc_enabled:
            f.__doc__ = ""
        return f

    return decorator if func is None else decorator(func)


def build_tool(
    instance: Any, func: Callable[..., Any], failure_probability=None
) -> AppTool:
    sig = inspect.signature(func)
    # ``include_extras`` preserves Annotated range metadata for tool schemas.
    hints = get_type_hints(func, include_extras=True)
    args: list[AppToolArg] = []
    for name, param in sig.parameters.items():
        if name == "self":
            continue
        type_obj = hints.get(name, str)
        type_name = getattr(type_obj, "__name__", "str")
        args.append(
            AppToolArg(
                name=name,
                arg_type=type_name,
                json_schema=_json_schema(type_obj),
                has_default=param.default is not inspect._empty,
                default=None if param.default is inspect._empty else param.default,
            )
        )
    return AppTool(
        class_name=instance.__class__.__name__,
        app_name=getattr(instance, "name", instance.__class__.__name__),
        name=func.__name__,
        function_description=inspect.getdoc(func),
        args=args,
        function=func,
        class_instance=instance,
        write_operation=getattr(func, "_event_operation_type", None)
        == OperationType.WRITE,
    )
