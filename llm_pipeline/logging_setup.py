import logging
import sys
import time

LOG_FORMAT = "%(asctime)s - %(levelname)-5s - %(name)-10.10s - %(message)s - (%(filename)s:%(lineno)s)"


def configure_logging(level: str):
    logging.Formatter.converter = time.gmtime
    formatter = logging.Formatter(LOG_FORMAT)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(level.upper())
