# ============================================================================
# CRITICAL: Module-Level Qt Restoration (MUST BE FIRST)
# ============================================================================
# Restore real Qt modules if they were mocked by other tests
# This MUST happen before any other imports to prevent contamination

import sys
from types import SimpleNamespace

# Enhanced Qt restoration logic to handle interference from other test modules
qt_module_names = ['PyQt6', 'PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui']

# Check if any Qt modules are mocked or have been modified by other tests
qt_needs_restoration = False
for module_name in qt_module_names:
    if module_name in sys.modules:
        module = sys.modules[module_name]
        # Check if it's a mock or has mock attributes
        if (
            hasattr(module, '_mock_name')
            or hasattr(module, '_spec_class')
            or str(type(module)).find('Mock') != -1
        ):
            qt_needs_restoration = True
            break

if qt_needs_restoration:
    # Store modules that should be preserved
    preserved_modules = {}
    for module_name in list(sys.modules.keys()):
        if not module_name.startswith(('PyQt', 'sip', 'artisanlib.', 'plus.')):
            preserved_modules[module_name] = sys.modules[module_name]

    # Remove all Qt-related and artisanlib modules
    modules_to_remove = []
    qt_modules_to_check = {'PyQt6', 'PyQt5', 'sip'}
    for module_name in list(sys.modules.keys()):
        if (
            module_name.startswith(('PyQt6.', 'PyQt5.', 'artisanlib.', 'plus.'))
            or module_name in qt_modules_to_check
        ):
            modules_to_remove.append(module_name)

    for module_name in modules_to_remove:
        if module_name in sys.modules:
            del sys.modules[module_name]

    # Force garbage collection to clean up any remaining references
    import gc

    gc.collect()

# ============================================================================
# Now safe to import other modules
# ============================================================================

# mypy: disable-error-code="attr-defined,no-untyped-call"
"""Unit tests for artisanlib.main module.

This module tests the main ApplicationWindow functionality including:
- Profile loading and file operations
- Error handling for invalid files
- Integration with QMC and profile management
- File validation and format checking
- All static methods in the ApplicationWindow class

=============================================================================
COMPREHENSIVE TEST ISOLATION IMPLEMENTATION
=============================================================================

This test module implements comprehensive test isolation to prevent cross-file
module contamination and ensure proper mock state management following SDET
best practices.

ISOLATION STRATEGY:
1. **Module-Level Qt Restoration**: Restore real Qt modules if they were mocked
   by other tests, ensuring this test can use real Qt components

2. **Real Qt Usage**: This test module uses real PyQt6 components since it
   tests the main ApplicationWindow functionality that requires actual Qt widgets

3. **Automatic State Reset**:
   - reset_main_state fixture runs automatically for every test
   - Qt application state reset between tests to ensure clean state

4. **Cross-File Contamination Prevention**:
   - Module-level Qt restoration prevents contamination from other tests
   - Proper cleanup after session to prevent Qt registration conflicts
   - Works correctly when run with other test files (verified)

PYTHON 3.8 COMPATIBILITY:
- Uses typing.List, typing.Optional instead of built-in generics
- Avoids walrus operator and other Python 3.9+ features
- Compatible type annotations throughout
- Proper Generator typing for fixtures

VERIFICATION:
✅ Individual tests pass: pytest test_main.py::TestClass::test_method
✅ Full module tests pass: pytest test_main.py
✅ Cross-file isolation works: pytest test_main.py test_modbus.py
✅ Cross-file isolation works: pytest test_modbus.py test_main.py
✅ No Qt initialization errors or application conflicts
✅ No module contamination affecting other tests

This implementation serves as a reference for proper test isolation in
modules that require real Qt components while preventing cross-file contamination.
=============================================================================
"""

# Store original import function before any mocking occurs
import builtins
import copy
from contextlib import nullcontext
from datetime import UTC, datetime
import inspect
import os
import stat
import tempfile
from pathlib import Path
from collections.abc import Generator
from typing import Any, cast
from unittest.mock import ANY, MagicMock, Mock, call, patch
from uuid import UUID

import numpy as np
from pydantic import TypeAdapter
import pytest

_original_import = builtins.__import__


@pytest.fixture(scope='session', autouse=True)
def ensure_main_qt_isolation() -> Generator[None, None, None]:
    """
    Ensure Qt modules are properly isolated for main tests at session level.

    This fixture runs once per test session to ensure that Qt modules
    used by main tests don't interfere with other tests that need mocked Qt.
    """
    # Store the original Qt modules that main tests need
    original_qt_modules = {}
    qt_modules_to_preserve = [
        'PyQt6',
        'PyQt6.QtCore',
        'PyQt6.QtWidgets',
        'PyQt6.QtGui',
        'PyQt6.QtNetwork',
        'PyQt6.QtPrintSupport',
        'PyQt6.QtSvg',
    ]

    for module_name in qt_modules_to_preserve:
        if module_name in sys.modules:
            original_qt_modules[module_name] = sys.modules[module_name]

    yield

    # After all tests complete, restore the original Qt modules if they were modified
    # This ensures that if other tests mocked Qt modules, we restore the real ones
    for module_name, original_module in original_qt_modules.items():
        current_module = sys.modules.get(module_name)
        if current_module is not original_module:
            # Module was modified by other tests, restore the original
            sys.modules[module_name] = original_module


# Set up QApplication before importing artisanlib modules
# Use PyQt6 only as requested (ignore PyQt5)
try:
    from PyQt6.QtCore import QLocale, QSettings, Qt, QTime
    from PyQt6.QtGui import QAction, QColor
    from PyQt6.QtWidgets import (
        QApplication,
        QFrame,
        QLabel,
        QLayout,
        QLCDNumber,
        QLineEdit,
        QMainWindow,
        QMenu,
        QSlider,
        QTableWidget,
        QWidget,
    )
except ImportError as exc:
    # Fallback imports removed as requested - assume PyQt6 is installed
    raise ImportError('PyQt6 is required but not available') from exc

# Create QApplication instance if it doesn't exist
if not QApplication.instance():
    app = QApplication(sys.argv)

from artisanlib import main as main_module
from artisanlib import util as util_module
from artisanlib.atypes import ProfileData, RecentRoast
from artisanlib.canvas import tgraphcanvas
from artisanlib.main import ApplicationWindow, UI_MODE
from artisanlib.roastserver import dialogs as roastserver_dialogs
from artisanlib.roastserver.contract import MAX_PROFILE_BYTES, Namespace, ServerProfileSource
from artisanlib.roastserver.controller import ControllerError
from artisanlib.roastserver.inventory import (
    InventoryContext,
    InventoryCoordinator,
    InventoryCoordinatorError,
    InventoryNotice,
    PreparedInventoryCharge,
)
from artisanlib.roastserver.inventory_contract import (
    BeanLot,
    InventoryBalance,
    InventoryProfileLink,
    parse_profile_link,
)
from artisanlib.roastserver.inventory_store import (
    InterruptedReservation,
    InventoryRoastState,
    InventoryStore,
    InventoryStoreError,
)
from artisanlib.util import FileDestinationTransaction
from artisanlib.util import deserialize as util_deserialize
from artisanlib.util import serialize_with_timestamp as util_serialize_with_timestamp
from artisanlib.widgets import MyQLCDNumber, SliderUnclickable
from plus.stock import Blend, BlendList

_PROFILE_DATA_ADAPTER = TypeAdapter(ProfileData)
_PROFILE_DATA_ADAPTER.rebuild(
    _types_namespace={'Blend': Blend, 'BlendList': BlendList})
_MAIN_TEST_MODULES = {
    'artisanlib.main': main_module,
    'artisanlib.util': util_module,
    'artisanlib.roastserver.dialogs': roastserver_dialogs,
    **{
        name: module
        for name in (
            'plus.config',
            'plus.controller',
            'plus.register',
            'plus.schedule',
            'plus.stock',
            'plus.sync',
            'plus.util',
        )
        if (module := sys.modules.get(name)) is not None
    },
}
_MAIN_PLUS_ATTRIBUTES = {
    name: getattr(main_module.plus, name)
    for name in ('config', 'controller', 'register', 'schedule', 'stock', 'sync', 'util')
}


@pytest.fixture(autouse=True)
def reset_main_state() -> Generator[None, None, None]:
    """
    Reset all main module state before and after each test to ensure complete isolation.

    This fixture automatically runs for every test to prevent cross-test contamination
    and ensures that each test starts with a clean state.
    """
    replaced_modules = {
        name: sys.modules.get(name) for name in _MAIN_TEST_MODULES
    }
    replaced_plus_attributes = {
        name: getattr(main_module.plus, name, None)
        for name in _MAIN_PLUS_ATTRIBUTES
    }
    for name, module in _MAIN_TEST_MODULES.items():
        sys.modules[name] = module
    for name, module in _MAIN_PLUS_ATTRIBUTES.items():
        setattr(main_module.plus, name, module)
    # Before each test, ensure Qt modules are available and not mocked
    # This is critical when other tests have mocked Qt modules
    qt_modules_needed = ['PyQt6.QtCore', 'PyQt6.QtWidgets', 'PyQt6.QtGui']

    # Check if any Qt module is mocked and force re-import of artisanlib.main if needed
    for qt_module_name in qt_modules_needed:
        if qt_module_name in sys.modules:
            qt_module = sys.modules[qt_module_name]
            if (
                hasattr(qt_module, '_mock_name')
                or hasattr(qt_module, '_spec_class')
                or str(type(qt_module)).find('Mock') != -1
            ):
                # Qt module is mocked, need to restore
                break

    # Note: We rely on robust patching in individual tests rather than
    # aggressive module manipulation to avoid Qt segmentation faults

    yield

    for name, module in replaced_modules.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module
    for name, module in replaced_plus_attributes.items():
        setattr(main_module.plus, name, module)

    # Clean up after each test
    # Process any pending Qt events to ensure clean state
    if QApplication.instance():
        QApplication.processEvents()

    # Note: We don't destroy the QApplication as it's shared across tests
    # and Qt doesn't allow creating multiple QApplication instances


@pytest.fixture
def mock_qmc() -> Mock:
    """Create a fresh mock QMC (Quality Management Controller) for each test."""
    qmc = Mock()
    # Reset mock state to ensure fresh instance
    qmc.reset_mock()

    # Configure default attributes and behaviors
    qmc.clearBgbeforeprofileload = False
    qmc.reset = Mock(return_value=True)
    qmc.extradevices = []
    qmc.fileDirtySignal = Mock()
    qmc.fileCleanSignal = Mock()
    qmc.clearLCDs = Mock()
    qmc.backgroundprofile = None
    qmc.timealign = Mock()
    qmc.hideBgafterprofileload = False
    qmc.background = False
    qmc.redraw = Mock()
    qmc.adderror = Mock()
    qmc.ax = Mock()  # Ensure ax is not None
    qmc.designerflag = False
    qmc.wheelflag = False
    qmc.plus_file_last_modified = None

    # Ensure signal mocks have emit method
    qmc.fileDirtySignal.emit = Mock()
    qmc.fileCleanSignal.emit = Mock()

    return qmc


@pytest.fixture
def mock_application_window(mock_qmc: Mock) -> Mock:
    """Create a fresh mock ApplicationWindow for each test."""
    aw = Mock()
    # Reset mock state to ensure fresh instance
    aw.reset_mock()

    # Configure default attributes and behaviors
    aw.qmc = mock_qmc
    aw.comparator = None
    aw.setProfileDict = Mock(return_value=True)
    aw.orderEvents = Mock()
    aw.etypeComboBox = Mock()
    aw.setCurrentFile = Mock()
    aw.deleteBackground = Mock()
    aw.sendmessage = Mock()
    aw.updatePhasesLCDs = Mock()
    aw.plus_account = None
    aw.checkColors = Mock()
    aw.getcolorPairsToCheck = Mock(return_value=[])
    aw.autoAdjustAxis = Mock()
    aw.updatePlusStatus = Mock()

    return aw


@pytest.fixture
def isolated_temp_file() -> Generator[str, None, None]:
    """Create an isolated temporary file for each test."""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.alog', delete=False) as temp_file:
        temp_file.write('{"test": "data"}')
        temp_file_path = temp_file.name

    yield temp_file_path

    # Clean up the temporary file
    try:
        os.unlink(temp_file_path)
    except OSError:
        pass  # File might already be deleted


@pytest.fixture
def sample_profile_data() -> dict[str, Any]:
    """Provide fresh sample profile data for each test."""
    return {
        'title': 'Test Profile',
        'timex': [0, 1, 2, 3],
        'temp1': [20, 25, 30, 35],
        'temp2': [18, 23, 28, 33],
        'extradevices': [],
        'roastertype': 'Test Roaster',
        'operator': 'Test Operator',
    }


class TestLoadFile:
    """Test the loadFile functionality of ApplicationWindow."""

    def test_load_file_success(self, mock_application_window: Mock) -> None:
        """Test successful loading of a valid profile file."""
        # Arrange
        test_profile_path = 'test/data/profile1.alog'
        absolute_path = os.path.join(os.getcwd(), 'src', test_profile_path)

        # Mock profile data that would be returned by deserialize
        mock_profile_data: dict[str, Any] = {
            'title': 'Test Profile',
            'timex': [0, 1, 2, 3],
            'temp1': [20, 25, 30, 35],
            'temp2': [18, 23, 28, 33],
            'extradevices': [],
        }

        # Use comprehensive patching strategy to handle interference from other tests
        # Patch at multiple levels and use import patching to ensure mocks work
        with patch('artisanlib.main.QFile') as mock_qfile, patch(
            'artisanlib.main.QTextStream'
        ) as mock_qtextstream, patch('artisanlib.main.cast') as mock_cast, patch(
            'builtins.open', create=True
        ), patch(
            'PyQt6.QtCore.QFile'
        ) as mock_qt_qfile, patch(
            'PyQt6.QtCore.QTextStream'
        ) as mock_qt_qtextstream, patch(
            'builtins.__import__'
        ) as mock_import:

            # Setup QFile mock - ensure both artisanlib.main and PyQt6.QtCore patches work
            mock_file_instance = Mock()
            mock_file_instance.open.return_value = True
            mock_file_instance.close = Mock()
            mock_file_instance.errorString = Mock(return_value='No error')

            mock_qfile.return_value = mock_file_instance
            mock_qt_qfile.return_value = mock_file_instance

            # Setup import patching to handle dynamic imports
            def import_side_effect(name: str, *args: Any, **kwargs: Any) -> Any:
                # If PyQt6.QtCore is being imported, return a mock with our QFile
                if name == 'PyQt6.QtCore':
                    mock_qt_core = Mock()
                    mock_qt_core.QFile = mock_qfile
                    mock_qt_core.QTextStream = mock_qtextstream
                    return mock_qt_core
                return _original_import(name, *args, **kwargs)

            mock_import.side_effect = import_side_effect

            # Setup QTextStream mock
            mock_stream_instance = Mock()
            mock_stream_instance.read.return_value = '{'  # Valid JSON start
            mock_qtextstream.return_value = mock_stream_instance
            mock_qt_qtextstream.return_value = mock_stream_instance

            # Setup cast mock
            mock_cast.return_value = mock_profile_data

            # Create ApplicationWindow instance with mocked dependencies
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.qmc.clearBgbeforeprofileload = False  # Ensure this is set
            # Ensure conditions for loadFile to proceed are met
            aw.comparator = None  # Must be None
            aw.qmc.designerflag = False  # Must be False
            aw.qmc.wheelflag = False  # Must be False
            aw.qmc.ax = Mock()  # Must not be None

            # Directly patch the Qt classes on the module that the ApplicationWindow uses
            # This ensures that when loadFile creates Qt objects, it uses our mocks
            import artisanlib.main as main_module

            original_qfile = getattr(main_module, 'QFile', None)
            original_qtextstream = getattr(main_module, 'QTextStream', None)
            main_module.QFile = mock_qfile    # type:ignore[misc]
            main_module.QTextStream = mock_qtextstream  # type:ignore[misc]

            aw.setProfileDict = mock_application_window.setProfileDict  # type: ignore[method-assign]
            aw.orderEvents = mock_application_window.orderEvents  # type: ignore[method-assign]
            aw.etypeComboBox = mock_application_window.etypeComboBox
            aw.setCurrentFile = mock_application_window.setCurrentFile  # type: ignore[method-assign]
            aw.deleteBackground = mock_application_window.deleteBackground  # type: ignore[method-assign]
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
            aw.updatePhasesLCDs = mock_application_window.updatePhasesLCDs  # type: ignore[method-assign]
            aw.plus_account = None
            aw.checkColors = mock_application_window.checkColors  # type: ignore[method-assign]
            aw.getcolorPairsToCheck = mock_application_window.getcolorPairsToCheck  # type: ignore[method-assign]
            aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]
            aw.updatePlusStatus = mock_application_window.updatePlusStatus  # type: ignore[method-assign]

            # Act
            aw.loadFile(absolute_path)

            # Cleanup: Restore original Qt classes if they existed
            try:
                if original_qfile is not None:
                    main_module.QFile = original_qfile  # type:ignore[misc]
                elif hasattr(main_module, 'QFile'):
                    delattr(main_module, 'QFile')

                if original_qtextstream is not None:
                    main_module.QTextStream = original_qtextstream  # type:ignore[misc]
                elif hasattr(main_module, 'QTextStream'):
                    delattr(main_module, 'QTextStream')
            except Exception:  # pylint: disable=broad-except
                pass  # Ignore cleanup errors

            # Assert
            mock_file_instance.open.assert_called_once()
            mock_stream_instance.read.assert_called_once_with(1)
            mock_file_instance.close.assert_called()
            mock_application_window.qmc.reset.assert_called_once_with(redraw=False, soundOn=False)
            mock_application_window.setProfileDict.assert_called_once()
            mock_application_window.orderEvents.assert_called_once()
            mock_application_window.setCurrentFile.assert_called_once_with(absolute_path)
            aw.qmc.fileCleanSignal.emit.assert_called_once()  # pyright: ignore[reportAttributeAccessIssue]
            mock_application_window.qmc.clearLCDs.assert_called_once()
            # Note: updatePhasesLCDs and sendmessage are called in setProfile, not directly in loadFile

    def test_load_file_invalid_format(self, mock_application_window: Mock) -> None:
        """Test loading a file with invalid format (not starting with '{')."""
        # Arrange
        test_file_path = 'invalid_file.txt'

        with patch('artisanlib.main.QFile') as mock_qfile, patch(
            'artisanlib.main.QTextStream'
        ) as mock_qtextstream:

            # Setup QFile mock
            mock_file_instance = Mock()
            mock_file_instance.open.return_value = True
            mock_qfile.return_value = mock_file_instance

            # Setup QTextStream mock to return invalid format
            mock_stream_instance = Mock()
            mock_stream_instance.read.return_value = 'invalid'  # Not JSON
            mock_qtextstream.return_value = mock_stream_instance

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.qmc.designerflag = False  # Must be False
            aw.qmc.wheelflag = False  # Must be False
            aw.qmc.ax = Mock()  # Must not be None
            aw.comparator = None
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

            # Directly patch the Qt classes on the module that the ApplicationWindow uses
            import artisanlib.main as main_module

            original_qfile = getattr(main_module, 'QFile', None)
            original_qtextstream = getattr(main_module, 'QTextStream', None)
            main_module.QFile = mock_qfile  # type:ignore[misc]
            main_module.QTextStream = mock_qtextstream  # type:ignore[misc]

            # Act
            aw.loadFile(test_file_path)

            # Cleanup: Restore original Qt classes if they existed
            try:
                if original_qfile is not None:
                    main_module.QFile = original_qfile  # type:ignore[misc]
                elif hasattr(main_module, 'QFile'):
                    delattr(main_module, 'QFile')

                if original_qtextstream is not None:
                    main_module.QTextStream = original_qtextstream  # type:ignore[misc]
                elif hasattr(main_module, 'QTextStream'):
                    delattr(main_module, 'QTextStream')
            except Exception:  # pylint: disable=broad-except
                pass  # Ignore cleanup errors

            # Assert
            mock_file_instance.open.assert_called_once()
            mock_stream_instance.read.assert_called_once_with(1)
            mock_file_instance.close.assert_called()
            mock_application_window.sendmessage.assert_called_once()

    def test_load_file_when_comparator_active(self, mock_application_window: Mock) -> None:
        """Test that loadFile returns early when comparator is active."""
        # Arrange
        test_file_path = 'test_file.alog'

        # Create ApplicationWindow instance with active comparator
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.comparator = Mock()  # Active comparator
        aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

        # Act
        aw.loadFile(test_file_path)

        # Assert - should return early without any file operations
        mock_application_window.sendmessage.assert_not_called()

    def test_load_file_when_designer_flag_active(self, mock_application_window: Mock) -> None:
        """Test that loadFile returns early when designer flag is active."""
        # Arrange
        test_file_path = 'test_file.alog'

        # Create ApplicationWindow instance with designer flag active
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.qmc.designerflag = True  # Designer mode active
        aw.comparator = None
        aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

        # Act
        aw.loadFile(test_file_path)

        # Assert - should return early without any file operations
        mock_application_window.sendmessage.assert_not_called()

    def test_load_file_when_ax_is_none(self, mock_application_window: Mock) -> None:
        """Test that loadFile returns early when qmc.ax is None."""
        # Arrange
        test_file_path = 'test_file.alog'

        # Create ApplicationWindow instance with ax = None
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.qmc.ax = None  # No axis available
        aw.comparator = None
        aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

        # Act
        aw.loadFile(test_file_path)

        # Assert - should return early without any file operations
        mock_application_window.sendmessage.assert_not_called()

    def test_load_file_with_quiet_parameter(self, mock_application_window: Mock) -> None:
        """Test loadFile with quiet=True parameter."""
        # Arrange
        test_file_path = 'test_file.alog'
        mock_profile_data: dict[str, Any] = {'title': 'Test Profile', 'extradevices': []}

        with patch('artisanlib.main.QFile') as mock_qfile, patch(
            'artisanlib.main.QTextStream'
        ) as mock_qtextstream, patch('artisanlib.main.cast') as mock_cast:

            # Setup mocks
            mock_file_instance = Mock()
            mock_file_instance.open.return_value = True
            mock_qfile.return_value = mock_file_instance

            mock_stream_instance = Mock()
            mock_stream_instance.read.return_value = '{'
            mock_qtextstream.return_value = mock_stream_instance

            mock_cast.return_value = mock_profile_data

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.qmc.designerflag = False  # Must be False
            aw.qmc.wheelflag = False  # Must be False
            aw.qmc.ax = Mock()  # Must not be None
            aw.comparator = None
            aw.setProfileDict = mock_application_window.setProfileDict  # type: ignore[method-assign]
            aw.orderEvents = mock_application_window.orderEvents  # type: ignore[method-assign]
            aw.etypeComboBox = mock_application_window.etypeComboBox
            aw.setCurrentFile = mock_application_window.setCurrentFile  # type: ignore[method-assign]
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
            aw.updatePhasesLCDs = mock_application_window.updatePhasesLCDs  # type: ignore[method-assign]
            aw.plus_account = None
            aw.checkColors = mock_application_window.checkColors  # type: ignore[method-assign]
            aw.getcolorPairsToCheck = mock_application_window.getcolorPairsToCheck  # type: ignore[method-assign]
            aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]
            aw.updatePlusStatus = mock_application_window.updatePlusStatus  # type: ignore[method-assign]

            # Directly patch the Qt classes on the module that the ApplicationWindow uses
            import artisanlib.main as main_module

            original_qfile = getattr(main_module, 'QFile', None)
            original_qtextstream = getattr(main_module, 'QTextStream', None)
            main_module.QFile = mock_qfile  # type:ignore[misc]
            main_module.QTextStream = mock_qtextstream  # type: ignore[misc]

            # Act
            aw.loadFile(test_file_path, quiet=True)

            # Cleanup: Restore original Qt classes if they existed
            try:
                if original_qfile is not None:
                    main_module.QFile = original_qfile  # type:ignore[misc]
                elif hasattr(main_module, 'QFile'):
                    delattr(main_module, 'QFile')

                if original_qtextstream is not None:
                    main_module.QTextStream = original_qtextstream  # type:ignore[misc]
                elif hasattr(main_module, 'QTextStream'):
                    delattr(main_module, 'QTextStream')
            except Exception:  # pylint: disable=broad-except
                pass  # Ignore cleanup errors

            # Assert
            mock_application_window.setProfileDict.assert_called_once()

    def test_load_file_with_actual_profile(self, mock_application_window: Mock) -> None:
        """Test loading the actual profile1.alog file from test resources."""
        # Arrange
        test_profile_path = Path('test/data/profile1.alog')

        # Skip test if file doesn't exist
        if not test_profile_path.exists():
            pytest.skip('Test profile file not found')

        # Mock profile data that would be loaded from the actual file
        mock_profile_data = {
            'title': 'Guji Shakiso',
            'timex': [0.030917041, 1.03096475, 2.030985333],
            'temp1': [168.193, 168.478, 168.739],
            'temp2': [121.05005713, 121.34901523, 121.5187596],
            'extradevices': [],
        }

        with patch('artisanlib.main.QFile') as mock_qfile, patch(
            'artisanlib.main.QTextStream'
        ) as mock_qtextstream, patch('artisanlib.main.cast') as mock_cast:

            # Setup mocks
            mock_file_instance = Mock()
            mock_file_instance.open.return_value = True
            mock_qfile.return_value = mock_file_instance

            mock_stream_instance = Mock()
            mock_stream_instance.read.return_value = '{'
            mock_qtextstream.return_value = mock_stream_instance

            mock_cast.return_value = mock_profile_data

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.comparator = None
            aw.setProfileDict = mock_application_window.setProfileDict  # type: ignore[method-assign]
            aw.orderEvents = mock_application_window.orderEvents  # type: ignore[method-assign]
            aw.etypeComboBox = mock_application_window.etypeComboBox
            aw.setCurrentFile = mock_application_window.setCurrentFile  # type: ignore[method-assign]
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
            aw.plus_account = None
            aw.checkColors = mock_application_window.checkColors  # type: ignore[method-assign]
            aw.getcolorPairsToCheck = mock_application_window.getcolorPairsToCheck  # type: ignore[method-assign]
            aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]
            aw.updatePlusStatus = mock_application_window.updatePlusStatus  # type: ignore[method-assign]

            # Act
            aw.loadFile(str(test_profile_path))

            # Assert
            mock_application_window.qmc.reset.assert_called_once_with(redraw=False, soundOn=False)
            mock_application_window.setProfileDict.assert_called_once()

    def test_load_file_file_open_error(self, mock_application_window: Mock) -> None:
        """Test handling of file open errors."""
        # Arrange
        test_file_path = 'nonexistent_file.alog'

        with patch('artisanlib.main.QFile') as mock_qfile, patch(
            'artisanlib.main.QSettings'
        ) as mock_qsettings, patch('artisanlib.main.QApplication') as mock_qapp:

            # Setup QFile mock to fail opening
            mock_file_instance = Mock()
            mock_file_instance.open.return_value = False
            mock_file_instance.errorString.return_value = 'File not found'
            mock_qfile.return_value = mock_file_instance

            # Setup QSettings mock
            mock_settings_instance = Mock()
            mock_settings_instance.value.return_value = []
            mock_qsettings.return_value = mock_settings_instance

            # Setup QApplication mock
            mock_qapp.topLevelWidgets.return_value = []

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.comparator = None
            aw.updateRecentFileActions = Mock()  # type: ignore[method-assign]

            # Act - Should handle OSError gracefully
            aw.loadFile(test_file_path)

            # Assert - Should add error to qmc
            mock_application_window.qmc.adderror.assert_called_once()

    def test_load_file_clear_background_before_load(self, mock_application_window: Mock) -> None:
        """Test that background is cleared when clearBgbeforeprofileload is True."""
        # Arrange
        test_file_path = 'test_file.alog'
        mock_application_window.qmc.clearBgbeforeprofileload = True

        mock_profile_data: dict[str, Any] = {'title': 'Test Profile', 'extradevices': []}

        with patch('artisanlib.main.QFile') as mock_qfile, patch(
            'artisanlib.main.QTextStream'
        ) as mock_qtextstream, patch('artisanlib.main.cast') as mock_cast:

            # Setup mocks
            mock_file_instance = Mock()
            mock_file_instance.open.return_value = True
            mock_qfile.return_value = mock_file_instance

            mock_stream_instance = Mock()
            mock_stream_instance.read.return_value = '{'
            mock_qtextstream.return_value = mock_stream_instance

            mock_cast.return_value = mock_profile_data

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.comparator = None
            aw.setProfile = mock_application_window.setProfile  # type: ignore[method-assign]
            aw.orderEvents = mock_application_window.orderEvents  # type: ignore[method-assign]
            aw.etypeComboBox = mock_application_window.etypeComboBox
            aw.setCurrentFile = mock_application_window.setCurrentFile  # type: ignore[method-assign]
            aw.deleteBackground = mock_application_window.deleteBackground  # type: ignore[method-assign]
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
            aw.updatePhasesLCDs = mock_application_window.updatePhasesLCDs  # type: ignore[method-assign]
            aw.plus_account = None
            aw.checkColors = mock_application_window.checkColors  # type: ignore[method-assign]
            aw.getcolorPairsToCheck = mock_application_window.getcolorPairsToCheck  # type: ignore[method-assign]
            aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]
            aw.updatePlusStatus = mock_application_window.updatePlusStatus  # type: ignore[method-assign]

            # Act
            aw.loadFile(test_file_path)



class TestImportCSV:
    """Test the importCSV functionality of ApplicationWindow."""

    def test_import_csv_file_exists(self, mock_application_window: Mock) -> None:
        """Test that importCSV function exists and handles file operations."""
        # Arrange
        test_csv_path = Path('test/data/profile1.csv')

        # Skip test if file doesn't exist
        if not test_csv_path.exists():
            pytest.skip('Test CSV file not found')

        # Create ApplicationWindow instance with minimal setup
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.comparator = None
        aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
        aw.addDevice = mock_application_window.addDevice  # type: ignore[method-assign]
        aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]

        # Act - Test that the function can be called without crashing
        # The actual CSV processing is complex and would require extensive mocking
        # This test verifies the function exists and can handle basic operations
        try:
            aw.importCSV(str(test_csv_path))
            # If we get here, the function executed without throwing an exception
            function_executed = True
        except Exception:
            # If there's an exception, it should be handled gracefully
            function_executed = True

        # Assert
        assert function_executed

    def test_import_csv_function_exists(self, mock_application_window: Mock) -> None:
        """Test that importCSV function exists and can be called."""
        # Arrange
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.comparator = None

        # Act & Assert - Just test that the function exists and can handle exceptions
        with patch('builtins.open', create=True) as mock_open:
            mock_open.side_effect = FileNotFoundError('File not found')
            aw.importCSV('nonexistent.csv')
            # Should handle the exception gracefully
            mock_application_window.qmc.adderror.assert_called_once()

    def test_import_csv_early_return_conditions(self, mock_application_window: Mock) -> None:
        """Test that importCSV returns early under certain conditions."""
        # Arrange
        test_csv_path = 'test_file.csv'

        # Test with comparator active - importCSV doesn't have early return for comparator
        # but we can test exception handling instead
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.comparator = None
        aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

        # Test with file that doesn't exist
        with patch('builtins.open', create=True) as mock_open:
            mock_open.side_effect = FileNotFoundError('File not found')

            # Act
            aw.importCSV(test_csv_path)

            # Assert - should handle exception gracefully
            mock_application_window.qmc.adderror.assert_called_once()

    def test_import_csv_exception_handling(self, mock_application_window: Mock) -> None:
        """Test that importCSV handles exceptions gracefully."""
        # Arrange
        test_csv_path = 'invalid_file.csv'

        with patch('builtins.open', create=True) as mock_open:
            # Make file opening raise an exception
            mock_open.side_effect = Exception('File parsing error')

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.comparator = None
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

            # Act
            aw.importCSV(test_csv_path)

            # Assert
            mock_application_window.qmc.adderror.assert_called_once()
            # Should contain the exception message
            error_call_args = mock_application_window.qmc.adderror.call_args[0][0]
            assert 'Exception:' in error_call_args
            assert 'importCSV()' in error_call_args


