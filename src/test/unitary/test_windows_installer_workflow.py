from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPOSITORY_ROOT / '.github' / 'workflows' / 'windows-installer.yml'


def _workflow() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.load(WORKFLOW_PATH.read_text(encoding='utf-8'), Loader=yaml.BaseLoader),
    )


def test_windows_installer_workflow_is_pr_only_and_unsigned() -> None:
    workflow = _workflow()

    assert workflow['on'] == {'pull_request': {'branches': ['master']}}
    assert workflow['permissions'] == {'contents': 'read'}
    assert 'ARTISAN_KEY' not in workflow.get('env', {})

    job = workflow['jobs']['windows-installer']
    assert job['runs-on'] == 'windows-2022'
    assert job['timeout-minutes'] == '90'
    assert job['env']['BUILD_PYINSTALLER'] == 'True'
    assert job['env']['PYQT'] == '6'
    assert job['env']['PYUIC'] == 'pyuic6.exe'

    steps = job['steps']
    actions = {step['uses']: step for step in steps if 'uses' in step}
    assert 'actions/checkout@v6' in actions
    assert actions['actions/setup-python@v6']['with']['python-version'] == '3.14.6'
    artifact = actions['actions/upload-artifact@v4']['with']
    assert artifact['path'] == 'src/artisan-win*setup.exe'
    assert artifact['if-no-files-found'] == 'error'
    assert artifact['retention-days'] == '7'
    assert artifact['compression-level'] == '0'

    steps_by_name = {step['name']: step for step in steps}
    install = steps_by_name['Install Windows build dependencies']
    build = steps_by_name['Build Windows installer']
    signature = steps_by_name['Set build revision and unsigned PR signature']
    assert install['env'] == {'APPVEYOR': 'True'}
    assert build['env'] == {'APPVEYOR': 'True'}
    assert 'APPVEYOR' not in signature.get('env', {})
    assert 'python generate_signature.py' in signature['run']
    assert 'build-win3-pi.bat' in build['run']


def test_windows_pyinstaller_spec_uses_github_checkout_directory() -> None:
    spec = (REPOSITORY_ROOT / 'src' / 'artisan-win.spec').read_text(encoding='utf-8')

    github_condition = spec.index("if os.environ.get('GITHUB_ACTIONS')")
    appveyor_condition = spec.index("elif os.environ.get('APPVEYOR')")
    dynamic_source = spec.index('ARTISAN_SRC = os.getcwd()')
    assert github_condition < dynamic_source < appveyor_condition
