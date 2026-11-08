"""Optional pytest adapter for the stdlib-only isolation layer.

The suite is run and verified with the standard library
(``python -m unittest discover -s tests``).  This file exists only so a
developer who already has pytest installed gets the same per-test isolation
from ``tests/isolation.py``; importing it never happens under unittest, and
pytest is not a dependency of the project or of any evidence run.

The shared constants and context manager live in ``tests/isolation.py``.  Keep
this module thin: adding pytest-only logic here would re-create the P3.4a
defect where ``test_p34a_isolation.py`` imported ``tests.conftest`` and failed
on a system Python without pytest.
"""

import pytest

from tests import isolation

HOME_AT_IMPORT = isolation.HOME_AT_IMPORT
DIR_ENV_VARS = isolation.DIR_ENV_VARS
FILE_ENV_VARS = isolation.FILE_ENV_VARS
CONFIG_ENV_VARS = isolation.CONFIG_ENV_VARS
CLEARED_ENV_VARS = isolation.CLEARED_ENV_VARS
ISOLATED_ENV_VARS = isolation.ISOLATED_ENV_VARS
case_env = isolation.case_env
isolated_case = isolation.isolated_case


@pytest.fixture(autouse=True)
def _isolated_case():
    """Autouse: one fresh case dir per test; removed afterwards."""
    with isolation.isolated_case() as case:
        yield case