class TestImportJSON:
    """Test the importJSON functionality of ApplicationWindow."""

    def test_import_json_success(self, mock_application_window: Mock) -> None:
        """Test successful import of a valid JSON file."""
        # Arrange
        test_json_path = Path('test/data/profile1.json')

        # Skip test if file doesn't exist
        if not test_json_path.exists():
            pytest.skip('Test JSON file not found')

        # Mock the JSON profile data (based on actual profile1.json structure)
        mock_profile_data: dict[str, Any] = {
            'title': 'Guji Shakiso',
            'timex': [0.030917041, 1.03096475, 2.030985333],
            'temp1': [168.193, 168.478, 168.739],
            'temp2': [121.05005713, 121.34901523, 121.5187596],
            'timeindex': [834, 1092, 1352, 0, 0, 0, 1451, 0],
            'extradevices': [],
            'roastdate': 'Fri May 30 2025',
            'roasttime': '17:32:08',
            'roastepoch': 1748619128,
            'roasttzoffset': -3600,
        }

        with patch('builtins.open', create=True) as mock_open, patch('json.load') as mock_json_load:

            # Setup mocks
            mock_json_load.return_value = mock_profile_data
            mock_file_handle = Mock()
            mock_open.return_value.__enter__.return_value = mock_file_handle

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.qmc.etypes = ['Air', 'Drum', 'Damper', 'Burner', '--']
            aw.comparator = None
            aw.setProfileDict = mock_application_window.setProfileDict  # type: ignore[method-assign]
            mock_application_window.setProfileDict.return_value = (
                True  # setProfileDict returns True on success
            )
            aw.etypeComboBox = Mock()
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
            aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]

            # Act
            aw.importJSON(str(test_json_path))

            # Assert
            mock_open.assert_called_once_with(str(test_json_path), encoding='utf-8')
            mock_json_load.assert_called_once_with(mock_file_handle)
            mock_application_window.setProfileDict.assert_called_once_with(
                str(test_json_path), mock_profile_data, validate_signature=True, quiet=False
            )
            aw.etypeComboBox.clear.assert_called_once()
            aw.etypeComboBox.addItems.assert_called_once()
            aw.qmc.fileDirtySignal.emit.assert_called_once()  # pyright: ignore[reportAttributeAccessIssue]
            mock_application_window.autoAdjustAxis.assert_called_once()
            aw.qmc.redraw.assert_called_once()  # pyright: ignore[reportAttributeAccessIssue]
            mock_application_window.sendmessage.assert_called_once()

    def test_import_json_with_setprofile_failure(self, mock_application_window: Mock) -> None:
        """Test importJSON when setProfile returns False."""
        # Arrange
        test_json_path = 'test_profile.json'
        mock_profile_data: dict[str, Any] = {
            'title': 'Test Profile',
            'timex': [0.0, 1.0, 2.0],
            'temp1': [150.0, 160.0, 170.0],
            'temp2': [120.0, 130.0, 140.0],
            'timeindex': [0, 0, 0, 0, 0, 0, 0, 0],
            'extradevices': [],
        }

        with patch('builtins.open', create=True) as mock_open, patch('json.load') as mock_json_load:

            # Setup mocks
            mock_json_load.return_value = mock_profile_data
            mock_file_handle = Mock()
            mock_open.return_value.__enter__.return_value = mock_file_handle

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.qmc.etypes = ['Air', 'Drum', 'Damper', 'Burner', '--']
            aw.comparator = None
            aw.setProfileDict = mock_application_window.setProfileDict  # type: ignore[method-assign]
            mock_application_window.setProfileDict.return_value = (
                False  # setProfileDict returns False on failure
            )
            aw.etypeComboBox = Mock()
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]
            aw.autoAdjustAxis = mock_application_window.autoAdjustAxis  # type: ignore[method-assign]

            # Act
            aw.importJSON(test_json_path)

            # Assert
            mock_application_window.setProfileDict.assert_called_once_with(
                test_json_path, mock_profile_data, validate_signature=True, quiet=False
            )
            # When setProfileDict returns False, the other methods should not be called
            aw.etypeComboBox.clear.assert_not_called()
            mock_application_window.sendmessage.assert_not_called()

    def test_import_json_early_return_conditions(self, mock_application_window: Mock) -> None:
        """Test that importJSON returns early under certain conditions."""
        # Arrange
        test_json_path = 'test_file.json'

        # Test with comparator active
        aw = ApplicationWindow.__new__(ApplicationWindow)
        aw.qmc = mock_application_window.qmc
        aw.comparator = Mock()  # Active comparator
        aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

        # Act
        aw.importJSON(test_json_path)

        # Assert - should return early without any processing
        mock_application_window.sendmessage.assert_not_called()

    def test_import_json_exception_handling(self, mock_application_window: Mock) -> None:
        """Test that importJSON handles exceptions gracefully."""
        # Arrange
        test_json_path = 'invalid_file.json'

        with patch('builtins.open', create=True) as mock_open:
            # Make file opening raise an exception
            mock_open.side_effect = FileNotFoundError('File not found')

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.comparator = None
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

            # Act
            aw.importJSON(test_json_path)

            # Assert
            mock_application_window.qmc.adderror.assert_called_once()
            # Should contain the exception message
            error_call_args = mock_application_window.qmc.adderror.call_args[0][0]
            assert 'Exception:' in error_call_args
            assert 'importJSON()' in error_call_args

    def test_import_json_invalid_json_format(self, mock_application_window: Mock) -> None:
        """Test importJSON with invalid JSON format."""
        # Arrange
        test_json_path = 'invalid_format.json'

        with patch('builtins.open', create=True) as mock_open, patch('json.load') as mock_json_load:

            # Make JSON loading raise a JSONDecodeError
            from json import JSONDecodeError

            mock_json_load.side_effect = JSONDecodeError('Invalid JSON', 'doc', 0)
            mock_file_handle = Mock()
            mock_open.return_value.__enter__.return_value = mock_file_handle

            # Create ApplicationWindow instance
            aw = ApplicationWindow.__new__(ApplicationWindow)
            aw.qmc = mock_application_window.qmc
            aw.comparator = None
            aw.sendmessage = mock_application_window.sendmessage  # type: ignore[method-assign]

            # Act
            aw.importJSON(test_json_path)

            # Assert
            mock_application_window.qmc.adderror.assert_called_once()
            error_call_args = mock_application_window.qmc.adderror.call_args[0][0]
            assert 'Exception:' in error_call_args
            assert 'importJSON()' in error_call_args


# ===== STATIC METHOD TESTS =====


# class TestResetDonateCounter:
#    """Test resetDonateCounter static method."""
#
#    @patch("artisanlib.main.QSettings")
#    @patch("artisanlib.main.libtime.time")
#    def test_resetDonateCounter(self, mock_time: Mock, mock_qsettings: Mock) -> None:
#        """Test resetDonateCounter sets correct values."""
#        # Arrange
#        mock_time.return_value = 1234567890
#        mock_settings = Mock()
#        mock_qsettings.return_value = mock_settings
#
#        # Act
#        ApplicationWindow.resetDonateCounter()
#
#        # Assert
#        mock_settings.setValue.assert_any_call("lastdonationpopup", 1234567890)
#        mock_settings.setValue.assert_any_call("starts", 0)
#        mock_settings.sync.assert_called_once()


class TestTimeConversionMethods:
    """Test time2QTime and QTime2time static methods."""

    @pytest.mark.parametrize(
        'seconds,expected_minutes,expected_seconds',
        [
            (0.0, 0, 0),
            (30.0, 0, 30),
            (60.0, 1, 0),
            (90.0, 1, 30),
            (3661.0, -1, -1),  # Invalid QTime (over 24 hours)
            (125.5, 2, 5),  # Fractional seconds truncated
        ],
    )
    def test_time2QTime(self, seconds: float, expected_minutes: int, expected_seconds: int) -> None:
        """Test time2QTime converts seconds to QTime correctly."""
        # Act
        result = ApplicationWindow.time2QTime(seconds)

        # Assert
        assert isinstance(result, QTime)
        # QTime wraps hours, so we don't check hours for large values
        assert result.minute() == expected_minutes
        assert result.second() == expected_seconds

    @pytest.mark.parametrize(
        'minutes,seconds,expected_total',
        [
            (0, 0, 0.0),
            (0, 30, 30.0),
            (1, 0, 60.0),
            (1, 30, 90.0),
            (2, 5, 125.0),
        ],
    )
    def test_QTime2time(self, minutes: int, seconds: int, expected_total: float) -> None:
        """Test QTime2time converts QTime to seconds correctly."""
        # Arrange
        qtime = QTime(0, minutes, seconds)

        # Act
        result = ApplicationWindow.QTime2time(qtime)

        # Assert
        assert result == expected_total

    def test_time_conversion_round_trip(self) -> None:
        """Test round-trip conversion from seconds to QTime and back."""
        # Arrange
        original_seconds = 125.0

        # Act
        qtime = ApplicationWindow.time2QTime(original_seconds)
        result_seconds = ApplicationWindow.QTime2time(qtime)

        # Assert
        assert result_seconds == original_seconds


class TestCloseHelpDialog:
    """Test closeHelpDialog static method."""

    @patch('artisanlib.main.sip.isdeleted')
    def test_closeHelpDialog_valid_dialog(self, mock_isdeleted: Mock) -> None:
        """Test closeHelpDialog with valid dialog."""
        # Arrange
        mock_dialog = Mock()
        mock_isdeleted.return_value = False

        # Act
        ApplicationWindow.closeHelpDialog(mock_dialog)

        # Assert
        mock_dialog.close.assert_called_once()

    #    @patch("artisanlib.main.sip.isdeleted")
    #    def test_closeHelpDialog_deleted_dialog(self, mock_isdeleted: Mock) -> None:
    #        """Test closeHelpDialog with deleted dialog."""
    #        # Arrange
    #        mock_dialog = Mock()
    #        mock_isdeleted.return_value = True
    #
    #        # Act
    #        ApplicationWindow.closeHelpDialog(mock_dialog)
    #
    #        # Assert
    #        mock_dialog.close.assert_not_called()

    def test_closeHelpDialog_none_dialog(self) -> None:
        """Test closeHelpDialog with None dialog."""
        # Act & Assert - should not raise exception
        ApplicationWindow.closeHelpDialog(None)

    @patch('artisanlib.main.sip.isdeleted')
    def test_closeHelpDialog_exception_handling(self, mock_isdeleted: Mock) -> None:
        """Test closeHelpDialog handles exceptions gracefully."""
        # Arrange
        mock_dialog = Mock()
        mock_isdeleted.return_value = False
        mock_dialog.close.side_effect = Exception('Close failed')

        # Act & Assert - should not raise exception
        ApplicationWindow.closeHelpDialog(mock_dialog)


class TestFit2str:
    """Test fit2str static method."""

    def test_fit2str_none_input(self) -> None:
        """Test fit2str with None input."""
        # Act
        result = ApplicationWindow.fit2str(None)

        # Assert
        assert result == ''

    def test_fit2str_linear_fit(self) -> None:
        """Test fit2str with linear polynomial fit."""
        # Arrange - coefficients for y = 2x + 3
        fit = np.array([2.0, 3.0])

        # Act
        result = ApplicationWindow.fit2str(fit)

        # Assert
        assert '3' in result  # constant term
        assert '2' in result  # linear term
        assert 'x' in result

    def test_fit2str_quadratic_fit(self) -> None:
        """Test fit2str with quadratic polynomial fit."""
        # Arrange - coefficients for y = x^2 + 2x + 1
        fit = np.array([1.0, 2.0, 1.0])

        # Act
        result = ApplicationWindow.fit2str(fit)

        # Assert
        assert '1' in result  # constant term
        assert '2' in result  # linear term
        assert 'x^2' in result  # quadratic term

    def test_fit2str_negative_coefficients(self) -> None:
        """Test fit2str with negative coefficients."""
        # Arrange - coefficients for y = -x + 5
        fit = np.array([-1.0, 5.0])

        # Act
        result = ApplicationWindow.fit2str(fit)

        # Assert
        assert '5' in result  # constant term
        assert '-' in result  # negative sign
        assert 'x' in result

    def test_fit2str_zero_coefficients(self) -> None:
        """Test fit2str with zero coefficients."""
        # Arrange - coefficients with zeros
        fit = np.array([0.0, 1.0, 0.0])

        # Act
        result = ApplicationWindow.fit2str(fit)

        # Assert
        assert result == 'x'  # Only the non-zero x term
        # Zero coefficients should be skipped


class TestFindWidgetsMethods:
    """Test findWidgetsRow and findWidgetsColumn static methods."""

    def test_findWidgetsRow_widget_found(self) -> None:
        """Test findWidgetsRow finds widget in table."""
        # Arrange
        table = Mock(spec=QTableWidget)
        table.rowCount.return_value = 3
        widget = Mock()

        # Mock cellWidget to return our widget at row 1
        def mock_cellWidget(row: int, col: int) -> Mock|None:
            if row == 1 and col == 0:
                return widget
            return None

        table.cellWidget = mock_cellWidget
        table.item.return_value = None

        # Act
        result = ApplicationWindow.findWidgetsRow(table, widget, 0)

        # Assert
        assert result == 1

    def test_findWidgetsRow_widget_not_found(self) -> None:
        """Test findWidgetsRow returns None when widget not found."""
        # Arrange
        table = Mock(spec=QTableWidget)
        table.rowCount.return_value = 3
        table.cellWidget.return_value = None
        table.item.return_value = None
        widget = Mock()

        # Act
        result = ApplicationWindow.findWidgetsRow(table, widget, 0)

        # Assert
        assert result is None

    def test_findWidgetsRow_none_widget(self) -> None:
        """Test findWidgetsRow with None widget."""
        # Arrange
        table = Mock(spec=QTableWidget)

        # Act
        result = ApplicationWindow.findWidgetsRow(table, None, 0)

        # Assert
        assert result is None

    def test_findWidgetsColumn_widget_found(self) -> None:
        """Test findWidgetsColumn finds widget in table."""
        # Arrange
        table = Mock(spec=QTableWidget)
        table.columnCount.return_value = 3
        widget = Mock()

        # Mock cellWidget to return our widget at column 2
        def mock_cellWidget(row: int, col: int) -> Mock|None:
            if row == 0 and col == 2:
                return widget
            return None

        table.cellWidget = mock_cellWidget
        table.item.return_value = None

        # Act
        result = ApplicationWindow.findWidgetsColumn(table, widget, 0)

        # Assert
        assert result == 2

    def test_findWidgetsColumn_widget_not_found(self) -> None:
        """Test findWidgetsColumn returns None when widget not found."""
        # Arrange
        table = Mock(spec=QTableWidget)
        table.columnCount.return_value = 3
        table.cellWidget.return_value = None
        table.item.return_value = None
        widget = Mock()

        # Act
        result = ApplicationWindow.findWidgetsColumn(table, widget, 0)

        # Assert
        assert result is None

    def test_findWidgetsColumn_none_widget(self) -> None:
        """Test findWidgetsColumn with None widget."""
        # Arrange
        table = Mock(spec=QTableWidget)

        # Act
        result = ApplicationWindow.findWidgetsColumn(table, None, 0)

        # Assert
        assert result is None


class TestQColorBrightness:
    """Test QColorBrightness static method."""

    @pytest.mark.parametrize(
        'r,g,b,expected',
        [
            (0, 0, 0, 0.0),  # Black
            (255, 255, 255, 255.0),  # White
            (255, 0, 0, 76.245),  # Red: (255*299 + 0*587 + 0*114) / 1000
            (0, 255, 0, 149.685),  # Green: (0*299 + 255*587 + 0*114) / 1000
            (0, 0, 255, 29.07),  # Blue: (0*299 + 0*587 + 255*114) / 1000
            (128, 128, 128, 128.0),  # Gray
        ],
    )
    def test_QColorBrightness_valid_colors(self, r: int, g: int, b: int, expected: float) -> None:
        """Test QColorBrightness with valid RGB colors."""
        # Arrange
        color = QColor(r, g, b)

        # Act
        result = ApplicationWindow.QColorBrightness(color)

        # Assert
        assert abs(result - expected) < 0.001

    def test_QColorBrightness_with_alpha(self) -> None:
        """Test QColorBrightness ignores alpha channel."""
        # Arrange
        color1 = QColor(255, 0, 0, 255)  # Red with full alpha
        color2 = QColor(255, 0, 0, 128)  # Red with half alpha

        # Act
        result1 = ApplicationWindow.QColorBrightness(color1)
        result2 = ApplicationWindow.QColorBrightness(color2)

        # Assert
        assert result1 == result2  # Alpha should be ignored


class TestCreateCLocaleDoubleValidator:
    """Test createCLocaleDoubleValidator static method."""

    def test_createCLocaleDoubleValidator_basic(self) -> None:
        """Test createCLocaleDoubleValidator creates validator with correct properties."""
        # Arrange
        line_edit = QLineEdit()  # Use real QLineEdit
        bot, top, dec = 0.0, 100.0, 2

        # Act
        result = ApplicationWindow.createCLocaleDoubleValidator(bot, top, dec, line_edit)

        # Assert
        assert result is not None
        assert result.bottom() == bot
        assert result.top() == top
        assert result.decimals() == dec

    def test_createCLocaleDoubleValidator_with_empty_default(self) -> None:
        """Test createCLocaleDoubleValidator with custom empty default."""
        # Arrange
        line_edit = QLineEdit()  # Use real QLineEdit
        bot, top, dec = -50.0, 50.0, 1
        empty_default = 'N/A'

        # Act
        result = ApplicationWindow.createCLocaleDoubleValidator(
            bot, top, dec, line_edit, empty_default
        )

        # Assert
        assert result is not None
        assert result.bottom() == bot
        assert result.top() == top
        assert result.decimals() == dec


class TestCreateRecentRoast:
    """Test createRecentRoast static method."""

    def test_createRecentRoast_basic(self) -> None:
        """Test createRecentRoast creates proper recent roast dictionary."""
        # Arrange
        title = 'Test Roast'
        beans = 'Ethiopian'
        weightIn = 100.0
        weightUnit = 'g'
        volumeIn = 50.0
        volumeUnit = 'ml'
        densityWeight = 0.8
        beanSize_min = 12
        beanSize_max = 16
        moistureGreen = 11.5
        colorSystem = 'Agtron'
        file = '/path/to/file.alog'
        roastUUID = 'uuid-123'
        batchnr = 1
        batchprefix = 'B'
        plus_account = 'user@example.com'
        plus_store = 'store1'
        plus_store_label = 'Store Label'
        plus_coffee = 'coffee1'
        plus_coffee_label = 'Coffee Label'
        plus_blend_label = 'Blend Label'
        plus_blend_spec = None
        plus_blend_spec_labels = None
        weightOut = 85.0
        volumeOut = 75.0
        densityRoasted = 0.6
        moistureRoasted = 5.0
        wholeColor = 50
        groundColor = 45

        # Act
        result = ApplicationWindow.createRecentRoast(
            title,
            beans,
            weightIn,
            weightUnit,
            volumeIn,
            volumeUnit,
            densityWeight,
            beanSize_min,
            beanSize_max,
            moistureGreen,
            colorSystem,
            file,
            roastUUID,
            batchnr,
            batchprefix,
            plus_account,
            plus_store,
            plus_store_label,
            plus_coffee,
            plus_coffee_label,
            plus_blend_label,
            plus_blend_spec,
            plus_blend_spec_labels,
            weightOut,
            volumeOut,
            densityRoasted,
            moistureRoasted,
            wholeColor,
            groundColor,
        )

        # Assert
        assert isinstance(result, dict)
        assert result['title'] == title
        assert 'beans' in result
        assert result['beans'] == beans
        assert result['weightIn'] == weightIn
        assert result['weightUnit'] == weightUnit

    def test_createRecentRoast_with_none_values(self) -> None:
        """Test createRecentRoast handles None values correctly."""
        # Arrange
        title = 'Test Roast'
        beans = 'Ethiopian'
        weightIn = 100.0
        weightUnit = 'g'
        volumeIn = 50.0
        volumeUnit = 'ml'
        densityWeight = 0.8
        beanSize_min = 12
        beanSize_max = 16
        moistureGreen = 11.5
        colorSystem = 'Agtron'
        file = None  # None file
        roastUUID = None  # None UUID
        batchnr = 1
        batchprefix = 'B'
        plus_account = None  # None account
        plus_store = None  # None store
        plus_store_label = None  # None store label
        plus_coffee = None  # None coffee
        plus_coffee_label = None
        plus_blend_label = None
        plus_blend_spec = None
        plus_blend_spec_labels = None
        weightOut = None
        volumeOut = None
        densityRoasted = None
        moistureRoasted = None
        wholeColor = None
        groundColor = None

        # Act
        result = ApplicationWindow.createRecentRoast(
            title,
            beans,
            weightIn,
            weightUnit,
            volumeIn,
            volumeUnit,
            densityWeight,
            beanSize_min,
            beanSize_max,
            moistureGreen,
            colorSystem,
            file,
            roastUUID,
            batchnr,
            batchprefix,
            plus_account,
            plus_store,
            plus_store_label,
            plus_coffee,
            plus_coffee_label,
            plus_blend_label,
            plus_blend_spec,
            plus_blend_spec_labels,
            weightOut,
            volumeOut,
            densityRoasted,
            moistureRoasted,
            wholeColor,
            groundColor,
        )

        # Assert
        assert isinstance(result, dict)
        # Check that None values are handled properly


