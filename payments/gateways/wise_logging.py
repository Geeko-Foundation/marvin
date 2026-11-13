"""Dual file + console logging for Wise API operations."""

import logging

LOGGER_NAME = 'marvin.wise'


def get_wise_logger() -> logging.Logger:
    """Return the shared logger used for Wise API request and step logging."""
    return logging.getLogger(LOGGER_NAME)


def log_wise_step(step: str, message: str, **context) -> None:
    """Log a verification step to file and console."""
    logger = get_wise_logger()
    if context:
        ctx = ' '.join(f'{key}={value}' for key, value in context.items())
        logger.info('[%s] %s (%s)', step, message, ctx)
    else:
        logger.info('[%s] %s', step, message)


def log_wise_request(method: str, path: str, status_code: int, *, mode: str = 'live') -> None:
    """Log one HTTP round-trip to the Wise API (method, path, status, sandbox/live)."""
    get_wise_logger().info(
        'HTTP %s %s -> %s (%s)',
        method,
        path,
        status_code,
        mode,
    )


def log_wise_error(step: str, message: str, **context) -> None:
    """Log a Wise verification or API failure at ERROR level."""
    logger = get_wise_logger()
    if context:
        ctx = ' '.join(f'{key}={value}' for key, value in context.items())
        logger.error('[%s] %s (%s)', step, message, ctx)
    else:
        logger.error('[%s] %s', step, message)


def record_wise_call(method, path, request_body, response, *, mode, started, error=''):
    """
    Log one call to Wise to the file and to the Wise log table.

    Recording must never be why a payment fails, so a problem writing the
    row is logged and swallowed.
    """
    import time

    status = response.status_code if response is not None else None
    log_wise_request(method, path, status if status is not None else 0, mode=mode)
    try:
        from payments.models import WiseLogEntry, masked, transfer_id_in

        if response is None:
            body = {'error': error}
        else:
            try:
                body = response.json()
            except ValueError:
                body = {'text': response.text[:5000]}
        WiseLogEntry.objects.create(
            kind=WiseLogEntry.API, mode=mode, method=method, path=path[:300], status_code=status,
            duration_ms=int((time.monotonic() - started) * 1000),
            request_body=masked(request_body), response_body=masked(body),
            transfer_id=transfer_id_in(path, body),
        )
    except Exception:  # pragma: no cover - logging must not break a payment
        get_wise_logger().exception('Could not write the Wise log entry for %s %s', method, path)
