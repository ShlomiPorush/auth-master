"""Keep the /validate token out of uvicorn's request log.

GET /validate takes the token in the query string, and uvicorn writes every request
to the ``uvicorn.access`` logger with the path and its query string. A filter on that
logger replaces the query of /validate requests with ``[redacted]``; the line itself
is kept. This is not the database access log in ``app.logger``.
"""
import logging

_REDACTED = "[redacted]"


class RedactValidateQuery(logging.Filter):
    """Replace the query string of /validate in uvicorn's request log lines.

    uvicorn logs ``'%s - "%s %s HTTP/%s" %d'`` with the arguments (client address,
    method, path with query string, HTTP version, status). Matching is on the path
    only, for every method and status. Trailing slashes are ignored, because
    ``/validate/`` and ``/validate//`` are redirected to ``/validate`` and logged
    with their query too. Records of any other shape are left as they are.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, sep, _query = args[2].partition("?")
            if sep and path.rstrip("/").endswith("/validate"):
                record.args = (*args[:2], f"{path}?{_REDACTED}", *args[3:])
        return True


def install() -> None:
    """Attach the filter to uvicorn's request logger, once.

    Logging configuration (uvicorn's own, or a ``--log-config`` file) replaces the
    logger's handlers but keeps its filters, so the order does not matter.
    """
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, RedactValidateQuery) for f in access_logger.filters):
        access_logger.addFilter(RedactValidateQuery())
