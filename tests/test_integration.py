"""Run against a real HTTP service and PostgreSQL, never a mocked datastore."""
import asyncio
import os

from scripts.burst import run


def test_concurrency_and_lifecycle():
    asyncio.run(run(os.getenv('BASE_URL', 'http://localhost:8000'),
                    os.environ['ADMIN_TOKEN'], requests=500, concurrency=100))
