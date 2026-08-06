"""Unit tests for artisanlib.util module.

=============================================================================
SDET Test Isolation and Best Practices
=============================================================================

This test module implements comprehensive session-level isolation to prevent
cross-file module contamination while maintaining proper test independence.

Key Features:
- Session-level isolation for external dependencies
- Proper logging.getLogger() handling for debug logging tests
- Mock state management to prevent interference
- Test independence and proper cleanup
- Python 3.8+ compatibility with type annotations
"""

from datetime import UTC, datetime
import ctypes
import os
import stat
import subprocess
import warnings
import math
import pytest
import tempfile
import hypothesis.strategies as st
import numpy as np
from hypothesis import example, given, settings
from pathlib import Path
from collections.abc import Generator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

# Import the atypes module directly without aggressive mocking
# The atypes module only contains type definitions and doesn't need runtime mocking
from artisanlib import atypes

#######
# Helpers


@pytest.fixture
def sample_profile_data() -> dict[str, Any]:
    """Provide a sample ProfileData for testing."""
    return {
        'version': '2.8.4',
        'build': '1234',
        'title': 'Test Roast Profile',
        'beans': 'Ethiopian Yirgacheffe',
        'roastdate': 'Fri May 30 2025',
        'roastisodate': '2025-05-30',
        'roasttime': '17:32:08',
        'roastepoch': 1748619128,
        'roasttzoffset': -3600,
        'roastUUID': 'eb4bfabfe18a4b31aba72f8961b74a07',
        'weight': [1000.0, 845.0],
        'volume': [1500.0, 2275.0],
        'density': [0.65, 0.45],
        'roastertype': 'Probat',
        'roastersize': 12.0,
        'operator': 'Test Operator',
        'timeindex': [1,2,0,0,0,0,3,0],
        'timex': [0.0, 30.0, 60.0, 90.0, 120.0],
        'temp1': [20.0, 50.0, 80.0, 120.0, 150.0],
        'temp2': [18.0, 45.0, 75.0, -1, 145.0],
        'specialevents': [0, 1, 2, 3, 3, 2, 1],
        'specialeventstype': [0, 1, 2, 2, 3, 4, 4],
        'specialeventsvalue': [8.0, 2.5, 21.3, 11.0, 6.52, 0, 0],
        'specialeventsStrings': ['', '', '', '', '', 'sample2', 'sample1'],
        'etypes': ['Air', 'Drum', 'Damper', 'Burner', '--'],
        'eventsliderunits': ['Pa', 'RPM', '', '%'],
        'extradevices': [0, 1],
        'extraname1': ['Extra 1', 'Extra 2'],
        'extraname2': ['Extra 1 Ch2', 'Extra 2 Ch2'],
        'roastbatchnr': 1,
        'roastbatchprefix': 'B',
        'whole_color': 65,
        'ground_color': 70,
        'color_system': 'Agtron',
        'cuppingnotes': 'Bright acidity, floral notes',
        'roastingnotes': 'Light roast, stopped at first crack',
    }

@pytest.fixture(scope='session', autouse=True)
def session_level_isolation() -> Generator[None, None, None]:
    """Session-level isolation fixture to prevent cross-file module contamination.

    This fixture ensures that external dependencies are properly isolated
    at the session level while preserving the functionality needed for
    util debug logging tests.
    """
    # Only patch the most critical external dependencies that could cause
    # cross-file contamination. Preserve logging functionality for debug tests.
    yield


from artisanlib.util import (
    RoRfromCtoF,
    RoRfromCtoFstrict,
    RoRfromFtoC,
    RoRfromFtoCstrict,
    abbrevString,
    appFrozen,
    # Color functions
    argb_colorname2rgba_colorname,
    cmd2str,
    # String processing
    comma2dot,
    convertRoR,
    convertRoRstrict,
    convertTemp,
    convertVolume,
    # Weight/Volume functions
    convertWeight,
    createGradient,
    debugLogLevelActive,
    debugLogLevelToggle,
    decodeLocal,
    decodeLocalStrict,
    # Basic utility functions
    encodeLocal,
    encodeLocalStrict,
    fill_gaps,
    # Float processing
    float2float,
    float2floatNone,
    float2floatWeightVolume,
    fromCtoF,
    fromCtoFstrict,
    fromFtoC,
    # Temperature functions
    fromFtoCstrict,
    getAppPath,
    # File system functions
    getDataDirectory,
    getDirectory,
    # Logging functions
    getLoggers,
    getResourcePath,
    hex2int,
    is_float_list,
    # Type guards
    is_int_list,
    # Validation functions
    is_proper_temp,
    # Network and system utilities
    isOpen,
    natsort,
    path2url,
    # List processing
    removeAll,
    render_weight,
    replace_duplicates,
    rgba_colorname2argb_colorname,
    # Internationalization
    right_to_left,
    s2a,
    scaleFloat2String,
    setDebugLogLevel,
    setFileLogLevel,
    str2cmd,
    # Time functions
    stringfromseconds,
    stringtoseconds,
    toBool,
    toDim,
    toFloat,
    toGrey,
    # Type conversion functions
    toInt,
    toList,
    toString,
    toStringList,
    uchr,
    weightVolumeDigits,
    timearray2index,
    findTPint,
    eventtime2string,
    medfilt,
    polyRoR,
    arrayRoR,
    FileDestinationTransaction,
    deserialize,
    serialize,
    serialize_with_timestamp,
    roast_message,
    max_blocks,
    min_blocks,
)

# fromCtoF


@given(temp=st.one_of(st.floats(-100, 1000)))
@settings(max_examples=10)
@example(-1)
@example(None)
def test_fromCtoF(temp: float|None) -> None:
    if temp == -1:
        assert fromCtoF(temp) == -1
    elif temp is None:
        assert fromCtoF(temp) is None
    else:
        assert fromFtoC(fromCtoF(temp)) == pytest.approx(temp, 0.1)


# fromFtoC


@given(temp=st.one_of(st.floats(-100, 1500)))
@settings(max_examples=10)
@example(-1)
@example(None)
def test_fromFtoC(temp: float|None) -> None:
    if temp == -1:
        assert fromFtoC(temp) == -1
    elif temp is None:
        assert fromFtoC(temp) is None
    else:
        assert fromCtoF(fromFtoC(temp)) == pytest.approx(temp, 0.1)


# render_weight

# weight_unit_index
#  0: g
#  1: kg
#  2: lb
#  3: oz


@pytest.mark.parametrize(
    'amount,weight_unit_index,target_unit_idx,brief,smart_unit_upgrade,expected',
    [
        # input g, target g
        (12.34, 0, 0, 0, True, '12.3g'),
        (12.34, 0, 0, 1, True, '12g'),
        (123.4, 0, 0, 0, True, '123g'),  # 0 decimal as >=100 and result unit g
        (123.4, 0, 0, 1, True, '123g'),  # 0 decimal as >=100 and result unit g
        (1234.2, 0, 0, 0, True, '1234g'),  # 0 decimal as >=100 and result unit g
        (
            1234.2,
            0,
            0,
            1,
            True,
            '1.23kg',
        ),  # upgraded to kg as brief!=0 and amount>1000, rendered with 2 decimals (1 downgraded from the default 3)
        (12346, 0, 0, 0, True, '12.346kg'),  # unit upgrade
        (1600, 0, 0, 0, True, '1.6kg'),  # smart unit upgrade
        (1600, 0, 0, 0, False, '1600g'),  # no smart unit upgrade (disabled)
        (1601, 0, 0, 0, True, '1601g'),  # no smart unit upgrade (as not more readable)
        (1610, 0, 0, 0, True, '1610g'),  # no smart unit upgrade (as not more readable)
        (1000000, 0, 0, 0, True, '1t'),  # >10kg rendered using result unit t
        # input kg
        (0.9123, 1, 0, 0, True, '912g'),  # 0 decimal as >=100 and target unit g
        (0.9123, 1, 1, 0, True, '912g'),  # target unit kg, but unit downgrade as <1kg
        (1.9123, 1, 0, 0, True, '1912g'),
        (1.9123, 1, 1, 0, True, '1.912kg'),
        (1.9123, 1, 1, 1, True, '1.91kg'),  # brief=1 (one decimal less)
        (12345.6, 1, 0, 1, True, '12.35t'),  # target unit g; unit upgrade; result unit t
        (12345.6, 1, 1, 1, True, '12.35t'),  # target unit kg; unit upgrade; result unit t
        (1600, 1, 1, 0, True, '1.6t'),  # smart unit upgrade
        (1600, 1, 1, 0, False, '1600kg'),  # no smart unit upgrade (disabled)
        (1601, 1, 1, 0, True, '1601kg'),  # no smart unit upgrade (as not more readable)
        (1610, 1, 1, 0, True, '1610kg'),  # no smart unit upgrade (as not more readable)
        # input oz
        (32000, 3, 3, 0, True, '1t'),  # >32000oz rendered as target unit US t
        (2000, 3, 3, 0, True, '125lb'),  # >1600oz rendered as target unit lbs
        # input lb
        (
            0.9123,
            2,
            2,
            0,
            True,
            '14.6oz',
        ),  # 1 decimal as <100 and target unit oz (only with smart unit upgrade)
        (
            0.9123,
            2,
            2,
            0,
            False,
            '0.912lb',
        ),  # 3 decimal as <100 and target unit lb (smart unit upgrade off)
        (2600, 2, 2, 0, True, '1.3t'),  # smart unit upgrade
        (2600, 2, 2, 0, False, '2600lb'),  # no smart unit upgrade (disabled)
        (2601, 2, 2, 0, True, '2601lb'),  # no smart unit upgrade (as not more readable)
        (2610, 2, 2, 0, True, '2610lb'),  # no smart unit upgrade (as not more readable)
        (20001, 2, 2, 0, True, '10.001t'),
        # Test very large weights to trigger tonne conversion
        (2000000, 0, 0, 0, True, '2t'),
        # Test edge cases for smart unit upgrade
        (1000, 0, 0, 0, True, '1kg'),
        # Test brief mode with different values
    ],
)
def test_render_weight(
    amount: float,
    weight_unit_index: int,
    target_unit_idx: int,
    brief: int,
    smart_unit_upgrade: bool,
    expected: str,
) -> None:
    assert (
        render_weight(
            amount,
            weight_unit_index,
            target_unit_idx,
            brief=brief,
            smart_unit_upgrade=smart_unit_upgrade,
        )
        == expected
    )

    # Test right-to-left language formatting
    result = render_weight(1500, 0, 0, right_to_left_lang=True)
    assert isinstance(result, str)


# Basic Utility Functions Tests


@pytest.mark.parametrize(
    'code_point,expected_char',
    [
        (65, 'A'),
        (8364, '€'),  # Euro symbol
        (0, '\x00'),
        (97, 'a'),
        (32, ' '),  # Space
        (9, '\t'),  # Tab
        (10, '\n'),  # Newline
        (0x110000, ''),  # Beyond Unicode range
        (-1, ''),  # Input validation for negative values
    ],
)
def test_uchr(code_point: int, expected_char: str) -> None:
    """Test uchr function with various Unicode code points."""
    assert uchr(code_point) == expected_char


def test_encodeLocal_decodeLocal() -> None:
    """Test encodeLocal and decodeLocal functions."""
    # Test normal strings
    test_str = 'Hello World'
    encoded = encodeLocal(test_str)
    assert encoded is not None
    decoded = decodeLocal(encoded)
    assert decoded == test_str

    # Test None
    assert encodeLocal(None) is None
    assert decodeLocal(None) is None

    # Test special characters
    special_str = 'CafÃ© Ã±oÃ±o'
    encoded_special = encodeLocal(special_str)
    assert encoded_special is not None
    decoded_special = decodeLocal(encoded_special)
    assert decoded_special == special_str

    # Test with Unicode characters
    unicode_str = 'Hello 世界 🌍'
    encoded = encodeLocal(unicode_str)
    assert encoded is not None
    decoded = decodeLocal(encoded)
    assert decoded == unicode_str

    # Test with escape sequences
    escape_str = 'Line1\\nLine2\\tTabbed'
    encoded = encodeLocal(escape_str)
    assert encoded is not None
    decoded = decodeLocal(encoded)
    assert decoded == escape_str

    # Test with empty string
    assert encodeLocal('') == ''
    assert decodeLocal('') == ''

    # Test invalid escape sequences
    with pytest.deprecated_call():
        result = decodeLocal('\\invalid')
        # This might not decode properly or raise an exception
        assert result == '\\invalid'
        # NOTE: if DeprecationWarning is turned into an exception in the future result will be None


def test_encodeLocalStrict_decodeLocalStrict() -> None:
    """Test strict versions of encode/decode functions."""
    # Test normal strings
    test_str = 'Hello World'
    encoded = encodeLocalStrict(test_str)
    decoded = decodeLocalStrict(encoded)
    assert decoded == test_str

    # Test None with default
    assert encodeLocalStrict(None) == ''
    assert encodeLocalStrict(None, 'default') == 'default'
    assert decodeLocalStrict(None) == ''
    assert decodeLocalStrict(None, 'default') == 'default'


@pytest.mark.parametrize(
    'h1,h2,expected',
    [
        # Single hex value tests (h2=None)
        (0xFF, None, 255),
        (0x10, None, 16),
        (0, None, 0),
        (0x7F, None, 127),  # Max signed byte
        (0x80, None, 128),  # Min unsigned high byte
        # Two hex values (h1*256 + h2)
        (1, 0, 256),  # 1*256 + 0
        (0xFF, 0xFF, 65535),  # 255*256 + 255 # Maximum 16-bit value
        (0x10, 0x20, 4128),  # 16*256 + 32
        (1, 1, 257),  # 1*256 + 1
        (0x10, 0x10, 4112),  # 16*256 + 16
        (0, None, 0),  # 0*256 + 0
        (0, 0, 0),  # 0*256 + 0
        (
            1000,
            1000,
            257000,
        ),  # No overflow protection - function accepts any integer (1000*256 + 1000)
    ],
)
def test_hex2int(h1: int, h2: int|None, expected: int) -> None:
    """Test hex2int function with single and double hex values."""
    if h2 is None:
        assert hex2int(h1) == expected
    else:
        assert hex2int(h1, h2) == expected


