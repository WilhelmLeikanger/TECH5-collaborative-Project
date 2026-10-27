"""Configuration settings for the TECH5 Decision Support System."""

import os
from pathlib import Path


class Config:
    """Base application configuration class."""

    SECRET_KEY: str = os.environ.get(
        "SECRET_KEY", "tech5-decision-support-secret-key-2026"
    )
    BASE_DIR: Path = Path(__file__).resolve().parent.parent
    DATA_DIR: Path = BASE_DIR / "instance"
    DATABASE_PATH: Path = DATA_DIR / "tech5_assignment.db"
    API_VERSION: str = "v1"
    DEFAULT_MAX_SOLUTIONS: int = 3
    SOLVER_TIME_LIMIT_SECONDS: int = 15


class TestingConfig(Config):
    """Testing configuration with dedicated test database."""

    TESTING: bool = True
    DATABASE_PATH: Path = Config.DATA_DIR / "tech5_test.db"
