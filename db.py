import os
import logging

logger = logging.getLogger("db")

def get_db_connection():
    """Mock database connection helper with safe fallback."""
    db_url = os.getenv("DATABASE_URL", "")
    if not db_url:
        logger.info("DATABASE_URL not configured. Running in stateless mode.")
        return None
    logger.info("Database URL detected.")
    return None
