from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PYTEST_WORKFLOW_PATH = REPOSITORY_ROOT / '.github' / 'workflows' / 'pytest.yaml'
MYPY_WORKFLOW_PATH = REPOSITORY_ROOT / '.github' / 'workflows' / 'mypy.yml'


def _workflow(path: Path) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.load(path.read_text(encoding='utf-8'), Loader=yaml.BaseLoader),
    )


def test_pytest_workflow_runs_tests_from_source_directory() -> None:
    workflow = _workflow(PYTEST_WORKFLOW_PATH)

    steps = workflow['jobs']['pytest']['steps']
    test_step = next(step for step in steps if step.get('name') == 'Test with pytest')
    assert test_step['working-directory'] == 'src'
    assert test_step['run'].strip() == 'pytest'


def test_mypy_workflow_checks_production_sources() -> None:
    workflow = _workflow(MYPY_WORKFLOW_PATH)

    steps = workflow['jobs']['mypy']['steps']
    mypy_step = next(step for step in steps if step.get('uses') == 'tsuyoshicho/action-mypy@v5')
    assert mypy_step['with']['workdir'] == 'src'
    assert mypy_step['with']['target'] == '*.py artisanlib plus'