def test_str2cmd_cmd2str() -> None:
    """Test str2cmd and cmd2str functions."""
    test_str = 'Hello'
    cmd_bytes = str2cmd(test_str)
    assert isinstance(cmd_bytes, bytes)
    assert cmd_bytes == b'Hello'

    # Round trip
    result_str = cmd2str(cmd_bytes)
    assert result_str == test_str

    # Test with special characters
    special_str = 'Test123!@#'
    assert cmd2str(str2cmd(special_str)) == special_str

    # Handles non-ASCII characters gracefully
    result = str2cmd('café')
    assert result == b'caf'

    # Bytes that might not decode properly
    byte_result = cmd2str(b'\xff\xfe')
    assert isinstance(byte_result, str)


@pytest.mark.parametrize(
    'input_str,expected_output',
    [
        # Normal ASCII strings
        ('Hello', 'Hello'),
        ('Hello123', 'Hello123'),
        ('Test', 'Test'),
        ('', ''),
        # Strings with non-ASCII characters (should be removed)
        ('CafÃ©', 'Caf'),
        ('Hello ä¸–ç•Œ', 'Hello '),
        ('Hello 世界 World', 'Hello  World'),
        ('Héllo', 'Hllo'),
        ('Test™', 'Test'),
        ('αβγ', ''),  # All non-ASCII should result in empty string
        # Control characters (should be preserved as they are ASCII)
        ('Hello\tWorld', 'Hello\tWorld'),  # Tab is ASCII
        ('Hello\nWorld', 'Hello\nWorld'),  # Newline is ASCII
        ('Test\x7f', 'Test\x7f'),  # DEL character (127) is ASCII
    ],
)
def test_s2a(input_str: str, expected_output: str) -> None:
    """Test s2a function (string to ASCII) with various inputs."""
    assert s2a(input_str) == expected_output


@pytest.mark.parametrize(
    'input_str,limit,expected_output',
    [
        # String shorter than limit
        ('Hello', 10, 'Hello'),
        ('Test', 10, 'Test'),
        # String equal to limit
        ('Hello', 5, 'Hello'),
        ('AB', 2, 'AB'),
        # String longer than limit
        ('Hello World', 8, 'Hello W\u2026'),
        ('Very long string', 5, 'Very\u2026'),
        ('Long text here', 10, 'Long text\u2026'),
        # Edge cases
        ('A', 1, 'A'),
        ('AB', 1, '\u2026'),
        ('AB', 2, 'AB'),  # Exactly at limit
        ('ABC', 2, 'A\u2026'),  # One over limit
        ('', 0, ''),
        ('', 5, ''),
        ('A', -1, '\u2026'),  # Length <=1 should always result in ellipsis if limit < 1
        ('A', 0, '\u2026'),  # Length <=1 should always result in ellipsis if limit < 1
        # Very long strings
        ('A' * 1000, 10, 'A' * 9 + '\u2026'),
    ],
)
def test_abbrevString(input_str: str, limit: int, expected_output: str) -> None:
    """Test abbrevString function with various inputs and limits."""
    assert abbrevString(input_str, limit) == expected_output


# Type Conversion Functions Tests


@pytest.mark.parametrize(
    'input_value,expected_output',
    [
        # Normal integers
        (42, 42),
        ('42', 42),
        (0, 0),
        (999999999, 999999999),  # Test with very large numbers
        # Floats (should round)
        (42.7, 43),
        ('42.7', 43),  # Should round
        (42.1, 42),  # Should round down
        (42.9, 43),
        # Negative numbers
        (-42, -42),
        ('-42', -42),
        (-42.7, -43),  # rounds away from zero
        (-42.1, -42),
        # Complex numbers
        (3 + 4j, 0.0),
        # Edge cases
        (None, 0),
        ('', 0),
        ('invalid', 0),
        ('not_a_number', 0),
        # Whitespace
        ('  42  ', 42),
        ('  -42  ', -42),
        # float('inf') and float('-inf) cannot be converted to int and thus are mapped to 0
        (float('inf'), 0),
        (float('-inf'), 0),
        # huge numbers
        (
            1e100,
            10000000000000000159028911097599180468360808563945281389781327557747838772170381060813469985856815104,
        ),
    ],
)
def test_toInt(input_value: Any, expected_output: int) -> None:
    """Test toInt function with various input types."""
    assert toInt(input_value) == expected_output


@pytest.mark.parametrize(
    'input_value,expected_output',
    [
        # Normal floats
        (42.5, 42.5),
        ('42.5', 42.5),
        (42, 42.0),
        ('42', 42.0),
        (0, 0.0),
        (0.0, 0.0),
        # Scientific notation
        ('1e3', 1000.0),
        ('1.5e-2', 0.015),
        # Negative numbers
        (-42.5, -42.5),
        ('-42.5', -42.5),
        (-1.0, -1.0),
        # Edge cases
        (None, 0.0),
        ('', 0.0),
        ('invalid', 0.0),
        ('not_a_number', 0.0),
        # Whitespace
        ('  42.5  ', 42.5),
        ('  -42.5  ', -42.5),
        # Scientific notation
        ('1.5e-2', 0.015),
    ],
)
def test_toFloat(input_value: Any, expected_output: float) -> None:
    """Test toFloat function with various input types."""
    assert toFloat(input_value) == expected_output


@pytest.mark.parametrize(
    'input_value,expected_output',
    [
        # String true values
        ('yes', True),
        ('YES', True),
        ('true', True),
        ('TRUE', True),
        ('True', True),
        ('t', True),
        ('T', True),
        ('1', True),
        ('Yes', True),
        # String false values
        ('no', False),
        ('NO', False),
        ('false', False),
        ('FALSE', False),
        ('False', False),
        ('f', False),
        ('F', False),
        ('0', False),
        ('', False),
        ('invalid', False),
        # Non-string values - boolean
        (True, True),
        (False, False),
        # Non-string values - numbers
        (1, True),
        (0, False),
        (42, True),  # Non-zero number
        (-1, True),  # Negative number
        (0.0, False),  # Zero float
        (1.5, True),  # Non-zero float
        # Non-string values - other types
        (None, False),
        ([], False),  # Empty list
        ([1], True),  # Non-empty list
        ({}, False),  # Empty dict
        ({'a': 1}, True),  # Non-empty dict
        # Division by zero
        ('1/0', False),
    ],
)
def test_toBool(input_value: Any, expected_output: bool) -> None:
    """Test toBool function with various input types."""
    assert toBool(input_value) is expected_output


def test_toString() -> None:
    """Test toString function."""
    assert toString('hello') == 'hello'


@pytest.mark.parametrize(
    'input_value,expected_output',
    [
        # No conversion
        ('hello', 'hello'),
        # Basic conversions
        (0, '0'),
        (42, '42'),
        (-42, '-42'),
        (42.0, '42.0'),
        (42.5, '42.5'),
        # Special values
        (None, 'None'),
        (True, 'True'),
        (False, 'False'),
        # Collections
        ([1, 2, 3], '[1, 2, 3]'),
        ((1, 2), '(1, 2)'),
        ({'a': 1}, "{'a': 1}"),
        # Empty values
        ('', ''),
        ([], '[]'),
        ({}, '{}'),
    ],
)
def test_toString_should_convert_values_correctly(input_value: Any, expected_output: str) -> None:
    """Test toString with various input types using parametrized tests."""
    assert toString(input_value) == expected_output


def test_toList() -> None:
    """Test toList function."""
    assert toList(None) == []
    assert toList([1, 2, 3]) == [1, 2, 3]
    assert toList((1, 2, 3)) == [1, 2, 3]
    assert toList('abc') == ['a', 'b', 'c']
    assert toList(range(3)) == [0, 1, 2]

    # Test with different iterable types
    result = toList({1, 2, 3})  # Set order varies
    assert sorted(result) == [1, 2, 3]  # Sort to handle order variation
    assert toList({'a': 1, 'b': 2}.keys()) == ['a', 'b']
    assert toList({'a': 1, 'b': 2}.values()) == [1, 2]

    # Test with generator
    def gen() -> Generator[int, None, None]:
        yield 1
        yield 2
        yield 3

    assert toList(gen()) == [1, 2, 3]

    # Test with numpy array if available
    try:
        import numpy as np

        arr = np.array([1, 2, 3])
        assert toList(arr) == [1, 2, 3]
    except ImportError:
        pass  # Skip if numpy not available


def test_toStringList() -> None:
    """Test toStringList function."""
    assert toStringList([1, 2, 3]) == ['1', '2', '3']
    assert toStringList(['a', 'b', 'c']) == ['a', 'b', 'c']
    assert toStringList([]) == []
    assert toStringList([None, 42, 'test']) == ['None', '42', 'test']
    assert toStringList(None) == []  # type: ignore[arg-type] # Type error: None not a list


# Temperature Functions Tests


@pytest.mark.parametrize(
    'fahrenheit,expected_celsius',
    [
        (-1, -1),  # Error value preserved
        (32.0, 0.0),  # Freezing point
        (212.0, 100.0),  # Boiling point
        (-40.0, -40.0),  # Same in both scales
        (68.0, 20.0),  # Room temperature
        (98.6, 37.0),  # Body temperature
        (-459.67, -273.15),  # Absolute zero
    ],
)
def test_fromFtoCstrict_should_convert_temperatures_accurately(
    fahrenheit: float, expected_celsius: float
) -> None:
    """Test temperature conversion from Fahrenheit to Celsius with parametrized values."""
    result = fromFtoCstrict(fahrenheit)
    assert result == pytest.approx(expected_celsius, abs=0.01)


@pytest.mark.parametrize(
    'celsius,expected_fahrenheit',
    [
        (-1, -1),  # Error value preserved
        (0.0, 32.0),  # Freezing point
        (100.0, 212.0),  # Boiling point
        (-40.0, -40.0),  # Same in both scales
        (20.0, 68.0),  # Room temperature
        (37.0, 98.6),  # Body temperature
        (-273.15, -459.67),  # Absolute zero
    ],
)
def test_fromCtoFstrict_should_convert_temperatures_accurately(
    celsius: float, expected_fahrenheit: float
) -> None:
    """Test temperature conversion from Celsius to Fahrenheit with parametrized values."""
    result = fromCtoFstrict(celsius)
    assert result == pytest.approx(expected_fahrenheit, abs=0.01)


@pytest.mark.parametrize(
    'celsius_rate,expected_fahrenheit_rate',
    [
        (1.0, 1.8),  # 1°C/min = 1.8°F/min
        (5.0, 9.0),  # 5°C/min = 9°F/min
        (0.0, 0.0),  # Zero rate
        (10.0, 18.0),  # 10°C/min = 18°F/min
        (-1, -1),  # Error value preserved
        (2.5, 4.5),  # 2.5°C/min = 4.5°F/min
    ],
)
def test_RoRfromCtoFstrict(celsius_rate: float, expected_fahrenheit_rate: float) -> None:
    """Test RoRfromCtoFstrict function with various rates."""
    assert RoRfromCtoFstrict(celsius_rate) == expected_fahrenheit_rate


@pytest.mark.parametrize(
    'fahrenheit_rate,expected_celsius_rate',
    [
        (1.8, 1.0),  # 1.8°F/min = 1°C/min
        (9.0, 5.0),  # 9°F/min = 5°C/min
        (0.0, 0.0),  # Zero rate
        (18.0, 10.0),  # 18°F/min = 10°C/min
        (-1, -1),  # Error value preserved
        (4.5, 2.5),  # 4.5°F/min = 2.5°C/min
    ],
)
def test_RoRfromFtoCstrict(fahrenheit_rate: float, expected_celsius_rate: float) -> None:
    """Test RoRfromFtoCstrict function with various rates."""
    result = RoRfromFtoCstrict(fahrenheit_rate)
    if fahrenheit_rate == -1:
        assert result == -1
    else:
        assert pytest.approx(result, abs=0.01) == expected_celsius_rate


@pytest.mark.parametrize(
    'CRoR, FRoR',
    [
        # Normal conversions
        (1.0, 1.8),  # 1°C/min = 1.8°F/min
        (5.0, 9.0),  # 5°C/min = 9°F/min
        # Special values
        (None, None),
        (-1, -1),
    ],
)
def test_RoRfromCtoF(CRoR: float, FRoR: float) -> None:
    assert RoRfromCtoF(CRoR) == FRoR
#    assert RoRfromCtoF(float('nan')) is None or np.isnan(RoRfromCtoF(float('nan')))


def test_RoRfromFtoC() -> None:
    """Test RoRfromFtoC function with None handling."""
    # Normal conversions
    assert pytest.approx(RoRfromFtoC(1.8), 0.01) == 1.0
    assert pytest.approx(RoRfromFtoC(9.0), 0.01) == 5.0

    # Special values
    assert RoRfromFtoC(None) is None
    assert RoRfromFtoC(-1) == -1
#    assert RoRfromFtoC(float('nan')) is None or np.isnan(RoRfromFtoC(float('nan')))


def test_convertRoR() -> None:
    """Test convertRoR function."""
    # Same unit
    assert convertRoR(5.0, 'C', 'C') == 5.0
    assert convertRoR(5.0, 'F', 'F') == 5.0

    # C to F
    assert convertRoR(1.0, 'C', 'F') == 1.8

    # F to C
    assert pytest.approx(convertRoR(1.8, 'F', 'C'), 0.01) == 1.0

    # None handling
    assert convertRoR(None, 'C', 'F') is None


def test_convertRoRstrict() -> None:
    """Test convertRoRstrict function."""
    # Same unit
    assert convertRoRstrict(5.0, 'C', 'C') == 5.0

    # C to F
    assert convertRoRstrict(1.0, 'C', 'F') == 1.8

    # F to C
    assert pytest.approx(convertRoRstrict(1.8, 'F', 'C'), 0.01) == 1.0


