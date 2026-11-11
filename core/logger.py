import logging
import copy
from pathlib import Path
from rich.logging import RichHandler
from rich.text import Text

ENABLE_FILE_LOGGING = True
LOG_FILENAME = Path(__file__).resolve().parent.parent / "application.log"

class DeMarkupFormatter(logging.Formatter):
    """Strips Rich markup (e.g. [bold red]) from messages written to the log file.

    Formats a copy of the record so the console handler still sees the markup.
    """
    def format(self, record):
        record_copy = copy.copy(record)
        if isinstance(record_copy.msg, str):
            record_copy.msg = Text.from_markup(record_copy.msg).plain
        return super().format(record_copy)

def setup_logger(name="appliance_utilization"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    # Prevent adding multiple handlers if the module is re-imported
    if not logger.handlers:
        console_handler = RichHandler(rich_tracebacks=True, markup=True)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(console_handler)

        if ENABLE_FILE_LOGGING:
            file_handler = logging.FileHandler(LOG_FILENAME, mode='a', encoding='utf-8')
            file_handler.setLevel(logging.INFO)
            fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            file_handler.setFormatter(DeMarkupFormatter(fmt))
            logger.addHandler(file_handler)

    return logger

logger = setup_logger()
