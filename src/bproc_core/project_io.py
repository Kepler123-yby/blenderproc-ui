"""Strict UTF-8 YAML persistence without implicit resource modification."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
import math
import os
import tempfile
import types

import yaml

from .config import ConfigError, ProjectConfig


class _UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConfigError(f"YAML 字段名必须是字符串（第 {key_node.start_mark.line + 1} 行）")
        if key in result:
            raise ConfigError(f"YAML 字段重复：{key}（第 {key_node.start_mark.line + 1} 行）")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _decode(value: Any, kind: Any, field_name: str, base_dir: Path):
    origin, args = get_origin(kind), get_args(kind)
    if origin in (Union, types.UnionType):
        if value is None and type(None) in args:
            return None
        choices = [arg for arg in args if arg is not type(None)]
        if len(choices) == 1:
            return _decode(value, choices[0], field_name, base_dir)
    if is_dataclass(kind):
        if not isinstance(value, dict):
            raise ConfigError(f"{field_name} 必须是字段映射")
        expected = {f.name for f in fields(kind)}
        unknown = set(value) - expected
        if unknown:
            raise ConfigError(f"{field_name} 包含未知字段：{', '.join(sorted(unknown))}")
        hints = get_type_hints(kind)
        kwargs = {name: _decode(item, hints[name], f"{field_name}.{name}", base_dir) for name, item in value.items()}
        try:
            return kind(**kwargs)
        except TypeError as exc:
            raise ConfigError(f"{field_name} 缺少必要字段：{exc}") from exc
    if origin is tuple:
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"{field_name} 必须是列表")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(item, args[0], f"{field_name}[{i}]", base_dir) for i, item in enumerate(value))
        if len(value) != len(args):
            raise ConfigError(f"{field_name} 需要 {len(args)} 个值")
        return tuple(_decode(item, arg, f"{field_name}[{i}]", base_dir) for i, (item, arg) in enumerate(zip(value, args)))
    if origin is Literal:
        if value not in args or isinstance(value, bool) and not any(type(arg) is bool for arg in args):
            raise ConfigError(f"{field_name} 只能为 {args}")
        return value
    if kind is Path:
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ConfigError(f"{field_name} 必须是非空路径字符串")
        path = Path(value).expanduser()
        return (base_dir / path).resolve() if not path.is_absolute() else path
    if kind is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{field_name} 必须是布尔值")
        return value
    if kind is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{field_name} 必须是整数")
        return value
    if kind is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ConfigError(f"{field_name} 必须是有限数值")
        return float(value)
    if kind is str:
        if not isinstance(value, str):
            raise ConfigError(f"{field_name} 必须是字符串")
        return value
    raise ConfigError(f"{field_name} 含不支持的类型 {kind}")


def project_from_dict(data: dict, *, base_dir: Path | None = None) -> ProjectConfig:
    if not isinstance(data, dict) or "schema_version" not in data:
        raise ConfigError("项目必须声明 schema_version: 1")
    config = _decode(data, ProjectConfig, "project", Path(base_dir or Path.cwd()).resolve())
    if isinstance(config.schema_version, bool) or config.schema_version != 1:
        raise ConfigError("仅支持 schema_version: 1；请先迁移配置")
    return config


def _encode(value):
    if is_dataclass(value):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, float) and not math.isfinite(value):
        raise ConfigError("配置不能包含 NaN 或 Infinity")
    return value


def project_to_dict(config: ProjectConfig) -> dict:
    return _encode(config)


# Explicit aliases keep callers readable in exporters and metadata writers.
config_to_dict = project_to_dict
config_from_dict = project_from_dict


def load_project(path: Path) -> ProjectConfig:
    path = Path(path).expanduser().resolve()
    try:
        with path.open("r", encoding="utf-8") as stream:
            data = yaml.load(stream, Loader=_UniqueLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"无法读取项目 {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("项目 YAML 顶层必须是字段映射")
    return project_from_dict(data, base_dir=path.parent)


def save_project(config: ProjectConfig, path: Path) -> None:
    """Atomically replace a user-owned configuration, including incomplete drafts."""
    path = Path(path).expanduser().resolve()
    data = project_to_dict(config)
    # Validate schema/type shape, not readiness; a new draft must remain saveable.
    project_from_dict(data, base_dir=path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except OSError as exc:
        raise ConfigError(f"无法保存项目 {path}：{exc}") from exc
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
