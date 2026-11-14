"""Keep the /validate token out of uvicorn's request log.

GET /validate takes the token in the query string, and uvicorn logs requests with
the path and its query string: HTTP requests on the ``uvicorn.access`` logger, and
WebSocket handshakes (which this app refuses with 403) on ``uvicorn.error``. A
filter on both loggers replaces the query of /validate with ``[redacted]``; the line
itself is kept. This is not the database access log in ``app.logger``.
"""
import logging

_REDACTED = "[redacted]"
_LOGGERS = ("uvicorn.access", "uvicorn.error")


def _redact(value: object) -> object:
    if isinstance(value, str):
        path, sep, _query = value.partition("?")
        if sep and path.rstrip("/").endswith("/validate"):
            return f"{path}?{_REDACTED}"
    return value


class RedactValidateQuery(logging.Filter):
    """Replace the query string of /validate in uvicorn's log arguments.

    uvicorn passes the path with its query string as one argument, for example
    ``'%s - "%s %s HTTP/%s" %d'`` (client, method, path, version, status) and
    ``'%s - "WebSocket %s" 403'`` (client, path). Matching is on the path only, for
    every method and status. Trailing slashes are ignored, because ``/validate/``
    and ``/validate//`` are redirected to ``/validate`` and logged with their query
    too. Other arguments and records are left as they are.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redact(arg) for arg in record.args)
        return True


def install() -> None:
    """Attach the filter to uvicorn's loggers, once each.

    Logging configuration (uvicorn's own, or a ``--log-config`` file) replaces a
    logger's handlers but keeps its filters, so the order does not matter.
    """
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if not any(isinstance(f, RedactValidateQuery) for f in logger.filters):
            logger.addFilter(RedactValidateQuery())