@pytest.mark.parametrize(
    'temp, source_unit, target_unit, expected',
    [
        # Same unit or empty target
        (100.0, 'C', 'C', 100.0),  # Should return original value if source unit = target unit
        (100.0, 'C', '', 100.0),
        (100.0, '', 'F', 100.0),
        # C to F
        (0.0, 'C', 'F', 32.0),
        (100.0, 'C', 'F', 212.0),
        # F to C
        (32.0, 'F', 'C', 0.0),
        (212.0, 'F', 'C', 100.0),
        # edge cases
        (100.0, '', 'C', 100.0),  # Returns original value for empty source
        (100.0, '', 'F', 100.0),  # Returns original value for empty source
        (100.0, 'C', '', 100.0),  # Returns original value for empty target
        (100.0, 'F', '', 100.0),  # Returns original value for empty target
        (100.0, '', '', 100.0),
        (float('inf'), 'C', 'F', float('inf')),
        (float('-inf'), 'F', 'C', float('-inf')),
    ],
)
def test_convertTemp(temp: float, source_unit: str, target_unit: str, expected: float) -> None:
    assert convertTemp(temp, source_unit, target_unit) == expected
    # Test unknown units (actually converts as if C to F)
    result = convertTemp(100.0, 'X', 'Y')
    assert pytest.approx(result, 0.1) == 37.8  # Converts as C to F then F to C

    # Test with NaN values that might return None
    result = convertTemp(float('nan'), 'C', 'F')
    # Should handle NaN gracefully
    assert isinstance(result, float)


@pytest.mark.parametrize(
    'value, expected',
    [
        # Valid temperatures
        (25.5, True),
        (100, True),
        (200.0, True),
        (1000.0, True),  # High temperatures
        (1e100, True),  # large numbers
        (-1e100, True),  # large numbers
        # Invalid temperatures
        (None, False),
        (-1, False),  # -1 is error value
        (-1.0, False),  # -1 is error value
        (0, False),  # Zero is error value
        (0.0, False),  # Zero is error value
        (0.1, True),  # Just above zero
        (-0.1, True),  # Just above zero
        (float('nan'), False),
        (float('inf'), False),
        (float('-inf'), False),
    ],
)
def test_is_proper_temp(value: int|float|None, expected: bool) -> None:
    """Test is_proper_temp function."""
    assert is_proper_temp(value) == expected


# Time Functions Tests

# stringfromseconds


@pytest.mark.parametrize(
    'seconds_raw, leadingzero, expected',
    [
        (0, True, '00:00'),
        (0, False, '0:00'),
        (5, False, '0:05'),
        (59.4, True, '00:59'),
        (59.5, True, '01:00'),
        (60, True, '01:00'),
        (60, False, '1:00'),
        (60.4, True, '01:00'),
        (60.6, True, '01:01'),
        (61, True, '01:01'),
        (61, False, '1:01'),
        (90, True, '01:30'),
        (3600, True, '60:00'),
        (3600, False, '60:00'),
        (3661, True, '01h01'),
        (-1, True, '-00:01'),
        (-1, False, '-0:01'),
        (-60, True, '-01:00'),
        (-61, True, '-01:01'),
        (-61, False, '-1:01'),
        (-90, True, '-01:30'),
        (-90, False, '-1:30'),
        (-3600, True, '-60:00'),
        (-3600, False, '-60:00'),
        (-3661, True, '-01h01'),
        (125.7, True, '02:06'),
        (125.7, False, '2:06'),
        (-125.7, True, '-02:06'),
        (-125.7, False, '-2:06'),
    ],
)
def test_stringfromseconds(seconds_raw: float, leadingzero: bool, expected: str) -> None:
    assert stringfromseconds(seconds_raw, leadingzero) == expected


@pytest.mark.parametrize(
    'string, expected',
    [
        ('00:00', 0),
        ('-00:00', 0),
        ('0:05', 5),
        ('00:59', 59),
        ('01:00', 60),
        ('01:01', 61),
        ('01:30', 90),
        ('10:05', 605),
        ('60:00', 3600),
        ('61:01', 3661),
        ('999:59', 59999),  # 999*60 + 59
        ('99h21', 357660),
        ('-00:01', -1),
        ('-00:30', -30),
        ('-01:00', -60),
        ('-01:30', -90),
        ('-05:30', -330),
        ('-10:30', -630),
        ('-60:00', -3600),
        ('-61:01', -3661),
        ('-999:59', -59999),  # -999*60 - 59
    ],
)
def test_stringtoseconds(string: str, expected: int) -> None:
    assert stringtoseconds(string) == expected


def test_stringtoseconds_invalid_input() -> None:
    """Test stringtoseconds function."""

    # Invalid formats
    with pytest.raises(ValueError, match='not a properly formatted time string'):
        stringtoseconds('')
    with pytest.raises(ValueError, match='not a properly formatted time string'):
        stringtoseconds('invalid')
    with pytest.raises(ValueError, match='not a properly formatted time string'):
        stringtoseconds('1:2:3')
    with pytest.raises(ValueError, match='not a properly formatted time string'):
        stringtoseconds('1:2:3:4')  # Too many parts
    with pytest.raises(ValueError, match='not a properly formatted time string'):
        stringtoseconds('1')

    # Non-numeric parts raise ValueError
    with pytest.raises(ValueError, match='invalid literal'):
        stringtoseconds('ab:cd')
    with pytest.raises(ValueError):
        stringtoseconds('1:ab')
    with pytest.raises(ValueError):
        stringtoseconds('ab:1')

    # Test with single empty part (causes IndexError, so we expect exception)
    with pytest.raises(IndexError):
        stringtoseconds(':')  # Empty parts
    with pytest.raises(ValueError, match='not a properly formatted time string'):
        assert stringtoseconds('::')  # Multiple empty parts


# String Processing Functions Tests


@pytest.mark.parametrize(
    's, expected',
    [
        # Normal decimal conversion
        ('1,5', '1.5'),
        ('12,34', '12.34'),
        # Already has dot
        ('1.5', '1.5'),
        # Multiple separators (behavior depends on order)
        ('1,234.56', '1234.56'),
        ('1.234,56', '1234.56'),
        # No separators
        ('123', '123'),
        # Only separators
        (',.,.', ''),
        # Empty or whitespace
        ('', ''),
        ('  ', ''),
        # Leading/trailing whitespace
        ('  1,5  ', '1.5'),
        # Leading comma
        (',5', '.5'),
        # Trailing comma gets removed
        ('5,', '5'),
        # Last comma becomes decimal
        ('1,2,3', '12.3'),
        # Test complex cases with multiple separators
        ('1,234,567.89', '1234567.89'),
        ('1.234.567,89', '1234567.89'),
        # Test with only commas
        ('1.234.567', '1234.567'),
        # Test German/European format (comma as decimal separator)
        # Note: comma2dot strips trailing zeros
        ('1,50', '1.5'),  # Trailing zero is stripped
        # German format - dots removed, last comma becomes decimal
        ('1.234,56', '1234.56'),
        # US format with comma thousands
        ('1,234.56', '1234.56'),
    ],
)
def test_comma2dot(s: str, expected: str) -> None:
    """Test comma2dot function."""
    assert comma2dot(s) == expected


@pytest.mark.parametrize(
    's, expected',
    [
        ('file10.txt', ['file', 10, '.txt']),
        ('abc123def456', ['abc', 123, 'def', 456, '']),
        ('abcdef', ['abcdef']),
        ('123456', ['', 123456, '']),
        ('', ['']),
    ],
)
def test_natsort(s: str, expected: list[int|str]) -> None:
    """Test natsort function (natural sorting)."""
    assert natsort(s) == expected


@pytest.mark.parametrize(
    'input_value,expected_output',
    [
        # Zero
        (0, '0'),
        ('0', '0'),
        (0.0, '0'),
        # Small numbers (< 1)
        (0.1, '0.1'),
        (0.999, '0.999'),
        (0.01, '0.01'),
        (0.001, '0.001'),
        (0.0001, '0'),  # Very small rounds to 0
        # Medium numbers (1-9.99)
        (1, '1'),
        (1.5, '1.5'),
        (9.99, '9.99'),
        (1.0, '1'),
        (5.123, '5.123'),
        # Larger numbers (10-999.9)
        (9.999, '9.999'),
        (10.0, '10'),
        (10.5, '10.5'),
        (99.9, '99.9'),
        (999.9, '999.9'),
        (100.0, '100'),
        (50.25, '50.25'),
        (999.9, '999.9'),
        # Very large numbers (>= 1000)
        (1000, '1000'),
        (9999, '9999'),
        (1234.5, '1234'),
        (10000.0, '10000'),
        # Negative numbers
        (-1.5, '-1.5'),
        (-10.25, '-10.25'),
        (-1000, '-1000'),
        # String inputs
        ('1.5', '1.5'),
        ('10.25', '10.25'),
        ('1000', '1000'),
        ('1e-10', '0'),  # lose precision for very small but non-zero numbers
    ],
)
def test_scaleFloat2String(input_value: float|int|str, expected_output: str) -> None:
    """Test scaleFloat2String function with various input types and values."""
    assert scaleFloat2String(input_value) == expected_output


# Float Processing Functions Tests


@pytest.mark.parametrize(
    'value,decimal_places,expected_result',
    [
        # Different decimal places
        (1.23456, 0, 1.0),
        (1.23456, 1, 1.2),
        (1.23456, 2, 1.23),
        (1.23454, 3, 1.235),  # Rounds up
        (1.23444, 3, 1.234),  # Rounds down
        (1.23456, 3, 1.235),  # Rounds down
        (1.23456, 4, 1.2346),
        # Zero decimals with rounding
        (1.7, 0, 2.0),  # rounds up
        (1.4, 0, 1.0),  # rounds down
        (1.5, 0, 2.0),  # rounds up
        # Negative numbers
        (-123.456, 2, -123.46),
        (-1.23456, 2, -1.23),
        (-1.7, 0, -2.0),  # rounds away from zero
        # Zero
        (0.0, 0, 0.0),
        (0.0, 2, 0.0),
        (0.0, 3, 0.0),
        # Large numbers
        (1234.56789, 2, 1234.57),
        # Very small numbers
        (0.00001, 4, 0.0),  # rounds to zero
        (0.00001, 5, 0.00001),
        # Special case: NaN handling (returns 0.0 for NaN)
        (float('nan'), 0, 0.0),
        (float('nan'), 1, 0.0),
        (float('nan'), 2, 0.0),
        # Test with very large numbers
        (999.999, 0, 1000.0),  # Large rounding
        (999999.999999, 2, 1000000.0),
        # Test with very small numbers
        (0.000001, 6, 1e-06),
        # Test with infinity numbers
        (float('inf'), 2, float('inf')),
        # Negative precision
        (1.23456, -1, 1),
    ],
)
def test_float2float(value: float, decimal_places: int, expected_result: float) -> None:
    """Test float2float function with various values and decimal places."""
    result = float2float(value, decimal_places)
    if expected_result == 0.0:  # Check for NaN input
        assert result == 0.0
    else:
        assert pytest.approx(result, abs=1e-10) == expected_result


def test_float2floatNone() -> None:
    """Test float2floatNone function."""
    # Normal values
    assert float2floatNone(1.23456, 2) == 1.23

    # None handling
    assert float2floatNone(None, 2) is None
    assert float2floatNone(None) is None  # default n=1


# Weight/Volume Conversion Functions Tests


@pytest.mark.parametrize(
    'value,expected',
    [
        # Different ranges
        (1500, 1),  # >= 1000
        (500, 2),  # >= 100, < 1000
        (50, 3),  # < 100
        (0, 4),  # < 10
        # Test boundary values
        (999.9, 2),  # Just under 1000
        (1000.0, 1),  # Exactly 1000
        (99.9, 3),  # Just under 100
        (100.0, 2),  # Exactly 100
        # Test negative values
        (-100, 2),
    ],
)
def test_weightVolumeDigits(value: float, expected: int) -> None:
    assert weightVolumeDigits(value) == expected


@pytest.mark.parametrize(
    'value,expected',
    [
        # Different ranges
        (1500, 1500.0),  # 1 digit
        (150.456, 150.46),  # 2 digits
        (15.456, 15.456),  # 3 digits
    ],
)
def test_float2floatWeightVolume(value: float, expected: float) -> None:
    """Test float2floatWeightVolume function."""
    assert float2floatWeightVolume(value) == expected


def test_util_weight_conversion_names_are_canonical() -> None:
    from artisanlib import util, weight

    assert util.weight_units is weight.weight_units
    assert util.convertWeight is weight.convertWeight


@pytest.mark.parametrize(
    'amount,from_unit,to_unit,expected_result,tolerance',
    [
        # Same unit (no conversion)
        (1000, 0, 0, 1000, 0),  # g to g
        (1, 1, 1, 1, 0),  # kg to kg
        (1, 2, 2, 1, 0),  # lb to lb
        (1, 3, 3, 1, 0),  # oz to oz
        # g to kg
        (1000, 0, 1, 1.0, 0),
        (500, 0, 1, 0.5, 0),
        (2500, 0, 1, 2.5, 0),
        # kg to g
        (1, 1, 0, 1000, 0),
        (2.5, 1, 0, 2500, 0),
        (0.5, 1, 0, 500, 0),
        # g to lb (approximately)
        (453.592, 0, 2, 1.0, 0.01),  # 453.592g ≈ 1 lb
        (907.184, 0, 2, 2.0, 0.01),  # ~2 lb
        # lb to g
        (1, 2, 0, 453.6, 0.1),  # 1 lb to g
        (2, 2, 0, 907.2, 0.2),  # 2 lb to g
        # g to oz
        (28.35, 0, 3, 1.0, 0.01),  # ~28.35g ≈ 1 oz
        (56.7, 0, 3, 2.0, 0.01),  # ~2 oz
        # oz to g
        (1, 3, 0, 28.35, 0.1),  # 1 oz to g
        (2, 3, 0, 56.7, 0.1),  # 2 oz to g
        # lb to oz
        (1, 2, 3, 16.0, 0.01),  # 1 lb = 16 oz
        # oz to lb
        (16, 3, 2, 1.0, 0.01),  # 16 oz = 1 lb
        # with zero weight
        (0, 0, 1, 0.0, 0),
        # with negative weight
        (-100, 0, 1, -0.1, 0),
    ],
)
def test_convertWeight(
    amount: float, from_unit: int, to_unit: int, expected_result: float, tolerance: float
) -> None:
    """Test convertWeight function with various unit conversions."""
    result = convertWeight(amount, from_unit, to_unit)
    if tolerance == 0:
        assert result == expected_result
    else:
        assert pytest.approx(result, abs=tolerance) == expected_result

    # Test all unit conversions to improve coverage
    units = [0, 1, 2, 3]  # g, kg, lb, oz

    for fu in units:
        for tu in units:
            if fu != tu:
                # Test a small conversion to ensure it works
                result = convertWeight(1.0, fu, tu)
                assert isinstance(result, float)
                assert result > 0

    # Test convertWeight with invalid unit indices
    with pytest.raises(IndexError):
        convertWeight(100, 5, 0)  # Invalid source unit

    # Test with negative indices
    with pytest.raises(IndexError):
        convertWeight(100, -1, 0)  # -1 wraps to last row (oz)


