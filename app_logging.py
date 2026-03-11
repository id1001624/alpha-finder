from __future__ import annotations

import builtins
import inspect
import logging
import os
import sys


_CONFIGURED = False
_PRINT_PROXY_INSTALLED = False
_ORIGINAL_PRINT = builtins.print


def configure_logging(level: str | None = None) -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    raw_level = str(level or os.getenv("ALPHA_FINDER_LOG_LEVEL") or os.getenv("LOG_LEVEL") or "INFO").upper()
    resolved_level = getattr(logging, raw_level, logging.INFO)
    logging.basicConfig(
        level=resolved_level,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        stream=sys.stdout,
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)


def _infer_level(message: str) -> int:
    lowered = str(message or "").lower()
    if "[x]" in lowered or "error" in lowered or "failed" in lowered or "missing" in lowered:
        return logging.ERROR
    if "[!]" in lowered or "warning" in lowered or "skip" in lowered:
        return logging.WARNING
    return logging.INFO


def build_print_logger(name: str):
    logger = get_logger(name)

    def _log_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
        if file not in (None, sys.stdout, sys.stderr):
            builtins.print(*args, sep=sep, end=end, file=file, flush=flush)
            return

        message = sep.join(str(arg) for arg in args)
        if end not in ("", "\n"):
            message = f"{message}{end.rstrip()}"
        logger.log(_infer_level(message), message.rstrip())

        if flush:
            for handler in logging.getLogger().handlers:
                handler.flush()

    return _log_print


def install_print_logging(namespace: dict, name: str) -> None:
    namespace["print"] = build_print_logger(name)


def build_builtin_print_proxy():
    def _log_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
        if file not in (None, sys.stdout, sys.stderr):
            _ORIGINAL_PRINT(*args, sep=sep, end=end, file=file, flush=flush)
            return

        caller_frame = inspect.currentframe().f_back
        caller_name = str(caller_frame.f_globals.get("__name__", __name__)) if caller_frame is not None else __name__
        logger = get_logger(caller_name)
        message = sep.join(str(arg) for arg in args)
        if end not in ("", "\n"):
            message = f"{message}{end.rstrip()}"
        logger.log(_infer_level(message), message.rstrip())

        if flush:
            for handler in logging.getLogger().handlers:
                handler.flush()

    return _log_print


def install_builtin_print_logging() -> None:
    global _PRINT_PROXY_INSTALLED
    if _PRINT_PROXY_INSTALLED:
        return
    builtins.print = build_builtin_print_proxy()
    _PRINT_PROXY_INSTALLED = True