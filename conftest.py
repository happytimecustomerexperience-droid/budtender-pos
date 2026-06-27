"""Hermetic test env — set BEFORE settings/.env load. BUDTENDER_TESTING makes
settings + stores skip the real .env so a local .env can't leak into tests."""

import os

os.environ["BUDTENDER_TESTING"] = "1"
os.environ.setdefault("BUDTENDER_DEBUG", "1")        # no SSL-redirect during tests
os.environ.setdefault("BUDTENDER_SECRET_KEY", "test-insecure-secret")
os.environ.setdefault("BUDTENDER_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver")