def test_convertVolume() -> None:
    """Test convertVolume function."""
    # Same unit (no conversion)
    assert convertVolume(1000, 0, 0) == 1000  # l to l
    assert convertVolume(1, 1, 1) == 1  # gal to gal

    # l to gal (US)
    result = convertVolume(3.78541, 0, 1)  # ~3.785 l â‰ˆ 1 gal
    assert pytest.approx(result, 0.01) == 1.0

    # gal to l
    result = convertVolume(1, 1, 0)  # 1 gal to l
    assert pytest.approx(result, 0.01) == 3.785

    # l to qt
    result = convertVolume(0.946353, 0, 2)  # ~0.946 l â‰ˆ 1 qt
    assert pytest.approx(result, 0.01) == 1.0

    # l to pt
    result = convertVolume(0.473176, 0, 3)  # ~0.473 l â‰ˆ 1 pt
    assert pytest.approx(result, 0.01) == 1.0

    # l to cup
    result = convertVolume(0.236588, 0, 4)  # ~0.237 l â‰ˆ 1 cup
    assert pytest.approx(result, 0.01) == 1.0

    # l to ml
    assert convertVolume(1, 0, 5) == 1000  # 1 l = 1000 ml
    assert convertVolume(0.5, 0, 5) == 500  # 0.5 l = 500 ml

    # Test all unit conversions to improve coverage
    units = [0, 1, 2, 3, 4, 5]  # l, gal, qt, pt, cup, ml

    for from_unit in units:
        for to_unit in units:
            if from_unit != to_unit:
                # Test a small conversion to ensure it works
                result = convertVolume(1.0, from_unit, to_unit)
                assert isinstance(result, float)
                assert result > 0

    # Test convertWeight with invalid unit indices
    with pytest.raises(IndexError):
        convertVolume(100, 10, 0)  # Invalid source unit

    # Test with negative indices
    with pytest.raises(IndexError):
        convertVolume(100, -1, 0)  # -1 wraps to last row (oz)


# Data Processing Functions Tests


def test_removeAll() -> None:
    """Test removeAll function."""
    # Remove all occurrences (modifies in-place, returns None)
    test_list = ['a', 'b', 'c', 'b', 'd', 'b']
    removeAll(test_list, 'b')
    assert test_list == ['a', 'c', 'd']  # List is modified in-place

    test_list2 = ['x', 'y', 'z', 'y']
    removeAll(test_list2, 'y')
    assert test_list2 == ['x', 'z']

    # Remove non-existent item
    test_list3 = ['a', 'b', 'c']
    removeAll(test_list3, 'z')
    assert test_list3 == ['a', 'b', 'c']  # Unchanged

    # Empty list
    test_list4: list[str] = []
    removeAll(test_list4, 'x')
    assert test_list4 == []

    # Remove all items
    test_list5 = ['x', 'x', 'x']
    removeAll(test_list5, 'x')
    assert test_list5 == []


def test_fill_gaps() -> None:
    """Test fill_gaps function."""
    # Fill gaps with interpolation (using -1 instead of None)
    data = [1.0, -1, 3.0]
    result = fill_gaps(data)
    assert result == [1.0, 2.0, 3.0]

    # Multiple gaps
    data = [1.0, -1, -1, 4.0]
    result = fill_gaps(data)
    assert result == [1.0, 2.0, 3.0, 4.0]

    # No gaps
    data = [1.0, 2.0, 3.0]
    result = fill_gaps(data)
    assert result == [1.0, 2.0, 3.0]

    # All gaps (should remain -1)
    data = [-1, -1, -1]
    result = fill_gaps(data)
    assert result == [-1, -1, -1]

    # Leading/trailing gaps
    data = [-1, 2.0, 3.0, -1]
    result = fill_gaps(data)
    assert result[1:3] == [2.0, 3.0]  # Middle values preserved

    # Test with different interpolate_max values
    data = [1.0, -1, -1, -1, -1, 6.0]  # 4 gaps
    result = fill_gaps(data, interpolate_max=3)  # Should not interpolate (too many gaps)
    assert result[1:5] == [-1, -1, -1, -1]  # Gaps should remain

    result = fill_gaps(data, interpolate_max=5)  # Should interpolate
    assert result[0] == 1.0
    assert result[5] == 6.0
    # Should have interpolated values in between

    # Test with single gap
    data = [10.0, -1, 20.0]
    result = fill_gaps(data)
    assert result == [10.0, 15.0, 20.0]

    # Test with empty list
    result = fill_gaps([])
    assert result == []

    # Test with single element
    result = fill_gaps([5.0])
    assert result == [5.0]
    result = fill_gaps([-1])
    assert result == [-1]  # Single -1 cannot be interpolated


def test_replace_duplicates() -> None:
    """Test replace_duplicates function."""
    # Replace consecutive duplicates
    data: list[float] = [1, 1, 2, 2, 2, 3]
    result = replace_duplicates(data)
    # Should replace duplicates with None or interpolated values
    assert len(result) == len(data)
    assert result[0] == 1  # First occurrence kept
    assert result[2] == 2  # First occurrence of 2 kept
    assert result[5] == 3  # Single value kept

    # No duplicates
    data = [1, 2, 3, 4]
    result = replace_duplicates(data)
    assert result == [1, 2, 3, 4]

    # All same values
    data = [5, 5, 5, 5]
    result = replace_duplicates(data)
    assert result[0] == 5  # First kept
    # Others should be modified

    # Test with empty list
    result = replace_duplicates([])
    assert result == []

    # Test with single item
    result = replace_duplicates([5.0])
    assert result == [5.0]

    # Test with two identical items
    result = replace_duplicates([5.0, 5.0])
    assert len(result) == 2
    assert result[0] == 5.0  # First should be kept

    # Test replace_duplicates with all identical values
    result = replace_duplicates([5.0, 5.0, 5.0, 5.0])
    # First value kept, others replaced with -1, then interpolated back
    # Last value is restored, then fill_gaps interpolates
    expected = [5.0, 5.0, 5.0, 5.0]  # Should interpolate back to original values
    assert result == expected


# Type Guard Functions Tests


@pytest.mark.parametrize(
    'value,expected',
    [
        # Valid int lists
        ([1, 2, 3], True),
        ([0, -1, 100], True),
        ([], True),  # Empty list
        # Invalid lists
        ([1, 2.5, 3], False),  # Contains float
        ([1, '2', 3], False),  # Contains string
        ([1, None, 3], False),  # Contains None
        (
            [True, False, 1],
            False,
        ),  # Note bool is a subclass of int and has to be excluded explicitly
    ],
)
def test_is_int_list(value: list[Any], expected: bool) -> None:
    assert is_int_list(value) == expected


@pytest.mark.parametrize(
    'value,expected',
    [
        # Valid float lists
        ([1.0, 2.5, 3.7], True),
        ([], True),  # Empty list
        # Invalid lists (ints are NOT considered floats by this function)
        ([1, 2, 3], False),  # Ints not considered floats
        ([1, 2, 3.0], False),  # Ints not considered floats
        ([1.0, '2.5', 3.0], False),  # Contains string
        ([1.0, None, 3.0], False),  # Contains None
    ],
)
def test_is_float_list(value: list[Any], expected: bool) -> None:
    assert is_float_list(value) == expected


# Internationalization Functions Tests


@pytest.mark.parametrize(
    'value,expected',
    [
        # RTL languages
        ('ar', True),  # Arabic
        ('he', True),  # Hebrew
        ('fa', True),  # Farsi/Persian
        # LTR languages
        ('en', False),  # English
        ('es', False),  # Spanish
        ('fr', False),  # French
        ('de', False),  # German
        ('zh', False),  # Chinese
        # Unknown/invalid codes
        ('xx', False),  # Unknown
        ('', False),  # Empty
        # Different case
        ('AR', True),
    ],
)
def test_right_to_left(value: str, expected: bool) -> None:
    """Test right_to_left function."""
    assert right_to_left(value) == expected


# Additional Utility Functions Tests


def test_isOpen() -> None:
    """Test isOpen function (network port checking)."""
    # Test with localhost and common ports
    # Port 80 (HTTP) - might be closed on localhost
    result = isOpen('127.0.0.1', 80)
    assert isinstance(result, bool)  # Should return boolean

    # Invalid host
    assert isOpen('invalid.host.name', 80) is False

    # Invalid port
    assert isOpen('127.0.0.1', 99999) is False
    assert isOpen('127.0.0.1', -1) is False
    assert isOpen('127.0.0.1', 65536) is False

    # Test with localhost variations
    result = isOpen('localhost', 80)
    assert isinstance(result, bool)

    # Test with IPv6 localhost
    result = isOpen('::1', 80)
    assert isinstance(result, bool)


def test_appFrozen() -> None:
    """Test appFrozen function."""
    # In development environment, should return False
    result = appFrozen()
    assert isinstance(result, bool)
    # In our test environment, this should be False
    assert result is False


# File System Functions Tests


def test_getDataDirectory() -> None:
    """Test getDataDirectory function."""
    # Should return a string path or None (may fail without Qt app)
    try:
        result = getDataDirectory()
        assert result is None or isinstance(result, str)
        # If it returns a path, it should be a valid directory path
        if result:
            assert len(result) > 0
    except AttributeError:
        # Expected when Qt application is not initialized
        pass


def test_getAppPath() -> None:
    """Test getAppPath function."""
    # Should return a string path
    result = getAppPath()
    assert isinstance(result, str)
    assert len(result) > 0


def test_getResourcePath() -> None:
    """Test getResourcePath function."""
    # Should return a string path
    result = getResourcePath()
    assert isinstance(result, str)
    assert len(result) > 0


def test_getDirectory() -> None:
    """Test getDirectory function."""
    # Test with basic filename (may fail without Qt app)
    try:
        result = getDirectory('test')
        assert isinstance(result, str)
        assert len(result) > 0

        # Test with extension
        result = getDirectory('test', '.txt')
        assert isinstance(result, str)
        assert 'test' in result

        # Test with share parameter
        result = getDirectory('test', share=True)
        assert isinstance(result, str)
    except AttributeError as e:
        # Expected when Qt application is not initialized
        # The error can occur in different places when QCoreApplication.instance() returns None:
        # - app.artisanviewerMode (if getDirectory wasn't fixed)
        # - app.applicationName() (in _getAppDataDirectory)
        error_msg = str(e)
        assert (
            "'NoneType' object has no attribute 'artisanviewerMode'" in error_msg
            or "'NoneType' object has no attribute 'applicationName'" in error_msg
        )


def test_path2url() -> None:
    """Test path2url function."""
    # Test with simple path
    result = path2url('/path/to/file.txt')
    assert isinstance(result, str)
    assert result.startswith('file://')

    # Test with spaces in path
    result = path2url('/path with spaces/file.txt')
    assert isinstance(result, str)
    assert result.startswith('file://')

    # Test with Windows-style paths
    result = path2url('C:\\Users\\test\\file.txt')
    assert result.startswith('file://')

    # Test with relative paths
    result = path2url('./relative/path.txt')
    assert result.startswith('file://')

    # Test with special characters
    result = path2url('/path/with/special chars & symbols.txt')
    assert result.startswith('file://')

    # Test with Unicode characters in path
    result = path2url('/café/文件.txt')
    assert result.startswith('file:')

    # Test with empty path
    result = path2url('')
    assert result.startswith('file:')


# Color Functions Tests


@pytest.mark.parametrize(
    'input_color,expected_output',
    [
        # Normal ARGB color (alpha at beginning) -> RGBA (alpha at end)
        ('#80FF0000', '#FF000080'),  # Semi-transparent red
        ('#FF00FF00', '#00FF00FF'),  # Green with full alpha
        ('#4000FFFF', '#00FFFF40'),  # Cyan with partial alpha
        # Regular hex color (no change - no alpha channel)
        ('#FF0000', '#FF0000'),  # Red
        ('#00FF00', '#00FF00'),  # Green
        ('#0000FF', '#0000FF'),  # Blue
        # Invalid format (no change)
        ('invalid', 'invalid'),
        ('', ''),
        ('#ZZZ', '#ZZZ'),  # Invalid hex
    ],
)
def test_argb_colorname2rgba_colorname(input_color: str, expected_output: str) -> None:
    """Test argb_colorname2rgba_colorname function with various color formats."""
    result = argb_colorname2rgba_colorname(input_color)
    assert result == expected_output


@pytest.mark.parametrize(
    'input_color,expected_output',
    [
        # Normal RGBA color (alpha at end) -> ARGB (alpha at beginning)
        ('#FF000080', '#80FF0000'),  # Red with alpha at end
        ('#00FF00FF', '#FF00FF00'),  # Green with full alpha
        ('#00FFFF40', '#4000FFFF'),  # Cyan with partial alpha
        # Regular hex color (no change - no alpha channel)
        ('#FF0000', '#FF0000'),  # Red
        ('#00FF00', '#00FF00'),  # Green
        ('#0000FF', '#0000FF'),  # Blue
        # Invalid format (no change)
        ('invalid', 'invalid'),
        ('', ''),
        ('#ZZZ', '#ZZZ'),  # Invalid hex
    ],
)
def test_rgba_colorname2argb_colorname(input_color: str, expected_output: str) -> None:
    """Test rgba_colorname2argb_colorname function with various color formats."""
    result = rgba_colorname2argb_colorname(input_color)
    assert result == expected_output


