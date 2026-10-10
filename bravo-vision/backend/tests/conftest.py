import os

from cryptography.fernet import Fernet

os.environ.setdefault("BV_ENCRYPTION_KEYS", Fernet.generate_key().decode())
os.environ["BV_ENV"] = "test"
os.environ["BV_DATABASE_URL"] = "sqlite://"

import pytest  # noqa: E402

from bravo import db  # noqa: E402

# Run the whole suite against PostgreSQL with: BV_TEST_DATABASE_URL=postgresql+psycopg2://... pytest
_TEST_URL = os.environ.get("BV_TEST_DATABASE_URL")
if _TEST_URL:
    _orig_init = db.init_engine
    db.init_engine = lambda url=None: _orig_init(_TEST_URL if url in (None, "sqlite://") else url)
import bravo.services.outbound  # noqa: E402,F401  (registers the real approval handlers)
import bravo.services.voice  # noqa: E402,F401


@pytest.fixture
def session():
    db.init_engine("sqlite://")
    db.create_all()
    s = db.session_factory()()
    yield s
    s.close()
    db.Base.metadata.drop_all(db.get_engine())


@pytest.fixture(autouse=True)
def _isolate_registries():
    from bravo import approvals
    from bravo.integrations import registry

    handlers, hooks = dict(approvals._handlers), dict(approvals._reject_hooks)
    yield
    approvals._handlers.clear()
    approvals._handlers.update(handlers)
    approvals._reject_hooks.clear()
    approvals._reject_hooks.update(hooks)
    registry.reset()