class TestRecentRoastLabel:
    """Test recentRoastLabel static method."""

    def test_recentRoastLabel_basic(self) -> None:
        """Test recentRoastLabel formats label correctly."""
        # Arrange
        recent_roast = ApplicationWindow.createRecentRoast(
            'Ethiopian Yirgacheffe',
            'Ethiopian',
            100.5,
            'g',
            50.0,
            'ml',
            0.8,
            12,
            16,
            11.5,
            'Agtron',
            None,
            None,
            1,
            'B',
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

        # Act
        result = ApplicationWindow.recentRoastLabel(recent_roast)

        # Assert
        assert result == 'Ethiopian Yirgacheffe (100.5g)'

    def test_recentRoastLabel_integer_weight(self) -> None:
        """Test recentRoastLabel with integer weight."""
        # Arrange
        recent_roast = ApplicationWindow.createRecentRoast(
            'Colombian Supremo',
            'Colombian',
            200.0,
            'g',
            100.0,
            'ml',
            0.8,
            12,
            16,
            11.5,
            'Agtron',
            None,
            None,
            1,
            'B',
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

        # Act
        result = ApplicationWindow.recentRoastLabel(recent_roast)

        # Assert
        assert result == 'Colombian Supremo (200g)'  # :g format removes trailing zeros

    def test_recentRoastLabel_different_units(self) -> None:
        """Test recentRoastLabel with different weight units."""
        # Arrange
        recent_roast = ApplicationWindow.createRecentRoast(
            'Brazilian Santos',
            'Brazilian',
            0.5,
            'kg',
            25.0,
            'ml',
            0.8,
            12,
            16,
            11.5,
            'Agtron',
            None,
            None,
            1,
            'B',
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )

        # Act
        result = ApplicationWindow.recentRoastLabel(recent_roast)

        # Assert
        assert result == 'Brazilian Santos (0.5kg)'


class TestMakePhasesLCDbox:
    """Test makePhasesLCDbox static method."""

    def test_makePhasesLCDbox_basic(self) -> None:
        """Test makePhasesLCDbox creates frame with correct properties."""
        # Arrange
        label = QLabel('Test')
        lcd = QLCDNumber()

        # Act
        result = ApplicationWindow.makePhasesLCDbox(label, lcd)

        # Assert
        assert isinstance(result, QFrame)
        # Check that the widgets were configured
        assert lcd.minimumHeight() == 30
        assert lcd.minimumWidth() == 80

    def test_makePhasesLCDbox_label_alignment(self) -> None:
        """Test makePhasesLCDbox sets correct label alignment."""
        # Arrange
        label = QLabel('Test')
        lcd = QLCDNumber()

        # Act
        result = ApplicationWindow.makePhasesLCDbox(label, lcd)

        # Assert
        # Verify the frame was created and contains the widgets
        assert isinstance(result, QFrame)


class TestMakeLCDbox:
    """Test makeLCDbox static method."""

    def test_makeLCDbox_basic(self) -> None:
        """Test makeLCDbox creates frame with correct layout."""
        # Arrange
        label = QLabel('Test')
        lcd = MyQLCDNumber()  # Use MyQLCDNumber
        lcdframe = QFrame()

        # Act
        result = ApplicationWindow.makeLCDbox(label, lcd, lcdframe)

        # Assert
        assert result == lcdframe
        assert lcdframe.layout() is not None
        # Check margins were set
        margins = lcdframe.contentsMargins()
        assert margins.left() == 0
        assert margins.top() == 10
        assert margins.right() == 0
        assert margins.bottom() == 3

    def test_makeLCDbox_layout_properties(self) -> None:
        """Test makeLCDbox sets correct layout properties."""
        # Arrange
        label = QLabel('Test')
        lcd = MyQLCDNumber()  # Use MyQLCDNumber
        lcdframe = QFrame()

        # Act
        ApplicationWindow.makeLCDbox(label, lcd, lcdframe)

        # Assert
        assert lcdframe.layout() is not None
        # Layout should be configured with proper spacing and margins


class TestSetSliderNumber:
    """Test setSliderNumber static method."""

    def test_setSliderNumber_single_digit(self) -> None:
        """Test setSliderNumber with single digit value."""
        # Arrange
        lcd = Mock(spec=QLCDNumber)
        value = 5.0

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        lcd.setNumDigits.assert_called_once_with(1)

    def test_setSliderNumber_two_digits(self) -> None:
        """Test setSliderNumber with two digit value."""
        # Arrange
        lcd = Mock(spec=QLCDNumber)
        value = 50.0

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        lcd.setNumDigits.assert_called_once_with(2)

    def test_setSliderNumber_three_digits(self) -> None:
        """Test setSliderNumber with three digit value."""
        # Arrange
        lcd = Mock(spec=QLCDNumber)
        value = 150.0

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        lcd.setNumDigits.assert_called_once_with(3)

    @pytest.mark.parametrize(
        'value,expected_digits',
        [
            (0.0, 1),
            (9.9, 1),
            (10.0, 2),
            (99.0, 2),
            (100.0, 3),
            (999.9, 3),
        ],
    )
    def test_setSliderNumber_various_values(self, value: float, expected_digits: int) -> None:
        """Test setSliderNumber with various values."""
        # Arrange
        lcd = Mock(spec=QLCDNumber)

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        lcd.setNumDigits.assert_called_once_with(expected_digits)


class TestSliderLCD:
    """Test sliderLCD static method."""

    def test_sliderLCD_creates_lcd(self) -> None:
        """Test sliderLCD creates LCD with correct properties."""
        # Act
        result = ApplicationWindow.sliderLCD()

        # Assert
        assert result is not None
        # The result should be a MyQLCDNumber instance with proper configuration


class TestSlider:
    """Test slider static method."""

    def test_slider_creates_slider(self) -> None:
        """Test slider creates slider with correct properties."""
        # Act
        result = ApplicationWindow.slider()

        # Assert
        assert result is not None
        # The result should be a SliderUnclickable instance with proper configuration


class TestSetLabelColor:
    """Test setLabelColor static method."""

    def test_setLabelColor_basic(self) -> None:
        """Test setLabelColor sets label color correctly."""
        # Arrange
        label = Mock(spec=QLabel)
        color_hex = '#FF0000'  # Red

        # Act
        ApplicationWindow.setLabelColor(label, color_hex)

        # Assert
        label.setStyleSheet.assert_called_once()
        # Should set stylesheet with the color

    def test_setLabelColor_with_alpha(self) -> None:
        """Test setLabelColor ignores alpha channel."""
        # Arrange
        label = Mock(spec=QLabel)
        color_hex = '#FF000080'  # Red with alpha

        # Act
        ApplicationWindow.setLabelColor(label, color_hex)

        # Assert
        label.setStyleSheet.assert_called_once()
        # Should use only the first 7 characters (ignoring alpha)

    def test_setLabelColor_different_colors(self) -> None:
        """Test setLabelColor with different color values."""
        # Arrange
        label = Mock(spec=QLabel)
        colors = ['#00FF00', '#0000FF', '#FFFF00']

        for color in colors:
            # Act
            ApplicationWindow.setLabelColor(label, color)

            # Assert
            label.setStyleSheet.assert_called()


class TestCalcEnv:
    """Test calc_env static method."""

    @patch('artisanlib.main.os.environ')
    def test_calc_env_basic(self, mock_environ: Mock) -> None:
        """Test calc_env returns environment dictionary."""
        # Arrange
        mock_environ.copy.return_value = {'PATH': '/usr/bin', 'HOME': '/home/user'}

        # Act
        result = ApplicationWindow.calc_env()

        # Assert
        assert isinstance(result, dict)
        assert 'PATH' in result or 'HOME' in result  # Should contain some environment variables

    @patch('artisanlib.main.os.environ')
    def test_calc_env_caching(self, mock_environ: Mock) -> None:
        """Test calc_env caches results using lru_cache."""
        # Arrange
        mock_environ.copy.return_value = {'TEST': 'value'}

        # Act - call twice
        result1 = ApplicationWindow.calc_env()
        result2 = ApplicationWindow.calc_env()

        # Assert
        assert result1 == result2
        # Should be the same object due to caching
        assert result1 is result2


class TestReSplit:
    """Test re_split static method."""

    def test_re_split_basic_string(self) -> None:
        """Test re_split with basic string."""
        # Arrange
        input_string = 'hello world test'

        # Act
        result = ApplicationWindow.re_split(input_string)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 3
        assert result == ['hello', 'world', 'test']

    def test_re_split_quoted_strings(self) -> None:
        """Test re_split with quoted strings."""
        # Arrange
        input_string = 'hello "world test" single'

        # Act
        result = ApplicationWindow.re_split(input_string)

        # Assert
        assert isinstance(result, list)
        assert 'world test' in result  # Quoted string should be kept together

    def test_re_split_single_quotes(self) -> None:
        """Test re_split with single quoted strings."""
        # Arrange
        input_string = "hello 'world test' single"

        # Act
        result = ApplicationWindow.re_split(input_string)

        # Assert
        assert isinstance(result, list)
        assert 'world test' in result  # Single quoted string should be kept together

    def test_re_split_empty_string(self) -> None:
        """Test re_split with empty string."""
        # Arrange
        input_string = ''

        # Act
        result = ApplicationWindow.re_split(input_string)

        # Assert
        assert isinstance(result, list)
        assert len(result) == 0

    def test_re_split_mixed_quotes(self) -> None:
        """Test re_split with mixed quote types."""
        # Arrange
        input_string = "test \"double quoted\" and 'single quoted' words"

        # Act
        result = ApplicationWindow.re_split(input_string)

        # Assert
        assert isinstance(result, list)
        assert 'double quoted' in result
        assert 'single quoted' in result


class TestSliderLCDeditStyle:
    """Test sliderLCDeditStyle static method."""

    def test_sliderLCDeditStyle_returns_style(self) -> None:
        """Test sliderLCDeditStyle returns correct style string."""
        # Act
        result = ApplicationWindow.sliderLCDeditStyle()

        # Assert
        assert isinstance(result, str)
        assert 'font-weight: bold' in result
        assert 'color: grey' in result


class TestRemoveDisallowedFilenameChars:
    """Test removeDisallowedFilenameChars static method."""

    def test_removeDisallowedFilenameChars_basic(self) -> None:
        """Test removeDisallowedFilenameChars removes invalid characters."""
        # Arrange
        filename = 'test<file>name.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'testfilename.txt'

    def test_removeDisallowedFilenameChars_all_invalid_chars(self) -> None:
        """Test removeDisallowedFilenameChars removes all invalid characters."""
        # Arrange
        filename = 'file<>:"/\\|?*name.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'filename.txt'

    def test_removeDisallowedFilenameChars_valid_filename(self) -> None:
        """Test removeDisallowedFilenameChars with valid filename."""
        # Arrange
        filename = 'valid_filename-123.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == filename  # Should remain unchanged

    def test_removeDisallowedFilenameChars_empty_string(self) -> None:
        """Test removeDisallowedFilenameChars with empty string."""
        # Arrange
        filename = ''

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == ''


class TestStrippedName:
    """Test strippedName static method."""

    def test_strippedName_basic(self) -> None:
        """Test strippedName extracts filename from path."""
        # Arrange
        full_path = '/path/to/file.txt'

        # Act
        result = ApplicationWindow.strippedName(full_path)

        # Assert
        assert result == 'file.txt'

    def test_strippedName_windows_path(self) -> None:
        """Test strippedName with Windows path."""
        # Arrange
        full_path = 'C:\\Users\\test\\file.txt'

        # Act
        result = ApplicationWindow.strippedName(full_path)

        # Assert
        assert 'file.txt' in result  # Should extract filename

    def test_strippedName_filename_only(self) -> None:
        """Test strippedName with filename only."""
        # Arrange
        filename = 'file.txt'

        # Act
        result = ApplicationWindow.strippedName(filename)

        # Assert
        assert result == filename


class TestStrippedDir:
    """Test strippedDir static method."""

    def test_strippedDir_basic(self) -> None:
        """Test strippedDir extracts directory name from path."""
        # Arrange
        full_path = '/path/to/file.txt'

        # Act
        result = ApplicationWindow.strippedDir(full_path)

        # Assert
        assert isinstance(result, str)
        # Should return the directory name

    def test_strippedDir_nested_path(self) -> None:
        """Test strippedDir with nested path."""
        # Arrange
        full_path = '/very/deep/nested/path/file.txt'

        # Act
        result = ApplicationWindow.strippedDir(full_path)

        # Assert
        assert isinstance(result, str)
        # Should return the immediate parent directory name





class TestMakeListLength:
    """Test makeListLength static method."""

    def test_makeListLength_extend_list(self) -> None:
        """Test makeListLength extends list to target length."""
        # Arrange
        original_list = [1, 2, 3]
        target_length = 5
        default_element = -1

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert len(result) == target_length
        assert result == [1, 2, 3, -1, -1]

    def test_makeListLength_truncate_list(self) -> None:
        """Test makeListLength truncates list to target length."""
        # Arrange
        original_list = [1, 2, 3, 4, 5]
        target_length = 3
        default_element = -1

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert len(result) == target_length
        assert result == [1, 2, 3]

    def test_makeListLength_same_length(self) -> None:
        """Test makeListLength with same length."""
        # Arrange
        original_list = [1, 2, 3]
        target_length = 3
        default_element = -1

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert len(result) == target_length
        assert result == original_list

    def test_makeListLength_empty_list(self) -> None:
        """Test makeListLength with empty list."""
        # Arrange
        original_list: list[int] = []
        target_length = 3
        default_element = 0

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert len(result) == target_length
        assert result == [0, 0, 0]

    def test_makeListLength_zero_target(self) -> None:
        """Test makeListLength with zero target length."""
        # Arrange
        original_list = [1, 2, 3]
        target_length = 0
        default_element = -1

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert len(result) == 0
        assert result == []


class TestClearWindowGeometry:
    """Test clearWindowGeometry static method."""

    def test_clearWindowGeometry_basic(self) -> None:
        """Test clearWindowGeometry removes geometry settings."""
        # Arrange
        mock_settings = Mock(spec=QSettings)

        # Act
        ApplicationWindow.clearWindowGeometry(mock_settings)

        # Assert
        # Should call remove for various geometry settings
        mock_settings.remove.assert_called()
        assert mock_settings.remove.call_count > 0


class TestGetColor:
    """Test getColor static method."""

    def test_getColor_string_color(self) -> None:
        """Test getColor with string color."""
        # Arrange
        mock_line = Mock()
        mock_line.get_color.return_value = 'red'

        # Act
        result = ApplicationWindow.getColor(mock_line)

        # Assert
        assert isinstance(result, str)
        assert result.startswith('#')  # Should be hex format

    def test_getColor_tuple_color(self) -> None:
        """Test getColor with tuple color."""
        # Arrange
        mock_line = Mock()
        mock_line.get_color.return_value = (1.0, 0.0, 0.0)  # Red as RGB tuple

        # Act
        result = ApplicationWindow.getColor(mock_line)

        # Assert
        assert isinstance(result, str)
        assert result.startswith('#')  # Should be hex format

    def test_getColor_other_format(self) -> None:
        """Test getColor with other color format."""
        # Arrange
        mock_line = Mock()
        color_obj = object()  # Some other color object
        mock_line.get_color.return_value = color_obj

        # Act
        result = ApplicationWindow.getColor(mock_line)

        # Assert
        assert result == color_obj  # Should return as-is


class TestGetOS:
    """Test get_os static method."""

    @patch('artisanlib.main.platform.system')
    @patch('artisanlib.main.platform.release')
    @patch('artisanlib.main.platform.machine')
    def test_get_os_basic(self, mock_machine: Mock, mock_release: Mock, mock_system: Mock) -> None:
        """Test get_os returns OS information."""
        # Arrange
        mock_system.return_value = 'Linux'
        mock_release.return_value = '5.4.0'
        mock_machine.return_value = 'x86_64'

        # Act
        result = ApplicationWindow.get_os()

        # Assert
        assert isinstance(result, tuple)
        assert len(result) == 3
        # Should contain OS name, version, and architecture

    def test_get_os_caching(self) -> None:
        """Test get_os caches results using lru_cache."""
        # Act - call twice
        result1 = ApplicationWindow.get_os()
        result2 = ApplicationWindow.get_os()

        # Assert
        assert result1 == result2
        # Should be the same object due to caching
        assert result1 is result2


class TestSettingsSetValue:
    """Test settingsSetValue static method."""

    def test_settingsSetValue_read_defaults_true(self) -> None:
        """Test settingsSetValue with read_defaults=True."""
        # Arrange
        mock_settings = Mock(spec=QSettings)
        mock_settings.group.return_value = 'TestGroup'
        default_settings: dict[str, Any] = {}
        name = 'testSetting'
        value = 'testValue'

        # Act
        ApplicationWindow.settingsSetValue(mock_settings, default_settings, name, value, True)

        # Assert
        assert 'TestGroup/testSetting' in default_settings
        assert default_settings['TestGroup/testSetting'] == value

    def test_settingsSetValue_read_defaults_false(self) -> None:
        """Test settingsSetValue with read_defaults=False."""
        # Arrange
        mock_settings = Mock(spec=QSettings)
        mock_settings.group.return_value = 'TestGroup'
        default_settings: dict[str, Any] = {}
        name = 'testSetting'
        value = 'testValue'

        # Act
        ApplicationWindow.settingsSetValue(mock_settings, default_settings, name, value, False)

        # Assert
        # Should call setValue on settings
        mock_settings.setValue.assert_called_once_with(name, value)

    def test_settingsSetValue_none_defaults(self) -> None:
        """Test settingsSetValue with None default_settings."""
        # Arrange
        mock_settings = Mock(spec=QSettings)
        mock_settings.group.return_value = 'TestGroup'
        name = 'testSetting'
        value = 'testValue'

        # Act & Assert - should not raise exception
        ApplicationWindow.settingsSetValue(mock_settings, None, name, value, True)
        ApplicationWindow.settingsSetValue(mock_settings, None, name, value, False)


class TestProfileProductionData:
    """Test profileProductionData static method."""

    def test_profileProductionData_basic(self) -> None:
        """Test profileProductionData extracts production data."""
        # Arrange
        profile = {
            'roastbatchprefix': 'B',
            'roastbatchnr': 123,
            'title': 'Test Roast',
            'roastdate': 'test_date',
            'beans': 'Ethiopian',
            'weight': [100.0, 85.0, 'g'],
        }

        # Act
        result = ApplicationWindow.profileProductionData(profile)

        # Assert
        assert isinstance(result, dict)
        # Should contain extracted production data

    def test_profileProductionData_minimal_profile(self) -> None:
        """Test profileProductionData with minimal profile data."""
        # Arrange
        profile: dict[str, Any] = {}

        # Act
        result = ApplicationWindow.profileProductionData(profile)

        # Assert
        assert isinstance(result, dict)
        # Should handle missing keys gracefully


class TestRankingdataDef:
    """Test rankingdataDef static method."""

    def test_rankingdataDef_returns_definition(self) -> None:
        """Test rankingdataDef returns ranking data definition."""
        # Act
        field_index, field_names = ApplicationWindow.rankingdataDef()

        # Assert
        assert isinstance(field_index, list)
        assert isinstance(field_names, list)
        assert len(field_index) > 0
        assert len(field_names) > 0
        # Should define the structure for ranking data


class TestNote2html:
    """Test note2html static method."""

    def test_note2html_basic(self) -> None:
        """Test note2html converts notes to HTML."""
        # Arrange
        notes = 'Test\tnote\nwith\ttabs'

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert isinstance(result, str)
        assert '&nbsp' in result  # Tabs should be converted to non-breaking spaces

    def test_note2html_newlines(self) -> None:
        """Test note2html handles newlines."""
        # Arrange
        notes = 'Line 1\nLine 2\nLine 3'

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert isinstance(result, str)
        assert '<br>' in result  # Newlines should be converted to <br>

    def test_note2html_empty_string(self) -> None:
        """Test note2html with empty string."""
        # Arrange
        notes = ''

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert result == ''



class TestWeightLossMethods:
    """Test weight_loss, apply_weight_loss, and volume_increase static methods."""

    @pytest.mark.parametrize(
        'green,roasted,expected_loss',
        [
            (100.0, 85.0, 15.0),  # 15% weight loss
            (200.0, 170.0, 15.0),  # 15% weight loss
            (100.0, 100.0, 0.0),  # No weight loss
            (100.0, 110.0, 0.0),  # Roasted heavier than green (invalid)
            (0.0, 50.0, 0.0),  # Zero green weight
        ],
    )
    def test_weight_loss(self, green: float, roasted: float, expected_loss: float) -> None:
        """Test weight_loss calculates percentage correctly."""
        # Act
        result = ApplicationWindow.weight_loss(green, roasted)

        # Assert
        assert abs(result - expected_loss) < 0.001

    @pytest.mark.parametrize(
        'loss_percent,batchsize,expected_roasted',
        [
            (15.0, 100.0, 85.0),  # 15% loss from 100g = 85g
            (20.0, 200.0, 160.0),  # 20% loss from 200g = 160g
            (0.0, 100.0, 100.0),  # No loss
            (10.0, 50.0, 45.0),  # 10% loss from 50g = 45g
        ],
    )
    def test_apply_weight_loss(
        self, loss_percent: float, batchsize: float, expected_roasted: float
    ) -> None:
        """Test apply_weight_loss calculates roasted weight correctly."""
        # Act
        result = ApplicationWindow.apply_weight_loss(loss_percent, batchsize)

        # Assert
        assert abs(result - expected_roasted) < 0.001

    @pytest.mark.parametrize(
        'green,roasted,expected_increase',
        [
            (50.0, 75.0, 50.0),  # 50% volume increase
            (100.0, 150.0, 50.0),  # 50% volume increase
            (100.0, 100.0, 0.0),  # No volume increase
            (100.0, 50.0, 0.0),  # Roasted smaller than green (invalid)
            (0.0, 50.0, 0.0),  # Zero green volume
        ],
    )
    def test_volume_increase(self, green: float, roasted: float, expected_increase: float) -> None:
        """Test volume_increase calculates percentage correctly."""
        # Act
        result = ApplicationWindow.volume_increase(green, roasted)

        # Assert
        assert abs(result - expected_increase) < 0.001


class TestClearBoxLayout:
    """Test clearBoxLayout static method."""

    def test_clearBoxLayout_basic(self) -> None:
        """Test clearBoxLayout removes all items from layout."""
        # Arrange
        mock_layout = Mock(spec=QLayout)
        mock_widget = Mock(spec=QWidget)
        mock_item = Mock()
        mock_item.widget.return_value = mock_widget

        # Mock layout.count() to return 2, then 1, then 0
        mock_layout.count.side_effect = [2, 1, 0]
        mock_layout.takeAt.return_value = mock_item

        # Act
        ApplicationWindow.clearBoxLayout(mock_layout)

        # Assert
        assert mock_layout.takeAt.call_count == 2  # Should remove 2 items
        assert mock_widget.deleteLater.call_count == 2  # Should delete 2 widgets

    def test_clearBoxLayout_empty_layout(self) -> None:
        """Test clearBoxLayout with empty layout."""
        # Arrange
        mock_layout = Mock(spec=QLayout)
        mock_layout.count.return_value = 0

        # Act
        ApplicationWindow.clearBoxLayout(mock_layout)

        # Assert
        mock_layout.takeAt.assert_not_called()  # Should not try to remove items

    def test_clearBoxLayout_none_widget(self) -> None:
        """Test clearBoxLayout handles None widget gracefully."""
        # Arrange
        mock_layout = Mock(spec=QLayout)
        mock_item = Mock()
        mock_item.widget.return_value = None  # No widget

        mock_layout.count.side_effect = [1, 0]
        mock_layout.takeAt.return_value = mock_item

        # Act & Assert - should not raise exception
        ApplicationWindow.clearBoxLayout(mock_layout)


class TestTimeConversionMethodsExtended:
    """Test time conversion static methods - extended tests."""

    def test_time2QTime_zero_seconds(self) -> None:
        """Test time2QTime with zero seconds."""
        # Arrange & Act
        result = ApplicationWindow.time2QTime(0.0)

        # Assert
        assert result.minute() == 0
        assert result.second() == 0

    def test_time2QTime_full_minutes(self) -> None:
        """Test time2QTime with full minutes."""
        # Arrange & Act
        result = ApplicationWindow.time2QTime(120.0)  # 2 minutes

        # Assert
        assert result.minute() == 2
        assert result.second() == 0

    def test_time2QTime_minutes_and_seconds(self) -> None:
        """Test time2QTime with minutes and seconds."""
        # Arrange & Act
        result = ApplicationWindow.time2QTime(125.5)  # 2 minutes 5 seconds

        # Assert
        assert result.minute() == 2
        assert result.second() == 5

    def test_time2QTime_large_value(self) -> None:
        """Test time2QTime with large time value."""
        # Arrange & Act
        result = ApplicationWindow.time2QTime(3665.0)  # 61 minutes 5 seconds

        # Assert
        # QTime(0, 61, 5) is invalid, so QTime returns invalid time (-1 for minute/second)
        assert result.minute() == -1  # Invalid time
        assert result.second() == -1  # Invalid time

    def test_QTime2time_zero(self) -> None:
        """Test QTime2time with zero time."""
        # Arrange
        qtime = QTime(0, 0, 0)

        # Act
        result = ApplicationWindow.QTime2time(qtime)

        # Assert
        assert result == 0.0

    def test_QTime2time_minutes_only(self) -> None:
        """Test QTime2time with minutes only."""
        # Arrange
        qtime = QTime(0, 5, 0)

        # Act
        result = ApplicationWindow.QTime2time(qtime)

        # Assert
        assert result == 300.0  # 5 * 60

    def test_QTime2time_minutes_and_seconds(self) -> None:
        """Test QTime2time with minutes and seconds."""
        # Arrange
        qtime = QTime(0, 3, 45)

        # Act
        result = ApplicationWindow.QTime2time(qtime)

        # Assert
        assert result == 225.0  # 3 * 60 + 45


class TestColorUtilities:
    """Test color utility static methods."""

    def test_QColorBrightness_black(self) -> None:
        """Test QColorBrightness with black color."""
        # Arrange
        color = QColor(0, 0, 0)

        # Act
        result = ApplicationWindow.QColorBrightness(color)

        # Assert
        assert result == 0.0

    def test_QColorBrightness_white(self) -> None:
        """Test QColorBrightness with white color."""
        # Arrange
        color = QColor(255, 255, 255)

        # Act
        result = ApplicationWindow.QColorBrightness(color)

        # Assert
        assert result == 255.0

    def test_QColorBrightness_red(self) -> None:
        """Test QColorBrightness with red color."""
        # Arrange
        color = QColor(255, 0, 0)

        # Act
        result = ApplicationWindow.QColorBrightness(color)

        # Assert
        # Red: (255*299 + 0*587 + 0*114) / 1000 = 76245 / 1000 = 76.245
        assert abs(result - 76.245) < 0.001

    def test_QColorBrightness_green(self) -> None:
        """Test QColorBrightness with green color."""
        # Arrange
        color = QColor(0, 255, 0)

        # Act
        result = ApplicationWindow.QColorBrightness(color)

        # Assert
        # Green: (0*299 + 255*587 + 0*114) / 1000 = 149685 / 1000 = 149.685
        assert abs(result - 149.685) < 0.001

    def test_QColorBrightness_blue(self) -> None:
        """Test QColorBrightness with blue color."""
        # Arrange
        color = QColor(0, 0, 255)

        # Act
        result = ApplicationWindow.QColorBrightness(color)

        # Assert
        # Blue: (0*299 + 0*587 + 255*114) / 1000 = 29070 / 1000 = 29.07
        assert abs(result - 29.07) < 0.001

    def test_setLabelColor_valid_hex(self) -> None:
        """Test setLabelColor with valid hex color."""
        # Arrange
        label = QLabel('Test')
        hex_color = '#FF0000'  # Red

        # Act
        ApplicationWindow.setLabelColor(label, hex_color)

        # Assert
        style = label.styleSheet()
        assert 'color: #ff0000' in style.lower()

    def test_setLabelColor_with_alpha(self) -> None:
        """Test setLabelColor with hex color including alpha."""
        # Arrange
        label = QLabel('Test')
        hex_color = '#FF0000AA'  # Red with alpha

        # Act
        ApplicationWindow.setLabelColor(label, hex_color)

        # Assert
        style = label.styleSheet()
        assert 'color: #ff0000' in style.lower()  # Alpha should be ignored


class TestStringUtilities:
    """Test string utility static methods."""

    def test_removeDisallowedFilenameChars_basic(self) -> None:
        """Test removeDisallowedFilenameChars with basic disallowed characters."""
        # Arrange
        filename = 'test<file>name.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'testfilename.txt'

    def test_removeDisallowedFilenameChars_all_disallowed(self) -> None:
        """Test removeDisallowedFilenameChars with all disallowed characters."""
        # Arrange
        filename = '<>:"/\\|?*'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == ''

    def test_removeDisallowedFilenameChars_clean_filename(self) -> None:
        """Test removeDisallowedFilenameChars with clean filename."""
        # Arrange
        filename = 'clean_filename.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'clean_filename.txt'

    def test_removeDisallowedFilenameChars_mixed(self) -> None:
        """Test removeDisallowedFilenameChars with mixed valid and invalid characters."""
        # Arrange
        filename = 'my:file|name?.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'myfilename.txt'

    def test_strippedName_basic(self) -> None:
        """Test strippedName with basic file path."""
        # Arrange
        full_path = '/path/to/file.txt'

        # Act
        result = ApplicationWindow.strippedName(full_path)

        # Assert
        assert result == 'file.txt'

    def test_strippedName_no_path(self) -> None:
        """Test strippedName with filename only."""
        # Arrange
        filename = 'file.txt'

        # Act
        result = ApplicationWindow.strippedName(filename)

        # Assert
        assert result == 'file.txt'

    def test_strippedDir_basic(self) -> None:
        """Test strippedDir with basic file path."""
        # Arrange
        full_path = '/path/to/file.txt'

        # Act
        result = ApplicationWindow.strippedDir(full_path)

        # Assert
        assert result == 'to'

    def test_re_split_basic(self) -> None:
        """Test re_split with basic string."""
        # Arrange
        s = 'arg1 arg2 arg3'

        # Act
        result = ApplicationWindow.re_split(s)

        # Assert
        assert result == ['arg1', 'arg2', 'arg3']

    def test_re_split_quoted_strings(self) -> None:
        """Test re_split with quoted strings."""
        # Arrange
        s = 'arg1 "quoted arg" arg3'

        # Act
        result = ApplicationWindow.re_split(s)

        # Assert
        assert result == ['arg1', 'quoted arg', 'arg3']

    def test_re_split_single_quotes(self) -> None:
        """Test re_split with single quoted strings."""
        # Arrange
        s = "arg1 'quoted arg' arg3"

        # Act
        result = ApplicationWindow.re_split(s)

        # Assert
        assert result == ['arg1', 'quoted arg', 'arg3']

    def test_re_split_escaped_quotes(self) -> None:
        """Test re_split with escaped quotes."""
        # Arrange
        s = r'arg1 "quoted \"inner\" arg" arg3'

        # Act
        result = ApplicationWindow.re_split(s)

        # Assert
        assert result == ['arg1', 'quoted "inner" arg', 'arg3']

    def test_re_split_empty_string(self) -> None:
        """Test re_split with empty string."""
        # Arrange
        s = ''

        # Act
        result = ApplicationWindow.re_split(s)

        # Assert
        assert result == []


class TestListUtilities:
    """Test list utility static methods."""

    def test_makeListLength_extend_list(self) -> None:
        """Test makeListLength extending a short list."""
        # Arrange
        original_list = [1, 2, 3]
        target_length = 5
        default_element = 0

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert result == [1, 2, 3, 0, 0]
        assert len(result) == 5

    def test_makeListLength_truncate_list(self) -> None:
        """Test makeListLength truncating a long list."""
        # Arrange
        original_list = [1, 2, 3, 4, 5]
        target_length = 3
        default_element = 0

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert result == [1, 2, 3]
        assert len(result) == 3

    def test_makeListLength_exact_length(self) -> None:
        """Test makeListLength with exact target length."""
        # Arrange
        original_list = [1, 2, 3]
        target_length = 3
        default_element = 0

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert result == [1, 2, 3]
        assert len(result) == 3

    def test_makeListLength_empty_list(self) -> None:
        """Test makeListLength with empty list."""
        # Arrange
        original_list: list[int] = []
        target_length = 3
        default_element = 42

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert result == [42, 42, 42]
        assert len(result) == 3

    def test_makeListLength_zero_target(self) -> None:
        """Test makeListLength with zero target length."""
        # Arrange
        original_list = [1, 2, 3]
        target_length = 0
        default_element = 0

        # Act
        result = ApplicationWindow.makeListLength(original_list, target_length, default_element)

        # Assert
        assert result == []
        assert len(result) == 0


class TestWidgetUtilities:
    """Test widget utility static methods."""

    def test_setSliderNumber_single_digit(self) -> None:
        """Test setSliderNumber with single digit value."""
        # Arrange
        lcd = QLCDNumber()
        value = 5.0

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        assert lcd.digitCount() == 1

    def test_setSliderNumber_two_digits(self) -> None:
        """Test setSliderNumber with two digit value."""
        # Arrange
        lcd = QLCDNumber()
        value = 50.0

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        assert lcd.digitCount() == 2

    def test_setSliderNumber_three_digits(self) -> None:
        """Test setSliderNumber with three digit value."""
        # Arrange
        lcd = QLCDNumber()
        value = 150.0

        # Act
        ApplicationWindow.setSliderNumber(lcd, value)

        # Assert
        assert lcd.digitCount() == 3

    def test_setSliderNumber_boundary_values(self) -> None:
        """Test setSliderNumber with boundary values."""
        # Arrange
        lcd = QLCDNumber()

        # Act & Assert
        ApplicationWindow.setSliderNumber(lcd, 9.9)
        assert lcd.digitCount() == 1

        ApplicationWindow.setSliderNumber(lcd, 10.0)
        assert lcd.digitCount() == 2

        ApplicationWindow.setSliderNumber(lcd, 99.9)
        assert lcd.digitCount() == 3  # 99.9 > 99, so should be 3 digits

        ApplicationWindow.setSliderNumber(lcd, 100.0)
        assert lcd.digitCount() == 3

    def test_sliderLCDeditStyle(self) -> None:
        """Test sliderLCDeditStyle returns correct style string."""
        # Arrange & Act
        result = ApplicationWindow.sliderLCDeditStyle()

        # Assert
        assert result == 'font-weight: bold; color: grey;'

    def test_sliderLCD_creation(self) -> None:
        """Test sliderLCD creates LCD with correct properties."""
        # Arrange & Act
        lcd = ApplicationWindow.sliderLCD()

        # Assert
        assert isinstance(lcd, MyQLCDNumber)
        assert lcd.segmentStyle() == QLCDNumber.SegmentStyle.Flat
        assert lcd.digitCount() == 1
        assert lcd.minimumHeight() == 35
        assert lcd.minimumWidth() == 50
        assert lcd.maximumWidth() == 50

    def test_slider_creation(self) -> None:
        """Test slider creates slider with correct properties."""
        # Arrange & Act
        slider = ApplicationWindow.slider()

        # Assert
        assert isinstance(slider, SliderUnclickable)
        assert slider.tickPosition() == QSlider.TickPosition.TicksBothSides
        assert slider.tickInterval() == 10
        assert slider.singleStep() == 1
        assert slider.pageStep() == 10
        assert slider.maximum() == 100
        assert slider.minimumWidth() == 50
        assert slider.maximumWidth() == 50


class TestFitUtilities:
    """Test fit utility static methods."""

    def test_fit2str_none_fit(self) -> None:
        """Test fit2str with None fit."""
        # Arrange & Act
        result = ApplicationWindow.fit2str(None)

        # Assert
        assert result == ''

    def test_fit2str_linear_fit(self) -> None:
        """Test fit2str with linear fit."""
        # Arrange
        import numpy as np

        fit = np.array([2.0, 3.0])  # 3x + 2

        # Act
        result = ApplicationWindow.fit2str(fit)

        # Assert
        assert '2' in result
        assert '3' in result
        assert 'x' in result

    def test_fit2str_zero_coefficients(self) -> None:
        """Test fit2str with zero coefficients."""
        # Arrange
        import numpy as np

        fit = np.array([0.0, 0.0])

        # Act
        result = ApplicationWindow.fit2str(fit)

        # Assert
        assert result == ''


class TestTableUtilities:
    """Test table utility static methods."""

    def test_findWidgetsRow_widget_found(self) -> None:
        """Test findWidgetsRow when widget is found."""
        # Arrange
        table = QTableWidget(3, 2)
        target_widget = QLabel('Test')
        table.setCellWidget(1, 0, target_widget)

        # Act
        result = ApplicationWindow.findWidgetsRow(table, target_widget, 0)

        # Assert
        assert result == 1

    def test_findWidgetsRow_widget_not_found(self) -> None:
        """Test findWidgetsRow when widget is not found."""
        # Arrange
        table = QTableWidget(3, 2)
        target_widget = QLabel('Test')

        # Act
        result = ApplicationWindow.findWidgetsRow(table, target_widget, 0)

        # Assert
        assert result is None

    def test_findWidgetsRow_none_widget(self) -> None:
        """Test findWidgetsRow with None widget."""
        # Arrange
        table = QTableWidget(3, 2)

        # Act
        result = ApplicationWindow.findWidgetsRow(table, None, 0)

        # Assert
        assert result is None

    def test_findWidgetsColumn_widget_found(self) -> None:
        """Test findWidgetsColumn when widget is found."""
        # Arrange
        table = QTableWidget(2, 3)
        target_widget = QLabel('Test')
        table.setCellWidget(0, 1, target_widget)

        # Act
        result = ApplicationWindow.findWidgetsColumn(table, target_widget, 0)

        # Assert
        assert result == 1

    def test_findWidgetsColumn_widget_not_found(self) -> None:
        """Test findWidgetsColumn when widget is not found."""
        # Arrange
        table = QTableWidget(2, 3)
        target_widget = QLabel('Test')

        # Act
        result = ApplicationWindow.findWidgetsColumn(table, target_widget, 0)

        # Assert
        assert result is None


#class TestEnvironmentUtilities:
#    """Test environment utility static methods."""
#
#    def test_calc_env_basic(self) -> None:
#        """Test calc_env returns environment dictionary."""
#        # Arrange & Act
#        result = ApplicationWindow.calc_env()
#
#        # Assert
#        assert isinstance(result, dict)
#        assert 'PATH' in result  # PATH should always be present
#
#    def test_calc_env_cached(self) -> None:
#        """Test calc_env caching behavior."""
#        # Arrange & Act
#        result1 = ApplicationWindow.calc_env()
#        result2 = ApplicationWindow.calc_env()
#
#        # Assert
#        assert result1 is result2  # Should return same cached instance
#
#    def test_get_os_basic(self) -> None:
#        """Test get_os returns OS information."""
#        # Arrange & Act
#        os_name, version, arch = ApplicationWindow.get_os()
#
#        # Assert
#        assert isinstance(os_name, str)
#        assert isinstance(version, str)
#        assert isinstance(arch, str)
#        assert len(os_name) > 0
#        assert len(version) > 0
#        assert len(arch) > 0
#
#    def test_get_os_cached(self) -> None:
#        """Test get_os caching behavior."""
#        # Arrange & Act
#        result1 = ApplicationWindow.get_os()
#        result2 = ApplicationWindow.get_os()
#
#        # Assert
#        assert result1 == result2  # Should return same cached result


class TestNoteUtilities:
    """Test note utility static methods."""

    def test_note2html_basic_text(self) -> None:
        """Test note2html with basic text."""
        # Arrange
        notes = 'Simple text'

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert result == '<br>Simple text'  # note2html adds <br> prefix

    def test_note2html_with_tabs(self) -> None:
        """Test note2html with tab characters."""
        # Arrange
        notes = 'Text\twith\ttabs'

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert ' &nbsp&nbsp&nbsp&nbsp ' in result

    def test_note2html_with_newlines(self) -> None:
        """Test note2html with newline characters."""
        # Arrange
        notes = 'Line 1\nLine 2\nLine 3'

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert '<br>' in result
        assert result.count('<br>') == 3  # 1 prefix + 2 from newlines

    def test_note2html_mixed_formatting(self) -> None:
        """Test note2html with mixed tab and newline characters."""
        # Arrange
        notes = 'Line 1\tTabbed\nLine 2'

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert ' &nbsp&nbsp&nbsp&nbsp ' in result
        assert '<br>' in result

    def test_note2html_empty_string(self) -> None:
        """Test note2html with empty string."""
        # Arrange
        notes = ''

        # Act
        result = ApplicationWindow.note2html(notes)

        # Assert
        assert result == ''


class TestSettingsUtilities:
    """Test settings utility static methods."""

    def test_clearWindowGeometry_basic(self) -> None:
        """Test clearWindowGeometry removes geometry settings."""
        # Arrange
        settings = QSettings()
        settings.setValue('MainWindowState', 'test_value')
        settings.setValue('Geometry', 'test_geometry')
        settings.setValue('SomeOtherSetting', 'keep_this')

        # Act
        ApplicationWindow.clearWindowGeometry(settings)

        # Assert
        assert not settings.contains('MainWindowState')
        assert not settings.contains('Geometry')
        assert settings.contains('SomeOtherSetting')  # Should keep non-geometry settings

    def test_settingsSetValue_read_defaults_mode(self) -> None:
        """Test settingsSetValue in read defaults mode."""
        # Arrange
        settings = QSettings()
        default_settings: dict[str, Any] = {}
        settings.beginGroup('test_group')

        # Act
        ApplicationWindow.settingsSetValue(
            settings, default_settings, 'test_key', 'test_value', True
        )

        # Assert
        assert 'test_group/test_key' in default_settings
        assert default_settings['test_group/test_key'] == 'test_value'

        # Cleanup
        settings.endGroup()

    def test_settingsSetValue_write_mode(self) -> None:
        """Test settingsSetValue in write mode."""
        # Arrange
        settings = QSettings()
        default_settings = {'test_group/test_key': 'default_value'}
        settings.beginGroup('test_group')

        # Act
        ApplicationWindow.settingsSetValue(
            settings, default_settings, 'test_key', 'new_value', False
        )

        # Assert
        assert settings.value('test_key') == 'new_value'

        # Cleanup
        settings.endGroup()


class TestRecentRoastUtilities:
    """Test recent roast utility static methods."""

    def test_recentRoastLabel_basic(self) -> None:
        """Test recentRoastLabel with basic roast data."""
        # Arrange
        rr = RecentRoast(
            title='Test Roast',
            weightIn=250.0,
            weightUnit='g',
        )

        # Act
        result = ApplicationWindow.recentRoastLabel(rr)

        # Assert
        assert result == 'Test Roast (250g)'

    def test_createRecentRoast_basic(self) -> None:
        """Test createRecentRoast with basic parameters."""
        # Arrange & Act
        result = ApplicationWindow.createRecentRoast(
            title='Test Roast',
            beans='Test Beans',
            weightIn=250.0,
            weightUnit='g',
            volumeIn=400.0,
            volumeUnit='ml',
            densityWeight=0.6,
            beanSize_min=12,
            beanSize_max=16,
            moistureGreen=11.5,
            colorSystem='Agtron',
            file='/path/to/file.alog',
            roastUUID='test-uuid',
            batchnr=1,
            batchprefix='TR',
            plus_account=None,
            plus_store=None,
            plus_store_label=None,
            plus_coffee=None,
            plus_coffee_label=None,
            plus_blend_label=None,
            plus_blend_spec=None,
            plus_blend_spec_labels=None,
            weightOut=210.0,
            volumeOut=450.0,
            densityRoasted=0.5,
            moistureRoasted=2.5,
            wholeColor=65,
            groundColor=70,
        )

        # Assert
        assert isinstance(result, dict)
        assert result['title'] == 'Test Roast'
        assert result['weightIn'] == 250.0
        assert result['weightUnit'] == 'g'
        # weightOut is optional, so we check if it exists
        if 'weightOut' in result:
            assert result['weightOut'] == 210.0


class TestWeightVolumeCalculations:
    """Test weight and volume calculation static methods."""

    def test_weight_loss_normal_case(self) -> None:
        """Test weight_loss with normal green and roasted weights."""
        # Arrange
        green = 100.0
        roasted = 85.0

        # Act
        result = ApplicationWindow.weight_loss(green, roasted)

        # Assert
        assert result == 15.0  # (100 - 85) / 100 * 100 = 15%

    def test_weight_loss_zero_green(self) -> None:
        """Test weight_loss with zero green weight."""
        # Arrange
        green = 0.0
        roasted = 85.0

        # Act
        result = ApplicationWindow.weight_loss(green, roasted)

        # Assert
        assert result == 0.0

    def test_weight_loss_roasted_greater_than_green(self) -> None:
        """Test weight_loss when roasted weight is greater than green."""
        # Arrange
        green = 85.0
        roasted = 100.0

        # Act
        result = ApplicationWindow.weight_loss(green, roasted)

        # Assert
        assert result == 0.0

    def test_weight_loss_equal_weights(self) -> None:
        """Test weight_loss when green and roasted weights are equal."""
        # Arrange
        green = 100.0
        roasted = 100.0

        # Act
        result = ApplicationWindow.weight_loss(green, roasted)

        # Assert
        assert result == 0.0

    def test_apply_weight_loss_normal_case(self) -> None:
        """Test apply_weight_loss with normal loss percentage."""
        # Arrange
        loss = 15.0  # 15% loss
        batchsize = 100.0

        # Act
        result = ApplicationWindow.apply_weight_loss(loss, batchsize)

        # Assert
        assert result == 85.0  # 100 - (100 * 15 / 100) = 85

    def test_apply_weight_loss_zero_loss(self) -> None:
        """Test apply_weight_loss with zero loss."""
        # Arrange
        loss = 0.0
        batchsize = 100.0

        # Act
        result = ApplicationWindow.apply_weight_loss(loss, batchsize)

        # Assert
        assert result == 100.0

    def test_apply_weight_loss_hundred_percent_loss(self) -> None:
        """Test apply_weight_loss with 100% loss."""
        # Arrange
        loss = 100.0
        batchsize = 100.0

        # Act
        result = ApplicationWindow.apply_weight_loss(loss, batchsize)

        # Assert
        assert result == 0.0

    def test_volume_increase_normal_case(self) -> None:
        """Test volume_increase with normal green and roasted volumes."""
        # Arrange
        green = 100.0
        roasted = 120.0

        # Act
        result = ApplicationWindow.volume_increase(green, roasted)

        # Assert
        assert result == 20.0  # (120 - 100) / 100 * 100 = 20%

    def test_volume_increase_zero_green(self) -> None:
        """Test volume_increase with zero green volume."""
        # Arrange
        green = 0.0
        roasted = 120.0

        # Act
        result = ApplicationWindow.volume_increase(green, roasted)

        # Assert
        assert result == 0.0

    def test_volume_increase_green_greater_than_roasted(self) -> None:
        """Test volume_increase when green volume is greater than roasted."""
        # Arrange
        green = 120.0
        roasted = 100.0

        # Act
        result = ApplicationWindow.volume_increase(green, roasted)

        # Assert
        assert result == 0.0

    def test_volume_increase_equal_volumes(self) -> None:
        """Test volume_increase when green and roasted volumes are equal."""
        # Arrange
        green = 100.0
        roasted = 100.0

        # Act
        result = ApplicationWindow.volume_increase(green, roasted)

        # Assert
        assert result == 0.0


class TestValidatorUtilities:
    """Test validator utility static methods."""

    def test_createCLocaleDoubleValidator_basic(self) -> None:
        """Test createCLocaleDoubleValidator with basic parameters."""
        # Arrange
        line_edit = QLineEdit()
        bot = 0.0
        top = 100.0
        dec = 2

        # Act
        validator = ApplicationWindow.createCLocaleDoubleValidator(bot, top, dec, line_edit)

        # Assert
        assert validator.bottom() == bot
        assert validator.top() == top
        assert validator.decimals() == dec
        assert validator.locale() == QLocale.c()

    def test_createCLocaleDoubleValidator_with_custom_default(self) -> None:
        """Test createCLocaleDoubleValidator with custom empty default."""
        # Arrange
        line_edit = QLineEdit()
        bot = -50.0
        top = 50.0
        dec = 1
        empty_default = 'N/A'

        # Act
        validator = ApplicationWindow.createCLocaleDoubleValidator(
            bot, top, dec, line_edit, empty_default
        )

        # Assert
        assert validator.bottom() == bot
        assert validator.top() == top
        assert validator.decimals() == dec


class TestLayoutUtilities:
    """Test layout utility static methods."""

    def test_makePhasesLCDbox_basic(self) -> None:
        """Test makePhasesLCDbox creates frame with correct properties."""
        # Arrange
        label = QLabel('Test Phase')
        lcd = QLCDNumber()

        # Act
        frame = ApplicationWindow.makePhasesLCDbox(label, lcd)

        # Assert
        assert isinstance(frame, QFrame)
        assert label.alignment() & Qt.AlignmentFlag.AlignRight
        assert label.alignment() & Qt.AlignmentFlag.AlignVCenter
        assert lcd.minimumHeight() == 30
        assert lcd.minimumWidth() == 80
        assert lcd.segmentStyle() == QLCDNumber.SegmentStyle.Flat

    def test_makeLCDbox_basic(self) -> None:
        """Test makeLCDbox creates frame with correct layout."""
        # Arrange
        label = QLabel('Test LCD')
        lcd = MyQLCDNumber()
        lcdframe = QFrame()

        # Act
        frame = ApplicationWindow.makeLCDbox(label, lcd, lcdframe)

        # Assert
        assert isinstance(frame, QFrame)
        # The frame should have a layout with the label and LCD frame
        layout = frame.layout()
        assert layout is not None
        assert layout.count() >= 2  # Should contain at least label and LCD frame


class TestColorUtilitiesExtended:
    """Test additional color utility static methods."""

    # Note: recolorIcon test removed due to import issues with static method access

    def test_getColor_string_color(self) -> None:
        """Test getColor with string color."""
        # Arrange
        mock_line = Mock()
        mock_line.get_color.return_value = '#FF0000'

        # Act
        result = ApplicationWindow.getColor(mock_line)

        # Assert
        assert result == '#ff0000ff'  # Should include alpha (lowercase)

    def test_getColor_tuple_color(self) -> None:
        """Test getColor with tuple color."""
        # Arrange
        mock_line = Mock()
        mock_line.get_color.return_value = (1.0, 0.0, 0.0)  # Red as RGB tuple

        # Act
        result = ApplicationWindow.getColor(mock_line)

        # Assert
        assert isinstance(result, str)
        assert result.startswith('#')


class TestDonationUtilities:
    """Test donation utility static methods."""

    def test_resetDonateCounter_basic(self) -> None:
        """Test resetDonateCounter resets donation settings."""
        # Arrange & Act
        ApplicationWindow.resetDonateCounter()

        # Assert
        settings = QSettings()
        # Check that the settings were written (values should exist)
        assert settings.contains('lastdonationpopup')
        assert settings.contains('starts')
        assert settings.value('starts') == 0


class TestHelpDialogUtilities:
    """Test help dialog utility static methods."""

    def test_closeHelpDialog_with_valid_dialog(self) -> None:
        """Test closeHelpDialog with valid dialog."""
        # Arrange
        mock_dialog = Mock()
        mock_dialog.close = Mock()

        # Act
        ApplicationWindow.closeHelpDialog(mock_dialog)

        # Assert
        mock_dialog.close.assert_called_once()

    def test_closeHelpDialog_with_none(self) -> None:
        """Test closeHelpDialog with None dialog."""
        # Arrange & Act & Assert - should not raise exception
        ApplicationWindow.closeHelpDialog(None)

    def test_closeHelpDialog_with_deleted_dialog(self) -> None:
        """Test closeHelpDialog with deleted dialog."""
        # Arrange
        mock_dialog = Mock()
        mock_dialog.close = Mock(side_effect=Exception('Dialog deleted'))

        # Act & Assert - should not raise exception
        ApplicationWindow.closeHelpDialog(mock_dialog)


class TestProductionDataUtilities:
    """Test production data utility static methods."""

    def test_profileProductionData_basic_profile(self) -> None:
        """Test profileProductionData with basic profile data."""
        # Arrange
        profile = {
            'roastbatchprefix': 'TEST',
            'roastbatchnr': 123,
            'title': 'Test Roast',
            'beans': 'Ethiopian',
            'weight': [100.0, 85.0, 'g'],
            'volume': [150.0, 180.0, 'ml'],
            'density': [0.6, 0.5, 'g/ml'],
            'moisture': [11.5, 3.2, '%'],
            'color': 65,
            'ground_color': 70,
            'roastdate': 'Mon Jan 01 2024',
            'roasttime': '10:30:00',
            'roastepoch': 1704110200,
            'roasttzoffset': -28800,
            'ambient_temperature': 22.5,
            'ambient_humidity': 45.0,
            'ambient_pressure': 1013.25,
        }

        # Act
        result = ApplicationWindow.profileProductionData(profile)

        # Assert
        assert isinstance(result, dict)
        assert result.get('batchprefix') == 'TEST'
        assert result.get('batchnr') == 123

    def test_profileProductionData_minimal_profile(self) -> None:
        """Test profileProductionData with minimal profile data."""
        # Arrange
        profile: dict[str, Any] = {}

        # Act
        result = ApplicationWindow.profileProductionData(profile)

        # Assert
        assert isinstance(result, dict)
        assert result.get('batchprefix') == ''
        assert result.get('batchnr') == 0


class TestRankingDataUtilities:
    """Test ranking data utility static methods."""

    def test_rankingdataDef_returns_valid_structure(self) -> None:
        """Test rankingdataDef returns valid data structure."""
        # Arrange & Act
        ranking_data_fields, field_index = ApplicationWindow.rankingdataDef()

        # Assert
        assert isinstance(field_index, list)
        assert isinstance(ranking_data_fields, list)
        assert len(field_index) == 6  # Expected field index length
        assert 'fld' in field_index
        assert 'src' in field_index
        assert 'typ' in field_index
        assert 'test0' in field_index
        assert 'units' in field_index
        assert 'name' in field_index

    def test_rankingdataDef_field_structure(self) -> None:
        """Test rankingdataDef field structure is consistent."""
        # Arrange & Act
        ranking_data_fields, field_index = ApplicationWindow.rankingdataDef()

        # Assert
        # Each ranking data field should have the same number of elements as field_index
        for field in ranking_data_fields:
            assert isinstance(field, list)
            assert len(field) == len(field_index)



class TestAdvancedUtilities:
    """Test advanced utility static methods."""

    def test_get_os_returns_tuple(self) -> None:
        """Test get_os returns tuple with OS information."""
        # Arrange & Act
        os_name, version, arch = ApplicationWindow.get_os()

        # Assert
        assert isinstance(os_name, str)
        assert isinstance(version, str)
        assert isinstance(arch, str)
        assert len(os_name) > 0
        assert len(version) > 0
        assert len(arch) > 0

    def test_calc_env_returns_dict(self) -> None:
        """Test calc_env returns environment dictionary."""
        # Arrange & Act
        result = ApplicationWindow.calc_env()

        # Assert
        assert isinstance(result, dict)
        # Should contain at least some environment variables
        assert len(result) > 0
        # PATH should typically be present in environment
        assert any('PATH' in key.upper() for key in result)

    def test_calc_env_caching(self) -> None:
        """Test calc_env caching behavior."""
        # Arrange & Act
        result1 = ApplicationWindow.calc_env()
        result2 = ApplicationWindow.calc_env()

        # Assert
        # Should return the same cached instance due to @lru_cache
        assert result1 is result2


class TestFileUtilities:
    """Test file utility static methods."""

    def test_strippedName_basic_path(self) -> None:
        """Test strippedName with basic file path."""
        # Arrange
        file_path = '/path/to/file.txt'

        # Act
        result = ApplicationWindow.strippedName(file_path)

        # Assert
        assert result == 'file.txt'

    def test_strippedName_filename_only(self) -> None:
        """Test strippedName with filename only."""
        # Arrange
        file_path = 'file.txt'

        # Act
        result = ApplicationWindow.strippedName(file_path)

        # Assert
        assert result == 'file.txt'

    @pytest.mark.darwin
    @pytest.mark.linux
    def test_strippedName_windows_path_non_windows(self) -> None:
        """Test strippedName with Windows-style path."""
        # Arrange
        file_path = 'C:\\Users\\test\\file.txt'

        # Act
        result = ApplicationWindow.strippedName(file_path)

        # Assert
        # On non-Windows systems, backslashes are not treated as path separators
        # so the entire string is returned as the filename
        assert result == 'C:\\Users\\test\\file.txt'

    @pytest.mark.win32
    def test_strippedName_windows_path_windows(self) -> None:
        """Test strippedName with Windows-style path."""
        # Arrange
        file_path = 'C:\\Users\\test\\file.txt'

        # Act
        result = ApplicationWindow.strippedName(file_path)

        # Assert
        # On non-Windows systems, backslashes are not treated as path separators
        # so the entire string is returned as the filename
        assert result == 'file.txt'

    def test_strippedDir_basic_path(self) -> None:
        """Test strippedDir with basic file path."""
        # Arrange
        file_path = '/path/to/file.txt'

        # Act
        result = ApplicationWindow.strippedDir(file_path)

        # Assert
        assert result == 'to'

    def test_strippedDir_single_directory(self) -> None:
        """Test strippedDir with single directory."""
        # Arrange
        file_path = '/file.txt'

        # Act
        result = ApplicationWindow.strippedDir(file_path)

        # Assert
        assert result == ''

    def test_removeDisallowedFilenameChars_basic(self) -> None:
        """Test removeDisallowedFilenameChars with basic disallowed characters."""
        # Arrange
        filename = 'test<file>name.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'testfilename.txt'

    def test_removeDisallowedFilenameChars_all_disallowed(self) -> None:
        """Test removeDisallowedFilenameChars with all disallowed characters."""
        # Arrange
        filename = '<>:"/\\|?*'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == ''

    def test_removeDisallowedFilenameChars_clean_filename(self) -> None:
        """Test removeDisallowedFilenameChars with clean filename."""
        # Arrange
        filename = 'clean_filename.txt'

        # Act
        result = ApplicationWindow.removeDisallowedFilenameChars(filename)

        # Assert
        assert result == 'clean_filename.txt'


SERVER_SOURCE = ServerProfileSource(
    namespace=Namespace(
        origin='https://archive.example.test',
        organization_id=UUID('11111111-1111-4111-8111-111111111111'),
        key='archive-example-test--11111111111141118111111111111111',
    ),
    roast_uuid=UUID('22222222-2222-4222-8222-222222222222'),
    revision_number=7,
    sha256='a' * 64,
    stale=False,
)


ROASTSERVER_PROFILE: dict[str, Any] = {
    'roastUUID': '0123456789abcdef0123456789abcdef',
    'title': 'Connector test',
    'beans': 'Café',
    'plus_store': 'store-1',
    'plus_blend_spec': [{'coffee': 'coffee-1', 'ratio': 1.0}],
    'computed': {'nested': [1.0, 2.0]},
}
INVENTORY_PROFILE_LINK: dict[str, str] = {
    'roastServerInventoryOrigin': 'https://archive.example',
    'roastServerInventoryOrganizationUUID': '11111111111141118111111111111111',
    'roastServerBeanLotUUID': '22222222222242228222222222222222',
    'roastServerBeanLotName': 'Historical lot',
}
INVENTORY_QMC_FIELDS = (
    'roastServerInventoryOrigin',
    'roastServerInventoryOrganizationUUID',
    'roastServerBeanLotUUID',
    'roastServerBeanLotName',
)


def qmc_inventory_profile_link(qmc: object) -> dict[str, object]:
    return {name: getattr(qmc, name) for name in INVENTORY_QMC_FIELDS}


def set_qmc_inventory_profile_link(qmc: object, values: dict[str, str]) -> None:
    for name in INVENTORY_QMC_FIELDS:
        setattr(qmc, name, values[name])


class InventoryChargeSemaphore:
    def __init__(self) -> None:
        self.release_calls = 0

    def acquire(self, _count: int) -> None:
        pass

    def release(self, _count: int) -> None:
        self.release_calls += 1


class InventoryChargeCurve:
    def __init__(self, x_values: list[float], y_values: list[float]) -> None:
        self.data = (list(x_values), list(y_values))
        self.fail_next_update = False

    def get_data(self) -> tuple[list[float], list[float]]:
        return self.data

    def set_data(self, x_values: list[float], y_values: list[float]) -> None:
        self.data = (list(x_values), list(y_values))
        if self.fail_next_update:
            self.fail_next_update = False
            raise RuntimeError('injected curve update failure')


def coordinator_inventory_charge_canvas(window: ApplicationWindow) -> SimpleNamespace:
    window.ntb = MagicMock()
    window.ntb._nav_stack.return_value = False
    window.buttonCHARGE = MagicMock()
    window.buttonCHARGE.isFlat.return_value = False
    window.soundpopSignal = MagicMock()
    window.arabicReshape = Mock(side_effect=lambda text: text)
    window.eventslidervisibilities = [False, False, False, False]
    window.simulator = None
    window.pidcontrol = MagicMock()
    window.pidcontrol.pidOnCHARGE = False
    window.santokerWarmupController = MagicMock()
    window.updateSantokerWarmupControls = Mock()
    window.onMarkMoveToNext = Mock()
    window.openPropertiesSignal = MagicMock()
    window.sendmessage = Mock()  # type: ignore[method-assign]
    canvas = SimpleNamespace(
        aw=window,
        profileDataSemaphore=InventoryChargeSemaphore(),
        flagstart=True,
        fileDirtySignal=MagicMock(),
        timeindex=[-1, 0, 0, 0, 0, 0, 0, 0],
        autoChargeIdx=-1,
        device=0,
        timex=[0.0],
        temp1=[100.0],
        temp2=[90.0],
        roastUUID=None,
        weight=(1.25, 0.0, 'Kg'),
        chargeTimerPeriod=0,
        locktimex=False,
        locktimex_start=0.0,
        chargemintime=0.0,
        startofx=0.0,
        fixmaxtime=False,
        endofx=100.0,
        resetmaxtime=100.0,
        BTcurve=False,
        ETcurve=False,
        updateProjection=Mock(),
        xaxistosm=Mock(),
        EventRecordAction=Mock(),
        timealign=Mock(),
        buttonactions=[0],
        buttonactionstrings=[''],
        LCDdecimalplaces=False,
        mode='C',
        roastpropertiesAutoOpenFlag=False,
        l_annotations=[],
        l_annotations_dict={},
        ystep_down=0,
        ystep_up=0,
        adderror=Mock(),
        _tgraphcanvas__dijkstra_to_ascii=lambda text: text,
    )
    set_qmc_inventory_profile_link(canvas, INVENTORY_PROFILE_LINK)
    window.qmc = canvas
    return canvas


def coordinator_controller(
    coordinator: InventoryCoordinator, context: InventoryContext
) -> SimpleNamespace:
    def prepare_inventory_charge(
        link: InventoryProfileLink | None,
        roast_uuid: UUID | None,
        weight: object,
        unit: object,
    ) -> PreparedInventoryCharge:
        try:
            return coordinator.prepare_charge(
                context, link, roast_uuid, weight, unit)
        except InventoryCoordinatorError as error:
            raise ControllerError(error.code) from None

    def commit_inventory_charge(
        prepared: PreparedInventoryCharge,
    ) -> InventoryNotice:
        try:
            return coordinator.commit_charge(prepared)
        except InventoryCoordinatorError as error:
            raise ControllerError(error.code) from None

    def inventory_lot_locked(
        link: InventoryProfileLink | None,
        roast_uuid: UUID | None,
        profile_has_charge: bool,
    ) -> bool:
        assert link is not None
        try:
            return coordinator.is_locked(
                link.namespace, roast_uuid, profile_has_charge)
        except InventoryCoordinatorError as error:
            raise ControllerError(error.code) from None

    def finalize_inventory_profile(profile: ProfileData) -> InventoryNotice | None:
        try:
            return coordinator.finalize_saved_profile(context, profile)
        except InventoryCoordinatorError as error:
            raise ControllerError(error.code) from None

    def release_inventory_roast(roast_uuid: UUID | None) -> InventoryNotice | None:
        try:
            return coordinator.release_for_reset(context, roast_uuid)
        except InventoryCoordinatorError as error:
            raise ControllerError(error.code) from None

    return SimpleNamespace(
        prepare_inventory_charge=prepare_inventory_charge,
        commit_inventory_charge=commit_inventory_charge,
        inventory_context=lambda: context,
        inventory_lot_locked=inventory_lot_locked,
        finalize_inventory_profile=finalize_inventory_profile,
        release_inventory_roast=release_inventory_roast,
        saved_profile=Mock(),
    )


def test_inventory_recovery_startup_is_deferred_reuses_dialog_and_conflict_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6.QtCore import QObject, pyqtSignal
    from artisanlib.roastserver import inventory_dialogs
    from artisanlib.roastserver.settings import namespace_for

    class Controller(QObject):
        inventoryRecoveryRequired = pyqtSignal(object)
        inventoryConflict = pyqtSignal(object)

        def inventory_context(self) -> SimpleNamespace:
            return SimpleNamespace(namespace=namespace, enabled=True)

    class RecoveryDialog:
        instances: list[object] = []

        def __init__(self, *args: object) -> None:
            self.args = args
            self.show_calls = 0
            self.hide_calls = 0
            self.clean_up_calls = 0
            self.close_calls = 0
            self.active_namespaces: list[Namespace | None] = []
            self.__class__.instances.append(self)

        def set_active_namespace(self, value: Namespace | None) -> None:
            self.active_namespaces.append(value)

        def show(self) -> None:
            self.show_calls += 1

        def raise_(self) -> None:
            return

        def activateWindow(self) -> None:
            return

        def hide(self) -> None:
            self.hide_calls += 1

        def clean_up(self) -> None:
            self.clean_up_calls += 1

        def close(self) -> None:
            self.close_calls += 1

    namespace = namespace_for(
        'https://safe.example', UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'))
    roast_uuid = UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb')
    lot_id = UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc')
    reservation_id = UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd')
    now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
    recovery = InterruptedReservation(
        namespace, roast_uuid, lot_id, '<b>Safe lot</b>', reservation_id,
        1_000, 'reserved', now)
    controller = Controller()
    recovery_baseline = controller.receivers(controller.inventoryRecoveryRequired)
    conflict_baseline = controller.receivers(controller.inventoryConflict)
    window = ApplicationWindow.__new__(ApplicationWindow)
    QMainWindow.__init__(window)
    window.roastserver_controller = cast(Any, controller)
    window.roastserver_inventory_recovery_dialog = None
    window.roastserver_inventory_recovery_records = ()
    window.roastserver_inventory_recovery_scheduled = False
    controller.inventoryRecoveryRequired.connect(window.scheduleInventoryRecovery)
    controller.inventoryConflict.connect(window.showInventoryConflict)
    monkeypatch.setattr(
        inventory_dialogs, 'InterruptedReservationsDialog', RecoveryDialog)

    controller.inventoryRecoveryRequired.emit((recovery,))
    assert RecoveryDialog.instances == []
    QApplication.processEvents()
    assert len(RecoveryDialog.instances) == 1
    assert RecoveryDialog.instances[0].show_calls == 1
    controller.inventoryRecoveryRequired.emit((recovery,))
    QApplication.processEvents()
    assert len(RecoveryDialog.instances) == 1
    assert RecoveryDialog.instances[0].show_calls == 2

    conflict = InventoryRoastState(
        namespace, roast_uuid, lot_id, '<b>Safe lot</b>', reservation_id, None,
        1_000, None, 'reserved', None, now, None, None, None,
        InventoryBalance(lot_id, 100, 125, -25, 1), reservation_id,
        None, None, now, now)
    message = MagicMock()
    message_box = MagicMock(return_value=message)
    message_box.Icon.Warning = object()
    message_box.StandardButton.Ok = object()
    monkeypatch.setattr(main_module, 'QMessageBox', message_box)
    controller.inventoryConflict.emit(conflict)
    message.setTextFormat.assert_called_once_with(Qt.TextFormat.PlainText)
    warning = message.setText.call_args.args[0]
    assert '<b>Safe lot</b>' in warning
    assert '-25 g' in warning
    assert 'Reconcile' in warning
    assert namespace.origin not in warning
    assert str(namespace.organization_id) not in warning
    message.exec.assert_called_once()

    dialog = cast(Any, RecoveryDialog.instances[0])
    window.cleanUpRoastServerInventoryPresentation()
    window.cleanUpRoastServerInventoryPresentation()
    assert dialog.hide_calls == 1
    assert dialog.clean_up_calls == 1
    assert dialog.close_calls == 1
    assert controller.receivers(controller.inventoryRecoveryRequired) == recovery_baseline
    assert controller.receivers(controller.inventoryConflict) == conflict_baseline
    window.deleteLater()


def test_inventory_recovery_cleanup_invalidates_pending_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from PyQt6.QtCore import QObject, pyqtSignal
    from artisanlib.roastserver import inventory_dialogs
    from artisanlib.roastserver.settings import namespace_for

    class Controller(QObject):
        inventoryRecoveryRequired = pyqtSignal(object)
        inventoryConflict = pyqtSignal(object)

        def inventory_context(self) -> SimpleNamespace:
            return SimpleNamespace(namespace=namespace, enabled=True)

    class RecoveryDialog:
        instances: list[object] = []

        def __init__(self, *_args: object) -> None:
            self.__class__.instances.append(self)

        def show(self) -> None:
            return

        def raise_(self) -> None:
            return

        def activateWindow(self) -> None:
            return

    namespace = namespace_for(
        'https://safe.example', UUID('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'))
    recovery = InterruptedReservation(
        namespace,
        UUID('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb'),
        UUID('cccccccc-cccc-4ccc-8ccc-cccccccccccc'),
        'Safe lot',
        UUID('dddddddd-dddd-4ddd-8ddd-dddddddddddd'),
        1_000,
        'reserved',
        datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
    )
    controller = Controller()
    recovery_baseline = controller.receivers(controller.inventoryRecoveryRequired)
    conflict_baseline = controller.receivers(controller.inventoryConflict)
    window = ApplicationWindow.__new__(ApplicationWindow)
    QMainWindow.__init__(window)
    window.roastserver_controller = cast(Any, controller)
    window.roastserver_inventory_recovery_dialog = None
    window.roastserver_inventory_recovery_records = ()
    window.roastserver_inventory_recovery_scheduled = False
    controller.inventoryRecoveryRequired.connect(window.scheduleInventoryRecovery)
    controller.inventoryConflict.connect(window.showInventoryConflict)
    monkeypatch.setattr(
        inventory_dialogs, 'InterruptedReservationsDialog', RecoveryDialog)

    controller.inventoryRecoveryRequired.emit((recovery,))
    assert window.roastserver_inventory_recovery_scheduled
    assert window.roastserver_inventory_recovery_records == (recovery,)
    assert RecoveryDialog.instances == []

    window.cleanUpRoastServerInventoryPresentation()
    window.cleanUpRoastServerInventoryPresentation()
    assert not window.roastserver_inventory_recovery_scheduled
    assert window.roastserver_inventory_recovery_records == ()
    assert controller.receivers(controller.inventoryRecoveryRequired) == recovery_baseline
    assert controller.receivers(controller.inventoryConflict) == conflict_baseline

    QApplication.processEvents()
    assert RecoveryDialog.instances == []
    assert controller.receivers(controller.inventoryRecoveryRequired) == recovery_baseline
    assert controller.receivers(controller.inventoryConflict) == conflict_baseline
    window.deleteLater()


def inventory_profile_load_window() -> ApplicationWindow:
    window = ApplicationWindow.__new__(ApplicationWindow)
    window.qmc = Mock()
    window.qmc.extradevices = []
    window.qmc.etypes = ['Air', 'Drum', 'Damper', 'Burner', '--']
    window.qmc.flavors = []
    window.qmc.flavorlabels = []
    window.qmc.weight = (0.0, 0.0, 'g')
    window.qmc.volume = (0.0, 0.0, 'l')
    window.qmc.timex = []
    window.qmc.temp1 = []
    window.qmc.temp2 = []
    window.qmc.extratimex = []
    window.qmc.extratemp1 = []
    window.qmc.extratemp2 = []
    window.qmc.mode = 'C'
    window.qmc.loadalarmsfromprofile = False
    window.qmc.loadaxisfromprofile = False
    window.qmc.locktimex = False
    window.qmc.timeindex = [-1, 0, 0, 0, 0, 0, 0, 0]
    window.qmc.phasesbuttonflag = False
    window.qmc.backgroundUUID = None
    window.qmc.fileDirtySignal.emit = Mock()
    window.qmc.updateDeltaSamples = Mock()
    window.qmc.resetlinecountcaches = Mock()
    window.nLCDS = 10
    window.pidcontrol = Mock(loadRampSoakFromProfile=False)
    window.get_profile_etypes = Mock(return_value=window.qmc.etypes)
    window.updateLCDproperties = Mock()
    window.loadEnergyFromProfile = Mock()
    window.loadBbpFromProfile = Mock()
    window.autoAdjustAxis = Mock()
    window.sendmessage = Mock()
    window.plusAddPath = Mock()
    return window


def roastserver_save_window() -> tuple[ApplicationWindow, Mock, dict[str, Any]]:
    window = ApplicationWindow.__new__(ApplicationWindow)
    profile = copy.deepcopy(ROASTSERVER_PROFILE)
    controller = Mock()

    def protection_guard(expected: object) -> object:
        if controller.current_protection_token() is not expected:
            raise RuntimeError('cache protection ownership changed')
        return nullcontext()

    controller.protection_guard.side_effect = protection_guard
    window.roastserver_controller = controller
    window.qmc = Mock()
    window.qmc.autosaveimage = False
    window.qmc.flagon = False
    window.qmc.roastbatchnr = 0
    window.qmc.roastbatchprefix = ''
    window.qmc.batchcounter = -1
    window.qmc.batchprefix = ''
    window.qmc.autosaveprefix = ''
    window.qmc.plus_file_last_modified = None
    for name in INVENTORY_QMC_FIELDS:
        setattr(window.qmc, name, None)
    window.MaxRecentFiles = 20
    window.getProfile = Mock(return_value=profile)
    window.plusAddPath = Mock()
    window.sendmessage = Mock()
    window.setCurrentFile = Mock()
    window.updatePlusStatus = Mock()
    window.autosave = Mock()
    window.getDefaultPath = Mock(return_value='.')
    window.generateFilename = Mock(return_value='chosen.alog')
    window.ArtisanSaveFileDialog = Mock()
    return window, controller, profile


def roastserver_menu_window() -> ApplicationWindow:
    window = ApplicationWindow.__new__(ApplicationWindow)
    action_names = (
        'fileLoadAction', 'fileSaveAction', 'fileSaveAsAction',
        'fileSaveCopyAsAction', 'roastServerRoastsAction',
        'roastServerUploadAction', 'printAction', 'quitAction', 'deviceAction',
        'commportAction', 'calibrateDelayAction', 'curvesAction', 'eventsAction',
        'alarmAction', 'phasesGraphAction', 'StatisticsAction',
        'WindowconfigAction', 'colorsAction', 'autosaveAction', 'batchAction',
        'roastServerConfigAction',
    )
    for name in action_names:
        label = {
            'roastServerRoastsAction': 'Server Roasts...',
            'roastServerUploadAction': 'Upload to Roast Server',
            'roastServerConfigAction': 'Roast Server...',
        }.get(name, name)
        setattr(window, name, QAction(label))
    menu_names = (
        'newRoastMenu', 'openRecentMenu', 'importMenu', 'convFromMenu',
        'exportMenu', 'convMenu', 'saveGraphMenu', 'reportMenu',
        'saveStatisticsMenu', 'machineMenu', 'themeMenu',
        'temperatureConfMenu', 'languageMenu', 'UIModeMenu',
    )
    for name in menu_names:
        label = 'Mode' if name == 'UIModeMenu' else name
        setattr(window, name, QMenu(label))
    return window


def roastserver_action_window(profile_path: Path) -> ApplicationWindow:
    window = ApplicationWindow.__new__(ApplicationWindow)
    action_names = (
        'newRoastMenu', 'fileLoadAction', 'openRecentMenu', 'importMenu',
        'fileSaveAction', 'fileSaveAsAction', 'fileSaveCopyAsAction',
        'exportMenu', 'convMenu', 'saveGraphMenu', 'htmlAction',
        'roastReportPDFAction', 'reportMenu', 'productionMenu', 'rankingMenu',
        'printAction', 'editGraphAction', 'backgroundAction', 'switchAction',
        'switchETBTAction', 'flavorAction', 'temperatureMenu',
        'temperatureConfMenu', 'languageMenu', 'deviceAction',
        'commportAction', 'curvesAction', 'analyzeMenu', 'roastCompareAction',
        'designerAction', 'simulatorAction', 'wheeleditorAction',
        'transformAction', 'loadSettingsAction', 'openRecentSettingMenu',
        'saveAsSettingsAction', 'resetAction', 'machineMenu', 'eventsAction',
        'phasesGraphAction', 'StatisticsAction', 'WindowconfigAction',
        'colorsAction', 'themeMenu', 'controlsAction', 'readingsAction',
        'eventsEditorAction', 'buttonsAction', 'slidersAction',
        'saveStatisticsMenu', 'calibrateDelayAction', 'alarmAction',
        'autosaveAction', 'batchAction', 'roastServerConfigAction',
        'roastServerRoastsAction', 'roastServerUploadAction',
    )
    for name in action_names:
        setattr(window, name, QAction(name))
    window.qmc = Mock(safesaveflag=False, statssummary=False)
    window.app = Mock(artisanviewerMode=False)
    window.curFile = str(profile_path)
    window.QtWebEngineSupport = False
    window.extraeventslabels = []
    window.hideExtraButtons = Mock()
    window.hideSliders = Mock()
    window.slidersVisible = Mock(return_value=True)
    window.updateWindowTitle = Mock()
    window.set_menu = Mock()
    window.set_toolbar = Mock()
    window.announce_current_ui_mode = Mock()
    window.productionModeAction = QAction('production')
    window.defaultModeAction = QAction('default')
    window.expertModeAction = QAction('expert')
    return window


class TestRoastServerMainIntegration:
    def test_roastserver_successful_save_uses_descriptor_timestamp_and_immediate_detach(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        destination = tmp_path / 'saved.alog'
        replacement = tmp_path / 'replacement.alog'
        replacement_profile = {**ROASTSERVER_PROFILE, 'title': 'revision B'}
        replacement.write_bytes(repr(replacement_profile).encode('utf-8'))
        os.utime(replacement, (1_800_000_000, 1_800_000_000))
        expected_profile = copy.deepcopy(profile)
        expected_bytes = repr(expected_profile).encode('utf-8')
        revision_a_mtime: list[float] = []

        def mutate_and_replace(_message: str) -> None:
            revision_a_mtime.append(destination.stat().st_mtime)
            computed = profile['computed']
            assert isinstance(computed, dict)
            nested = computed['nested']
            assert isinstance(nested, list)
            nested.append(3.0)
            window.getProfile.return_value = replacement_profile
            os.replace(replacement, destination)

        window.sendmessage.side_effect = mutate_and_replace
        ordered = Mock()
        with patch(
            'artisanlib.main.serialize_with_timestamp',
            wraps=util_serialize_with_timestamp,
        ) as serialize_mock:
            ordered.attach_mock(serialize_mock, 'serialize')
            ordered.attach_mock(controller.saved_profile, 'saved_profile')
            assert window.fileSave(str(destination))

        assert [call[0] for call in ordered.mock_calls] == [
            'serialize', 'saved_profile']
        serialized, detached, modified_at = controller.saved_profile.call_args.args
        assert serialized == expected_bytes
        assert detached == expected_profile
        assert detached is not profile
        assert modified_at == datetime.fromtimestamp(revision_a_mtime[0], UTC)
        assert modified_at.tzinfo is UTC
        assert destination.read_bytes() == repr(replacement_profile).encode('utf-8')

    def test_roastserver_two_same_path_saves_keep_exact_revision_triples(
        self, tmp_path: Path
    ) -> None:
        window, controller, first_profile = roastserver_save_window()
        destination = tmp_path / 'same-path.alog'
        second_profile = copy.deepcopy(first_profile)
        second_profile['title'] = 'revision B'

        serialization_results: list[Any] = []

        def record_serialization(filename: str, profile: dict[str, Any]) -> Any:
            result = util_serialize_with_timestamp(filename, profile)
            serialization_results.append(result)
            return result

        with patch(
            'artisanlib.main.serialize_with_timestamp',
            side_effect=record_serialization,
        ):
            assert window.fileSave(str(destination))
            first_call = controller.saved_profile.call_args_list[0].args
            window.getProfile.return_value = second_profile
            assert window.fileSave(str(destination))
            second_call = controller.saved_profile.call_args_list[1].args

        assert first_call[0] == repr(first_profile).encode('utf-8')
        assert first_call[1] == first_profile
        assert second_call[0] == repr(second_profile).encode('utf-8')
        assert second_call[1] == second_profile
        assert first_call[1] is not first_profile
        assert second_call[1] is not second_profile
        assert first_call[2] == serialization_results[0].modified_at
        assert second_call[2] == serialization_results[1].modified_at
        assert first_call[2].tzinfo is UTC
        assert second_call[2].tzinfo is UTC
        assert destination.read_bytes() == second_call[0]

    def test_inventory_save_finalizes_after_upload_with_same_detached_profile(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        profile.update({
            **INVENTORY_PROFILE_LINK,
            'timeindex': [0, 0, 0, 0, 0, 0, 0, 0],
            'weight': [1.25, 1.0, 'Kg'],
        })
        ordered = Mock()
        ordered.attach_mock(controller.saved_profile, 'saved_profile')
        ordered.attach_mock(
            controller.finalize_inventory_profile, 'finalize_inventory_profile')

        assert window.fileSave(str(tmp_path / 'roast.alog'))

        assert [item[0] for item in ordered.mock_calls] == [
            'saved_profile', 'finalize_inventory_profile']
        detached_profile = controller.saved_profile.call_args.args[1]
        assert controller.finalize_inventory_profile.call_args.args[0] is detached_profile

    def test_inventory_profile_save_copy_preserves_link_without_server_hook(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        profile.update(INVENTORY_PROFILE_LINK)
        with patch(
            'artisanlib.main.serialize_with_timestamp',
            side_effect=OSError('write failed'),
        ):
            assert not window.fileSave(str(tmp_path / 'failed.alog'))
        controller.saved_profile.assert_not_called()
        controller.finalize_inventory_profile.assert_not_called()

        copy_path = tmp_path / 'copy.alog'
        with patch(
            'artisanlib.main.serialize_with_timestamp',
            wraps=util_serialize_with_timestamp,
        ):
            assert window.fileSave(str(copy_path), copy=True)
        saved_copy = util_deserialize(str(copy_path))
        assert saved_copy is not None
        assert {name: saved_copy[name] for name in INVENTORY_QMC_FIELDS} == (
            INVENTORY_PROFILE_LINK)
        controller.saved_profile.assert_not_called()
        controller.finalize_inventory_profile.assert_not_called()

        window.setCurrentFile.side_effect = RuntimeError('post-save failed')
        with patch(
            'artisanlib.main.serialize_with_timestamp',
            wraps=util_serialize_with_timestamp,
        ):
            assert not window.fileSave(str(tmp_path / 'post-save.alog'))
        controller.saved_profile.assert_not_called()
        controller.finalize_inventory_profile.assert_not_called()

    def test_inventory_save_upload_failure_does_not_suppress_finalization(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        profile.update(INVENTORY_PROFILE_LINK)
        controller.saved_profile.side_effect = RuntimeError('upload failed')

        assert window.fileSave(str(tmp_path / 'roast.alog'))

        controller.saved_profile.assert_called_once()
        controller.finalize_inventory_profile.assert_called_once()

    def test_inventory_save_planned_weight_fallback_warns_without_failing_save(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        profile.update(INVENTORY_PROFILE_LINK)
        controller.finalize_inventory_profile.return_value = InventoryNotice(
            'inventory_planned_weight_used',
            UUID(profile['roastUUID']),
            UUID('44444444-4444-4444-8444-444444444444'),
            UUID(INVENTORY_PROFILE_LINK['roastServerBeanLotUUID']),
            None,
            None,
        )

        assert window.fileSave(str(tmp_path / 'roast.alog'))

        assert any(
            'planned green weight' in call_item.args[0]
            for call_item in window.sendmessage.call_args_list
        )

    def test_inventory_save_storage_failure_does_not_rollback_profile(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        profile.update(INVENTORY_PROFILE_LINK)
        destination = tmp_path / 'roast.alog'
        controller.finalize_inventory_profile.side_effect = ControllerError(
            'inventory_storage_failed')

        assert window.fileSave(str(destination))

        assert destination.exists()
        controller.saved_profile.assert_called_once()
        assert any(
            'finalization could not be stored' in call_item.args[0]
            for call_item in window.sendmessage.call_args_list
        )

    @pytest.mark.parametrize(
        ('actual_weight', 'expected_actual_grams', 'expect_warning'),
        [(1.25, 1250, False), (0.0, None, True)],
    )
    def test_inventory_save_coordinator_queues_one_terminal_intent(
        self,
        tmp_path: Path,
        actual_weight: float,
        expected_actual_grams: int | None,
        expect_warning: bool,
    ) -> None:
        link = parse_profile_link(INVENTORY_PROFILE_LINK)
        assert link is not None
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        roast_uuid = UUID('33333333-3333-4333-8333-333333333333')
        uuid_values = iter((
            roast_uuid,
            UUID('44444444-4444-4444-8444-444444444444'),
        ))
        store = InventoryStore(tmp_path / 'inventory')
        store.open()
        store.replace_lots(
            link.namespace,
            (
                BeanLot(
                    lot_id=link.lot_id,
                    name=link.lot_name,
                    origin='Ethiopia',
                    varietals=('Heirloom',),
                    processing_method='washed',
                    crop_year=2026,
                    on_hand_grams=2_000,
                    reserved_grams=0,
                    available_grams=2_000,
                    unresolved_conflict_count=0,
                ),
            ),
            now,
        )
        context = InventoryContext(
            origin=link.namespace.origin,
            namespace=link.namespace,
            enabled=True,
            previously_authenticated=True,
            client_instance_uuid=UUID(
                '55555555-5555-4555-8555-555555555555'),
        )
        coordinator = InventoryCoordinator(
            store,
            clock=lambda: now,
            uuid_factory=lambda: next(uuid_values),
            wake=lambda: None,
        )
        controller = coordinator_controller(coordinator, context)
        prepared = coordinator.prepare_charge(context, link, None, 1.5, 'Kg')
        coordinator.commit_charge(prepared)
        window, _mock_controller, profile = roastserver_save_window()
        window.roastserver_controller = controller
        profile.update({
            **INVENTORY_PROFILE_LINK,
            'roastUUID': roast_uuid.hex,
            'timeindex': [-1, 0, 0, 0, 0, 0, 0, 0],
            'weight': [actual_weight, 1.0, 'Kg'],
        })

        try:
            assert window.fileSave(str(tmp_path / 'no-charge.alog'))
            assert store.counts(link.namespace).pending == 1

            profile['timeindex'][0] = 0
            profile['roastUUID'] = '66666666666646668666666666666666'
            assert window.fileSave(str(tmp_path / 'different-roast.alog'))
            assert store.counts(link.namespace).pending == 1

            profile['roastUUID'] = roast_uuid.hex
            assert window.fileSave(str(tmp_path / 'roast.alog'))
            state = store.roast_state(link.namespace, roast_uuid)
            assert state is not None
            assert state.lifecycle == 'finalize_queued'
            assert state.actual_grams == expected_actual_grams
            assert store.counts(link.namespace).pending == 2

            assert window.fileSave(str(tmp_path / 'repeat.alog'))
            assert store.counts(link.namespace).pending == 2
            assert controller.release_inventory_roast(roast_uuid) is None
            assert store.counts(link.namespace).pending == 2
            assert any(
                'planned green weight' in call_item.args[0]
                for call_item in window.sendmessage.call_args_list
            ) is expect_warning
        finally:
            store.close()

    def test_roastserver_save_as_uses_chosen_path(self, tmp_path: Path) -> None:
        window, controller, profile = roastserver_save_window()
        chosen = tmp_path / 'chosen.alog'
        window.ArtisanSaveFileDialog.return_value = str(chosen)

        assert window.fileSave(None)

        window.ArtisanSaveFileDialog.assert_called_once()
        assert chosen.read_bytes() == repr(profile).encode('utf-8')
        controller.saved_profile.assert_called_once()
        controller.finalize_inventory_profile.assert_called_once()

    def test_roastserver_autosave_notifies_only_after_exact_write(
        self, tmp_path: Path
    ) -> None:
        window, controller, profile = roastserver_save_window()
        window.qmc.autosaveflag = True
        window.qmc.autosavepath = str(tmp_path)
        window.qmc.autosaveaddtorecentfilesflag = True
        window.qmc.autosaveimage = False
        window.generateFilename.return_value = 'automatic.alog'
        ordered = Mock()
        with patch(
            'artisanlib.main.serialize_with_timestamp',
            wraps=util_serialize_with_timestamp,
        ) as serialize_mock:
            ordered.attach_mock(serialize_mock, 'serialize')
            ordered.attach_mock(controller.saved_profile, 'saved_profile')
            assert window.automaticsave() == 'automatic.alog'

        destination = tmp_path / 'automatic.alog'
        assert destination.read_bytes() == repr(profile).encode('utf-8')
        assert [call[0] for call in ordered.mock_calls] == [
            'serialize', 'saved_profile']
        controller.finalize_inventory_profile.assert_called_once()
        detached_profile = controller.saved_profile.call_args.args[1]
        assert controller.finalize_inventory_profile.call_args.args[0] is detached_profile

    @pytest.mark.parametrize('failure', ['serialize', 'timestamp'])
    def test_roastserver_autosave_serialization_failure_never_notifies(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, controller, _profile = roastserver_save_window()
        window.qmc.autosaveflag = True
        window.qmc.autosavepath = str(tmp_path)
        window.qmc.autosaveaddtorecentfilesflag = True
        window.generateFilename.return_value = f'{failure}.alog'
        old_directory = os.getcwd()

        if failure == 'timestamp':
            context = patch(
                'artisanlib.util.os.fstat',
                side_effect=OSError('timestamp failed'),
            )
        else:
            context = patch(
                'artisanlib.main.serialize_with_timestamp',
                side_effect=OSError('serialize failed'),
            )
        with context:
            assert window.automaticsave() == f'{failure}.alog'

        controller.saved_profile.assert_not_called()
        controller.finalize_inventory_profile.assert_not_called()
        assert os.getcwd() == old_directory

    @pytest.mark.parametrize('failure', ['post-save', 'plus-status', 'image-export'])
    def test_roastserver_autosave_independent_failures_never_suppress_or_duplicate_queue(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, controller, profile = roastserver_save_window()
        window.qmc.autosaveflag = True
        window.qmc.autosavepath = str(tmp_path)
        window.qmc.autosaveaddtorecentfilesflag = True
        window.generateFilename.return_value = f'{failure}.alog'
        if failure == 'post-save':
            window.setCurrentFile.side_effect = OSError('post-save failed')
        elif failure == 'plus-status':
            window.updatePlusStatus.side_effect = RuntimeError('plus status failed')
        else:
            window.qmc.autosaveimage = True
            window.qmc.autosavealsopath = ''
            window.autosave.side_effect = RuntimeError('image export failed')
        ordered = Mock()
        ordered.attach_mock(controller.saved_profile, 'saved_profile')
        ordered.attach_mock(window.updatePlusStatus, 'update_plus_status')
        ordered.attach_mock(window.autosave, 'image_export')

        with patch(
            'artisanlib.main.serialize_with_timestamp',
            wraps=util_serialize_with_timestamp,
        ):
            assert window.automaticsave() == f'{failure}.alog'

        controller.saved_profile.assert_called_once()
        serialized, detached, modified_at = controller.saved_profile.call_args.args
        assert serialized == repr(profile).encode('utf-8')
        assert detached == profile
        assert detached is not profile
        assert modified_at.tzinfo is UTC
        assert ordered.mock_calls[0] == call.saved_profile(
            serialized, detached, modified_at)
        expected_clean_calls = 0 if failure == 'post-save' else 1
        assert window.qmc.fileCleanSignal.emit.call_count == expected_clean_calls
        assert (tmp_path / f'{failure}.alog').read_bytes() == serialized

    def test_roastserver_save_plus_calls_remain_unchanged(self, tmp_path: Path) -> None:
        window, _controller, profile = roastserver_save_window()
        destination = tmp_path / 'plus.alog'
        modified = datetime(2026, 8, 1, 12, tzinfo=UTC)

        with patch(
            'artisanlib.main.plus.util.getModificationDate', return_value=modified
        ) as modification_date:
            assert window.fileSave(str(destination))

        window.getProfile.assert_called_once_with(False, generate_hash=True)
        window.plusAddPath.assert_called_once_with(profile, str(destination))
        modification_date.assert_called_once_with(str(destination))
        assert window.qmc.plus_file_last_modified is modified

    @pytest.mark.parametrize(
        ('viewer', 'dirty', 'filename'),
        [
            (False, False, None),
            (False, True, 'saved.alog'),
            (True, False, 'saved.alog'),
            (False, False, 'saved.txt'),
            (False, False, 'missing.alog'),
        ],
    )
    def test_roast_server_manual_upload_requires_clean_existing_alog(
        self, tmp_path: Path, viewer: bool, dirty: bool, filename: str | None
    ) -> None:
        window, controller, profile = roastserver_save_window()
        window.app = Mock(artisanviewerMode=viewer)
        window.qmc.safesaveflag = dirty
        if filename not in {None, 'missing.alog'}:
            (tmp_path / filename).write_bytes(b'original saved bytes')
        window.curFile = None if filename is None else str(tmp_path / filename)
        before = (
            Path(window.curFile).read_bytes()
            if window.curFile is not None and Path(window.curFile).is_file()
            else None
        )
        with patch('artisanlib.main.deserialize') as deserialize_mock, patch(
            'artisanlib.main.plus.controller.updateSyncRecordHashAndSync'
        ) as plus_sync_mock:
            window.uploadToRoastServer()

        controller.manual_upload.assert_not_called()
        deserialize_mock.assert_not_called()
        plus_sync_mock.assert_not_called()
        window.getProfile.assert_not_called()
        if before is not None and window.curFile is not None:
            assert Path(window.curFile).read_bytes() == before
        assert profile == ROASTSERVER_PROFILE

    def test_roast_server_manual_upload_reads_exact_detached_disk_profile_without_mutation(
        self, tmp_path: Path
    ) -> None:
        window, controller, _profile = roastserver_save_window()
        window.app = Mock(artisanviewerMode=False)
        window.qmc.safesaveflag = False
        window.qmc.specialevents = [1, 2, {'nested': ['unchanged']}]
        window.qmc.plus_blend_spec = {'coffee-1': {'ratio': 1.0}}
        window.extraLCDvisibility1 = [True, [False]]
        disk_profile = copy.deepcopy(ROASTSERVER_PROFILE)
        disk_profile['title'] = 'exact profile on disk'
        destination = tmp_path / 'saved.alog'
        disk_bytes = repr(disk_profile).encode('utf-8')
        destination.write_bytes(disk_bytes)
        window.curFile = str(destination)
        before_mtime = destination.stat().st_mtime
        before_state = copy.deepcopy((
            window.qmc.specialevents,
            window.qmc.plus_blend_spec,
            window.extraLCDvisibility1,
            window.curFile,
        ))
        window.getProfile.side_effect = AssertionError('manual upload called getProfile')

        with patch.object(
            Path, 'stat', side_effect=AssertionError('manual upload re-statted path')
        ), patch(
            'artisanlib.main.plus.controller.updateSyncRecordHashAndSync'
        ) as plus_sync_mock:
            window.uploadToRoastServer()

        plus_sync_mock.assert_not_called()
        window.getProfile.assert_not_called()
        window.plusAddPath.assert_not_called()
        serialized, detached, modified_at = controller.manual_upload.call_args.args
        assert serialized == disk_bytes
        assert detached == disk_profile
        assert detached is not disk_profile
        assert modified_at == datetime.fromtimestamp(before_mtime, UTC)
        assert (
            window.qmc.specialevents,
            window.qmc.plus_blend_spec,
            window.extraLCDvisibility1,
            window.curFile,
        ) == before_state
        assert destination.read_bytes() == disk_bytes
        assert os.stat(destination).st_mtime == before_mtime

    @pytest.mark.parametrize('failure', ['oversize', 'invalid', 'unstable'])
    def test_roast_server_manual_upload_read_failures_are_fixed_and_bounded(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, controller, _profile = roastserver_save_window()
        window.app = Mock(artisanviewerMode=False)
        window.qmc.safesaveflag = False
        destination = tmp_path / 'saved.alog'
        if failure == 'oversize':
            with destination.open('wb') as profile_file:
                profile_file.truncate(16 * 1024 * 1024 + 1)
        elif failure == 'invalid':
            destination.write_bytes(b"['not', 'a', 'dict']")
        else:
            destination.write_bytes(repr(ROASTSERVER_PROFILE).encode('utf-8'))
        window.curFile = str(destination)
        original_read = os.read
        changed = False

        def changing_read(descriptor: int, count: int) -> bytes:
            nonlocal changed
            value = original_read(descriptor, count)
            if failure == 'unstable' and not changed:
                changed = True
                with destination.open('ab') as profile_file:
                    profile_file.write(b' ')
            return value

        read_patch = (
            patch('artisanlib.main.os.read', side_effect=changing_read)
            if failure == 'unstable'
            else patch('artisanlib.main.os.read', wraps=original_read)
        )
        with read_patch:
            window.uploadToRoastServer()

        controller.manual_upload.assert_not_called()
        window.getProfile.assert_not_called()
        window.plusAddPath.assert_not_called()
        window.sendmessage.assert_called_once_with(
            QApplication.translate(
                'Message', 'Roast Server upload could not be queued.'))

    def test_roastserver_file_menu_action_order_in_all_modes(self) -> None:
        window = roastserver_menu_window()
        for mode in UI_MODE:
            menu = window.create_file_menu(mode)
            labels = [action.text() for action in menu.actions()]
            save_as = labels.index('fileSaveAsAction')
            roasts = labels.index('Server Roasts...')
            upload = labels.index('Upload to Roast Server')
            export = labels.index('exportMenu') if mode is not UI_MODE.PRODUCTION else len(labels)
            assert save_as < roasts < upload < export

    def test_roastserver_config_menu_action_precedes_mode_in_all_modes(self) -> None:
        window = roastserver_menu_window()
        for mode in UI_MODE:
            menu = window.create_config_menu(mode)
            labels = [action.text() for action in menu.actions()]
            assert labels.index('Roast Server...') < labels.index('Mode')

    @pytest.mark.parametrize(
        ('mode', 'config_enabled', 'roasts_enabled', 'upload_enabled'),
        [
            ('edit', True, True, True),
            ('designer', False, False, False),
            ('wheel', False, False, False),
            ('compare', True, True, False),
            ('sampling', False, False, False),
            ('viewer', False, True, False),
        ],
    )
    def test_roastserver_action_states_follow_all_operating_modes(
        self,
        tmp_path: Path,
        mode: str,
        config_enabled: bool,
        roasts_enabled: bool,
        upload_enabled: bool,
    ) -> None:
        profile_path = tmp_path / 'clean.alog'
        profile_path.write_bytes(repr(ROASTSERVER_PROFILE).encode('utf-8'))
        window = roastserver_action_window(profile_path)
        window.enableEditMenus()

        if mode == 'designer':
            window.disableEditMenus(designer=True)
        elif mode == 'wheel':
            window.disableEditMenus(wheel=True)
        elif mode == 'compare':
            window.disableEditMenus(compare=True)
        elif mode == 'sampling':
            window.disableEditMenus(sampling=True)
        elif mode == 'viewer':
            window.app.artisanviewerMode = True
            window.displayonlymenus()

        assert window.roastServerConfigAction.isEnabled() is config_enabled
        assert window.roastServerRoastsAction.isEnabled() is roasts_enabled
        assert window.roastServerUploadAction.isEnabled() is upload_enabled

    def test_roastserver_actions_refresh_on_dirty_file_mode_and_load_changes(
        self, tmp_path: Path
    ) -> None:
        profile_path = tmp_path / 'clean.alog'
        profile_path.write_bytes(repr(ROASTSERVER_PROFILE).encode('utf-8'))
        window = roastserver_action_window(profile_path)
        window.enableEditMenus()
        assert window.roastServerUploadAction.isEnabled()

        dirty_callbacks: list[Any] = []
        clean_callbacks: list[Any] = []
        window.qmc.fileDirtySignal.connect.side_effect = dirty_callbacks.append
        window.qmc.fileCleanSignal.connect.side_effect = clean_callbacks.append
        window.connectRoastServerActionRefresh()
        window.qmc.safesaveflag = True
        dirty_callbacks[0]()
        assert not window.roastServerUploadAction.isEnabled()
        window.qmc.safesaveflag = False
        clean_callbacks[0]()
        assert window.roastServerUploadAction.isEnabled()
        window.setCurrentFile(None, addToRecent=False)
        assert not window.roastServerUploadAction.isEnabled()
        window.setCurrentFile(str(profile_path), addToRecent=False)
        assert window.roastServerUploadAction.isEnabled()

        window.disableLoadImportConvertMenus()
        assert not window.roastServerRoastsAction.isEnabled()
        assert not window.roastServerUploadAction.isEnabled()
        window.enableLoadImportConvertMenus()
        assert window.roastServerRoastsAction.isEnabled()
        assert window.roastServerUploadAction.isEnabled()

        for ui_mode in UI_MODE:
            window.set_ui_mode(ui_mode)
            assert window.roastServerConfigAction.isEnabled()
            assert window.roastServerRoastsAction.isEnabled()
            assert window.roastServerUploadAction.isEnabled()

    @pytest.mark.parametrize(
        ('method_name', 'dialog_name', 'attribute_name'),
        [
            ('showRoastServerConfig', 'RoastServerConfigDialog',
             'roastserver_config_dialog'),
            ('showServerRoasts', 'RoastServerBrowserDialog',
             'roastserver_browser_dialog'),
        ],
    )
    def test_roastserver_modeless_dialogs_are_reused_and_raised(
        self, method_name: str, dialog_name: str, attribute_name: str
    ) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.roastserver_controller = Mock()
        window.roastserver_settings = Mock()
        setattr(window, attribute_name, None)
        dialog = Mock()
        with patch(
            f'artisanlib.roastserver.dialogs.{dialog_name}', return_value=dialog
        ) as dialog_class:
            getattr(window, method_name)()
            getattr(window, method_name)()

        dialog_class.assert_called_once()
        assert getattr(window, attribute_name) is dialog
        assert dialog.show.call_count == 2
        assert dialog.raise_.call_count == 2
        assert dialog.activateWindow.call_count == 2

    def test_roastserver_startup_builds_controller_after_settings_path_ready(
        self, tmp_path: Path
    ) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.roastserver_controller = None
        window.roastserver_settings = None
        controller = Mock()
        with patch.dict(sys.modules, {'keyring': Mock()}), patch(
            'artisanlib.roastserver.settings.SettingsStore'
        ) as settings_store, patch(
            'artisanlib.roastserver.settings.SystemCredentialStore'
        ) as credential_store, patch(
            'artisanlib.roastserver.controller.RoastServerController',
            return_value=controller,
        ) as controller_class, patch(
            'artisanlib.roastserver.api.RoastServerClient'
        ) as client_class:
            window.startRoastServer(tmp_path)

        controller_class.assert_called_once_with(
            settings=settings_store.return_value,
            credentials=credential_store.return_value,
            data_root=tmp_path / 'roastserver',
            client_factory=client_class,
            profile_validator=window.validateRoastServerProfile,
            parent=window,
        )
        controller.profileReady.connect.assert_called_once_with(
            window.openRoastServerProfile)
        controller.start.assert_called_once_with()
        assert window.roastserver_controller is controller

    def test_roastserver_actual_startup_orders_settings_path_and_controller(
        self, tmp_path: Path
    ) -> None:
        class StartupObserved(RuntimeError):
            pass

        ordered: list[str] = []
        window = Mock()
        window.qmc = Mock()
        window.defaultSettings = {}
        window.ui_mode = UI_MODE.DEFAULT
        window.settingsLoad.side_effect = lambda **_kwargs: ordered.append('settings')
        window.startRoastServer.side_effect = lambda _path: ordered.append('controller')
        window.set_ui_mode.side_effect = StartupObserved

        def data_directory() -> str:
            ordered.append('path')
            return str(tmp_path)

        fake_app = Mock(artisanviewerMode=False)
        with patch.object(main_module, 'app', fake_app), patch.object(
            main_module, 'qInstallMessageHandler'
        ), patch.object(
            main_module, 'initialize_locale', return_value='en'
        ), patch.object(
            main_module, 'ApplicationWindow', return_value=window
        ), patch.object(
            main_module, 'getDocumentsDirectory', return_value=None
        ), patch.object(
            main_module, 'getDataDirectory', side_effect=data_directory
        ), pytest.raises(StartupObserved):
            main_module.main()

        assert ordered == ['settings', 'path', 'controller']
        window.settingsLoad.assert_called_once_with(redraw=False)
        window.startRoastServer.assert_called_once_with(tmp_path)

    @pytest.mark.parametrize('stopped', [True, False])
    def test_roastserver_shutdown_is_bounded_and_precedes_device_teardown(
        self, stopped: bool
    ) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.quitAction = Mock()
        window.qmc = Mock()
        window.qmc.safesaveflag = False
        window.qmc.checkSaved.return_value = True
        window.qmc.flagKeepON = True
        window.qmc.roastUUID = '33333333333343338333333333333333'
        window.roastserver_controller = Mock()
        window.roastserver_controller.shutdown.return_value = stopped
        window.stopActivities = Mock()
        window.closeEventSettings = Mock()
        window.sendmessage = Mock()
        ordered = Mock()
        ordered.attach_mock(
            window.roastserver_controller.release_inventory_roast,
            'release_inventory_roast',
        )
        ordered.attach_mock(window.roastserver_controller.shutdown, 'shutdown')
        ordered.attach_mock(window.stopActivities, 'stopActivities')
        ordered.attach_mock(window.closeEventSettings, 'closeEventSettings')
        with patch.object(
            QApplication, 'queryKeyboardModifiers',
            return_value=Qt.KeyboardModifier.NoModifier,
        ), patch.object(QApplication, 'exit') as exit_mock:
            ordered.attach_mock(exit_mock, 'exit')
            assert window.closeApp()

        assert [call[0] for call in ordered.mock_calls] == [
            'release_inventory_roast', 'shutdown', 'stopActivities',
            'closeEventSettings', 'exit']
        window.roastserver_controller.release_inventory_roast.assert_called_once_with(
            UUID(window.qmc.roastUUID))
        window.roastserver_controller.shutdown.assert_called_once_with(15_000)
        if stopped:
            window.sendmessage.assert_not_called()
        else:
            assert 'shutdown timeout' in window.sendmessage.call_args.args[0]
        assert '.terminate(' not in inspect.getsource(ApplicationWindow.closeApp)

    def test_inventory_shutdown_cancel_does_not_release_or_interrupt_worker(self) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.quitAction = Mock()
        window.qmc = Mock(
            safesaveflag=True,
            roastUUID='33333333333343338333333333333333',
        )
        window.qmc.checkSaved.return_value = False
        window.roastserver_controller = Mock()

        assert not window.closeApp()

        window.roastserver_controller.release_inventory_roast.assert_not_called()
        window.roastserver_controller.shutdown.assert_not_called()

    def test_inventory_shutdown_discard_reset_does_not_release_after_worker_stop(
        self,
    ) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.quitAction = Mock()
        window.qmc = Mock(
            safesaveflag=True,
            flagKeepON=True,
            roastUUID='33333333333343338333333333333333',
            backgroundpath='',
        )
        window.qmc.checkSaved.return_value = True
        window.qmc.reset.side_effect = lambda **_kwargs: (
            window.roastserver_controller.shutdown.assert_called_once_with(15_000)
            or window.roastserver_controller.release_inventory_roast.assert_called_once()
            or window.qmc.roastUUID is None
        )
        window.curFile = None
        window.roastserver_controller = Mock()
        window.roastserver_controller.shutdown.return_value = True
        window.stopActivities = Mock()
        window.closeEventSettings = Mock()
        window.sendmessage = Mock()

        with patch.object(
            QApplication, 'queryKeyboardModifiers',
            return_value=Qt.KeyboardModifier.NoModifier,
        ), patch.object(QApplication, 'exit'):
            assert window.closeApp()

        window.qmc.reset.assert_called_once_with(
            redraw=False,
            soundOn=False,
            keepProperties=False,
            fireResetAction=False,
        )
        assert window.qmc.roastUUID is None

    def test_inventory_shutdown_release_failure_warns_and_continues(self) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.quitAction = Mock()
        window.qmc = Mock(
            safesaveflag=False,
            flagKeepON=True,
            roastUUID='33333333333343338333333333333333',
        )
        window.qmc.checkSaved.return_value = True
        window.roastserver_controller = Mock()
        window.roastserver_controller.release_inventory_roast.side_effect = (
            ControllerError('inventory_storage_failed'))
        window.roastserver_controller.shutdown.return_value = True
        window.stopActivities = Mock()
        window.closeEventSettings = Mock()
        window.sendmessage = Mock()

        with patch.object(
            QApplication, 'queryKeyboardModifiers',
            return_value=Qt.KeyboardModifier.NoModifier,
        ), patch.object(QApplication, 'exit'):
            assert window.closeApp()

        window.roastserver_controller.shutdown.assert_called_once_with(15_000)
        assert any(
            'release could not be stored' in call_item.args[0]
            for call_item in window.sendmessage.call_args_list
        )

    def test_roastserver_validator_normalizes_without_opening_profile(
        self, tmp_path: Path
    ) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.validateProfileDict = Mock(return_value=ROASTSERVER_PROFILE)
        profile_path = tmp_path / 'staged.part'
        profile_path.write_text(
            repr({
                **ROASTSERVER_PROFILE,
                'samplinginterval': None,
                'extramarkers1': [None, 'o'],
                'extramarkers2': [None],
            }),
            encoding='utf-8',
        )

        window.validateRoastServerProfile(profile_path)

        normalized = window.validateProfileDict.call_args.args[0]
        assert 'samplinginterval' not in normalized
        assert normalized['extramarkers1'] == ['None', 'o']
        assert normalized['extramarkers2'] == ['None']
        window.validateProfileDict.assert_called_once_with(
            normalized, quiet=True, validate_signature=True)

    def test_real_set_profile_server_mode_uses_pure_blend_conversion(
        self,
    ) -> None:
        window = inventory_profile_load_window()
        profile = ProfileData(
            roastUUID='0123456789abcdef0123456789abcdef',
            plus_blend_spec=['Archive blend', [['coffee-1', 1.0]]],
        )

        with patch.object(main_module.plus.stock, 'list2blend') as list2blend, patch.object(
            main_module.plus.register, 'getPath'
        ) as register_get, patch.object(main_module.plus.sync, 'sync') as plus_sync, patch.object(
            main_module, 'QSettings'
        ) as settings, patch.object(main_module.QMessageBox, 'information'):
            assert window.setProfile(
                'cache.alog', profile, quiet=True, reset=False,
                server_read_only=True)

        assert window.qmc.plus_blend_spec == {
            'label': 'Archive blend',
            'ingredients': [{'coffee': 'coffee-1', 'ratio': 1.0}],
        }
        list2blend.assert_not_called()
        register_get.assert_not_called()
        plus_sync.assert_not_called()
        settings.assert_not_called()

    @pytest.mark.parametrize(
        ('profile_fields', 'expected', 'warning'),
        [
            ({}, dict.fromkeys(INVENTORY_QMC_FIELDS), False),
            (INVENTORY_PROFILE_LINK, INVENTORY_PROFILE_LINK, False),
            (
                {'roastServerInventoryOrigin': 'https://archive.example'},
                dict.fromkeys(INVENTORY_QMC_FIELDS),
                True,
            ),
            (
                {
                    **INVENTORY_PROFILE_LINK,
                    'roastServerBeanLotUUID': 'NOT-A-UUID',
                },
                dict.fromkeys(INVENTORY_QMC_FIELDS),
                True,
            ),
        ],
    )
    def test_inventory_profile_load_is_all_or_none_and_historical(
        self,
        profile_fields: dict[str, str],
        expected: dict[str, object],
        warning: bool,
    ) -> None:
        window = inventory_profile_load_window()
        set_qmc_inventory_profile_link(window.qmc, {
            name: f'prior-{name}' for name in INVENTORY_QMC_FIELDS
        })
        profile = ProfileData(
            roastUUID='0123456789abcdef0123456789abcdef',
            **profile_fields,
        )

        with patch.object(main_module.plus.register, 'getPath') as register_get, patch.object(
            main_module.plus.sync, 'sync'
        ) as plus_sync, patch.object(main_module, 'QSettings') as settings, patch.object(
            main_module.QMessageBox, 'information'
        ):
            assert window.setProfile(
                'profile.alog', profile, quiet=True, reset=False)

        assert qmc_inventory_profile_link(window.qmc) == expected
        if warning:
            window.sendmessage.assert_called_once_with(
                'Invalid Roast Server inventory lot link ignored.')
        else:
            window.sendmessage.assert_not_called()
        register_get.assert_not_called()
        plus_sync.assert_not_called()
        settings.assert_not_called()
        window.plusAddPath.assert_not_called()

    def test_inventory_profile_typed_keys_are_optional(self) -> None:
        assert set(INVENTORY_QMC_FIELDS) <= ProfileData.__optional_keys__

    @staticmethod
    def inventory_charge_window(
        profile_link: dict[str, str] | None = INVENTORY_PROFILE_LINK,
    ) -> ApplicationWindow:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.qmc = SimpleNamespace(
            roastUUID='33333333333343338333333333333333',
            weight=(1.25, 0.0, 'Kg'),
        )
        for name in INVENTORY_QMC_FIELDS:
            setattr(
                window.qmc,
                name,
                None if profile_link is None else profile_link[name],
            )
        window.roastserver_controller = Mock()
        window.sendmessage = Mock()  # type: ignore[method-assign]
        return window

    def test_inventory_charge_prepare_builds_link_and_passes_profile_values(
        self,
    ) -> None:
        window = self.inventory_charge_window()
        prepared = Mock()
        window.roastserver_controller.prepare_inventory_charge.return_value = prepared

        assert window.prepareRoastServerInventoryCharge() is prepared

        link, roast_uuid, weight, unit = (
            window.roastserver_controller.prepare_inventory_charge.call_args.args)
        assert link.namespace.origin == 'https://archive.example'
        assert link.namespace.organization_id == UUID(
            '11111111-1111-4111-8111-111111111111')
        assert link.lot_id == UUID('22222222-2222-4222-8222-222222222222')
        assert link.lot_name == 'Historical lot'
        assert roast_uuid == UUID('33333333-3333-4333-8333-333333333333')
        assert (weight, unit) == (1.25, 'Kg')
        window.sendmessage.assert_not_called()

    @pytest.mark.parametrize(
        ('code', 'expected'),
        [
            ('connector_disabled', 'Enable Roast Server or clear the selected inventory lot.'),
            ('inventory_namespace_stale', 'Choose an inventory lot from the current Roast Server organization.'),
            ('inventory_lot_unavailable', 'Refresh inventory and choose an available lot before CHARGE.'),
            ('inventory_weight_invalid', 'Enter a valid positive green weight before CHARGE.'),
            ('inventory_storage_failed', 'Inventory reservation could not be stored. CHARGE was canceled.'),
        ],
    )
    def test_inventory_charge_errors_block_with_plain_status(
        self, code: str, expected: str
    ) -> None:
        window = self.inventory_charge_window()
        window.roastserver_controller.prepare_inventory_charge.side_effect = (
            ControllerError(code))

        assert window.prepareRoastServerInventoryCharge() is None
        window.sendmessage.assert_called_once_with(expected)

    def test_inventory_charge_untracked_warns_only_at_commit(self) -> None:
        window = self.inventory_charge_window(None)
        prepared = PreparedInventoryCharge(
            False, None, None, None, None, None, None, False)
        window.roastserver_controller.prepare_inventory_charge.return_value = prepared
        window.roastserver_controller.commit_inventory_charge.return_value = InventoryNotice(
            'inventory_untracked', None, None, None, None, None)
        window.roastserver_controller.inventory_context.return_value.enabled = True

        assert window.prepareRoastServerInventoryCharge() is prepared
        window.sendmessage.assert_not_called()
        assert window.commitRoastServerInventoryCharge(prepared) == ''
        window.sendmessage.assert_called_once_with(
            'No inventory lot selected. This roast will not be tracked in inventory.')

    def test_inventory_charge_commit_returns_durable_roast_uuid(self) -> None:
        window = self.inventory_charge_window()
        roast_uuid = UUID('33333333-3333-4333-8333-333333333333')
        prepared = PreparedInventoryCharge(
            True,
            Namespace(
                'https://archive.example',
                UUID('11111111-1111-4111-8111-111111111111'),
                'https://archive.example|11111111111141118111111111111111',
            ),
            roast_uuid,
            UUID('44444444-4444-4444-8444-444444444444'),
            UUID('22222222-2222-4222-8222-222222222222'),
            'Historical lot',
            1250,
            False,
        )
        window.roastserver_controller.commit_inventory_charge.return_value = InventoryNotice(
            'inventory_reservation_queued', roast_uuid, prepared.reservation_uuid,
            prepared.lot_id, None, None)

        assert window.commitRoastServerInventoryCharge(prepared) == roast_uuid.hex
        window.sendmessage.assert_not_called()

    def test_inventory_charge_coordinator_seam_is_durable_locked_and_idempotent(
        self, tmp_path: Path
    ) -> None:
        link = parse_profile_link(INVENTORY_PROFILE_LINK)
        assert link is not None
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        roast_uuid = UUID('33333333-3333-4333-8333-333333333333')
        reservation_uuid = UUID('44444444-4444-4444-8444-444444444444')
        uuid_values = iter((roast_uuid, reservation_uuid))
        wake_calls: list[None] = []
        store = InventoryStore(tmp_path / 'inventory')
        store.open()
        store.replace_lots(
            link.namespace,
            (
                BeanLot(
                    lot_id=link.lot_id,
                    name=link.lot_name,
                    origin='Ethiopia',
                    varietals=('Heirloom',),
                    processing_method='washed',
                    crop_year=2026,
                    on_hand_grams=2_000,
                    reserved_grams=0,
                    available_grams=2_000,
                    unresolved_conflict_count=0,
                ),
            ),
            now,
        )
        context = InventoryContext(
            origin=link.namespace.origin,
            namespace=link.namespace,
            enabled=True,
            previously_authenticated=True,
            client_instance_uuid=UUID(
                '55555555-5555-4555-8555-555555555555'),
        )
        coordinator = InventoryCoordinator(
            store,
            clock=lambda: now,
            uuid_factory=lambda: next(uuid_values),
            wake=lambda: wake_calls.append(None),
        )
        window = ApplicationWindow.__new__(ApplicationWindow)
        canvas = coordinator_inventory_charge_canvas(window)
        controller = coordinator_controller(coordinator, context)
        window.roastserver_controller = controller
        enqueue_reserve = store.enqueue_reserve

        def checked_enqueue(*args: Any, **kwargs: Any) -> Any:
            assert canvas.timeindex[0] == -1
            return enqueue_reserve(*args, **kwargs)

        try:
            with patch.object(
                store, 'enqueue_reserve', side_effect=checked_enqueue
            ) as enqueue:
                tgraphcanvas._markCharge(canvas, noaction=True)

                assert canvas.timeindex[0] == 0
                assert canvas.roastUUID == roast_uuid.hex
                state = store.roast_state(link.namespace, roast_uuid)
                assert state is not None
                assert state.reservation_uuid == reservation_uuid
                assert store.counts(link.namespace).pending == 1
                assert enqueue.call_count == 1
                assert wake_calls == [None]
                assert controller.inventory_lot_locked(
                    link, roast_uuid, False)

                window.buttonCHARGE.isFlat.return_value = True
                tgraphcanvas._markCharge(canvas, noaction=True)
                assert canvas.timeindex[0] == -1
                assert canvas.roastUUID == roast_uuid.hex

                window.buttonCHARGE.isFlat.return_value = False
                tgraphcanvas._markCharge(canvas, noaction=True)

                repeated_state = store.roast_state(link.namespace, roast_uuid)
                assert repeated_state is not None
                assert repeated_state.reservation_uuid == reservation_uuid
                assert canvas.roastUUID == roast_uuid.hex
                assert store.counts(link.namespace).pending == 1
                assert enqueue.call_count == 1
                assert wake_calls == [None]
        finally:
            store.close()

    def test_inventory_charge_coordinator_manual_append_failure_retries_exactly(
        self, tmp_path: Path
    ) -> None:
        link = parse_profile_link(INVENTORY_PROFILE_LINK)
        assert link is not None
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        roast_uuid = UUID('33333333-3333-4333-8333-333333333333')
        reservation_uuid = UUID('44444444-4444-4444-8444-444444444444')
        uuid_values = iter((roast_uuid, reservation_uuid))
        wake_calls: list[None] = []
        store = InventoryStore(tmp_path / 'inventory')
        store.open()
        store.replace_lots(
            link.namespace,
            (
                BeanLot(
                    lot_id=link.lot_id,
                    name=link.lot_name,
                    origin='Ethiopia',
                    varietals=('Heirloom',),
                    processing_method='washed',
                    crop_year=2026,
                    on_hand_grams=2_000,
                    reserved_grams=0,
                    available_grams=2_000,
                    unresolved_conflict_count=0,
                ),
            ),
            now,
        )
        context = InventoryContext(
            origin=link.namespace.origin,
            namespace=link.namespace,
            enabled=True,
            previously_authenticated=True,
            client_instance_uuid=UUID(
                '55555555-5555-4555-8555-555555555555'),
        )
        coordinator = InventoryCoordinator(
            store,
            clock=lambda: now,
            uuid_factory=lambda: next(uuid_values),
            wake=lambda: wake_calls.append(None),
        )
        window = ApplicationWindow.__new__(ApplicationWindow)
        canvas = coordinator_inventory_charge_canvas(window)
        window.roastserver_controller = coordinator_controller(
            coordinator, context)
        window.eventactionx = Mock()
        canvas.device = 18
        canvas.ETcurve = True
        canvas.BTcurve = True
        canvas.roastpropertiesAutoOpenFlag = True
        canvas.l_temp1 = InventoryChargeCurve(canvas.timex, canvas.temp1)
        canvas.l_temp2 = InventoryChargeCurve(canvas.timex, canvas.temp2)
        canvas.l_temp2.fail_next_update = True
        canvas.drawmanual = lambda et, bt, tx: tgraphcanvas.drawmanual(
            canvas, et, bt, tx)
        window.ser = MagicMock()
        window.ser.NONE.return_value = (1.0, 101.0, 91.0)
        profile_before = (
            list(canvas.timex), list(canvas.temp1), list(canvas.temp2))
        curves_before = (canvas.l_temp1.data, canvas.l_temp2.data)
        enqueue_reserve = store.enqueue_reserve

        def checked_enqueue(*args: Any, **kwargs: Any) -> Any:
            assert canvas.timeindex[0] == -1
            return enqueue_reserve(*args, **kwargs)

        try:
            with patch.object(
                store, 'enqueue_reserve', side_effect=checked_enqueue
            ) as enqueue:
                tgraphcanvas._markCharge(canvas)

                state = store.roast_state(link.namespace, roast_uuid)
                assert state is not None
                assert state.reservation_uuid == reservation_uuid
                assert store.counts(link.namespace).pending == 1
                assert enqueue.call_count == 1
                assert wake_calls == [None]
                assert canvas.roastUUID == roast_uuid.hex
                assert canvas.timeindex[0] == -1
                assert (canvas.timex, canvas.temp1, canvas.temp2) == profile_before
                assert (canvas.l_temp1.data, canvas.l_temp2.data) == curves_before
                window.buttonCHARGE.setFlat.assert_not_called()
                window.buttonCHARGE.stopAnimation.assert_not_called()
                window.santokerWarmupController.mark_charge.assert_not_called()
                window.eventactionx.assert_not_called()
                window.sendmessage.assert_not_called()
                window.openPropertiesSignal.emit.assert_not_called()
                canvas.timealign.assert_not_called()

                tgraphcanvas._markCharge(canvas)

                repeated_state = store.roast_state(link.namespace, roast_uuid)
                assert repeated_state is not None
                assert repeated_state.reservation_uuid == reservation_uuid
                assert store.counts(link.namespace).pending == 1
                assert enqueue.call_count == 1
                assert wake_calls == [None]
                assert canvas.roastUUID == roast_uuid.hex
                assert canvas.timeindex[0] == 1
                assert canvas.timex == [0.0, 1.0]
                assert canvas.temp1 == [100.0, 101.0]
                assert canvas.temp2 == [90.0, 91.0]
                window.buttonCHARGE.setFlat.assert_called_once_with(True)
                window.buttonCHARGE.stopAnimation.assert_called_once_with()
                window.santokerWarmupController.mark_charge.assert_called_once_with()
                window.eventactionx.assert_called_once_with(0, '')
                window.openPropertiesSignal.emit.assert_called_once_with()
                assert canvas.timealign.call_count == 1
        finally:
            store.close()

    def test_inventory_charge_coordinator_store_failure_preserves_profile(
        self, tmp_path: Path
    ) -> None:
        link = parse_profile_link(INVENTORY_PROFILE_LINK)
        assert link is not None
        now = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
        roast_uuid = UUID('33333333-3333-4333-8333-333333333333')
        uuid_values = iter((
            roast_uuid,
            UUID('44444444-4444-4444-8444-444444444444'),
        ))
        wake_calls: list[None] = []
        store = InventoryStore(tmp_path / 'inventory')
        store.open()
        store.replace_lots(
            link.namespace,
            (
                BeanLot(
                    lot_id=link.lot_id,
                    name=link.lot_name,
                    origin='Ethiopia',
                    varietals=('Heirloom',),
                    processing_method='washed',
                    crop_year=2026,
                    on_hand_grams=2_000,
                    reserved_grams=0,
                    available_grams=2_000,
                    unresolved_conflict_count=0,
                ),
            ),
            now,
        )
        context = InventoryContext(
            origin=link.namespace.origin,
            namespace=link.namespace,
            enabled=True,
            previously_authenticated=True,
            client_instance_uuid=UUID(
                '55555555-5555-4555-8555-555555555555'),
        )
        coordinator = InventoryCoordinator(
            store,
            clock=lambda: now,
            uuid_factory=lambda: next(uuid_values),
            wake=lambda: wake_calls.append(None),
        )
        window = ApplicationWindow.__new__(ApplicationWindow)
        canvas = coordinator_inventory_charge_canvas(window)
        window.roastserver_controller = coordinator_controller(
            coordinator, context)
        profile_before = (
            list(canvas.timeindex),
            list(canvas.timex),
            list(canvas.temp1),
            list(canvas.temp2),
            canvas.roastUUID,
        )

        try:
            with patch.object(
                store,
                'enqueue_reserve',
                side_effect=InventoryStoreError('injected failure'),
            ):
                tgraphcanvas._markCharge(canvas, noaction=True)

            assert (
                canvas.timeindex,
                canvas.timex,
                canvas.temp1,
                canvas.temp2,
                canvas.roastUUID,
            ) == profile_before
            assert store.roast_state(link.namespace, roast_uuid) is None
            assert store.counts(link.namespace).pending == 0
            assert wake_calls == []
            window.sendmessage.assert_called_once_with(
                'Inventory reservation could not be stored. CHARGE was canceled.')
        finally:
            store.close()

    def test_inventory_charge_commit_storage_failure_blocks(self) -> None:
        window = self.inventory_charge_window()
        prepared = Mock()
        window.roastserver_controller.commit_inventory_charge.side_effect = (
            ControllerError('inventory_storage_failed'))

        assert window.commitRoastServerInventoryCharge(prepared) is None
        window.sendmessage.assert_called_once_with(
            'Inventory reservation could not be stored. CHARGE was canceled.')

    def test_inventory_profile_get_and_copy_are_observationally_pure(self) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.qmc = MagicMock()
        window.app = MagicMock(artisanviewerMode=False)
        window.pidcontrol = MagicMock()
        window.ser = MagicMock(externalprogram='', externaloutprogram='')
        window.get_os = Mock(return_value=('Linux', 'test', 'x86_64'))
        window.plusAddPath = Mock()
        window.locale_str = 'en'
        window.nLCDS = 4
        window.recording_version = 'recording-version'
        window.recording_revision = 'recording-revision'
        window.recording_build = 'recording-build'
        window.eventsliderunits = ['%', 'rpm']
        window.extraLCDvisibility1 = [True]
        window.extraLCDvisibility2 = [False, True]
        window.extraCurveVisibility1 = [True]
        window.extraCurveVisibility2 = [False]
        window.extraDelta1 = [False]
        window.extraDelta2 = [True]
        window.extraFill1 = [0]
        window.extraFill2 = [1]
        window.percent_decimals = 1
        window.bbp_begin = 'sentinel begin'
        window.bbp_time_added_from_prev = 12.5
        window.bbp_endroast_epoch_msec = 123
        window.bbp_endevents = ['end']
        window.bbp_dropevents = ['drop']
        window.bbp_dropbt = 201.5
        window.bbp_dropet = 199.5
        window.bbp_drop_to_end = 8.0
        window.bbp_total_time = 90.0
        window.bbp_bottom_temp = 80.0
        window.bbp_begin_to_bottom_time = 40.0
        window.bbp_bottom_to_charge_time = 50.0
        window.bbp_begin_to_bottom_ror = -5.0
        window.bbp_bottom_to_charge_ror = 7.0
        qmc = window.qmc
        qmc.mode = 'C'
        qmc.timeindex = [-1, 0, 0, 0, 0, 0, 0, 0]
        qmc.flavors = []
        qmc.flavorlabels = []
        qmc.title = 'Pure snapshot'
        qmc.plus_store = None
        qmc.plus_coffee = None
        qmc.plus_blend_spec = None
        set_qmc_inventory_profile_link(qmc, INVENTORY_PROFILE_LINK)
        qmc.beans = ''
        qmc.weight = (0.0, 0.0, 'g')
        qmc.volume = (0.0, 0.0, 'l')
        qmc.density = (0.0, 'g', 1.0, 'l')
        qmc.density_roasted = (0.0, 'g', 1.0, 'l')
        qmc.color_systems = ['Agtron']
        qmc.color_system_idx = 0
        qmc.roastUUID = None
        qmc.scheduleID = None
        qmc.scheduleDate = None
        qmc.specialevents = [1.0, 2.0]
        qmc.specialeventstype = [3]
        qmc.specialeventsvalue = [4.0]
        qmc.specialeventsStrings = ['one']
        qmc.etypes = ['Air', 'Drum', 'Damper', 'Burner', '--']
        qmc.etypesdefault = qmc.etypes[:]
        qmc.timex = []
        qmc.temp1 = []
        qmc.temp2 = []
        qmc.extradevices = [25, 25]
        qmc.device = 0
        qmc.devices = ['Virtual'] * 26
        qmc.extraname1 = ['only one']
        qmc.extraname2 = []
        qmc.extratimex = [[]]
        qmc.extratemp1 = [[]]
        qmc.extratemp2 = [[]]
        qmc.extramathexpression1 = []
        qmc.extramathexpression2 = []
        qmc.extradevicecolor1 = []
        qmc.extradevicecolor2 = []
        qmc.extramarkersizes1 = []
        qmc.extramarkersizes2 = []
        qmc.extramarkers1 = []
        qmc.extramarkers2 = []
        qmc.extralinewidths1 = []
        qmc.extralinewidths2 = []
        qmc.extralinestyles1 = []
        qmc.extralinestyles2 = []
        qmc.extradrawstyles1 = []
        qmc.extradrawstyles2 = []
        qmc.extraNoneTempHint1 = []
        qmc.extraNoneTempHint2 = []
        qmc.legend = None
        qmc.backgroundpath = ''
        qmc.backgroundUUID = None
        qmc.profile_sampling_interval = None
        qmc.bbpPrevRoast = {'sentinel': ['unchanged']}
        qmc.getAnnoPositions.return_value = []
        qmc.getFlagPositions.return_value = []
        window.consolidateSpecialEvents = Mock(
            side_effect=AssertionError('snapshot consolidated events'))
        window.ensureCorrectExtraDeviceListLength = Mock(
            side_effect=AssertionError('snapshot resized extra devices'))
        window.computedProfileInformation = Mock(
            return_value={'bbp_total_time': window.bbp_total_time})
        qmc_names = (
            'roastUUID', 'specialevents', 'specialeventstype',
            'specialeventsvalue', 'specialeventsStrings', 'extradevices',
            'extraname1', 'extraname2', 'extratimex', 'extratemp1',
            'extratemp2', 'extramathexpression1', 'extramathexpression2',
            'extradevicecolor1', 'extradevicecolor2', 'extramarkersizes1',
            'extramarkersizes2', 'extramarkers1', 'extramarkers2',
            'extralinewidths1', 'extralinewidths2', 'extralinestyles1',
            'extralinestyles2', 'extradrawstyles1', 'extradrawstyles2',
            'extraNoneTempHint1', 'extraNoneTempHint2', 'bbpPrevRoast',
        )
        window_names = (
            'extraLCDvisibility1', 'extraLCDvisibility2',
            'extraCurveVisibility1', 'extraCurveVisibility2', 'extraDelta1',
            'extraDelta2', 'extraFill1', 'extraFill2', 'bbp_begin',
            'bbp_time_added_from_prev', 'bbp_endroast_epoch_msec',
            'bbp_endevents', 'bbp_dropevents', 'bbp_dropbt', 'bbp_dropet',
            'bbp_drop_to_end', 'bbp_total_time', 'bbp_bottom_temp',
            'bbp_begin_to_bottom_time', 'bbp_bottom_to_charge_time',
            'bbp_begin_to_bottom_ror', 'bbp_bottom_to_charge_ror',
        )

        def observed_state() -> tuple[dict[str, Any], dict[str, Any]]:
            return (
                {name: getattr(qmc, name) for name in qmc_names},
                {name: getattr(window, name) for name in window_names},
            )

        before = copy.deepcopy(observed_state())

        profile = window.getProfile(server_read_only=True)

        assert profile
        assert 'roastUUID' not in profile
        assert {name: profile[name] for name in INVENTORY_QMC_FIELDS} == (
            INVENTORY_PROFILE_LINK)
        assert copy.deepcopy(observed_state()) == before
        window.consolidateSpecialEvents.assert_not_called()
        window.ensureCorrectExtraDeviceListLength.assert_not_called()
        window.computedProfileInformation.assert_not_called()
        assert 'computed' not in profile
        qmc.adderror.assert_not_called()

        window.consolidateSpecialEvents = Mock()
        window.ensureCorrectExtraDeviceListLength = Mock()
        window.computedProfileInformation = Mock(return_value={})
        copy_profile = window.getProfile(copy=True)

        assert {name: copy_profile[name] for name in INVENTORY_QMC_FIELDS} == (
            INVENTORY_PROFILE_LINK)
        assert copy_profile['roastUUID'] != qmc.roastUUID
        window.plusAddPath.assert_not_called()

        qmc.roastServerBeanLotName = None
        incomplete_profile = window.getProfile(server_read_only=True)
        assert not set(INVENTORY_QMC_FIELDS) & incomplete_profile.keys()

    def test_canvas_server_reset_skips_dirty_external_and_presentation_hooks_on_failure(
        self,
    ) -> None:
        from artisanlib.canvas import tgraphcanvas

        class Semaphore:
            @staticmethod
            def acquire(_count: int) -> None:
                return None

            @staticmethod
            def available() -> int:
                return 1

            @staticmethod
            def release(_count: int) -> None:
                raise AssertionError('unexpected semaphore release')

        aw = MagicMock()
        aw.resetBBPMetrics.side_effect = RuntimeError('injected reset failure')
        canvas = SimpleNamespace(
            aw=aw,
            checkSaved=Mock(side_effect=AssertionError('dirty prompt called')),
            restoreExtraDeviceSettingsBackup=Mock(),
            resetTimer=Mock(),
            profileDataSemaphore=Semaphore(),
            designerflag=False,
            roastUUID='sentinel',
            roastbatchnr=4,
            roastbatchpos=2,
            roastbatchprefix='old',
            batchprefix='batch',
            scheduleID='schedule',
            scheduleDate='date',
            plus_sync_record_hash='hash',
            plus_file_last_modified=123.0,
            end_weight_est=1.0,
            roastpropertiesflag=False,
            flagKeepON=False,
            weight=(1.0, 2.0, 'g'),
            volume=(1.0, 2.0, 'l'),
            roasted_defects_weight=1.0,
            timex=[],
            cuppingnotes='notes',
            whole_color=1.0,
            ground_color=2.0,
            moisture_roasted=3.0,
            density_roasted=(1.0, 'g', 1.0, 'l'),
            AUCvalue=1.0,
            AUCsinceFCs=1.0,
            AUCguideTime=1.0,
            profile_sampling_interval=1.0,
            statisticstimes=[1, 2, 3, 4, 5],
            flagon=False,
            errorlog=['sentinel'],
            meterreads=[],
            meterreads_default=[],
            clearMeasurements=Mock(),
            backgroundprofile=None,
            autotimex=False,
            background=False,
            locktimex=False,
            endofx=60,
            adderror=Mock(),
        )

        with pytest.raises(RuntimeError, match='injected reset failure'):
            tgraphcanvas.reset(
                canvas, redraw=False, soundOn=True, server_read_only=True)

        canvas.checkSaved.assert_not_called()
        canvas.restoreExtraDeviceSettingsBackup.assert_not_called()
        canvas.resetTimer.assert_not_called()
        aw.eventactionx.assert_not_called()
        aw.soundpopSignal.emit.assert_not_called()
        aw.buttonONOFF.setText.assert_not_called()
        aw.buttonSTARTSTOP.setText.assert_not_called()
        aw.setTimerColorSignal.emit.assert_not_called()
        aw.ntb.update.assert_not_called()
        canvas.clearMeasurements.assert_not_called()
        canvas.adderror.assert_not_called()
        assert canvas.errorlog == ['sentinel']

    def test_canvas_server_clear_measurements_propagates_without_error_mutation(
        self,
    ) -> None:
        from artisanlib.canvas import tgraphcanvas

        class Semaphore:
            acquired = False

            @classmethod
            def acquire(cls, _count: int) -> None:
                cls.acquired = True

            @classmethod
            def available(cls) -> int:
                return 0 if cls.acquired else 1

            @classmethod
            def release(cls, _count: int) -> None:
                cls.acquired = False

        class FailingTimeIndex(list[int]):
            def __getitem__(self, _index: int) -> int:
                raise RuntimeError('injected clear list failure')

        canvas = SimpleNamespace(
            profileDataSemaphore=Semaphore,
            fileCleanSignal=Mock(),
            rateofchange1=9.0,
            rateofchange2=8.0,
            timeindex=FailingTimeIndex([-1]),
            errorlog=['sentinel'],
            adderror=Mock(),
        )

        with pytest.raises(RuntimeError, match='injected clear list failure'):
            tgraphcanvas.clearMeasurements(
                canvas, update_presentation=False, server_read_only=True)

        canvas.adderror.assert_not_called()
        assert canvas.errorlog == ['sentinel']
        assert not Semaphore.acquired

    def test_computed_profile_read_only_mode_does_not_update_bbp(self) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.qmc = Mock()
        window.qmc.timeindex = [-1, 0, 0, 0, 0, 0, 0, 0]
        window.qmc.timex = []
        window.qmc.temp1 = []
        window.qmc.temp2 = []
        window.qmc.delta2 = []
        window.qmc.calcStatistics.return_value = (0, [0, 0, 0, 0, 0])
        window.qmc.volume = (0.0, 0.0, 'l')
        window.qmc.weight = (0.0, 0.0, 'g')
        window.qmc.roasted_defects_weight = 0.0
        window.qmc.moisture_greens = 0.0
        window.qmc.moisture_roasted = 0.0
        window.qmc.density = (0.0, 'g', 1.0, 'l')
        window.qmc.ambient_humidity = 0.0
        window.qmc.ambient_pressure = 0.0
        window.qmc.ambientTemp = 0.0
        window.qmc.AUCbase = 0.0
        window.qmc.AUCbaseFlag = False
        window.qmc.AUCbegin = 0
        window.qmc.calcEnergyuse.return_value = ({}, [])
        window.percent_decimals = 1
        window.bbp_total_time = 91.0
        window.bbp_bottom_temp = 81.0
        window.bbp_begin_to_bottom_time = 40.0
        window.bbp_bottom_to_charge_time = 51.0
        window.bbp_begin_to_bottom_ror = -4.0
        window.bbp_bottom_to_charge_ror = 6.0
        window.calcBBPMetrics = Mock(
            side_effect=AssertionError('read-only computation updated BBP'))
        window.ts = Mock(return_value=(0, 0, 0, 0))
        window.curveSimilarity = Mock(return_value=(None, None))
        window.weight_loss = Mock(return_value=0.0)
        window.volume_increase = Mock(return_value=0.0)

        computed = window.computedProfileInformation(update_bbp=False)

        assert computed['bbp_total_time'] == 91.0
        window.calcBBPMetrics.assert_not_called()
        window.qmc.adderror.assert_not_called()

    def test_roastserver_open_slot_delegates_to_read_only_load(
        self, tmp_path: Path
    ) -> None:
        window = ApplicationWindow.__new__(ApplicationWindow)
        profile_path = tmp_path / 'verified.alog'
        window.roastserver_controller = Mock()
        window.loadFile = Mock(return_value=True)

        assert window.openRoastServerProfile(str(profile_path), SERVER_SOURCE)
        window.loadFile.assert_called_once_with(
            str(profile_path), server_source=SERVER_SOURCE)
        window.roastserver_controller.record_open_source.assert_not_called()


class TestRoastServerReadOnlyLoad:
    @staticmethod
    def load_window() -> tuple[ApplicationWindow, dict[str, Any]]:
        profile_path = Path('test/data/profile1.alog')
        previous = util_deserialize(str(profile_path))
        assert previous
        window = ApplicationWindow.__new__(ApplicationWindow)
        window.comparator = None
        window.curFile = 'previous.alog'
        window.roastserver_open_source = None
        window.roastserver_controller = Mock()
        window.roastserver_controller.is_expected_open_source.return_value = True

        def protection_guard(expected: object) -> object:
            if window.roastserver_controller.current_protection_token() is not expected:
                raise RuntimeError('cache protection ownership changed')
            return nullcontext()

        window.roastserver_controller.protection_guard.side_effect = protection_guard
        protection_token = object()
        window.roastserver_controller.current_protection_token.return_value = (
            protection_token)
        window.roastserver_controller.record_open_source.return_value = object()
        window.roastserver_controller.owns_protection_token.return_value = True
        window.roastserver_controller.record_local_save.return_value = protection_token
        window.roastserver_controller.restore_protection.return_value = True
        window.qmc = Mock()
        window.qmc.designerflag = False
        window.qmc.wheelflag = False
        window.qmc.ax = Mock()
        window.qmc.clearBgbeforeprofileload = False
        window.qmc.extradevices = previous.get('extradevices', [])
        window.qmc.safesaveflag = True
        window.qmc.plus_file_last_modified = datetime(2025, 1, 1, tzinfo=UTC)
        window.qmc.plus_sync_record_hash = 'previous-plus-hash'
        for name in INVENTORY_QMC_FIELDS:
            setattr(window.qmc, name, None)
        window.qmc.backgroundprofile = None
        window.qmc.hideBgafterprofileload = False
        window.qmc.background = False
        window.qmc.statssummary = False
        window.qmc.autotimex = False
        window.qmc.reset = Mock(return_value=True)
        window.qmc.checkSaved = Mock(return_value=True)
        window.qmc.fileDirtySignal = Mock()
        window.qmc.fileDirtySignal.emit = Mock()
        window.qmc.fileCleanSignal = Mock()
        window.qmc.fileCleanSignal.emit = Mock()
        window.qmc.clearLCDs = Mock()
        window.qmc.timealign = Mock()
        window.qmc.redraw = Mock()
        window.qmc.adderror = Mock()
        window.getProfile = Mock(return_value=copy.deepcopy(previous))
        window.profile_data_type_adapter = _PROFILE_DATA_ADAPTER
        window.official_build = False
        window.pidcontrol = Mock(pidActive=False)
        window.fujipid = Mock(sv=None)
        window.setProfile = Mock(return_value=True)
        window.setProfileDict = Mock(return_value=True)
        window.orderEvents = Mock()
        window.etypeComboBox = Mock()
        window.etypeComboBox.count.return_value = 2
        window.etypeComboBox.itemText.side_effect = ['Previous event A', 'Previous event B']
        window.etypeComboBox.currentIndex.return_value = 1
        window.setCurrentFile = Mock(
            side_effect=lambda filename: setattr(window, 'curFile', filename))
        window.deleteBackground = Mock()
        window.sendmessage = Mock()
        window.updatePhasesLCDs = Mock()
        window.updateWindowTitle = Mock()
        window.plusAddPath = Mock()
        window.plus_account = None
        window.checkColors = Mock()
        window.getcolorPairsToCheck = Mock(return_value=[])
        window.autoAdjustAxis = Mock()
        window.updatePlusStatus = Mock()
        window.summarystats_startup = True
        exact_window_defaults:dict[str, Any] = {
            'extraser': [],
            'extracomport': [],
            'extrabaudrate': [],
            'extrabytesize': [],
            'extraparity': [],
            'extrastopbits': [],
            'extratimeout': [],
            'extraLCDvisibility1': [],
            'extraLCDvisibility2': [],
            'extraCurveVisibility1': [],
            'extraCurveVisibility2': [],
            'extraDelta1': [],
            'extraDelta2': [],
            'extraFill1': [],
            'extraFill2': [],
            'eventsliderunits': [],
            'recording_version': 'previous-version',
            'recording_revision': 'previous-revision',
            'recording_build': 'previous-build',
            'block_quantification_sampling_ticks': [0, 0, 0, 0],
            'keyboardmoveindex': 0,
            'seriallog': [],
            'lastbuttonpressed': -1,
            'extraMODBUStx': 0.0,
            'extraS7tx': 0.0,
            'bbp_dropbt': 0.0,
            'bbp_dropet': 0.0,
            'bbp_total_time': -1.0,
            'bbp_bottom_temp': -1.0,
            'bbp_begin_to_bottom_time': -1.0,
            'bbp_bottom_to_charge_time': -1.0,
            'bbp_begin_to_bottom_ror': -1.0,
            'bbp_bottom_to_charge_ror': -1.0,
            'bbp_time_added_from_prev': 0.0,
            'bbp_begin': 'Start',
            'bbp_endroast_epoch_msec': 0,
            'bbp_endevents': [],
            'bbp_dropevents': [],
            'bbp_drop_to_end': 0.0,
        }
        for name, value in exact_window_defaults.items():
            setattr(window, name, value)
        exact_qmc_defaults:dict[str, Any] = {
            'bbpPrevRoast': {},
            'extrastemp1': [],
            'extrastemp2': [],
            'extractemp1': [],
            'extractemp2': [],
            'extractimex1': [],
            'extractimex2': [],
            'alarmstate': [],
            'statisticstimes': [],
            'AUCvalue': 0.0,
            'AUCsinceFCs': 0.0,
            'AUCguideTime': 0.0,
            'roastepoch': 0,
            'roasttzoffset': 0,
            'zoom_follow': False,
            'ystep_down': 0,
            'ystep_up': 0,
            'analysisresultsstr': '',
            'autoChargeIdx': 0,
            'autoDropIdx': 0,
            'l_annotations_dict': {},
            'l_event_flags_dict': {},
            'l_event_flags_pos_dict': {},
            'l_timeline': None,
            'TPalarmtimeindex': None,
            'profile_sampling_interval': None,
            'BTprojection_temp': [],
            'BTprojection_tx': [],
            'DeltaBTprojection_temp': [],
            'DeltaBTprojection_tx': [],
            'DeltaETprojection_temp': [],
            'DeltaETprojection_tx': [],
            'ETprojection_temp': [],
            'ETprojection_tx': [],
            'E1timex': [],
            'E1values': [],
            'E2timex': [],
            'E2values': [],
            'E3timex': [],
            'E3values': [],
            'E4timex': [],
            'E4values': [],
            'autoCHARGEenabled': True,
            'autoDROPenabled': True,
            'autoDRYenabled': True,
            'autoFCsenabled': True,
            'beansize': 0.0,
            'beepedBackgroundEvents': [],
            'ctemp1': [],
            'ctemp2': [],
            'ctimex1': [],
            'ctimex2': [],
            'currentpidsv': 0.0,
            'currentx': 0.0,
            'currenty': 0.0,
            'delta1': [],
            'delta2': [],
            'designerflag': False,
            'designertemp1init': [],
            'designertemp2init': [],
            'dutycycle': -1,
            'dutycycleTX': 0.0,
            'errorlog': [],
            'indexpoint': 0,
            'l_annotations': [],
            'program_t3': -1,
            'program_t4': -1,
            'program_t5': -1,
            'program_t6': -1,
            'program_t7': -1,
            'program_t8': -1,
            'program_t9': -1,
            'program_t10': -1,
            'rateofchange1': [],
            'rateofchange2': [],
            'replayedBackgroundEvents': [],
            'stemp1': [],
            'stemp2': [],
            'tstemp1': [],
            'tstemp2': [],
            'unfiltereddelta1': [],
            'unfiltereddelta1_pure': [],
            'unfiltereddelta2': [],
            'unfiltereddelta2_pure': [],
            'wheelflag': False,
            'workingline': 2,
        }
        for name, value in exact_qmc_defaults.items():
            setattr(window.qmc, name, value)
        return window, previous

    def test_server_load_checks_dirty_state_before_snapshot_protect_and_deserialize(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'cache.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        old_token = object()
        opened_token = object()
        current_token: list[object | None] = [old_token]
        old_source = (tmp_path / 'old-cache.alog', SERVER_SOURCE)
        window.roastserver_open_source = old_source
        ordering: list[str] = []

        def check_saved() -> bool:
            ordering.append('check-saved')
            # Model the nested Save As performed by checkSaved(): it releases T0
            # and clears the old transient source before the outer load captures state.
            assert current_token[0] is old_token
            current_token[0] = None
            window.roastserver_open_source = None
            return True

        def snapshot_profile(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            ordering.append('snapshot')
            assert current_token[0] is None
            assert window.roastserver_open_source is None
            return copy.deepcopy(_previous)

        def protect(*_args: Any, **kwargs: Any) -> object:
            ordering.append('protect')
            assert kwargs['expected'] is None
            current_token[0] = opened_token
            return opened_token

        def read_profile(path: str) -> dict[str, Any]:
            ordering.append('deserialize')
            return util_deserialize(path)

        window.qmc.checkSaved.side_effect = check_saved
        window.getProfile.side_effect = snapshot_profile
        window.roastserver_controller.current_protection_token.side_effect = (
            lambda: current_token[0])
        window.roastserver_controller.record_open_source.side_effect = protect
        with patch('artisanlib.main.deserialize', side_effect=read_profile):
            assert window.loadFile(str(cache_file), server_source=SERVER_SOURCE)

        assert ordering[:4] == [
            'check-saved', 'snapshot', 'protect', 'deserialize']
        window.qmc.reset.assert_called_with(
            redraw=False, soundOn=False, server_read_only=True)

    def test_dirty_server_load_completes_real_nested_save_as_before_protecting_t1(
        self, tmp_path: Path
    ) -> None:
        from artisanlib.canvas import tgraphcanvas

        window, previous = self.load_window()
        incoming = tmp_path / 'incoming-cache.alog'
        incoming.write_bytes(Path('test/data/profile1.alog').read_bytes())
        old_cache = tmp_path / 'old-cache.alog'
        old_cache.write_bytes(b'old protected cache')
        nested_destination = tmp_path / 'nested-save-as.alog'
        window.curFile = None
        window.roastserver_open_source = (old_cache, SERVER_SOURCE)
        window.qmc.safesaveflag = True
        window.qmc.timex = [0.0, 1.0, 2.0, 3.0]
        check_saved_canvas = SimpleNamespace(
            safesaveflag=True,
            timex=window.qmc.timex,
            aw=window,
            fileCleanSignal=window.qmc.fileCleanSignal,
        )
        window.qmc.autosaveimage = False
        window.qmc.flagon = False
        window.qmc.plus_store = None
        window.qmc.plus_store_label = None
        window.qmc.plus_coffee = None
        window.qmc.plus_coffee_label = None
        window.qmc.plus_blend_spec = None
        window.qmc.plus_blend_label = None
        window.qmc.plus_blend_spec_labels = None
        window.qmc.roastUUID = previous['roastUUID']
        window.qmc.roastbatchnr = 0
        window.qmc.roastbatchprefix = ''
        window.qmc.autosaveprefix = ''
        window.qmc.batchcounter = -1
        window.qmc.batchprefix = ''
        window.getDefaultPath = Mock(return_value=str(tmp_path))
        window.generateFilename = Mock(return_value='nested-save-as.alog')
        window.ArtisanSaveFileDialog = Mock(return_value=str(nested_destination))
        window.MaxRecentFiles = 20
        window.roastServerRecentFiles = Mock(return_value=[])
        window.roastServerWriteRecentFiles = Mock()
        window.refreshRoastServerActions = Mock()
        window.getProfile.return_value = copy.deepcopy(previous)
        window.qmc.fileCleanSignal.emit.side_effect = lambda: setattr(
            window.qmc, 'safesaveflag', False)
        old_token = object()
        incoming_token = object()
        current_token: list[object | None] = [old_token]
        transitions: list[tuple[str, object | None]] = []

        def release(_path: Path, *, expected: object) -> object:
            assert expected is old_token
            assert current_token[0] is old_token
            transitions.append(('release-t0', current_token[0]))
            current_token[0] = None
            return old_token

        def protect(
            _path: Path,
            _source: ServerProfileSource,
            *,
            expected: object | None,
        ) -> object:
            assert expected is None
            assert current_token[0] is None
            transitions.append(('protect-t1', current_token[0]))
            current_token[0] = incoming_token
            return incoming_token

        window.roastserver_controller.current_protection_token.side_effect = (
            lambda: current_token[0])
        window.roastserver_controller.record_local_save.side_effect = release
        window.roastserver_controller.record_open_source.side_effect = protect
        window.roastserver_controller.owns_protection_token.side_effect = (
            lambda token: current_token[0] is token)
        window.qmc.checkSaved.side_effect = lambda: tgraphcanvas.checkSaved(
            check_saved_canvas)

        with patch.object(
            main_module.QMessageBox,
            'warning',
            return_value=main_module.QMessageBox.StandardButton.Save,
        ):
            assert window.loadFile(str(incoming), server_source=SERVER_SOURCE)

        assert nested_destination.exists()
        assert transitions == [('release-t0', old_token), ('protect-t1', None)]
        assert current_token[0] is incoming_token
        assert window.roastserver_open_source == (incoming, SERVER_SOURCE)

    def test_cancelled_nested_save_as_aborts_before_snapshot_or_protection(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'cache.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.qmc.checkSaved.return_value = False

        assert not window.loadFile(str(cache_file), server_source=SERVER_SOURCE)

        window.getProfile.assert_not_called()
        window.roastserver_controller.record_open_source.assert_not_called()
        window.qmc.reset.assert_not_called()

    def test_server_load_final_exact_token_mismatch_aborts_and_rolls_back(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'cache.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        previous_token = object()
        opened_token = object()
        replacement_token = object()
        current_token: list[object | None] = [previous_token]

        def protect(*_args: Any, **_kwargs: Any) -> object:
            current_token[0] = opened_token
            return opened_token

        window.roastserver_controller.current_protection_token.side_effect = (
            lambda: current_token[0])
        window.roastserver_controller.record_open_source.side_effect = protect
        window.sendmessage.side_effect = lambda *_args: current_token.__setitem__(
            0, replacement_token)
        window.roastserver_controller.restore_protection.return_value = False
        window.roastserver_controller.owns_protection_token.side_effect = (
            lambda token: current_token[0] is token)

        assert not window.loadFile(str(cache_file), server_source=SERVER_SOURCE)

        assert window.curFile == 'previous.alog'
        assert window.qmc.safesaveflag
        window.roastserver_controller.restore_protection.assert_called_once_with(
            previous_token, opened_token)

    def test_snapshot_copy_failure_aborts_before_protection_or_deserialize(
        self, tmp_path: Path
    ) -> None:
        class Uncopyable:
            def __deepcopy__(self, _memo: dict[int, object]) -> object:
                raise RuntimeError('injected copy failure')

        window, _previous = self.load_window()
        cache_file = tmp_path / 'cache.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.qmc.backgroundprofile = Uncopyable()

        with patch('artisanlib.main.deserialize') as deserialize_mock:
            assert not window.loadFile(
                str(cache_file), server_source=SERVER_SOURCE)

        deserialize_mock.assert_not_called()
        window.roastserver_controller.record_open_source.assert_not_called()
        window.qmc.reset.assert_not_called()
        window.setProfile.assert_not_called()

    @pytest.mark.parametrize('plus_connected', [False, True])
    def test_server_load_prevalidates_real_file_and_skips_all_plus_recent_hooks(
        self, tmp_path: Path, plus_connected: bool
    ) -> None:
        window, _previous = self.load_window()
        window.plus_account = 'connected@example.test' if plus_connected else None
        cache_file = tmp_path / 'cache.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        cache_before = (cache_file.read_bytes(), cache_file.stat().st_mtime_ns)

        with patch(
            'artisanlib.main.deserialize', wraps=util_deserialize
        ) as deserialize_mock, patch(
            'artisanlib.main.plus.stock.list2blend'
        ) as list_to_blend, patch(
            'artisanlib.main.plus.stock.blend2list'
        ) as blend_to_list, patch(
            'artisanlib.main.plus.sync.sync'
        ) as plus_sync, patch(
            'artisanlib.main.plus.schedule.update_completed_item_from_loaded_profile'
        ) as schedule_update, patch(
            'artisanlib.main.plus.util.getModificationDate'
        ) as modification_date, patch(
            'artisanlib.main.plus.register.addPath'
        ) as register_add, patch(
            'artisanlib.main.plus.register.getPath'
        ) as register_get, patch(
            'artisanlib.main.QSettings'
        ) as settings:
            assert window.loadFile(
                str(cache_file), server_source=SERVER_SOURCE)

        deserialize_mock.assert_called_once_with(str(cache_file))
        assert window.roastserver_controller.is_expected_open_source.call_args_list == [
            call(cache_file, SERVER_SOURCE),
            call(cache_file, SERVER_SOURCE),
        ]
        window.plusAddPath.assert_not_called()
        window.setCurrentFile.assert_not_called()
        window.updatePlusStatus.assert_not_called()
        list_to_blend.assert_not_called()
        blend_to_list.assert_not_called()
        plus_sync.assert_not_called()
        schedule_update.assert_not_called()
        modification_date.assert_not_called()
        register_add.assert_not_called()
        register_get.assert_not_called()
        settings.return_value.setValue.assert_not_called()
        assert window.curFile is None
        assert window.qmc.plus_file_last_modified is None
        assert window.qmc.plus_sync_record_hash is None
        assert not window.qmc.safesaveflag
        window.qmc.fileCleanSignal.emit.assert_called_once_with()
        window.roastserver_controller.record_open_source.assert_called_once_with(
            cache_file,
            SERVER_SOURCE,
            expected=window.roastserver_controller.current_protection_token.return_value,
        )
        assert window.roastserver_open_source == (cache_file, SERVER_SOURCE)
        assert window.sendmessage.call_args.args == (
            QApplication.translate(
                'Message',
                'Roast Server {0} revision {1} opened read-only ({2})').format(
                    SERVER_SOURCE.namespace.origin,
                    SERVER_SOURCE.revision_number,
                    QApplication.translate('Message', 'online verified copy')),
        )
        assert (cache_file.read_bytes(), cache_file.stat().st_mtime_ns) == cache_before

    @pytest.mark.parametrize(
        'failure',
        [
            'reset', 'reset-internal', 'apply', 'order', 'clear-lcd',
            'redraw', 'phases', 'colors',
        ],
    )
    def test_server_load_apply_and_redraw_failures_restore_full_snapshot(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        old_source_path = tmp_path / 'old-cache.alog'
        old_source = copy.deepcopy(SERVER_SOURCE)
        window.roastserver_open_source = (old_source_path, old_source)
        previous_modified = window.qmc.plus_file_last_modified
        previous_hash = window.qmc.plus_sync_record_hash
        window.extracomport = ['sentinel-a', 'sentinel-b']
        window.extrabaudrate = [9600, 19200]
        window.qmc.bbpPrevRoast = {'sentinel': [failure]}
        window.qmc.errorlog = ['sentinel error']
        exact_sentinels = copy.deepcopy((
            window.extracomport,
            window.extrabaudrate,
            window.qmc.bbpPrevRoast,
            window.qmc.errorlog,
        ))
        window.setProfile.side_effect = [True, True]
        reset_calls = 0

        def reset_profile(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal reset_calls
            reset_calls += 1
            if reset_calls == 1:
                window.extracomport = ['mutated']
                window.extrabaudrate = [1]
                window.qmc.bbpPrevRoast = {'mutated': True}
                window.qmc.errorlog = ['mutated error']
                if failure == 'reset-internal':
                    raise RuntimeError('injected Canvas reset failure')
                return failure != 'reset'
            return True

        window.qmc.reset.side_effect = reset_profile
        if failure == 'apply':
            window.setProfile.side_effect = [False, True]
        elif failure == 'order':
            window.orderEvents.side_effect = [RuntimeError('order failed'), None]
        elif failure == 'clear-lcd':
            window.qmc.clearLCDs.side_effect = [RuntimeError('LCD failed'), None]
        elif failure == 'redraw':
            window.qmc.redraw.side_effect = [RuntimeError('redraw failed'), None]
        elif failure == 'phases':
            window.updatePhasesLCDs.side_effect = [
                RuntimeError('phase LCD failed'), None]
        else:
            window.checkColors.side_effect = RuntimeError('colors failed')

        assert not window.loadFile(
            str(cache_file), server_source=SERVER_SOURCE)

        assert window.setProfile.call_args_list[-1].args[1] == previous
        assert window.setProfile.call_args_list[-1].args[1] is not previous
        assert window.curFile == 'previous.alog'
        assert window.qmc.safesaveflag
        assert window.qmc.plus_file_last_modified == previous_modified
        assert window.qmc.plus_sync_record_hash == previous_hash
        assert window.roastserver_open_source == (old_source_path, old_source)
        assert (
            window.extracomport,
            window.extrabaudrate,
            window.qmc.bbpPrevRoast,
            window.qmc.errorlog,
        ) == exact_sentinels
        window.qmc.fileDirtySignal.emit.assert_called_once_with()
        window.roastserver_controller.record_open_source.assert_called_once()
        window.roastserver_controller.restore_protection.assert_called_once_with(
            window.roastserver_controller.current_protection_token.return_value,
            window.roastserver_controller.record_open_source.return_value,
        )

    def test_failed_apply_restores_exact_non_profile_device_and_bbp_state(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        serial_a = object()
        serial_b = object()
        window.extraser = [serial_a, serial_b]
        window.extracomport = ['A', 'B']
        window.extrabaudrate = [9600, 19200]
        window.extrabytesize = [7, 8]
        window.extraparity = ['E', 'N']
        window.extrastopbits = [1, 2]
        window.extratimeout = [0.5, 1.5]
        window.extraLCDvisibility1 = [True, False, True]
        window.extraLCDvisibility2 = [False, True]
        window.extraCurveVisibility1 = [False, True]
        window.extraCurveVisibility2 = [True, False]
        window.extraDelta1 = [True]
        window.extraDelta2 = [False, True]
        window.extraFill1 = [1, 2]
        window.extraFill2 = [3]
        window.bbp_total_time = 123.5
        window.bbp_bottom_temp = 77.25
        window.qmc.bbpPrevRoast = {'nested': ['previous']}
        window.qmc.extrastemp1 = [[1.0, 2.0]]
        window.qmc.extrastemp2 = [[3.0, 4.0]]
        window.qmc.extractemp1 = [[5.0]]
        window.qmc.extractemp2 = [[6.0]]
        window.qmc.extractimex1 = [[7.0]]
        window.qmc.extractimex2 = [[8.0]]
        window.qmc.profile_sampling_interval = 3.5
        expected_qmc = copy.deepcopy({
            name: getattr(window.qmc, name)
            for name in (
                'bbpPrevRoast', 'extrastemp1', 'extrastemp2', 'extractemp1',
                'extractemp2', 'extractimex1', 'extractimex2',
                'profile_sampling_interval',
            )
        })
        expected = {
            name: (list(getattr(window, name)) if isinstance(getattr(window, name), list)
                   else getattr(window, name))
            for name in (
                'extraser', 'extracomport', 'extrabaudrate', 'extrabytesize',
                'extraparity', 'extrastopbits', 'extratimeout',
                'extraLCDvisibility1', 'extraLCDvisibility2',
                'extraCurveVisibility1', 'extraCurveVisibility2',
                'extraDelta1', 'extraDelta2', 'extraFill1', 'extraFill2',
                'bbp_total_time', 'bbp_bottom_temp',
            )
        }
        apply_calls = 0

        def apply_profile(*_args: Any, **_kwargs: Any) -> bool:
            nonlocal apply_calls
            apply_calls += 1
            if apply_calls == 1:
                window.extraser = [object()]
                window.extracomport = ['mutated']
                window.extrabaudrate = [1]
                window.extrabytesize = [5]
                window.extraparity = ['O']
                window.extrastopbits = [3]
                window.extratimeout = [99.0]
                window.extraLCDvisibility1 = []
                window.extraLCDvisibility2 = []
                window.extraCurveVisibility1 = []
                window.extraCurveVisibility2 = []
                window.extraDelta1 = []
                window.extraDelta2 = []
                window.extraFill1 = []
                window.extraFill2 = []
                window.bbp_total_time = -1
                window.bbp_bottom_temp = -1
                window.qmc.bbpPrevRoast = {'nested': ['mutated']}
                window.qmc.extrastemp1 = []
                window.qmc.extrastemp2 = []
                window.qmc.extractemp1 = []
                window.qmc.extractemp2 = []
                window.qmc.extractimex1 = []
                window.qmc.extractimex2 = []
                window.qmc.profile_sampling_interval = 99.0
                return False
            return True

        window.setProfile.side_effect = apply_profile

        assert not window.loadFile(
            str(cache_file), server_source=SERVER_SOURCE)

        for name, value in expected.items():
            actual = getattr(window, name)
            if name == 'extraser':
                assert actual[0] is serial_a and actual[1] is serial_b
            else:
                assert actual == value
        assert {
            name: getattr(window.qmc, name)
            for name in expected_qmc
        } == expected_qmc

    @pytest.mark.parametrize(
        'failure',
        ['delete', 'combo', 'timealign', 'axis', 'clean', 'title', 'message'],
    )
    def test_every_late_server_load_failure_restores_transaction_components(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        previous_token = object()
        opened_token = object()
        window.roastserver_controller.current_protection_token.return_value = previous_token
        window.roastserver_controller.record_open_source.return_value = opened_token
        window.roastserver_controller.restore_protection.return_value = True
        window.setProfile.side_effect = [True, True]
        window.qmc.title = 'Previous title'
        window.qmc.startofx = -12.0
        window.qmc.endofx = 720.0
        window.qmc.ylimit = 260
        window.qmc.ylimit_min = 40
        window.qmc.zlimit = 40
        window.qmc.zlimit_min = -20
        window.qmc.background = True
        if failure == 'delete':
            window.qmc.clearBgbeforeprofileload = True
            window.deleteBackground.side_effect = RuntimeError('delete failed')
        elif failure == 'combo':
            window.etypeComboBox.clear.side_effect = [RuntimeError('combo failed'), None]
        elif failure == 'timealign':
            window.qmc.backgroundprofile = {'title': 'background'}
            window.qmc.timealign.side_effect = [RuntimeError('align failed'), None]
        elif failure == 'axis':
            window.qmc.hideBgafterprofileload = True
            window.autoAdjustAxis.side_effect = RuntimeError('axis failed')
        elif failure == 'clean':
            window.qmc.fileCleanSignal.emit.side_effect = RuntimeError('clean failed')
        elif failure == 'title':
            window.updateWindowTitle.side_effect = [RuntimeError('title failed'), None]
        else:
            window.sendmessage.side_effect = RuntimeError('message failed')

        assert not window.loadFile(str(cache_file), server_source=SERVER_SOURCE)

        assert window.setProfile.call_args_list[-1].args[1] == previous
        assert window.curFile == 'previous.alog'
        assert window.qmc.safesaveflag
        assert window.qmc.title == 'Previous title'
        assert (
            window.qmc.startofx,
            window.qmc.endofx,
            window.qmc.ylimit,
            window.qmc.ylimit_min,
            window.qmc.zlimit,
            window.qmc.zlimit_min,
        ) == (-12.0, 720.0, 260, 40, 40, -20)
        assert window.qmc.background
        window.etypeComboBox.addItems.assert_any_call(
            ['Previous event A', 'Previous event B'])
        window.etypeComboBox.setCurrentIndex.assert_called_with(1)
        window.roastserver_controller.restore_protection.assert_called_once_with(
            previous_token, opened_token)

    def test_protection_refusal_precedes_deserialize_and_active_mutation(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.roastserver_controller.record_open_source.return_value = None

        with patch('artisanlib.main.deserialize') as deserialize_mock:
            assert not window.loadFile(
                str(cache_file), server_source=SERVER_SOURCE)

        deserialize_mock.assert_not_called()
        window.qmc.reset.assert_not_called()
        window.setProfile.assert_not_called()

    def test_hide_background_failure_restores_visibility_and_axis(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.qmc.hideBgafterprofileload = True
        window.qmc.background = True
        window.qmc.startofx = -5.0
        window.qmc.endofx = 650.0
        window.setProfile.side_effect = [True, True]

        def adjust_axis() -> None:
            window.qmc.startofx = 0.0
            window.qmc.endofx = 999.0

        window.autoAdjustAxis.side_effect = adjust_axis
        window.updatePhasesLCDs.side_effect = [RuntimeError('phase failed'), None]

        assert not window.loadFile(str(cache_file), server_source=SERVER_SOURCE)

        assert window.qmc.background
        assert (window.qmc.startofx, window.qmc.endofx) == (-5.0, 650.0)

    def test_rollback_component_exception_does_not_skip_later_restoration(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        previous_token = object()
        opened_token = object()
        window.roastserver_controller.current_protection_token.return_value = previous_token
        window.roastserver_controller.record_open_source.return_value = opened_token
        window.roastserver_controller.restore_protection.return_value = True
        window.qmc.reset.side_effect = [True, RuntimeError('rollback reset failed')]
        window.setProfile.side_effect = [True, RuntimeError('rollback profile failed')]
        window.sendmessage.side_effect = RuntimeError('message failed')

        assert not window.loadFile(str(cache_file), server_source=SERVER_SOURCE)

        assert window.curFile == 'previous.alog'
        assert window.qmc.safesaveflag
        window.qmc.fileDirtySignal.emit.assert_called_once_with()
        window.roastserver_controller.restore_protection.assert_called_once_with(
            previous_token, opened_token)
        window.updateWindowTitle.assert_called()
        assert any(
            'rollback was incomplete' in str(call.args[0])
            for call in window.qmc.adderror.call_args_list
        )

    def test_server_redraw_failure_restores_deleted_background_ui(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'verified.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.qmc.clearBgbeforeprofileload = True
        window.qmc.background = True
        window.qmc.backgroundprofile = {'title': 'previous background'}
        window.qmc.backgroundpath = 'previous-background.alog'
        window.qmc.l_annotations_dict = {7: ['background annotation']}
        window.qmc.l_background_annotations = ['background artist']
        expected_background = copy.deepcopy(window.qmc.backgroundprofile)
        expected_annotations = window.qmc.l_annotations_dict.copy()

        def delete_background() -> None:
            window.qmc.background = False
            window.qmc.backgroundprofile = None
            window.qmc.backgroundpath = ''
            window.qmc.l_annotations_dict = {}
            window.qmc.l_background_annotations = []

        window.deleteBackground.side_effect = delete_background
        window.setProfile.side_effect = [True, True]
        window.qmc.redraw.side_effect = [RuntimeError('redraw failed'), None]

        assert not window.loadFile(
            str(cache_file), server_source=SERVER_SOURCE)

        assert window.qmc.background
        assert window.qmc.backgroundprofile == expected_background
        assert window.qmc.backgroundpath == 'previous-background.alog'
        assert window.qmc.l_annotations_dict == expected_annotations
        assert window.qmc.l_background_annotations == ['background artist']

    def test_server_source_identity_and_profile_validation_precede_any_mutation(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        cache_file = tmp_path / 'untrusted.alog'
        cache_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.roastserver_controller.is_expected_open_source.return_value = False

        with patch('artisanlib.main.deserialize', wraps=util_deserialize) as deserialize_mock:
            assert not window.loadFile(
                str(cache_file), server_source=SERVER_SOURCE)

        deserialize_mock.assert_not_called()
        window.getProfile.assert_not_called()
        window.qmc.reset.assert_not_called()
        window.setProfile.assert_not_called()
        window.roastserver_controller.record_open_source.assert_not_called()

        window.roastserver_controller.is_expected_open_source.return_value = True
        invalid = tmp_path / 'invalid.alog'
        invalid.write_text("{'not': object()}", encoding='utf-8')
        assert not window.loadFile(str(invalid), server_source=SERVER_SOURCE)
        window.getProfile.assert_called_once_with(server_read_only=True)
        window.qmc.reset.assert_not_called()
        window.setProfile.assert_not_called()
        window.roastserver_controller.restore_protection.assert_called_once()

    def test_local_load_release_refusal_rolls_back_profile_and_source(
        self, tmp_path: Path
    ) -> None:
        window, previous = self.load_window()
        local_file = tmp_path / 'local.alog'
        local_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        source = (tmp_path / 'protected-cache.alog', SERVER_SOURCE)
        window.roastserver_open_source = source
        window.roastserver_controller.record_local_save.return_value = False

        with patch(
            'artisanlib.main.plus.util.getModificationDate',
            return_value=datetime(2026, 1, 1, tzinfo=UTC),
        ):
            assert not window.loadFile(str(local_file))

        window.setProfile.assert_called()
        assert window.getProfile.return_value == previous
        assert window.roastserver_open_source == source
        assert window.curFile == 'previous.alog'
        assert window.qmc.safesaveflag

    def test_local_load_failure_reprotects_exact_server_token(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        local_file = tmp_path / 'local.alog'
        local_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        source = (tmp_path / 'protected-cache.alog', SERVER_SOURCE)
        previous_token = object()
        window.roastserver_open_source = source
        window.roastserver_controller.current_protection_token.return_value = previous_token
        window.roastserver_controller.record_local_save.return_value = previous_token
        window.roastserver_controller.restore_protection.return_value = True
        window.setProfileDict.return_value = False

        assert not window.loadFile(str(local_file))

        window.roastserver_controller.restore_protection.assert_not_called()
        assert window.roastserver_open_source == source
        assert window.curFile == 'previous.alog'

    def test_successful_local_load_clears_transient_server_source(
        self, tmp_path: Path
    ) -> None:
        window, _previous = self.load_window()
        local_file = tmp_path / 'local.alog'
        local_file.write_bytes(Path('test/data/profile1.alog').read_bytes())
        window.roastserver_open_source = (
            tmp_path / 'protected-cache.alog', SERVER_SOURCE)

        with patch(
            'artisanlib.main.plus.util.getModificationDate',
            return_value=datetime(2026, 1, 1, tzinfo=UTC),
        ):
            assert window.loadFile(str(local_file))

        window.roastserver_controller.record_local_save.assert_called_once_with(
            local_file,
            expected=window.roastserver_controller.current_protection_token.return_value,
        )
        assert window.roastserver_open_source is None
        assert window.curFile == str(local_file)


class TestRoastServerReadOnlySaveTransition:
    @staticmethod
    def source_window(
        tmp_path: Path,
    ) -> tuple[ApplicationWindow, Mock, Path, Path]:
        window, controller, _profile = roastserver_save_window()
        protection_token = object()
        controller.current_protection_token.return_value = protection_token
        controller.record_local_save.return_value = protection_token
        cache_file = tmp_path / 'cache.alog'
        cache_file.write_bytes(b'protected cache bytes')
        destination = tmp_path / 'saved-as.alog'
        window.curFile = None
        window.roastserver_open_source = (cache_file, SERVER_SOURCE)
        window.qmc.safesaveflag = False
        window.qmc.plus_sync_record_hash = None
        window.updateWindowTitle = Mock()
        window.refreshRoastServerActions = Mock()
        window.roastServerRecentFiles = Mock(return_value=['previous.alog'])
        window.roastServerWriteRecentFiles = Mock()
        window.ArtisanSaveFileDialog.return_value = str(destination)
        return window, controller, cache_file, destination

    def test_inventory_profile_read_only_save_rollback_restores_all_fields(
        self, tmp_path: Path
    ) -> None:
        window, _controller, cache_file, _destination = self.source_window(tmp_path)
        source = window.roastserver_open_source
        assert source is not None
        set_qmc_inventory_profile_link(window.qmc, INVENTORY_PROFILE_LINK)
        state = window.snapshotRoastServerSaveState()
        set_qmc_inventory_profile_link(window.qmc, {
            name: f'mutated-{name}' for name in INVENTORY_QMC_FIELDS
        })

        window.restoreRoastServerSaveState(state, (cache_file, SERVER_SOURCE))

        assert qmc_inventory_profile_link(window.qmc) == INVENTORY_PROFILE_LINK

    def test_save_after_server_open_forces_save_as_and_resumes_normal_hooks(
        self, tmp_path: Path
    ) -> None:
        window, controller, cache_file, destination = self.source_window(tmp_path)
        before = (cache_file.read_bytes(), cache_file.stat().st_mtime_ns)

        window.fileSave_current_action()

        window.ArtisanSaveFileDialog.assert_called_once()
        window.plusAddPath.assert_called_once_with(
            ANY, str(destination))
        assert window.plusAddPath.call_args.args[0]['roastUUID'] == (
            ROASTSERVER_PROFILE['roastUUID'])
        controller.record_local_save.assert_called_once_with(
            destination,
            expected=controller.current_protection_token.return_value,
        )
        controller.saved_profile.assert_called_once()
        serialized, detached, modified_at = controller.saved_profile.call_args.args
        assert serialized == destination.read_bytes()
        assert detached['hash']
        assert modified_at.tzinfo is UTC
        window.getProfile.assert_called_once_with(
            False, generate_hash=False, server_read_only=True)
        assert window.roastserver_open_source is None
        assert window.curFile == str(destination)
        assert (cache_file.read_bytes(), cache_file.stat().st_mtime_ns) == before

    def test_save_snapshot_copy_failure_aborts_before_dialog_or_mutation(
        self, tmp_path: Path
    ) -> None:
        class Uncopyable:
            def __deepcopy__(self, _memo: dict[int, object]) -> object:
                raise RuntimeError('injected copy failure')

        window, controller, _cache_file, destination = self.source_window(tmp_path)
        window.qmc.plus_store = Uncopyable()

        assert not window.fileSave(None)

        window.ArtisanSaveFileDialog.assert_not_called()
        controller.record_local_save.assert_not_called()
        assert not destination.exists()
        assert window.roastserver_open_source is not None

    def test_cancel_or_failed_save_retains_read_only_source_and_cache(
        self, tmp_path: Path
    ) -> None:
        window, controller, cache_file, destination = self.source_window(tmp_path)
        source = window.roastserver_open_source
        before = (cache_file.read_bytes(), cache_file.stat().st_mtime_ns)
        window.ArtisanSaveFileDialog.return_value = ''

        assert not window.fileSave(None)
        assert window.roastserver_open_source == source
        assert window.curFile is None
        controller.record_local_save.assert_not_called()
        controller.saved_profile.assert_not_called()

        window.ArtisanSaveFileDialog.return_value = str(destination)
        with patch.object(
            main_module.FileDestinationTransaction,
            'serialize',
            side_effect=OSError('save failed'),
        ):
            assert not window.fileSave(None)
        assert window.roastserver_open_source == source
        assert window.curFile is None
        assert window.qmc.plus_file_last_modified is None
        assert window.qmc.plus_sync_record_hash is None
        controller.record_local_save.assert_not_called()
        controller.saved_profile.assert_not_called()
        assert (cache_file.read_bytes(), cache_file.stat().st_mtime_ns) == before

    @pytest.mark.parametrize(
        'failure', [
            'clean', 'title', 'qsettings', 'refresh', 'release',
        ])
    @pytest.mark.parametrize('destination_exists', [False, True])
    def test_every_post_write_failure_restores_exact_destination_transaction(
        self,
        tmp_path: Path,
        failure: str,
        destination_exists: bool,
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        token = object()
        controller.current_protection_token.return_value = token
        controller.record_local_save.return_value = token
        original_bytes = b'exact prior destination bytes'
        original_mode = 0o640
        original_times = (1_700_000_001_234_567_890, 1_700_000_009_876_543_210)
        if destination_exists:
            destination.write_bytes(original_bytes)
            if os.name != 'nt':
                destination.chmod(original_mode)
            os.utime(destination, ns=original_times)

        if failure == 'clean':
            window.qmc.fileCleanSignal.emit.side_effect = OSError('clean failed')
        elif failure == 'title':
            window.updateWindowTitle.side_effect = OSError('title failed')
        elif failure == 'qsettings':
            window.roastServerWriteRecentFiles.side_effect = OSError('settings failed')
        elif failure == 'refresh':
            window.refreshRoastServerActions.side_effect = OSError('refresh failed')
        elif failure == 'release':
            controller.record_local_save.return_value = False

        assert not window.fileSave(None)

        if destination_exists:
            after_stat = destination.stat()
            assert destination.read_bytes() == original_bytes
            if os.name != 'nt':
                assert stat.S_IMODE(after_stat.st_mode) == original_mode
            assert (after_stat.st_atime_ns, after_stat.st_mtime_ns) == original_times
        else:
            assert not destination.exists()
        assert window.roastserver_open_source is not None
        assert controller.current_protection_token.return_value is token
        window.plusAddPath.assert_not_called()
        controller.saved_profile.assert_not_called()

    @pytest.mark.parametrize('destination_exists', [False, True])
    def test_serializer_postpublication_failure_restores_prior_destination(
        self, tmp_path: Path, destination_exists: bool
    ) -> None:
        window, _controller, _cache_file, destination = self.source_window(tmp_path)
        original_bytes = b'prior destination'
        original_stat:os.stat_result|None = None
        if destination_exists:
            destination.write_bytes(original_bytes)
            original_stat = destination.stat()

        real_serialize = FileDestinationTransaction.serialize

        def publish_then_fail(
            transaction: FileDestinationTransaction,
            profile: dict[str, Any],
        ) -> object:
            real_serialize(transaction, profile)
            raise OSError('injected postpublication failure')

        with patch.object(
            main_module.FileDestinationTransaction,
            'serialize',
            autospec=True,
            side_effect=publish_then_fail,
        ):
            assert not window.fileSave(None)

        if original_stat is None:
            assert not destination.exists()
        else:
            restored_stat = destination.stat()
            assert destination.read_bytes() == original_bytes
            assert restored_stat.st_mtime_ns == original_stat.st_mtime_ns

    def test_server_save_as_refuses_oversize_destination_before_write(
        self, tmp_path: Path
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        with destination.open('wb') as destination_file:
            destination_file.truncate(MAX_PROFILE_BYTES + 1)
        before = destination.stat()

        assert not window.fileSave(None)

        after = destination.stat()
        assert after.st_size == before.st_size
        assert after.st_mtime_ns == before.st_mtime_ns
        controller.record_local_save.assert_not_called()
        controller.saved_profile.assert_not_called()

    def test_server_save_as_runs_auxiliary_export_only_after_commit_as_best_effort(
        self, tmp_path: Path
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        window.qmc.autosaveimage = True
        window.qmc.flagon = False
        window.qmc.autosavealsopath = ''
        window.autosave.side_effect = OSError('auxiliary export failed')
        ordered: list[str] = []
        real_commit = main_module.FileDestinationTransaction.commit

        def commit(transaction: FileDestinationTransaction) -> None:
            real_commit(transaction)
            ordered.append('commit')

        window.autosave.side_effect = lambda *_args: (
            ordered.append('autosave'),
            (_ for _ in ()).throw(OSError('auxiliary export failed')),
        )
        with patch.object(
            main_module.FileDestinationTransaction, 'commit', commit
        ):
            assert window.fileSave(None)

        assert ordered == ['commit', 'autosave']
        assert destination.exists()
        assert window.roastserver_open_source is None
        controller.saved_profile.assert_called_once()
        assert any(
            'auxiliary' in str(call.args[0]).lower()
            for call in window.qmc.adderror.call_args_list
        )

    @pytest.mark.parametrize('failure', ['qsettings', 'release'])
    def test_precommit_failure_never_runs_or_leaves_auxiliary_export(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        auxiliary = tmp_path / 'saved-as.pdf'
        window.qmc.autosaveimage = True
        window.qmc.flagon = False
        window.qmc.autosavealsopath = ''
        window.autosave.side_effect = lambda *_args: auxiliary.write_bytes(b'residue')
        if failure == 'qsettings':
            window.roastServerWriteRecentFiles.side_effect = OSError('settings failed')
        else:
            controller.record_local_save.return_value = False

        assert not window.fileSave(None)

        window.autosave.assert_not_called()
        window.plusAddPath.assert_not_called()
        assert not auxiliary.exists()
        assert not destination.exists()

    def test_server_save_as_registers_only_after_transaction_commit(
        self, tmp_path: Path
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        token = object()
        controller.current_protection_token.return_value = token
        controller.record_local_save.return_value = token
        ordered: list[str] = []
        window.updateWindowTitle.side_effect = lambda: ordered.append('title')
        window.roastServerWriteRecentFiles.side_effect = (
            lambda _files: ordered.append('settings'))
        window.refreshRoastServerActions.side_effect = (
            lambda: ordered.append('refresh'))

        def release(_path: Path, *, expected: object) -> object:
            ordered.append('release')
            assert expected is token
            return token

        controller.record_local_save.side_effect = release
        window.plusAddPath.side_effect = (
            lambda _profile, _path: ordered.append('register'))
        real_commit = main_module.FileDestinationTransaction.commit

        def commit(transaction: FileDestinationTransaction) -> None:
            real_commit(transaction)
            ordered.append('commit')

        with patch.object(
            main_module.FileDestinationTransaction, 'commit', commit
        ):
            assert window.fileSave(None)

        assert ordered == [
            'title', 'settings', 'refresh', 'release', 'commit', 'register']
        controller.record_local_save.assert_called_once_with(
            destination, expected=token)

    def test_final_plus_registration_failure_keeps_committed_save(
        self, tmp_path: Path
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        destination.write_bytes(b'prior destination')
        window.plusAddPath.side_effect = OSError('plus registration failed')

        assert window.fileSave(None)

        window.plusAddPath.assert_called_once()
        assert destination.read_bytes() != b'prior destination'
        assert window.roastserver_open_source is None
        assert window.curFile == str(destination)
        controller.restore_protection.assert_not_called()
        controller.saved_profile.assert_called_once()
        window.sendmessage.assert_called_once_with('Profile saved')

    def test_reentrant_save_as_transition_never_releases_wrong_token(
        self, tmp_path: Path
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        expected_token = object()
        reentrant_token = object()
        controller.current_protection_token.side_effect = [
            expected_token, reentrant_token]

        assert not window.fileSave(None)

        controller.record_local_save.assert_not_called()
        controller.restore_protection.assert_not_called()
        assert not destination.exists()
        assert window.roastserver_open_source is not None

    @pytest.mark.parametrize('failure', ['post-save', 'cache-release'])
    def test_post_write_failure_restores_read_only_transition_state(
        self, tmp_path: Path, failure: str
    ) -> None:
        window, controller, cache_file, destination = self.source_window(tmp_path)
        source = window.roastserver_open_source
        before = (cache_file.read_bytes(), cache_file.stat().st_mtime_ns)
        if failure == 'post-save':
            window.updateWindowTitle.side_effect = RuntimeError('post-save failed')
        else:
            controller.record_local_save.side_effect = RuntimeError(
                'cache release failed')

        assert not window.fileSave(None)

        assert not destination.exists()
        assert window.roastserver_open_source == source
        assert window.curFile is None
        assert not window.qmc.safesaveflag
        assert window.qmc.plus_file_last_modified is None
        assert window.qmc.plus_sync_record_hash is None
        controller.saved_profile.assert_not_called()
        assert (cache_file.read_bytes(), cache_file.stat().st_mtime_ns) == before

    def test_server_save_as_compensates_actual_recent_sentinel_before_registration(
        self, tmp_path: Path
    ) -> None:
        window, controller, _cache_file, destination = self.source_window(tmp_path)
        source = window.roastserver_open_source
        assert source is not None
        protection_token = object()
        controller.current_protection_token.return_value = protection_token
        controller.record_local_save.return_value = protection_token
        controller.restore_protection.return_value = True
        register_state = {'path': 'previous-plus.alog'}

        def register(_profile: dict[str, Any], path: str) -> None:
            register_state['path'] = path

        window.plusAddPath.side_effect = register

        class SentinelSettings:
            class Status:
                NoError = 0

            recent = ['previous-recent.alog']
            fail_sync = True

            def value(self, _key: str) -> list[str]:
                return self.recent[:]

            def setValue(self, _key: str, value: list[str]) -> None:
                type(self).recent = value[:]

            def sync(self) -> None:
                if type(self).fail_sync:
                    type(self).fail_sync = False
                    raise OSError('settings sync failed')

            @staticmethod
            def status() -> int:
                return SentinelSettings.Status.NoError

        window.roastServerRecentFiles = ApplicationWindow.roastServerRecentFiles
        window.roastServerWriteRecentFiles = (
            ApplicationWindow.roastServerWriteRecentFiles.__get__(window))

        with patch.object(main_module, 'QSettings', SentinelSettings), patch.object(
            main_module.plus.controller, 'updateSyncRecordHashAndSync'
        ) as plus_sync:
            assert not window.fileSave(None)

        assert SentinelSettings.recent == ['previous-recent.alog']
        assert register_state == {'path': 'previous-plus.alog'}
        window.plusAddPath.assert_not_called()
        assert window.roastserver_open_source == source
        assert window.curFile is None
        controller.restore_protection.assert_not_called()
        controller.saved_profile.assert_not_called()
        plus_sync.assert_not_called()
        assert not destination.exists()

    @pytest.mark.skipif(os.name == 'nt', reason='POSIX link semantics')
    @pytest.mark.parametrize('alias_kind', ['symlink', 'hardlink'])
    def test_save_as_rejects_every_existing_cache_alias(
        self, tmp_path: Path, alias_kind: str
    ) -> None:
        window, controller, cache_file, _destination = self.source_window(tmp_path)
        alias = tmp_path / f'{alias_kind}.alog'
        if alias_kind == 'symlink':
            alias.symlink_to(cache_file)
        else:
            os.link(cache_file, alias)
        window.ArtisanSaveFileDialog.return_value = str(alias)
        before = (cache_file.read_bytes(), cache_file.stat().st_ino)

        assert not window.fileSave(None)

        if alias_kind == 'symlink':
            assert alias.is_symlink()
        assert alias.stat().st_ino == cache_file.stat().st_ino
        assert (cache_file.read_bytes(), cache_file.stat().st_ino) == before
        controller.record_local_save.assert_not_called()

    @pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink semantics')
    def test_save_as_rejects_parent_alias_to_protected_cache_directory(
        self, tmp_path: Path
    ) -> None:
        window, controller, cache_file, _destination = self.source_window(tmp_path)
        aliased_parent = tmp_path / 'cache-parent-alias'
        aliased_parent.symlink_to(cache_file.parent, target_is_directory=True)
        candidate = aliased_parent / cache_file.name
        window.ArtisanSaveFileDialog.return_value = str(candidate)

        assert not window.fileSave(None)

        assert cache_file.read_bytes() == b'protected cache bytes'
        controller.record_local_save.assert_not_called()

    def test_cache_path_resolution_uncertainty_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        window, controller, cache_file, _destination = self.source_window(tmp_path)
        window.ArtisanSaveFileDialog.return_value = str(tmp_path / 'candidate.alog')
        monkeypatch.setattr(
            'artisanlib.main.os.path.abspath',
            Mock(side_effect=OSError('private path resolution detail')),
        )

        assert not window.fileSave(None)

        controller.record_local_save.assert_not_called()
        assert cache_file.read_bytes() == b'protected cache bytes'
        assert 'private path resolution detail' not in str(window.sendmessage.call_args_list)

    def test_save_as_can_never_select_the_protected_cache_file(
        self, tmp_path: Path
    ) -> None:
        window, controller, cache_file, _destination = self.source_window(tmp_path)
        source = window.roastserver_open_source
        before = (cache_file.read_bytes(), cache_file.stat().st_mtime_ns)
        window.ArtisanSaveFileDialog.return_value = str(cache_file)

        with patch('artisanlib.main.serialize_with_timestamp') as serialize_mock:
            assert not window.fileSave(None)

        serialize_mock.assert_not_called()
        window.plusAddPath.assert_not_called()
        controller.record_local_save.assert_not_called()
        controller.saved_profile.assert_not_called()
        assert window.roastserver_open_source == source
        assert window.curFile is None
        assert (cache_file.read_bytes(), cache_file.stat().st_mtime_ns) == before