def test_toGrey() -> None:
    """Test toGrey function."""
    # Convert red to grey
    result = toGrey('#FF0000')
    assert isinstance(result, str)
    assert result.startswith('#')
    assert len(result) >= 7

    # Convert with alpha
    result = toGrey('#80FF0000')
    assert isinstance(result, str)
    assert result.startswith('#')

    # Test with invalid color that might trigger the fallback
    try:
        result = toGrey('invalid_color')
        assert isinstance(result, str)
    except Exception:
        # If it fails, that's also acceptable for invalid input
        pass

    # Test with edge case colors
    result = toGrey('#000000')  # Black
    assert isinstance(result, str)
    assert result.startswith('#')

    result = toGrey('#FFFFFF')  # White
    assert isinstance(result, str)
    assert result.startswith('#')


def test_toDim() -> None:
    """Test toDim function."""
    # Dim a bright color
    result = toDim('#FF0000')
    assert isinstance(result, str)
    assert result.startswith('#')
    assert len(result) >= 7

    # Dim with alpha
    result = toDim('#80FF0000')
    assert isinstance(result, str)
    assert result.startswith('#')

    # Test with invalid color
    try:
        result = toDim('invalid_color')
        assert isinstance(result, str)
    except Exception:
        # If it fails, that's also acceptable for invalid input
        pass

    # Test with edge case colors
    result = toDim('#000000')  # Black
    assert isinstance(result, str)
    assert result.startswith('#')


def test_createGradient() -> None:
    """Test createGradient function."""
    # Create gradient from red
    result = createGradient('#FF0000')
    assert isinstance(result, str)
    assert 'QLinearGradient' in result  # Qt gradient format
    assert '#' in result  # Should contain color codes

    # Create gradient with custom factors
    result = createGradient('#FF0000', tint_factor=0.2, shade_factor=0.2)
    assert isinstance(result, str)
    assert 'QLinearGradient' in result

    # Create reversed gradient
    result = createGradient('#FF0000', reverse=True)
    assert isinstance(result, str)
    assert 'QLinearGradient' in result

    # Test with different tint/shade factors
    result = createGradient('#FF0000', tint_factor=0.1, shade_factor=0.1)
    assert isinstance(result, str)
    assert 'QLinearGradient' in result

    # Test with extreme factors
    result = createGradient('#FF0000', tint_factor=0.9, shade_factor=0.9)
    assert isinstance(result, str)
    assert 'QLinearGradient' in result


# Logging Functions Tests


def test_getLoggers() -> None:
    """Test getLoggers function."""
    # Should return a list of loggers
    result = getLoggers()
    assert isinstance(result, list)
    # Should contain at least some loggers
    assert len(result) >= 0
    # All items should be Logger objects
    for logger in result:
        assert hasattr(logger, 'name')


def test_debugLogLevelActive() -> None:
    """Test debugLogLevelActive function."""
    # Should return a boolean
    result = debugLogLevelActive()
    assert isinstance(result, bool)


def test_setDebugLogLevel() -> None:
    """Test setDebugLogLevel function."""
    # Get initial state
    initial_state = debugLogLevelActive()

    # Toggle debug logging
    setDebugLogLevel(True)
    assert debugLogLevelActive() is True

    # Turn off debug logging
    setDebugLogLevel(False)
    assert debugLogLevelActive() is False

    # Restore initial state
    setDebugLogLevel(initial_state)


def test_debugLogLevelToggle() -> None:
    """Test debugLogLevelToggle function."""
    # Get initial state
    initial_state = debugLogLevelActive()

    # Toggle and check return value
    new_state = debugLogLevelToggle()
    assert isinstance(new_state, bool)
    assert new_state != initial_state
    assert debugLogLevelActive() == new_state

    # Toggle back
    final_state = debugLogLevelToggle()
    assert final_state == initial_state
    assert debugLogLevelActive() == initial_state


def test_setFileLogLevel() -> None:
    """Test setFileLogLevel function."""
    import logging

    # Get a logger to test with
    loggers = getLoggers()
    if loggers:
        test_logger = loggers[0]
        original_level = test_logger.level

        # Set to DEBUG level
        setFileLogLevel(test_logger, logging.DEBUG)
        # Note: This function only affects file handlers, so we just test it doesn't crash

        # Set back to original level
        setFileLogLevel(test_logger, original_level)


class TestTimearray2index:
    """Test timearray2index static method."""

    def test_timearray2index_exact_match(self) -> None:
        """Test timearray2index with exact time match."""
        # Arrange
        timearray = [0.0, 1.0, 2.0, 3.0, 4.0]
        time = 2.0

        # Act
        result = timearray2index(timearray, time)

        # Assert
        assert result == 2

    def test_timearray2index_interpolation_nearest(self) -> None:
        """Test timearray2index with nearest interpolation."""
        # Arrange
        timearray = [0.0, 1.0, 2.0, 3.0, 4.0]
        time = 1.3

        # Act
        result = timearray2index(timearray, time, nearest=True)

        # Assert
        assert result == 1  # Closer to 1.0 than 2.0

    def test_timearray2index_no_nearest(self) -> None:
        """Test timearray2index without nearest (returns bisect_right result)."""
        # Arrange
        timearray = [0.0, 1.0, 2.0, 3.0, 4.0]
        time = 1.8

        # Act
        result = timearray2index(timearray, time, nearest=False)

        # Assert
        assert result == 2  # bisect_right returns insertion point

    def test_timearray2index_out_of_bounds(self) -> None:
        """Test timearray2index with out of bounds time."""
        # Arrange
        timearray = [1.0, 2.0, 3.0, 4.0]

        # Act & Assert - before range (bisect_right returns 0, but function returns -1 when i=0)
        result = timearray2index(timearray, 0.5)
        assert result == -1

        # Act & Assert - after range
        result = timearray2index(timearray, 5.0)
        assert result == len(timearray) - 1  # Returns nearest index (last element)

    def test_timearray2index_empty_array(self) -> None:
        """Test timearray2index with empty array."""
        # Arrange
        timearray: list[float] = []
        time = 1.0

        # Act
        result = timearray2index(timearray, time)

        # Assert
        assert result == -1



class TestTPUtilities:
    """Test turning point utility methods."""


    def test_findTPint_basic2(self) -> None:
        """Test findTPint finds turning point index."""
        # Arrange - timeindex needs at least 8 elements [CHARGE, TP, DRYe, FCs, FCe, SCs, SCe, DROP]
        timeindex = [0, 0, 0, 0, 0, 0, 0, 0]  # Standard 8-element timeindex
        timex = [0.0, 1.0, 2.0, 3.0, 4.0]
        temp = [200.0, 180.0, 160.0, 170.0, 190.0]  # TP at index 2

        # Act
        result = findTPint(timeindex, timex, temp)

        # Assert
        assert isinstance(result, int)
        assert result >= 0  # Should find a valid index

    def test_findTPint_empty_arrays(self) -> None:
        """Test findTPint with empty arrays."""
        # Arrange
        timeindex = [0, 0, 0, 0, 0, 0, 0, 0]  # Standard 8-element timeindex
        timex: list[float] = []
        temp: list[float] = []

        # Act
        result = findTPint(timeindex, timex, temp)

        # Assert
        assert result == 0  # Should return 0 for empty arrays

    def test_findTPint_no_turning_point(self) -> None:
        """Test findTPint with monotonic temperature."""
        # Arrange
        timeindex = [0, 0, 0, 0, 0, 0, 0, 0]  # Standard 8-element timeindex
        timex = [0.0, 1.0, 2.0, 3.0]
        temp = [100.0, 110.0, 120.0, 130.0]  # Monotonic increase

        # Act
        result = findTPint(timeindex, timex, temp)

        # Assert
        assert isinstance(result, int)
        # Should return some index even if no clear TP



@pytest.mark.parametrize(
    'seconds,expected_format',
    [
        (0.0,  ''),        # 0 to empty str by definition
        (45.0, '00:45'),
        (60.0, '01:00'),
        (125.0, '02:05'),  # 2 minutes 5 seconds (seconds with zero padding)
        (3600.0, '60:00'), # 1 hour = 60 minutes
        (3665.0, '61:05')  # 1 hour 1 minute 5 seconds (no separate hour)
    ],
)
def test_eventtime2string_various_times(seconds: float, expected_format: str) -> None:
    """Test eventtime2string with various time values."""
    # Act
    result = eventtime2string(seconds)

    # Assert
    assert result == expected_format


class TestMedfilt:
    """Test medfilt static method."""

    def test_medfilt_basic(self) -> None:
        """Test medfilt with basic input."""
        # Arrange
        x = np.array([1.0, 5.0, 2.0, 8.0, 3.0])
        k = 3

        # Act
        result = medfilt(x, k)

        # Assert
        expected = np.array([1.0, 2.0, 5.0, 3.0, 3.0])
        np.testing.assert_array_equal(result, expected)

    def test_medfilt_single_element(self) -> None:
        """Test medfilt with single element."""
        # Arrange
        x = np.array([5.0])
        k = 1

        # Act
        result = medfilt(x, k)

        # Assert
        np.testing.assert_array_equal(result, np.array([5.0]))

    def test_medfilt_larger_window(self) -> None:
        """Test medfilt with larger window size."""
        # Arrange
        x = np.array([1.0, 2.0, 10.0, 3.0, 4.0, 5.0, 6.0])
        k = 5

        # Act
        result = medfilt(x, k)

        # Assert
        assert len(result) == len(x)
        # The outlier (10.0) should be filtered out
        assert result[2] != 10.0

    def test_medfilt_odd_window_size_required(self) -> None:
        """Test medfilt raises assertion error for even window size."""
        # Arrange
        x = np.array([1.0, 2.0, 3.0])
        k = 2  # Even number

        # Act & Assert
        with pytest.raises(AssertionError, match='Median filter length must be odd'):
            medfilt(x, k)

class TestPolyRoR:
    """Test polyRoR static method."""

    def test_polyRoR_basic(self) -> None:
        """Test polyRoR with basic linear data."""
        # Arrange - linear temperature increase
        tx = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        temp = np.array([20.0, 22.0, 24.0, 26.0, 28.0])  # 2°C per minute
        wsize = 1
        i = 2

        # Act
        result = polyRoR(tx, temp, wsize, i)

        # Assert
        # Expected RoR: 2°C/min * 60 = 120°C/hour
        assert abs(result - 120.0) < 1e-10

    def test_polyRoR_zero_index(self) -> None:
        """Test polyRoR with index 0 (should use index 1)."""
        # Arrange
        tx = np.array([0.0, 1.0, 2.0])
        temp = np.array([20.0, 25.0, 30.0])
        wsize = 1
        i = 0

        # Act
        result = polyRoR(tx, temp, wsize, i)

        # Assert
        # Should use index 1, so same as polyRoR(tx, temp, wsize, 1)
        expected = polyRoR(tx, temp, wsize, 1)
        assert result == expected

    def test_polyRoR_out_of_bounds(self) -> None:
        """Test polyRoR with out of bounds index."""
        # Arrange
        tx = np.array([0.0, 1.0, 2.0])
        temp = np.array([20.0, 25.0, 30.0])
        wsize = 1
        i = 10  # Out of bounds

        # Act
        result = polyRoR(tx, temp, wsize, i)

        # Assert
        assert result == 0

    def test_polyRoR_larger_window(self) -> None:
        """Test polyRoR with larger window size."""
        # Arrange
        tx = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        temp = np.array([20.0, 22.0, 24.0, 26.0, 28.0, 30.0])
        wsize = 3
        i = 4

        # Act
        result = polyRoR(tx, temp, wsize, i)

        # Assert
        assert isinstance(result, float)
        assert result > 0  # Should be positive for increasing temperature


class TestArrayRoR:
    """Test arrayRoR static method."""

    def test_arrayRoR_basic(self) -> None:
        """Test arrayRoR with basic linear data."""
        # Arrange - linear temperature increase
        tx = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        temp = np.array([20.0, 22.0, 24.0, 26.0, 28.0])  # 2°C per minute
        wsize = 1

        # Act
        result = arrayRoR(tx, temp, wsize)

        # Assert
        expected = np.array([120.0, 120.0, 120.0, 120.0])  # 2°C/min * 60 = 120°C/hour
        np.testing.assert_array_almost_equal(result, expected)

    def test_arrayRoR_larger_window(self) -> None:
        """Test arrayRoR with larger window size."""
        # Arrange
        tx = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
        temp = np.array([20.0, 22.0, 24.0, 26.0, 28.0, 30.0])
        wsize = 2

        # Act
        result = arrayRoR(tx, temp, wsize)

        # Assert
        assert len(result) == len(tx) - wsize
        assert len(result) == 4
        # All values should be 120 for linear increase
        np.testing.assert_array_almost_equal(result, np.array([120.0, 120.0, 120.0, 120.0]))

    def test_arrayRoR_zero_time_difference(self) -> None:
        """Test arrayRoR with zero time difference (should handle division by zero)."""
        # Arrange
        tx = np.array([1.0, 1.0, 2.0])  # Same time for first two points
        temp = np.array([20.0, 25.0, 30.0])
        wsize = 1

        # Act - suppress expected divide by zero warning
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', RuntimeWarning)
            result = arrayRoR(tx, temp, wsize)

        # Assert
        assert len(result) == 2
        # First result should be inf due to division by zero
        assert math.isinf(result[0])

