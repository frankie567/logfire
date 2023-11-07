from __future__ import annotations as _annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Set, TypeVar

from typing_extensions import get_args, get_origin

from logfire.exporters.console import ConsoleColorsValues

from ._constants import LOGFIRE_BASE_URL
from .exceptions import LogfireConfigError

T = TypeVar('T')

slots_true = {'slots': True} if sys.version_info >= (3, 10) else {}

ShowSummaryValues = Literal['always', 'never', 'new-project']
"""Possible values for the `show_summary` parameter."""


@dataclass(**slots_true)
class ConfigParam:
    """A parameter that can be configured for a Logfire instance."""

    env_vars: list[str]
    """Environment variables to check for the parameter."""
    allow_file_config: bool = False
    """Whether the parameter can be set in the config file."""
    default: Any = None
    """Default value if no other value is found."""
    tp: Any = str
    """Type of the parameter."""


# fmt: off
BASE_URL = ConfigParam(env_vars=['LOGFIRE_BASE_URL', 'OTEL_EXPORTER_OTLP_ENDPOINT'], allow_file_config=True, default=LOGFIRE_BASE_URL)
"""Use to set the base URL of the Logfire backend."""
SEND_TO_LOGFIRE = ConfigParam(env_vars=['LOGFIRE_SEND_TO_LOGFIRE'], allow_file_config=True, default=True, tp=bool)
"""Whether to send spans to Logfire."""
TOKEN = ConfigParam(env_vars=['LOGFIRE_TOKEN'])
"""Token for the Logfire API."""
PROJECT_NAME = ConfigParam(env_vars=['LOGFIRE_PROJECT_NAME'], allow_file_config=True)
"""Name of the project."""
SERVICE_NAME = ConfigParam(env_vars=['LOGFIRE_SERVICE_NAME', 'OTEL_SERVICE_NAME'], allow_file_config=True, default='unknown')
"""Name of the service emitting spans. See https://opentelemetry.io/docs/specs/semconv/resource/#service"""
SERVICE_VERSION = ConfigParam(env_vars=['LOGFIRE_SERVICE_VERSION', 'OTEL_SERVICE_VERSION'], allow_file_config=True)
"""Version number of the service emitting spans. See https://opentelemetry.io/docs/specs/semconv/resource/#service"""
SHOW_SUMMARY = ConfigParam(env_vars=['LOGFIRE_SHOW_SUMMARY'], allow_file_config=True, default='new-project', tp=ShowSummaryValues)
"""Whether to show the summary when a new project is created."""
CREDENTIALS_DIR = ConfigParam(env_vars=['LOGFIRE_CREDENTIALS_DIR'], allow_file_config=True, default='.logfire', tp=Path)
"""The directory where to store the configuration file."""
LOGFIRE_EXPORTER_FALLBACK_TO_LOCAL_FILE = ConfigParam(env_vars=['LOGFIRE_EXPORTER_FALLBACK_TO_LOCAL_FILE'], allow_file_config=True, default=True, tp=bool)
"""Path to the file where spans are stored when the exporter is disabled."""
COLLECT_SYSTEM_METRICS = ConfigParam(env_vars=['LOGFIRE_COLLECT_SYSTEM_METRICS'], allow_file_config=True, default=True, tp=bool)
"""Whether to collect system metrics."""
CONSOLE_ENABLED = ConfigParam(env_vars=['LOGFIRE_CONSOLE_ENABLED'], allow_file_config=True, default=True, tp=bool)
"""Whether to enable the console exporter."""
CONSOLE_COLORS = ConfigParam(env_vars=['LOGFIRE_CONSOLE_COLORS'], allow_file_config=True, default='auto', tp=ConsoleColorsValues)
"""Whether to use colors in the console."""
CONSOLE_INDENT_SPAN = ConfigParam(env_vars=['LOGFIRE_CONSOLE_INDENT_SPAN'], allow_file_config=True, default=True, tp=bool)
"""Whether to indent the spans in the console."""
CONSOLE_INCLUDE_TIMESTAMP = ConfigParam(env_vars=['LOGFIRE_CONSOLE_INCLUDE_TIMESTAMP'], allow_file_config=True, default=True, tp=bool)
"""Whether to include the timestamp in the console."""
CONSOLE_VERBOSE = ConfigParam(env_vars=['LOGFIRE_CONSOLE_VERBOSE'], allow_file_config=True, default=False, tp=bool)
"""Whether to log in verbose mode in the console."""
DISABLE_PYDANTIC_PLUGIN = ConfigParam(env_vars=['LOGFIRE_DISABLE_PYDANTIC_PLUGIN'], allow_file_config=True, default=False, tp=bool)
"""Whether to disable the Logfire Pydantic plugin."""
PYDANTIC_PLUGIN_INCLUDE = ConfigParam(env_vars=['LOGFIRE_PYDANTIC_PLUGIN_INCLUDE'], allow_file_config=True, default=set(), tp=Set[str])
"""Set of items that should be included in Logfire Pydantic plugin instrumentation."""
PYDANTIC_PLUGIN_EXCLUDE = ConfigParam(env_vars=['LOGFIRE_PYDANTIC_PLUGIN_EXCLUDE'], allow_file_config=True, default=set(), tp=Set[str])
"""Set of items that should be excluded from Logfire Pydantic plugin instrumentation."""
# fmt: on

