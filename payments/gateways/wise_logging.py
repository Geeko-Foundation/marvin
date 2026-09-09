"""Dual file + console logging for Wise API operations."""

import logging

LOGGER_NAME = 'marvin.wise'


def get_wise_logger() -> logging.Logger:
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
    get_wise_logger().info(
        'HTTP %s %s -> %s (%s)',
        method,
        path,
        status_code,
        mode,
    )


def log_wise_error(step: str, message: str, **context) -> None:
    logger = get_wise_logger()
    if context:
        ctx = ' '.join(f'{key}={value}' for key, value in context.items())
        logger.error('[%s] %s (%s)', step, message, ctx)
    else:
        logger.error('[%s] %s', step, message)