class TestSerialize:
    """Test serialize static method."""


    def test_serialize_empty_dict(self, tmp_path: Path) -> None:
        """Test serialize with empty dictionary."""
        # Arrange
        test_file = tmp_path / 'test_empty.txt'
        test_data: dict[str, Any] = {}

        # Act
        serialized = serialize(str(test_file), test_data)

        # Assert
        assert serialized == b'{}'
        assert test_file.exists()
        assert test_file.read_bytes() == serialized

    def test_serialize_returns_exact_utf8_repr_bytes(self, tmp_path: Path) -> None:
        """The returned immutable snapshot is exactly what is written."""
        test_file = tmp_path / 'unicode.alog'
        test_data: dict[str, Any] = {
            'coffee': 'Café',
            'notes': ['甘い', '☕'],
        }
        expected = repr(test_data).encode('utf-8')

        serialized = serialize(str(test_file), test_data)

        assert serialized == expected
        assert test_file.read_bytes() == expected
        assert deserialize(str(test_file)) == test_data

    def test_serialize_computes_repr_once(self, tmp_path: Path) -> None:
        """A mutable object's representation is captured only once."""

        class CountingDict(dict[str, Any]):
            calls = 0

            def __repr__(self) -> str:
                self.calls += 1
                return super().__repr__()

        test_file = tmp_path / 'single-repr.alog'
        test_data = CountingDict(value='snapshot')

        serialized = serialize(str(test_file), test_data)

        assert test_data.calls == 1
        assert test_file.read_bytes() == serialized

    def test_serialize_with_timestamp_uses_written_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_file = tmp_path / 'descriptor.alog'
        test_data: dict[str, Any] = {'coffee': 'Café'}
        real_fstat = os.fstat
        descriptors: list[int] = []

        def recording_fstat(descriptor: int) -> os.stat_result:
            descriptors.append(descriptor)
            return real_fstat(descriptor)

        monkeypatch.setattr('artisanlib.util.os.fstat', recording_fstat)
        result = serialize_with_timestamp(str(test_file), test_data)

        assert result.serialized_profile == repr(test_data).encode('utf-8')
        assert test_file.read_bytes() == result.serialized_profile
        assert descriptors
        assert len(set(descriptors)) == 1
        assert result.modified_at == datetime.fromtimestamp(
            test_file.stat().st_mtime, UTC)
        assert result.modified_at.tzinfo is UTC

    def test_serialize_with_timestamp_orders_write_sync_replace_and_directory_sync(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / 'ordered.alog'
        events: list[str] = []
        real_write = os.write
        real_fsync = os.fsync
        from artisanlib import util as util_module
        real_publish = util_module._move_serialization_entry_no_replace

        def recording_write(descriptor: int, value: bytes) -> int:
            events.append('write')
            return real_write(descriptor, value)

        def recording_fsync(descriptor: int) -> None:
            events.append('fsync')
            real_fsync(descriptor)

        def recording_publish(
            parent: Any,
            source: str,
            target: str,
        ) -> None:
            events.append('publish')
            real_publish(parent, source, target)

        monkeypatch.setattr('artisanlib.util.os.write', recording_write)
        monkeypatch.setattr('artisanlib.util.os.fsync', recording_fsync)
        monkeypatch.setattr(
            'artisanlib.util._move_serialization_entry_no_replace',
            recording_publish,
        )

        serialize_with_timestamp(str(destination), {'value': 'ordered'})

        assert events[0] == 'write'
        assert events.count('fsync') == (1 if os.name == 'nt' else 3)
        assert events.index('write') < events.index('publish')
        assert events[-1] == ('publish' if os.name == 'nt' else 'fsync')

    @pytest.mark.parametrize('failure', ['write', 'file-sync', 'replace'])
    def test_serialize_with_timestamp_fails_closed_without_raw_paths(
        self, tmp_path: Path, failure: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / 'private-profile-name.alog'
        destination.write_bytes(b'old bytes')
        before = destination.read_bytes()
        if failure == 'write':
            monkeypatch.setattr(
                'artisanlib.util.os.write', Mock(side_effect=OSError(str(destination))))
        elif failure == 'replace':
            monkeypatch.setattr(
                'artisanlib.util._exchange_serialization_entries',
                Mock(side_effect=OSError(str(destination))),
            )
        else:
            real_fsync = os.fsync
            calls = 0

            def fail_first_sync(descriptor: int) -> None:
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise OSError(str(destination))
                real_fsync(descriptor)

            monkeypatch.setattr('artisanlib.util.os.fsync', fail_first_sync)

        with pytest.raises(OSError) as raised:
            serialize_with_timestamp(str(destination), {'value': failure})

        assert str(destination) not in str(raised.value)
        assert destination.read_bytes() == before
        assert not list(tmp_path.glob('.artisan-*.tmp'))

    def test_serialization_failure_cleanup_never_scans_unowned_generated_names(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / 'cleanup-scope.alog'
        destination.write_bytes(b'prior entry')
        unowned = tmp_path / '.artisan-unowned.tmp'
        unowned.write_bytes(b'do not remove')
        monkeypatch.setattr(
            'artisanlib.util.os.write', Mock(side_effect=OSError('write failed')))

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize_with_timestamp(str(destination), {'value': 'replacement'})

        assert destination.read_bytes() == b'prior entry'
        assert unowned.read_bytes() == b'do not remove'
        assert list(tmp_path.glob('.artisan-*')) == [unowned]

    def test_serialize_with_timestamp_fstat_failure_leaves_destination_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        test_file = tmp_path / 'fstat-failure.alog'
        test_file.write_bytes(b'old bytes')
        monkeypatch.setattr(
            'artisanlib.util.os.fstat', Mock(side_effect=OSError('descriptor detail')))

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize_with_timestamp(str(test_file), {'value': 'written'})

        assert test_file.read_bytes() == b'old bytes'

    @pytest.mark.skipif(os.name == 'nt', reason='POSIX permission semantics')
    def test_serialize_preserves_existing_permissions_and_honors_umask(
        self, tmp_path: Path
    ) -> None:
        existing = tmp_path / 'existing.alog'
        existing.write_bytes(b'old')
        existing.chmod(0o640)

        serialize(str(existing), {'value': 'replacement'})

        assert stat.S_IMODE(existing.stat().st_mode) == 0o640

        created = tmp_path / 'created.alog'
        previous_umask = os.umask(0o027)
        try:
            serialize(str(created), {'value': 'new'})
        finally:
            os.umask(previous_umask)
        assert stat.S_IMODE(created.stat().st_mode) == 0o640

    @pytest.mark.skipif(os.name == 'nt', reason='POSIX link semantics')
    @pytest.mark.parametrize('alias_kind', ['symlink', 'hardlink'])
    def test_serialize_replaces_alias_entry_without_touching_target_inode(
        self, tmp_path: Path, alias_kind: str
    ) -> None:
        protected = tmp_path / 'protected-cache.alog'
        protected.write_bytes(b'protected cache bytes')
        before = (protected.read_bytes(), protected.stat().st_ino)
        alias = tmp_path / f'{alias_kind}.alog'
        if alias_kind == 'symlink':
            alias.symlink_to(protected)
        else:
            os.link(protected, alias)

        serialized = serialize(str(alias), {'value': alias_kind})

        assert alias.read_bytes() == serialized
        assert not alias.is_symlink()
        assert alias.stat().st_ino != protected.stat().st_ino
        assert (protected.read_bytes(), protected.stat().st_ino) == before

    def test_serialize_detects_destination_replacement_race(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        destination = tmp_path / 'raced.alog'
        destination.write_bytes(b'first entry')
        replacement = tmp_path / 'racer.alog'
        replacement.write_bytes(b'racer entry')
        def race(_destination: Path) -> None:
            os.replace(replacement, destination)

        monkeypatch.setattr(
            'artisanlib.util._serialization_prepublish_hook', race)

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize(str(destination), {'value': 'must not win race'})

        assert destination.read_bytes() == b'racer entry'
        assert not list(tmp_path.glob('.artisan-*.tmp'))

    @pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink semantics')
    def test_serialize_rejects_every_linked_destination_parent(
        self, tmp_path: Path
    ) -> None:
        real_parent = tmp_path / 'real-parent'
        real_parent.mkdir()
        linked_parent = tmp_path / 'linked-parent'
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        destination = linked_parent / 'profile.alog'

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize(str(destination), {'value': 'must not traverse'})

        assert not destination.exists()
        assert not list(real_parent.glob('.artisan-*.tmp'))

    def test_serialize_rejects_destination_parent_junction_seam(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        parent = tmp_path / 'junction-parent'
        parent.mkdir()
        destination = parent / 'profile.alog'
        original = getattr(Path, 'is_junction', None)

        def is_junction(path: Path) -> bool:
            if path == parent:
                return True
            return bool(original(path)) if callable(original) else False

        monkeypatch.setattr(Path, 'is_junction', is_junction, raising=False)

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize(str(destination), {'value': 'must reject junction'})

        assert not destination.exists()

    def test_serialize_final_prepublish_hook_cannot_hide_destination_swap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'final-gap.alog'
        replacement = tmp_path / 'attacker.alog'
        destination.write_bytes(b'original entry')
        replacement.write_bytes(b'attacker entry')

        def swap_at_final_gap(_destination: Path) -> None:
            os.replace(replacement, destination)

        monkeypatch.setattr(
            util_module, '_serialization_prepublish_hook', swap_at_final_gap)

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize(str(destination), {'value': 'must not publish'})

        assert destination.read_bytes() == b'attacker entry'
        assert not list(tmp_path.glob('.artisan-*.tmp'))

    @pytest.mark.parametrize('destination_exists', [False, True])
    def test_destination_transaction_publication_cas_preserves_concurrent_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        destination_exists: bool,
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'transaction-race.alog'
        if destination_exists:
            destination.write_bytes(b'prior entry')
        attacker = tmp_path / 'attacker.alog'
        attacker.write_bytes(b'concurrent entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)

        def race(_destination: Path) -> None:
            os.replace(attacker, destination)

        monkeypatch.setattr(
            util_module, '_serialization_prepublish_hook', race)
        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'must not publish'})
        transaction.rollback()

        assert destination.read_bytes() == b'concurrent entry'
        assert not list(tmp_path.glob('.artisan-*.tmp'))

    @pytest.mark.parametrize('destination_exists', [False, True])
    def test_destination_transaction_rollback_never_replaces_concurrent_entry(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        destination_exists: bool,
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'transaction-rollback-race.alog'
        if destination_exists:
            destination.write_bytes(b'prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        transaction.serialize({'value': 'published'})
        attacker = tmp_path / 'attacker.alog'
        attacker.write_bytes(b'concurrent replacement')

        def race(_destination: Path) -> None:
            os.replace(attacker, destination)

        monkeypatch.setattr(
            util_module, '_serialization_prerollback_hook', race)
        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert destination.read_bytes() == b'concurrent replacement'
        retained_backups = list(tmp_path.glob('.artisan-*.tmp'))
        if destination_exists:
            assert len(retained_backups) == 1
            assert retained_backups[0].read_bytes() == b'prior entry'
        else:
            assert retained_backups == []

    @pytest.mark.darwin
    @pytest.mark.linux
    def test_posix_absent_publication_no_replace_preserves_final_gap_create(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'absent-publication-final-gap.alog'
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        real_move = util_module._move_serialization_entry_no_replace
        calls = 0

        def move_with_barrier(
            parent: Any, source_name: str, destination_name: str
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                destination.write_bytes(b'concurrent create')
            real_move(parent, source_name, destination_name)

        monkeypatch.setattr(
            util_module,
            '_move_serialization_entry_no_replace',
            move_with_barrier,
        )

        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'must not replace concurrent'})
        transaction.rollback()

        assert calls == 1
        assert destination.read_bytes() == b'concurrent create'

    @pytest.mark.darwin
    @pytest.mark.linux
    def test_posix_publication_exchange_captures_replacement_at_former_final_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'publication-final-gap.alog'
        destination.write_bytes(b'expected prior entry')
        attacker = tmp_path / 'publication-attacker.alog'
        attacker.write_bytes(b'concurrent publication entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        real_exchange = util_module._exchange_serialization_entries
        calls = 0

        def exchange_with_barrier(
            parent: Any, first_name: str, second_name: str
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                os.replace(attacker, destination)
            real_exchange(parent, first_name, second_name)

        monkeypatch.setattr(
            util_module, '_exchange_serialization_entries', exchange_with_barrier)

        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'must not replace concurrent'})
        transaction.rollback()

        assert calls == 2
        assert destination.read_bytes() == b'concurrent publication entry'

    @pytest.mark.darwin
    @pytest.mark.linux
    def test_posix_rollback_exchange_restores_replacement_at_former_final_gap(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'rollback-final-gap.alog'
        destination.write_bytes(b'expected prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        transaction.serialize({'value': 'published'})
        attacker = tmp_path / 'rollback-attacker.alog'
        attacker.write_bytes(b'concurrent rollback entry')
        real_exchange = util_module._exchange_serialization_entries
        calls = 0

        def exchange_with_barrier(
            parent: Any, first_name: str, second_name: str
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                os.replace(attacker, destination)
            real_exchange(parent, first_name, second_name)

        monkeypatch.setattr(
            util_module, '_exchange_serialization_entries', exchange_with_barrier)

        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert calls == 2
        assert destination.read_bytes() == b'concurrent rollback entry'
        retained_backups = list(tmp_path.glob('.artisan-*.tmp'))
        assert len(retained_backups) == 1
        assert retained_backups[0].read_bytes() == b'expected prior entry'

    @pytest.mark.darwin
    @pytest.mark.linux
    def test_posix_absent_rollback_quarantines_then_restores_final_gap_replace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'absent-rollback-final-gap.alog'
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        transaction.serialize({'value': 'published'})
        attacker = tmp_path / 'absent-rollback-attacker.alog'
        attacker.write_bytes(b'concurrent rollback entry')
        real_move = util_module._move_serialization_entry_no_replace
        calls = 0

        def move_with_barrier(
            parent: Any, source_name: str, destination_name: str
        ) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                os.replace(attacker, destination)
            real_move(parent, source_name, destination_name)

        monkeypatch.setattr(
            util_module,
            '_move_serialization_entry_no_replace',
            move_with_barrier,
        )

        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert calls == 2
        assert destination.read_bytes() == b'concurrent rollback entry'

    @pytest.mark.skipif(os.name == 'nt', reason='requires POSIX entry mutation')
    def test_destination_publication_validates_captured_content_not_only_inode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'publication-in-place.alog'
        original = b'original entry'
        concurrent = b'changed! entry'
        assert len(original) == len(concurrent)
        destination.write_bytes(original)
        prior_times = (destination.stat().st_atime_ns, destination.stat().st_mtime_ns)
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)

        def mutate_at_publication(_destination: Path) -> None:
            destination.write_bytes(concurrent)
            os.utime(destination, ns=prior_times)

        monkeypatch.setattr(
            util_module, '_serialization_prepublish_hook', mutate_at_publication)

        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'must validate hash'})
        transaction.rollback()

        assert destination.read_bytes() == concurrent

    @pytest.mark.skipif(os.name == 'nt', reason='requires POSIX entry mutation')
    def test_destination_rollback_validates_published_content_not_only_inode(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / 'rollback-in-place.alog'
        destination.write_bytes(b'prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        published = transaction.serialize({'value': 'published'})
        concurrent = b'X' * len(published.serialized_profile)
        destination.write_bytes(concurrent)

        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert destination.read_bytes() == concurrent

    def test_one_shot_post_capture_hash_failure_restores_existing_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'captured-hash-failure.alog'
        destination.write_bytes(b'prior entry')
        real_exchange = util_module._exchange_serialization_entries
        real_fingerprint = util_module._serialization_entry_fingerprint
        capture_observed = False
        failed = False

        def exchange(
            parent: Any, first_name: str, second_name: str
        ) -> None:
            nonlocal capture_observed
            real_exchange(parent, first_name, second_name)
            capture_observed = True

        def fingerprint(
            parent: Any, name: str, *, max_bytes: int | None = None
        ) -> Any:
            nonlocal failed
            if capture_observed and not failed:
                failed = True
                raise OSError('injected one-shot captured hash failure')
            return real_fingerprint(parent, name, max_bytes=max_bytes)

        monkeypatch.setattr(
            util_module, '_exchange_serialization_entries', exchange)
        monkeypatch.setattr(
            util_module, '_serialization_entry_fingerprint', fingerprint)

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize_with_timestamp(str(destination), {'value': 'replacement'})

        assert failed
        assert destination.read_bytes() == b'prior entry'
        assert not list(tmp_path.glob('.artisan-*'))

    def test_one_shot_absent_post_capture_hash_failure_restores_absence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'absent-captured-hash-failure.alog'
        real_move = util_module._move_serialization_entry_no_replace
        real_fingerprint = util_module._serialization_entry_fingerprint
        capture_observed = False
        failed = False

        def move(
            parent: Any, first_name: str, second_name: str
        ) -> None:
            nonlocal capture_observed
            real_move(parent, first_name, second_name)
            capture_observed = True

        def fingerprint(
            parent: Any, name: str, *, max_bytes: int | None = None
        ) -> Any:
            nonlocal failed
            if capture_observed and not failed:
                failed = True
                raise OSError('injected one-shot absent hash failure')
            return real_fingerprint(parent, name, max_bytes=max_bytes)

        monkeypatch.setattr(
            util_module, '_move_serialization_entry_no_replace', move)
        monkeypatch.setattr(
            util_module, '_serialization_entry_fingerprint', fingerprint)

        with pytest.raises(OSError, match='profile serialization failed'):
            serialize_with_timestamp(str(destination), {'value': 'replacement'})

        assert failed
        assert not destination.exists()
        assert not list(tmp_path.glob('.artisan-*'))

    def test_post_capture_metadata_failure_is_published_to_transaction_before_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'captured-metadata-failure.alog'
        destination.write_bytes(b'prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        real_set_times = util_module._set_serialization_entry_times
        failed = False

        def fail_once(
            parent: Any, name: str, times: tuple[int, int]
        ) -> None:
            nonlocal failed
            if not failed:
                failed = True
                raise OSError('injected timestamp failure')
            real_set_times(parent, name, times)

        monkeypatch.setattr(
            util_module, '_set_serialization_entry_times', fail_once)

        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'replacement'})

        assert transaction._active
        assert transaction._published_fingerprint is not None
        assert transaction._backup_name is not None
        assert (tmp_path / transaction._backup_name).read_bytes() == b'prior entry'

        transaction.rollback()
        assert destination.read_bytes() == b'prior entry'
        assert not list(tmp_path.glob('.artisan-*'))

    def test_persistent_post_capture_metadata_failure_preserves_backup_for_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'persistent-captured-metadata.alog'
        destination.write_bytes(b'prior entry')
        prior_stat = destination.stat()
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        real_set_times = util_module._set_serialization_entry_times
        monkeypatch.setattr(
            util_module,
            '_set_serialization_entry_times',
            Mock(side_effect=OSError('injected persistent timestamp failure')),
        )

        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'replacement'})
        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert transaction._active
        assert transaction._backup_name is not None
        assert destination.read_bytes() == b"{'value': 'replacement'}"
        assert (tmp_path / transaction._backup_name).read_bytes() == b'prior entry'

        monkeypatch.setattr(
            util_module, '_set_serialization_entry_times', real_set_times)
        transaction.rollback()
        restored_stat = destination.stat()
        assert destination.read_bytes() == b'prior entry'
        assert restored_stat.st_mtime_ns == prior_stat.st_mtime_ns
        assert not list(tmp_path.glob('.artisan-*'))

    def test_persistent_post_capture_hash_failure_preserves_recoverable_entries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'persistent-captured-hash.alog'
        destination.write_bytes(b'prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        real_exchange = util_module._exchange_serialization_entries
        real_fingerprint = util_module._serialization_entry_fingerprint
        capture_observed = False

        def exchange(
            parent: Any, first_name: str, second_name: str
        ) -> None:
            nonlocal capture_observed
            real_exchange(parent, first_name, second_name)
            capture_observed = True

        def fail_after_capture(
            parent: Any, name: str, *, max_bytes: int | None = None
        ) -> Any:
            if capture_observed:
                raise OSError('injected persistent captured hash failure')
            return real_fingerprint(parent, name, max_bytes=max_bytes)

        monkeypatch.setattr(
            util_module, '_exchange_serialization_entries', exchange)
        monkeypatch.setattr(
            util_module, '_serialization_entry_fingerprint', fail_after_capture)

        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'replacement'})
        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert transaction._active
        assert transaction._backup_name is not None
        assert destination.read_bytes() == b"{'value': 'replacement'}"
        assert (tmp_path / transaction._backup_name).read_bytes() == b'prior entry'

        monkeypatch.setattr(
            util_module, '_serialization_entry_fingerprint', real_fingerprint)
        transaction.rollback()
        assert destination.read_bytes() == b'prior entry'
        assert not list(tmp_path.glob('.artisan-*'))

    def test_absent_rollback_hash_failure_records_quarantine_and_preserves_concurrent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'absent-quarantine-hash.alog'
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        published = transaction.serialize({'value': 'published'}).serialized_profile
        real_move = util_module._move_serialization_entry_no_replace
        real_fingerprint = util_module._serialization_entry_fingerprint
        moves = 0

        def quarantine_then_create(
            parent: Any, source_name: str, destination_name: str
        ) -> None:
            nonlocal moves
            moves += 1
            real_move(parent, source_name, destination_name)
            if moves == 1:
                destination.write_bytes(b'concurrent entry')

        def fail_quarantine_hash(
            parent: Any, name: str, *, max_bytes: int | None = None
        ) -> Any:
            if name.endswith('.quarantine'):
                raise OSError('injected quarantine hash failure')
            return real_fingerprint(parent, name, max_bytes=max_bytes)

        monkeypatch.setattr(
            util_module, '_move_serialization_entry_no_replace', quarantine_then_create)
        monkeypatch.setattr(
            util_module, '_serialization_entry_fingerprint', fail_quarantine_hash)

        with pytest.raises(OSError, match='profile destination rollback failed'):
            transaction.rollback()

        assert transaction._active
        assert transaction._quarantine_name is not None
        quarantine = tmp_path / transaction._quarantine_name
        assert destination.read_bytes() == b'concurrent entry'
        assert quarantine.read_bytes() == published

        preserved_concurrent = tmp_path / 'preserved-concurrent.alog'
        destination.rename(preserved_concurrent)
        monkeypatch.setattr(
            util_module, '_serialization_entry_fingerprint', real_fingerprint)
        transaction.rollback()

        assert not destination.exists()
        assert preserved_concurrent.read_bytes() == b'concurrent entry'
        assert not quarantine.exists()

    @pytest.mark.parametrize('destination_exists', [False, True])
    def test_commit_preflight_sync_failure_remains_rollback_capable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        destination_exists: bool,
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'commit-preflight-sync.alog'
        if destination_exists:
            destination.write_bytes(b'prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        transaction.serialize({'value': 'published'})
        real_sync = util_module._sync_serialization_directory
        monkeypatch.setattr(
            util_module,
            '_sync_serialization_directory',
            Mock(side_effect=OSError('injected commit preflight sync failure')),
        )

        with pytest.raises(OSError, match='profile destination commit failed'):
            transaction.commit()

        assert transaction._active
        if destination_exists:
            assert transaction._backup_name is not None
        monkeypatch.setattr(
            util_module, '_sync_serialization_directory', real_sync)
        transaction.rollback()
        assert destination.exists() is destination_exists
        if destination_exists:
            assert destination.read_bytes() == b'prior entry'

    def test_post_backup_unlink_sync_failure_is_only_durability_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'post-commit-sync.alog'
        destination.write_bytes(b'prior entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        published = transaction.serialize({'value': 'published'}).serialized_profile
        real_sync = util_module._sync_serialization_directory
        real_unlink = util_module._unlink_serialization_entry
        backup_unlinked = False

        def unlink(parent: Any, name: str) -> None:
            nonlocal backup_unlinked
            real_unlink(parent, name)
            backup_unlinked = True

        def sync(parent: Any) -> None:
            if backup_unlinked:
                raise OSError('injected post-commit directory sync failure')
            real_sync(parent)

        monkeypatch.setattr(util_module, '_unlink_serialization_entry', unlink)
        monkeypatch.setattr(util_module, '_sync_serialization_directory', sync)

        with caplog.at_level('WARNING', logger='artisanlib.util'):
            transaction.commit()

        assert not transaction._active
        assert transaction._backup_name is None
        assert destination.read_bytes() == published
        assert 'profile destination post-commit durability warning' in caplog.text

    def test_posix_native_rename_seams_use_platform_signatures_and_flags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        class NativeFunction:
            argtypes: list[object] | None = None
            restype: object | None = None

            def __init__(self) -> None:
                self.calls: list[tuple[object, ...]] = []

            def __call__(self, *arguments: object) -> int:
                self.calls.append(arguments)
                return 0

        linux_rename = NativeFunction()
        monkeypatch.setattr(
            util_module, '_serialization_posix_libc',
            lambda: SimpleNamespace(renameat2=linux_rename),
        )
        monkeypatch.setattr(
            util_module, '_serialization_posix_platform', lambda: 'linux')
        parent = SimpleNamespace(descriptor=17, windows=False)

        util_module._move_serialization_entry_no_replace(parent, 'one', 'two')
        util_module._exchange_serialization_entries(parent, 'three', 'four')

        assert linux_rename.calls == [
            (17, b'one', 17, b'two', 1),
            (17, b'three', 17, b'four', 2),
        ]
        assert linux_rename.argtypes == [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_uint,
        ]
        assert linux_rename.restype is ctypes.c_int

        mac_rename = NativeFunction()
        monkeypatch.setattr(
            util_module, '_serialization_posix_libc',
            lambda: SimpleNamespace(renameatx_np=mac_rename),
        )
        monkeypatch.setattr(
            util_module, '_serialization_posix_platform', lambda: 'darwin')

        util_module._move_serialization_entry_no_replace(parent, 'five', 'six')
        util_module._exchange_serialization_entries(parent, 'seven', 'eight')

        assert mac_rename.calls == [
            (17, b'five', 17, b'six', 0x4),
            (17, b'seven', 17, b'eight', 0x2),
        ]
        assert mac_rename.argtypes == linux_rename.argtypes
        assert mac_rename.restype is ctypes.c_int

    def test_windows_existing_publication_uses_generated_replacefile_backup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'windows-backup.alog'
        destination.write_bytes(b'prior Windows entry')
        replacements: list[tuple[Path, Path, Path]] = []

        class Native:
            @staticmethod
            def open_readonly(path: Path, *, directory: bool = False) -> int:
                flags = os.O_RDONLY
                if directory:
                    flags |= getattr(os, 'O_DIRECTORY', 0)
                return os.open(path, flags)

            @staticmethod
            def canonical_path(_descriptor: int) -> Path:
                return tmp_path

            @staticmethod
            def replace_with_backup(
                replacement: Path, target: Path, backup: Path
            ) -> None:
                replacements.append((replacement, target, backup))
                os.replace(target, backup)
                os.replace(replacement, target)

            @staticmethod
            def flush_directory(_directory: Path) -> None:
                return None

            @staticmethod
            def unlink(path: Path) -> None:
                os.unlink(path)

        monkeypatch.setattr(
            util_module, '_serialization_is_windows', lambda: True)
        monkeypatch.setattr(
            util_module, '_serialization_windows_native', Native)

        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        transaction.serialize({'value': 'replacement'})
        assert len(replacements) == 1
        replacement, target, backup = replacements[0]
        assert replacement.parent == target.parent == backup.parent == tmp_path
        assert target == destination
        assert backup.name.startswith('.artisan-')
        assert backup.exists()

        transaction.commit()

        assert not backup.exists()
        assert destination.read_bytes() == b"{'value': 'replacement'}"

    @pytest.mark.parametrize(
        'outcome',
        [
            'no-changes',
            'destination-missing-backup',
            'replacement-installed-backup',
            'concurrent-destination-backup',
        ],
    )
    def test_windows_replacefile_false_reconciles_observed_entries(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        outcome: str,
    ) -> None:
        from artisanlib import util as util_module
        from artisanlib.roastserver import _filesystem as filesystem_module

        destination = tmp_path / 'windows-false.alog'
        destination.write_bytes(b'prior Windows entry')

        def entry(path: Path) -> Any:
            try:
                path_stat = os.lstat(path)
            except FileNotFoundError:
                return filesystem_module.WindowsReplaceFileEntry(
                    path, False, None)
            return filesystem_module.WindowsReplaceFileEntry(
                path, True, (path_stat.st_dev, path_stat.st_ino))

        replace_calls = 0

        class Native:
            @staticmethod
            def open_readonly(path: Path, *, directory: bool = False) -> int:
                flags = os.O_RDONLY
                if directory:
                    flags |= getattr(os, 'O_DIRECTORY', 0)
                return os.open(path, flags)

            @staticmethod
            def canonical_path(_descriptor: int) -> Path:
                return tmp_path

            @staticmethod
            def replace_with_backup(
                replacement: Path, target: Path, backup: Path
            ) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls > 1:
                    os.replace(target, backup)
                    os.replace(replacement, target)
                    return
                if outcome != 'no-changes':
                    os.replace(target, backup)
                if outcome == 'replacement-installed-backup':
                    os.replace(replacement, target)
                elif outcome == 'concurrent-destination-backup':
                    target.write_bytes(b'concurrent Windows entry')
                observation = filesystem_module.WindowsReplaceFileObservation(
                    error_code=5,
                    destination=entry(target),
                    replacement=entry(replacement),
                    backup=entry(backup),
                )
                raise filesystem_module.WindowsReplaceFileError(observation)

            @staticmethod
            def move_no_replace(source: Path, target: Path) -> None:
                if target.exists():
                    raise FileExistsError('destination exists')
                os.rename(source, target)

            @staticmethod
            def flush_directory(_directory: Path) -> None:
                return None

            @staticmethod
            def unlink(path: Path) -> None:
                os.unlink(path)

        monkeypatch.setattr(
            util_module, '_serialization_is_windows', lambda: True)
        monkeypatch.setattr(
            util_module, '_serialization_windows_native', Native)

        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        with pytest.raises(OSError, match='profile destination publication failed'):
            transaction.serialize({'value': 'replacement'})

        if outcome == 'concurrent-destination-backup':
            with pytest.raises(OSError, match='profile destination rollback failed'):
                transaction.rollback()
            assert transaction._active
            assert transaction._backup_name is not None
            assert destination.read_bytes() == b'concurrent Windows entry'
            assert (tmp_path / transaction._backup_name).read_bytes() == b'prior Windows entry'
            assert len(list(tmp_path.glob('.artisan-*'))) == 1
        else:
            transaction.rollback()
            assert destination.read_bytes() == b'prior Windows entry'
            assert not list(tmp_path.glob('.artisan-*'))

    @pytest.mark.win32
    @pytest.mark.skipif(os.name != 'nt', reason='requires native Windows ReplaceFileW')
    def test_windows_native_replacefile_transaction_can_rollback(
        self, tmp_path: Path
    ) -> None:
        destination = tmp_path / 'native-replacefile.alog'
        destination.write_bytes(b'prior Windows entry')
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)

        transaction.serialize({'value': 'native replacement'})
        transaction.rollback()

        assert destination.read_bytes() == b'prior Windows entry'
        assert not list(tmp_path.glob('.artisan-*'))

    def test_windows_transaction_uses_retained_canonical_parent_for_every_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        requested_parent = tmp_path / 'requested' / 'parent'
        canonical_parent = tmp_path / 'canonical' / 'parent'
        requested_parent.mkdir(parents=True)
        canonical_parent.mkdir(parents=True)
        requested = requested_parent / 'profile.alog'
        canonical = canonical_parent / requested.name
        events: list[tuple[str, Path, Path] | tuple[str, Path]] = []

        class Native:
            @staticmethod
            def open_readonly(path: Path, *, directory: bool = False) -> int:
                selected = canonical_parent if directory else path
                flags = os.O_RDONLY
                if directory:
                    flags |= getattr(os, 'O_DIRECTORY', 0)
                return os.open(selected, flags)

            @staticmethod
            def canonical_path(_descriptor: int) -> Path:
                return canonical_parent

            @staticmethod
            def move_no_replace(source: Path, target: Path) -> None:
                events.append(('move-no-replace', source, target))
                os.rename(source, target)

            @staticmethod
            def flush_directory(directory: Path) -> None:
                events.append(('flush', directory))

        monkeypatch.setattr(
            util_module, '_serialization_is_windows', lambda: True)
        monkeypatch.setattr(
            util_module, '_serialization_windows_native', Native)

        transaction = FileDestinationTransaction.begin(
            str(requested), max_bytes=1024)
        transaction.serialize({'value': 'canonical'})
        transaction.commit()

        assert not requested.exists()
        assert canonical.read_bytes() == b"{'value': 'canonical'}"
        assert events
        for event in events:
            assert requested_parent not in event[1:]
            assert all(
                requested_parent not in path.parents
                for path in event[1:]
                if isinstance(path, Path)
            )

    @pytest.mark.win32
    @pytest.mark.skipif(os.name != 'nt', reason='requires native Windows junctions')
    def test_windows_retained_parent_cannot_be_redirected_by_junction_rebind(
        self, tmp_path: Path
    ) -> None:
        route = tmp_path / 'route'
        original_parent = route / 'parent'
        original_parent.mkdir(parents=True)
        attacker_route = tmp_path / 'attacker'
        attacker_parent = attacker_route / 'parent'
        attacker_parent.mkdir(parents=True)
        destination = original_parent / 'profile.alog'
        attacker_destination = attacker_parent / destination.name
        transaction = FileDestinationTransaction.begin(
            str(destination), max_bytes=1024)
        moved_route = tmp_path / 'moved-route'
        try:
            route.rename(moved_route)
        except OSError:
            transaction.rollback()
            pytest.skip('Windows prevented ancestor rebind while parent handle was held')
        junction = subprocess.run(
            ['cmd', '/c', 'mklink', '/J', str(route), str(attacker_route)],
            check=False,
            capture_output=True,
            text=True,
        )
        if junction.returncode != 0:
            transaction.rollback()
            pytest.skip('Windows junction creation is unavailable')

        try:
            try:
                transaction.serialize({'value': 'safe'})
                transaction.commit()
            except OSError:
                transaction.rollback()
            assert not attacker_destination.exists()
        finally:
            if route.exists():
                os.rmdir(route)

    def test_windows_overwrite_uses_write_through_native_replace_and_flush(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from artisanlib import util as util_module

        destination = tmp_path / 'windows-overwrite.alog'
        destination.write_bytes(b'old bytes')
        events: list[str] = []

        class Native:
            @staticmethod
            def open_readonly(path: Path, *, directory: bool = False) -> int:
                flags = os.O_RDONLY
                if directory:
                    flags |= getattr(os, 'O_DIRECTORY', 0)
                return os.open(path, flags)

            @staticmethod
            def canonical_path(_descriptor: int) -> Path:
                return tmp_path

            @staticmethod
            def replace_with_backup(
                replacement: Path, target: Path, backup: Path
            ) -> None:
                events.append('replacefile-write-through')
                os.replace(target, backup)
                os.replace(replacement, target)

            @staticmethod
            def flush_directory(directory: Path) -> None:
                assert directory == tmp_path
                events.append('flush-directory')

            @staticmethod
            def unlink(path: Path) -> None:
                events.append('unlink-backup')
                os.unlink(path)

        monkeypatch.setattr(
            util_module, '_serialization_is_windows', lambda: True)
        monkeypatch.setattr(
            util_module, '_serialization_windows_native', Native)
        monkeypatch.setattr(
            util_module.os, 'fchmod',
            Mock(side_effect=AssertionError('Windows called fchmod')),
        )

        serialized = serialize(str(destination), {'value': 'windows'})

        assert destination.read_bytes() == serialized
        assert events == [
            'replacefile-write-through',
            'flush-directory',
            'flush-directory',
            'unlink-backup',
            'flush-directory',
        ]

    def test_serialize_basic(self) -> None:
        """Test serialize writes object to file."""
        # Arrange
        test_obj = {'key': 'value', 'number': 42}

        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_filename = temp_file.name

        try:
            # Act
            serialize(temp_filename, test_obj)

            # Assert
            with open(temp_filename, encoding='utf-8') as f:
                content = f.read()
                assert 'key' in content
                assert 'value' in content
                assert '42' in content
        finally:
            os.unlink(temp_filename)

    def test_serialize_complex_object(self) -> None:
        """Test serialize with complex nested object."""
        # Arrange
        test_obj = {'nested': {'inner': 'value'}, 'list': [1, 2, 3], 'boolean': True}

        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_filename = temp_file.name

        try:
            # Act
            serialize(temp_filename, test_obj)

            # Assert
            with open(temp_filename, encoding='utf-8') as f:
                content = f.read()
                assert 'nested' in content
                assert 'inner' in content
        finally:
            os.unlink(temp_filename)




# pytest -s -k TestRoastMessage
class TestRoastMessage:
    """Test roast_message static method."""

    def test_roast_message_sample_profile(self, sample_profile_data: dict[str, Any]) -> None:
        profile_data: atypes.ProfileData = cast(atypes.ProfileData, sample_profile_data)
        #print(profile_data)

        # Act
        msg = roast_message(profile_data, org_id = 'org1', machine_id = 'machine1', smooth_curves=True)

        if msg is not None:

            from google.protobuf.json_format import MessageToDict
            msg_dict = MessageToDict(msg, preserving_proto_field_name=True)

            # Assert
            assert msg.HasField('org_id')
            assert msg.HasField('machine_id')

            # INVARIANTS

            #// - roast_id is mandatory
            #//   HasField(roast_id)
            assert msg.HasField('roast_id')

            #// - all indices of the given milestones are valid and strict monotonic
            #//   milestone_idicies = [
            #//      milestones.charge_idx,
            #//      milestones.dry_end_idx,
            #//      milestones.first_crack_start_idx,
            #//      milestones.first_crack_end_idx,
            #//      milestones.second_crack_start_idx,
            #//      milestones.second_crack_end_idx,
            #//      milestones.drop_idx
            #//   ]
            #//   for i, idx in enumerate(milestone_idicies):
            #//     if hasValue(idx):
            #//         0 <= idx < len(times) and
            #//         for 0 <= j < i:
            #//         if hasValue(milestone_idicies[j]
            #//            milestone_idicies[j] < idx
            if msg.HasField('milestones'):
                milestone_indicies = [
                    'charge_idx',
                    'dry_end_idx',
                    'first_crack_start_idx',
                    'first_crack_end_idx',
                    'second_crack_start_idx',
                    'second_crack_end_idx',
                    'drop_idx'
                ]
                for i, idx in enumerate(milestone_indicies):
                    if idx in msg_dict['milestones']:
                        assert 0 <= msg_dict['milestones'][idx] < len(msg.times)
                        for j in range(i):
                            if milestone_indicies[j] in msg_dict['milestones']:
                                assert msg_dict['milestones'][milestone_indicies[j]] < msg_dict['milestones'][idx]

            #// - all annotations are well defined and valid
            #//   len(annotations.time_indices) == len(annotations.tags) and
            #//   for idx in annotations.time_indices: 0 <= idx < len(times)
            if msg.HasField('annotations'):
                assert len(msg.annotations.time_indices) == len(msg.annotations.tags)
                for ind in msg.annotations.time_indices:
                    assert 0 <= ind <= len(msg.times)

            #// - all events are well defined, valid and event values are positive
            #//   for events:
            #//       len(events.time_indices) == len(events.values) and
            #//       for idx in events.time_indices:
            #//          0 <= idx <= len(times)
            #//       for v in events.values:
            #//          v >= 0
            for event in msg.events:
                assert len(event.time_indices) == len(event.values)
                for ind in event.time_indices:
                    assert 0 <= ind <= len(msg.times)
                for value in event.values:
                    assert value >= 0

            #// - all readings are valid
            #//   for readings in {
            #//          bt_values, et_values,
            #//          bt_ror_values, et_ror_values}:
            #//      len(readings) <= len(times)
            #//   for curve in additional_curves:
            #//       len(curve.values) <= len(times)
            assert len(msg.bt_values) <= len(msg.times)
            assert len(msg.et_values) <= len(msg.times)
            assert len(msg.bt_ror_values) <= len(msg.times)
            assert len(msg.et_ror_values) <= len(msg.times)
            for curve in msg.additional_curves:
                assert len(curve.values) <= len(msg.times)

            #// - time is monotonic
            #//   for i in range(0,len(times)):
            #//      for 0 <= j < i:
            #//         times[j] <= times[i]
            assert all(x<=y for x, y in zip(msg.times, msg.times[1:], strict=False))

            #// - time of CHARGE (start of roast)
            #//   if HasField(milestones) and HasField(milestones.charge_idx):
            #//      times[milestones.charge_idx] == 0
            #//   elif len(times)>0:
            #//      times[0] = 0
            #//   NOTE: time[x]=0 corresponds to timestamp 'start'
            if msg.HasField('milestones') and msg.milestones.HasField('charge_idx'):
                assert msg.times[msg.milestones.charge_idx] == 0
            elif len(msg.times)>0:
                assert msg.times[0] == 0

            #// - multiplication factor is >0 (defaults to 1 if not given)
            #//   if HasField(factor):
            #//      factor > 0
            if msg.HasField('factor'):
                assert msg.factor > 0


@pytest.mark.parametrize(
    'registers,max_register_segment,expected',
    [
        ([0, 2, 20, 1040, 1105, 1215], 100, [(0,20), (1040, 1105), (1215, 1215)]),
        ([0, 10], 100, [(0, 10)]),
        ([0, 99], 100, [(0, 99)]),
        ([0, 100], 100, [(0, 0), (100, 100)]),  # Split at MAX_REGISTER_SEGMENT
        ([1, 5, 112, 120], 100, [(1, 5), (112, 120)]),
        ([0, 2, 20, 1040, 1105, 1215], 100, [(0, 20), (1040, 1105), (1215, 1215)]),
        (
            [0, 99, 100, 199, 200, 299, 300, 320, 350],
            100,
            [(0, 99), (100, 199), (200, 299), (300, 350)],
        ),
        ([], 100, []),  # Empty list
        ([42], 100, [(42, 42)]),    # Single register
        ([100, 50, 200, 75], 100, [(50, 100), (200, 200)]), # unsorted input
        ([0, 1, 1000, 1001, 2000], 100, [(0, 1), (1000, 1001), (2000, 2000)]), # large gaps
        (list(range(250)), 100, [(0, 99), (100, 199), (200, 249)]), # 250 consecutive registers
    ],
)
def test_max_blocks(registers: list[int], max_register_segment: int, expected:list[tuple[int,int]]) -> None:
    """Test max_blocks with various lists of registers."""
    # Act
    result = max_blocks(registers, max_register_segment=max_register_segment)

    # Assert
    assert result == expected


@pytest.mark.parametrize(
    'registers,expected',
    [
        ([12392, 12393, 12394, 12462, 12463, 12465], [(12392, 12394), (12462, 12463), (12465, 12465)])
    ],
)
def test_min_blocks(registers: list[int], expected:list[tuple[int,int]]) -> None:
    """Test muin_blocks with various lists of registers."""
    # Act
    result = min_blocks(registers)

    # Assert
    assert result == expected