CONFIG_PARAMS = {
    'base_url': BASE_URL,
    'send_to_logfire': SEND_TO_LOGFIRE,
    'token': TOKEN,
    'project_name': PROJECT_NAME,
    'service_name': SERVICE_NAME,
    'service_version': SERVICE_VERSION,
    'show_summary': SHOW_SUMMARY,
    'data_dir': CREDENTIALS_DIR,
    'exporter_fallback_to_local_file': LOGFIRE_EXPORTER_FALLBACK_TO_LOCAL_FILE,
    'collect_system_metrics': COLLECT_SYSTEM_METRICS,
    'console_enabled': CONSOLE_ENABLED,
    'console_colors': CONSOLE_COLORS,
    'console_indent_span': CONSOLE_INDENT_SPAN,
    'console_include_timestamp': CONSOLE_INCLUDE_TIMESTAMP,
    'console_verbose': CONSOLE_VERBOSE,
    'disable_pydantic_plugin': DISABLE_PYDANTIC_PLUGIN,
    'pydantic_plugin_include': PYDANTIC_PLUGIN_INCLUDE,
    'pydantic_plugin_exclude': PYDANTIC_PLUGIN_EXCLUDE,
}


@dataclass(**slots_true)
class ParamManager:
    """Manage parameters for a Logfire instance."""

    config_from_file: dict[str, Any]
    """Config loaded from the config file."""

    def load_param(self, name: str, runtime: Any = None) -> Any:
        """Load a parameter given its name.

        The parameter is loaded in the following order:
        1. From the runtime argument, if provided.
        2. From the environment variables.
        3. From the config file, if allowed.

        If none of the above is found, the default value is returned.

        Args:
            name: Name of the parameter.
            runtime: Value provided at runtime.

        Returns:
            The value of the parameter.
        """
        if runtime is not None:
            return runtime

        param = CONFIG_PARAMS[name]
        for env_var in param.env_vars:
            value = os.getenv(env_var)
            if value is not None:
                return self._cast(value, name, param.tp)

        if param.allow_file_config:
            value = self.config_from_file.get(name)
            if value is not None:
                return self._cast(value, name, param.tp)

        return self._cast(param.default, name, param.tp)

    def _cast(self, value: Any, name: str, tp: type[T]) -> T | None:
        if tp is str:
            return value
        if get_origin(tp) is Literal:
            return _check_literal(value, name, tp)
        if tp is bool:
            return _check_bool(value, name)  # type: ignore
        if tp is Path:
            return Path(value)  # type: ignore
        if get_origin(tp) is set and get_args(tp) == (str,):
            return _extract_set_of_str(value)  # type: ignore
        raise RuntimeError(f'Unexpected type {tp}')


def _check_literal(value: Any, name: str, tp: type[T]) -> T | None:
    if value is None:
        return None
    literals = get_args(tp)
    if value not in literals:
        raise LogfireConfigError(f'Expected {name} to be one of {literals}, got {value!r}')
    return value


def _check_bool(value: Any, name: str) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if value.lower() in ('1', 'true', 't'):
            return True
        if value.lower() in ('0', 'false', 'f'):
            return False
    raise LogfireConfigError(f'Expected {name} to be a boolean, got {value!r}')


def _extract_set_of_str(value: str | set[str]) -> set[str]:
    return set(map(str.strip, value.split(','))) if isinstance(value, str) else value