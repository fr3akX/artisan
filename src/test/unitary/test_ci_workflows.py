from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
PYTEST_WORKFLOW_PATH = REPOSITORY_ROOT / '.github' / 'workflows' / 'pytest.yaml'
MYPY_WORKFLOW_PATH = REPOSITORY_ROOT / '.github' / 'workflows' / 'mypy.yml'
OUTBOX_TEST_PATH = (
    REPOSITORY_ROOT / 'src/test/unitary/artisanlib/roastserver/test_outbox.py'
)


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


def test_outbox_large_content_parameters_have_concise_ids() -> None:
    module = ast.parse(OUTBOX_TEST_PATH.read_text(encoding='utf-8'))
    content_parameterizations = [
        decorator
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Attribute)
        and decorator.func.attr == 'parametrize'
        and decorator.args
        and isinstance(decorator.args[0], ast.Constant)
        and decorator.args[0].value == 'content'
    ]

    assert len(content_parameterizations) == 2
    for parameterization in content_parameterizations:
        ids = next(
            (keyword.value for keyword in parameterization.keywords if keyword.arg == 'ids'),
            None,
        )
        assert isinstance(ids, (ast.List, ast.Tuple))
        assert len(ids.elts) == 2
        assert all(
            isinstance(element, ast.Constant)
            and isinstance(element.value, str)
            and len(element.value) <= 32
            for element in ids.elts
        )


def test_mypy_workflow_checks_production_sources() -> None:
    workflow = _workflow(MYPY_WORKFLOW_PATH)

    steps = workflow['jobs']['mypy']['steps']
    mypy_step = next(step for step in steps if step.get('uses') == 'tsuyoshicho/action-mypy@v5')
    assert mypy_step['with']['workdir'] == 'src'
    assert mypy_step['with']['target'] == '*.py artisanlib plus'
    assert mypy_step['with']['install_types'] == 'false'
