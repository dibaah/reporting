"""Run the tests against a separate database when TEST_DATABASE_URL is set.

Once real reports are in the main database, create report_intel_test (see README) and set
TEST_DATABASE_URL so tests can never touch real data or real alert state."""
import os

if os.environ.get("TEST_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
