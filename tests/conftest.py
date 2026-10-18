import os
import sys
import uuid
import tempfile
import asyncio
from pathlib import Path
import pytest

# Configure disposable test database path
temp_dir = Path(tempfile.gettempdir())
test_db_filename = f"newtonite_test_{uuid.uuid4().hex[:8]}.db"
test_db_path = temp_dir / test_db_filename

# If DATABASE_URL is unset, points to default dev db, or explicitly requested for test isolation:
dev_db_match = "newtonite_ops.db" in os.environ.get("DATABASE_URL", "")
if "DATABASE_URL" not in os.environ or dev_db_match:
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{test_db_path.as_posix()}"

from app.core.config import settings
# Ensure settings also reflects the test database URL
settings.DATABASE_URL = os.environ["DATABASE_URL"]

from app.core.database import engine, Base, AsyncSessionLocal
from app.scripts.seed import seed_data

@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """
    Session-level test fixture providing complete test isolation:
    1. Initializes schema and seeds test data on a disposable database.
    2. Runs all tests safely without touching or modifying the developer database.
    3. Safely disposes connections and unlinks the temporary database on teardown.
    """
    # 1. Initialize schema and seed data
    asyncio.run(seed_data(item_count=1000))

    yield

    # 2. Cleanup: dispose connections and remove temporary database
    asyncio.run(engine.dispose())
    if test_db_path.exists():
        try:
            test_db_path.unlink()
        except Exception:
            pass
