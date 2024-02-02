from __future__ import annotations

import warnings
from functools import cached_property, wraps
from inspect import Parameter as SignatureParameter, signature as inspect_signature
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    ContextManager,
    Sequence,
    TypedDict,
    TypeVar,
    Union,
    cast,
)

import opentelemetry.context as context_api
import opentelemetry.trace as trace_api
import rich.traceback
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Tracer
from opentelemetry.util import types as otel_types
from typing_extensions import LiteralString

from logfire._config import GLOBAL_CONFIG, LogfireConfig
from logfire._constants import ATTRIBUTES_JSON_SCHEMA_KEY
from logfire.version import VERSION

from . import AutoTraceModule, _async
from ._auto_trace import install_auto_tracing
from ._stack_info import StackInfo, get_caller_stack_info, get_filepath_attribute

try:
    from pydantic import ValidationError
except ImportError:
    ValidationError = None
from typing_extensions import ParamSpec

from logfire._formatter import logfire_format

from ._constants import (
    ATTRIBUTES_LOG_LEVEL_NAME_KEY,
    ATTRIBUTES_LOG_LEVEL_NUM_KEY,
    ATTRIBUTES_MESSAGE_KEY,
    ATTRIBUTES_MESSAGE_TEMPLATE_KEY,
    ATTRIBUTES_SAMPLE_RATE_KEY,
    ATTRIBUTES_SPAN_TYPE_KEY,
    ATTRIBUTES_TAGS_KEY,
    ATTRIBUTES_VALIDATION_ERROR_KEY,
    LEVEL_NUMBERS,
    NULL_ARGS_KEY,
    OTLP_MAX_INT_SIZE,
    LevelName,
)
from ._json_encoder import json_dumps_traceback, logfire_json_dumps
from ._json_schema import logfire_json_schema
from ._tracer import ProxyTracerProvider

_CWD = Path('.').resolve()

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.requests import Request
    from starlette.websockets import WebSocket


