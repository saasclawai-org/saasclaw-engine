#!/usr/bin/env python3
"""Test runner for CI environments.

Installs the engine package in development mode, then runs pytest.
"""

import os
import subprocess
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    # Install the engine package (editable mode so imports work)
    # This pulls django, psycopg, httpx, redis, sunglasses from dependencies
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--break-system-packages"],
        cwd=ENGINE_DIR,
    )

    # Install test-only dependencies
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "pytest", "pytest-django", "pytest-timeout",
         "--break-system-packages"],
    )

    # Run pytest
    result = subprocess.call(
        [sys.executable, "-m", "pytest", "saasclaw_engine", "-v", "--tb=short"],
        cwd=ENGINE_DIR,
    )
    sys.exit(result)


if __name__ == "__main__":
    main()