class Logfire:
    """The main logfire class."""

    def __init__(
        self,
        tags: Sequence[str] = (),
        config: LogfireConfig = GLOBAL_CONFIG,
        sample_rate: float | None = None,
    ) -> None:
        self._tags = list(tags)
        self._config = config
        self._sample_rate = sample_rate

    @property
    def config(self) -> LogfireConfig:
        return self._config

    def with_tags(self, *tags: str) -> Logfire:
        """A new Logfire instance with the given tags applied.

        ```py
        import logfire

        with logfire.with_tags('tag1'):
            logfire.info('new log 1')
        ```

        Args:
            tags: The tags to bind.

        Returns:
            A new Logfire instance with the tags applied.
        """
        return Logfire(self._tags + list(tags), self._config, self._sample_rate)

    def with_trace_sample_rate(self, sample_rate: float) -> Logfire:
        """A new Logfire instance with the given sampling ratio applied.

        ```py
        import logfire

        with logfire.with_trace_sample_rate(0.5):
            logfire.info('new log 1')
        ```

        Args:
            sample_rate: The sampling ratio to use.

        Returns:
            A new Logfire instance with the sampling ratio applied.
        """
        if sample_rate > 1 or sample_rate < 0:
            raise ValueError('sample_rate must be between 0 and 1')
        return Logfire(self._tags, self._config, sample_rate)

    @cached_property
    def _tracer_provider(self) -> ProxyTracerProvider:
        return self._config.get_tracer_provider()

    @cached_property
    def _logs_tracer(self) -> Tracer:
        return self._get_tracer(is_span_tracer=False)

    @cached_property
    def _spans_tracer(self) -> Tracer:
        return self._get_tracer(is_span_tracer=True)

    def _get_tracer(self, *, is_span_tracer: bool) -> Tracer:
        return self._tracer_provider.get_tracer(
            'logfire',  # the name here is really not important, logfire itself doesn't use it
            VERSION,
            is_span_tracer=is_span_tracer,
        )

    # If any changes are made to this method, they may need to be reflected in `_fast_span` as well.
    def _span(
        self,
        msg_template: LiteralString,
        attributes: dict[str, Any],
        *,
        span_name: str | None = None,
        stacklevel: int = 3,
        decorator: bool = False,
    ) -> LogfireSpan:
        stack_info = get_caller_stack_info(stacklevel=stacklevel)

        merged_attributes = {**stack_info, **attributes}
        merged_attributes[ATTRIBUTES_MESSAGE_TEMPLATE_KEY] = msg_template

        tags, merged_attributes = _merge_tags_into_attributes(merged_attributes, self._tags)

        span_name_: str
        if span_name is not None:
            span_name_ = span_name
        else:
            span_name_ = msg_template
        format_kwargs = {'span_name': span_name_, **merged_attributes}
        log_message = logfire_format(msg_template, format_kwargs, fallback='...', stacklevel=stacklevel)

        merged_attributes[ATTRIBUTES_MESSAGE_KEY] = log_message

        otlp_attributes = user_attributes(merged_attributes)

        if attributes_json_schema := logfire_json_schema(attributes):
            otlp_attributes[ATTRIBUTES_JSON_SCHEMA_KEY] = attributes_json_schema

        if tags:
            otlp_attributes[ATTRIBUTES_TAGS_KEY] = tags

        sample_rate = (
            self._sample_rate
            if self._sample_rate is not None
            else otlp_attributes.pop(ATTRIBUTES_SAMPLE_RATE_KEY, None)
        )
        if sample_rate is not None and sample_rate != 1:
            otlp_attributes[ATTRIBUTES_SAMPLE_RATE_KEY] = sample_rate

        exit_stacklevel = stacklevel + (2 if decorator else 1)
        return LogfireSpan(
            span_name_,
            otlp_attributes,
            self._spans_tracer,
            {
                'format_string': msg_template,
                'kwargs': merged_attributes,
                'stacklevel': exit_stacklevel,
            },
        )

    def _fast_span(self, msg: LiteralString, attributes: otel_types.Attributes) -> FastLogfireSpan:
        """A simple version of `_span` optimized for auto-tracing that doesn't support message formatting.

        Returns a similarly simplified version of `LogfireSpan` which must immediately be used as a context manager.
        """
        span = self._spans_tracer.start_span(name=msg, attributes=attributes)
        return FastLogfireSpan(span)

    def _fast_span_attributes(
        self, filename: str, module_name: str, function_name: str, lineno: int
    ) -> tuple[str, dict[str, otel_types.AttributeValue]]:
        stack_info: StackInfo = {
            **get_filepath_attribute(filename),
            'code.lineno': lineno,
            'code.function': function_name,
        }
        msg = f'Calling {module_name}.{function_name}'
        attributes: dict[str, otel_types.AttributeValue] = {
            **stack_info,
            ATTRIBUTES_MESSAGE_TEMPLATE_KEY: msg,
            ATTRIBUTES_MESSAGE_KEY: msg,
            ATTRIBUTES_TAGS_KEY: tuple(uniquify_sequence(self._tags + ['auto-trace'])),
        }
        if self._sample_rate not in (None, 1):
            attributes[ATTRIBUTES_SAMPLE_RATE_KEY] = self._sample_rate
        return msg, attributes

    def span(
        self,
        msg_template: LiteralString,
        *,
        span_name: str | None = None,
        **attributes: Any,
    ) -> LogfireSpan:
        """Context manager for creating a span.

        ```py
        import logfire

        with logfire.span('This is a span {a=}', a='data'):
            logfire.info('new log 1')
        ```

        Args:
            msg_template: The template for the span message.
            span_name: The span name. If not provided, the `msg_template` will be used.
            attributes: The arguments to format the span message template with.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        return self._span(
            msg_template,
            attributes,
            span_name=span_name,
        )

    def instrument(
        self,
        msg_template: LiteralString | None = None,
        *,
        span_name: str | None = None,
        extract_args: bool | None = None,
    ) -> Callable[[Callable[_PARAMS, _RETURN]], Callable[_PARAMS, _RETURN]]:
        """Decorator for instrumenting a function as a span.

        ```py
        import logfire


        @logfire.instrument('This is a span {a=}')
        def my_function(a: int):
            logfire.info('new log {a=}', a=a)
        ```

        Args:
            msg_template: The template for the span message. If not provided, the span name will be used.
            span_name: The name of the span. If not provided, the function name will be used.
            extract_args: Whether to extract arguments from the function signature and log them as span attributes.
                If not provided, this will be enabled if `msg_template` is provided and contains `{}`.
        """
        if extract_args is None:
            extract_args = bool(msg_template and '{' in msg_template)

        def decorator(func: Callable[_PARAMS, _RETURN]) -> Callable[_PARAMS, _RETURN]:
            nonlocal span_name
            if span_name is None:
                if func.__module__:
                    span_name_ = f'{func.__module__}.{getattr(func, "__qualname__", func.__name__)}'
                else:
                    span_name_ = getattr(func, '__qualname__', func.__name__)
            else:
                span_name_ = span_name

            pos_params = ()
            if extract_args:
                sig = inspect_signature(func)
                pos_params = tuple(n for n, p in sig.parameters.items() if p.kind in _POSITIONAL_PARAMS)

            @wraps(func)
            def _instrument_wrapper(*args: _PARAMS.args, **kwargs: _PARAMS.kwargs) -> _RETURN:
                if extract_args:
                    pos_args = {k: v for k, v in zip(pos_params, args)}
                    extracted_attributes = {**pos_args, **kwargs}
                else:
                    extracted_attributes = {}

                with self._span(msg_template, extracted_attributes, span_name=span_name_, decorator=True):  # type: ignore
                    return func(*args, **kwargs)

            return _instrument_wrapper

        return decorator

    def log(
        self, level: LevelName, msg_template: LiteralString, attributes: dict[str, Any], stack_offset: int = 0
    ) -> None:
        """Log a message.

        ```py
        import logfire

        logfire.log('info', 'This is a log {a}', {'a': 'Apple'})
        ```

        Args:
            level: The level of the log.
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
            stack_offset: The stack level offset to use when collecting stack info, also affects the warning which
                message formatting might emit, defaults to `0` which means the stack info will be collected from the
                position where `logfire.log` was called.
        """
        if level not in LEVEL_NUMBERS:
            warnings.warn('Invalid log level')
            level = 'error'
        level_no = LEVEL_NUMBERS[level]
        stacklevel = stack_offset + 2
        stack_info = get_caller_stack_info(stacklevel)

        merged_attributes = {**stack_info, **attributes}
        tags, merged_attributes = _merge_tags_into_attributes(merged_attributes, self._tags)
        msg = logfire_format(msg_template, merged_attributes, stacklevel=stacklevel + 2)
        otlp_attributes = user_attributes(merged_attributes)
        otlp_attributes = {
            ATTRIBUTES_SPAN_TYPE_KEY: 'log',
            ATTRIBUTES_LOG_LEVEL_NAME_KEY: level,
            ATTRIBUTES_LOG_LEVEL_NUM_KEY: level_no,
            ATTRIBUTES_MESSAGE_TEMPLATE_KEY: msg_template,
            ATTRIBUTES_MESSAGE_KEY: msg,
            **otlp_attributes,
        }
        if attributes_json_schema := logfire_json_schema(attributes):
            otlp_attributes[ATTRIBUTES_JSON_SCHEMA_KEY] = attributes_json_schema

        if tags:
            otlp_attributes[ATTRIBUTES_TAGS_KEY] = tags

        sample_rate = (
            self._sample_rate
            if self._sample_rate is not None
            else otlp_attributes.pop(ATTRIBUTES_SAMPLE_RATE_KEY, None)
        )
        if sample_rate is not None and sample_rate != 1:
            otlp_attributes[ATTRIBUTES_SAMPLE_RATE_KEY] = sample_rate

        start_time = self._config.ns_timestamp_generator()

        span = self._logs_tracer.start_span(
            msg,
            attributes=otlp_attributes,
            start_time=start_time,
        )
        with trace_api.use_span(span, end_on_exit=False, record_exception=False):
            span.set_status(trace_api.Status(trace_api.StatusCode.OK))
            span.end(start_time)

    def trace(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a trace message.

        ```py
        import logfire

        logfire.trace('This is a trace log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('trace', msg_template, attributes, stack_offset=1)

    def debug(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a debug message.

        ```py
        import logfire

        logfire.debug('This is a debug log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('debug', msg_template, attributes, stack_offset=1)

    def info(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log an info message.

        ```py
        import logfire

        logfire.info('This is an info log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('info', msg_template, attributes, stack_offset=1)

    def notice(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a notice message.

        ```py
        import logfire

        logfire.notice('This is a notice log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('notice', msg_template, attributes, stack_offset=1)

    def warn(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a warning message.

        ```py
        import logfire

        logfire.warn('This is a warning log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('warn', msg_template, attributes, stack_offset=1)

    def error(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log an error message.

        ```py
        import logfire

        logfire.error('This is an error log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('error', msg_template, attributes, stack_offset=1)

    def fatal(self, msg_template: LiteralString, /, **attributes: Any) -> None:
        """Log a fatal message.

        ```py
        import logfire

        logfire.fatal('This is a fatal log')
        ```

        Args:
            msg_template: The message to log.
            attributes: The attributes to bind to the log.
        """
        if any(k.startswith('_') for k in attributes):
            raise ValueError('Attribute keys cannot start with an underscore.')
        self.log('fatal', msg_template, attributes, stack_offset=1)

    def force_flush(self, timeout_millis: int = 3_000) -> bool:
        """Force flush all spans.

        Args:
            timeout_millis: The timeout in milliseconds.

        Returns:
            Whether the flush was successful.
        """
        return self._tracer_provider.force_flush(timeout_millis)

    def log_slow_async_callbacks(self, slow_duration: float = 0.1) -> ContextManager[None]:
        """Log a warning whenever a function running in the asyncio event loop blocks for too long.

        This works by patching the `asyncio.events.Handle._run` method.

        Args:
            slow_duration: the threshold in seconds for when a callback is considered slow.

        Returns:
            A context manager that will revert the patch when exited.
                This context manager doesn't take into account threads or other concurrency.
                Calling this method will immediately apply the patch
                without waiting for the context manager to be opened,
                i.e. it's not necessary to use this as a context manager.
        """
        return _async.log_slow_callbacks(self, slow_duration)

    def install_auto_tracing(self, modules: Sequence[str] | Callable[[AutoTraceModule], bool] | None = None) -> None:
        """Install automatic tracing.

        This will trace all function calls in the modules specified by the modules argument.
        It's equivalent to wrapping the body of every function in matching modules in `with logfire.span(...):`.

        !!! note
            This function MUST be called before any of the modules are imported.

        This works by inserting a new meta path finder into `sys.meta_path`, so inserting another finder before it
        may prevent it from working.

        It relies on being able to retrieve the source code via at least one other existing finder in the meta path,
        so it may not work if standard finders are not present or if the source code is not available.
        A modified version of the source code is then compiled and executed in place of the original module.

        Args:
            modules: List of module names to trace, or a function which returns True for modules that should be traced.
                If a list is provided, any submodules within a given module will also be traced.

                Defaults to the root of the calling module, so e.g. calling this inside the module `foo.bar`
                will trace all functions in `foo`, `foo.bar`, `foo.spam`, etc.
        """
        install_auto_tracing(self, modules)

    def instrument_fastapi(
        self,
        app: FastAPI,
        *,
        attributes_mapper: Callable[
            [
                Request | WebSocket,
                dict[str, Any],
            ],
            dict[str, Any] | None,
        ]
        | None = None,
        use_opentelemetry_instrumentation: bool = True,
    ) -> ContextManager[None]:
        """Instrument a FastAPI app so that spans and logs are automatically created for each request.

        Args:
            app: The FastAPI app to instrument.
            attributes_mapper: A function that takes a [`Request`][fastapi.Request] or [`WebSocket`][fastapi.WebSocket]
                and a dictionary of attributes and returns a new dictionary of attributes.
                The input dictionary will contain:

                - `values`: A dictionary mapping argument names of the endpoint function to parsed and validated values.
                - `errors`: A list of validation errors for any invalid inputs.

                The returned dictionary will be used as the attributes for a log message.
                If `None` is returned, no log message will be created.

                You can use this to e.g. only log validation errors, or nothing at all.
                You can also add custom attributes.

                The default implementation will return the input dictionary unchanged.
                The function mustn't modify the contents of `values` or `errors`.
            use_opentelemetry_instrumentation: If True (the default) then
                [`FastAPIInstrumentor`][opentelemetry.instrumentation.fastapi.FastAPIInstrumentor]
                will also instrument the app.

                See [OpenTelemetry FastAPI Instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/fastapi/fastapi.html).

        Returns:
            A context manager that will revert the instrumentation when exited.
                This context manager doesn't take into account threads or other concurrency.
                Calling this method will immediately apply the instrumentation
                without waiting for the context manager to be opened,
                i.e. it's not necessary to use this as a context manager.
        """
        from .integrations._fastapi import instrument_fastapi

        return instrument_fastapi(
            self,
            app,
            attributes_mapper=attributes_mapper,
            use_opentelemetry_instrumentation=use_opentelemetry_instrumentation,
        )


class FastLogfireSpan:
    """A simple version of `LogfireSpan` optimized for auto-tracing."""

    __slots__ = ('_span', '_token')

    def __init__(self, span: trace_api.Span) -> None:
        self._span = span
        self._token = context_api.attach(trace_api.set_span_in_context(self._span))

    def __enter__(self) -> FastLogfireSpan:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: Any) -> None:
        context_api.detach(self._token)
        _exit_span(self._span, exc_type, exc_value, traceback)
        self._span.end()


# Changes to this class may need to be reflected in `FastLogfireSpan` as well.
class LogfireSpan(ReadableSpan):
    def __init__(
        self,
        span_name: str,
        otlp_attributes: dict[str, otel_types.AttributeValue],
        tracer: Tracer,
        format_args: _FormatArgs,
    ) -> None:
        self._span_name = span_name
        self._otlp_attributes = otlp_attributes
        self._tracer = tracer
        self._end_on_exit: bool | None = None
        self._token: None | object = None
        self._format_args = format_args
        self._span: None | trace_api.Span = None
        self.end_on_exit = True

    if not TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any:
            return getattr(self._span, name)

    def __enter__(self) -> LogfireSpan:
        self.end_on_exit = True
        if self._span is None:
            self._span = self._tracer.start_span(
                name=self._span_name,
                attributes=self._otlp_attributes,
            )
        if self._token is None:
            self._token = context_api.attach(trace_api.set_span_in_context(self._span))
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: Any) -> None:
        if self._token is None:
            return

        context_api.detach(self._token)
        self._token = None

        assert self._span is not None
        _exit_span(self._span, exc_type, exc_value, traceback)

        # We allow attributes to be set while the span is active, so we need to
        # reformat the message in case any new attributes were added.
        format_args = self._format_args
        log_message = logfire_format(
            format_string=format_args['format_string'],
            kwargs={'span_name': self._span_name, **format_args['kwargs']},
            stacklevel=format_args['stacklevel'],
        )
        self._span.set_attribute(ATTRIBUTES_MESSAGE_KEY, log_message)

        end_on_exit_ = self.end_on_exit
        if end_on_exit_:
            self._span.end()

        self._token = None

    @property
    def message_template(self) -> str | None:
        attributes = getattr(self._span, 'attributes')
        if not attributes:
            return None
        if ATTRIBUTES_MESSAGE_TEMPLATE_KEY not in attributes:
            return None
        return str(attributes[ATTRIBUTES_MESSAGE_TEMPLATE_KEY])

    @property
    def tags(self) -> Sequence[str]:
        attributes = getattr(self._span, 'attributes')
        if not attributes:
            return []
        if ATTRIBUTES_TAGS_KEY not in attributes:
            return []
        return cast(Sequence[str], attributes[ATTRIBUTES_TAGS_KEY])

    def end(self) -> None:
        """Sets the current time as the span's end time.

        The span's end time is the wall time at which the operation finished.

        Only the first call to this method is recorded, further calls are ignored so you
        can call this within the span's context manager to end it before the context manager
        exits.
        """
        if self._span is None:
            raise RuntimeError('Span has not been started')
        if self._span.is_recording():
            self._span.end()

    def set_attribute(self, key: str, value: otel_types.AttributeValue) -> None:
        """Sets an attribute on the span.

        Args:
            key: The key of the attribute.
            value: The value of the attribute.
        """
        if self._span is None:
            self._otlp_attributes[key] = value
        else:
            self._span.set_attribute(key, value)
        self._format_args['kwargs'][key] = value


OK_STATUS = trace_api.Status(status_code=trace_api.StatusCode.OK)


def _exit_span(
    span: trace_api.Span, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: Any
) -> None:
    if not span.is_recording():
        return

    # record exception if present
    # isinstance is to ignore BaseException
    if exc_type is not None and isinstance(exc_value, Exception):
        # stolen from OTEL's codebase
        span.set_status(
            trace_api.Status(
                status_code=trace_api.StatusCode.ERROR,
                description=f'{exc_type.__name__}: {exc_value}',
            )
        )
        # insert a more detailed breakdown of pydantic errors
        tb = rich.traceback.Traceback.from_exception(exc_type, exc_value, traceback)
        tb.trace.stacks = [_filter_frames(stack) for stack in tb.trace.stacks]
        attributes: dict[str, otel_types.AttributeValue] = {
            'exception.logfire.trace': json_dumps_traceback(tb.trace),
        }
        if ValidationError is not None and isinstance(exc_value, ValidationError):
            err_json = exc_value.json(include_url=False)
            span.set_attribute(ATTRIBUTES_VALIDATION_ERROR_KEY, exc_value.json(include_url=False))
            attributes[ATTRIBUTES_VALIDATION_ERROR_KEY] = err_json
        span.record_exception(exc_value, attributes=attributes, escaped=True)
    else:
        span.set_status(OK_STATUS)


class _FormatArgs(TypedDict):
    format_string: LiteralString
    kwargs: dict[str, Any]
    stacklevel: int


AttributesValueType = TypeVar('AttributesValueType', bound=Union[Any, otel_types.AttributeValue])


def _merge_tags_into_attributes(
    attributes: dict[str, Any], tags: list[str]
) -> tuple[Sequence[str] | None, dict[str, Any]]:
    # merge tags into attributes preserving any existing tags
    if ATTRIBUTES_TAGS_KEY in attributes:
        res, attributes = (
            cast('list[str]', attributes[ATTRIBUTES_TAGS_KEY]) + tags,
            {k: v for k, v in attributes.items() if k != ATTRIBUTES_TAGS_KEY},
        )
    else:
        res = tags
    if res:
        return uniquify_sequence(res), attributes
    return None, attributes


def user_attributes(attributes: dict[str, Any]) -> dict[str, otel_types.AttributeValue]:
    """Prepare attributes for sending to OpenTelemetry.

    This will convert any non-OpenTelemetry compatible types to JSON.
    """
    prepared: dict[str, otel_types.AttributeValue] = {}
    null_args: list[str] = []

    for key, value in attributes.items():
        if value is None:
            null_args.append(key)
        elif isinstance(value, int):
            if value > OTLP_MAX_INT_SIZE:
                warnings.warn(
                    f'Integer value {value} is larger than the maximum OTLP integer size of {OTLP_MAX_INT_SIZE} (64-bits), '
                    ' if you need support for sending larger integers, please open a feature request',
                    UserWarning,
                )
                prepared[key] = str(value)
            else:
                prepared[key] = value
        elif isinstance(value, (str, bool, float)):
            prepared[key] = value
        else:
            prepared[key] = logfire_json_dumps(value)

    if null_args:
        prepared[NULL_ARGS_KEY] = tuple(null_args)

    return prepared


def _filter_frames(stack: rich.traceback.Stack) -> rich.traceback.Stack:
    """Filter out the `record_exception` call itself."""
    stack.frames = [f for f in stack.frames if not (f.filename.endswith('logfire/_main.py') and f.name.startswith('_'))]
    return stack


_RETURN = TypeVar('_RETURN')
_PARAMS = ParamSpec('_PARAMS')
_POSITIONAL_PARAMS = {SignatureParameter.POSITIONAL_ONLY, SignatureParameter.POSITIONAL_OR_KEYWORD}

T = TypeVar('T')


def uniquify_sequence(seq: Sequence[T]) -> list[T]:
    """Remove duplicates from a sequence preserving order."""
    seen: set[T] = set()
    seen_add = seen.add
    return [x for x in seq if not (x in seen or seen_add(x))]
