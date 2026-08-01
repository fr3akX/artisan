from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime
from typing import Final, cast
from uuid import UUID

import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver import metadata as metadata_module
from artisanlib.roastserver.contract import JS_SAFE_INTEGER_MAX, MAX_METADATA_BYTES
from artisanlib.roastserver.metadata import project_profile

MODIFIED = datetime(2026, 8, 1, 12, 34, 56, 123456, tzinfo=UTC)

# Locally pinned from the compatible /aroast schema. Tests intentionally do not
# import or execute Artisan Server code.
_PINNED_STRING_MAX: Final[dict[str, int]] = {
    'label': 255,
    'batch_prefix': 50,
    'machine': 50,
    'setup': 50,
    'notes': 1023,
    'cupping_notes': 1023,
    'color_system': 25,
}
_PINNED_INTEGER_RANGES: Final[dict[str, tuple[int, int]]] = {
    'batch_number': (0, 65_534),
    'batch_pos': (0, 255),
}
_PINNED_FLOAT_RANGES: Final[dict[str, tuple[float, float]]] = {
    'amount': (0, 65_534),
    'end_weight': (0, 65_534),
    'defects_weight': (0, 65_534),
    'moisture': (0, 100),
    'density_roasted': (0, 1_000),
    'whole_color': (0, 255),
    'ground_color': (0, 255),
    'temperature': (-1_000, 1_000),
    'pressure': (800, 1_200),
    'humidity': (0, 100),
    'charge_temp_ET': (-1_000, 1_000),
    'charge_temp': (-1_000, 1_000),
    'TP_temp': (-1_000, 1_000),
    'DRY_temp': (-1_000, 1_000),
    'FCs_temp': (-1_000, 1_000),
    'FCe_temp': (-1_000, 1_000),
    'drop_temp': (-1_000, 1_000),
    'drop_temp_ET': (-1_000, 1_000),
    'charge_time': (0, 86_400),
    'TP_time': (0, 86_400),
    'DRY_time': (0, 86_400),
    'FCs_time': (0, 86_400),
    'FCe_time': (0, 86_400),
    'drop_time': (0, 86_400),
    'FCs_RoR': (-1_000, 1_000),
    'DEV_time': (0, 86_400),
    'DEV_ratio': (0, 100),
    'BTU_ELEC': (0, 1_000_000_000),
    'BTU_LPG': (0, 1_000_000_000),
    'BTU_NG': (0, 1_000_000_000),
    'BTU_roast': (0, 1_000_000_000),
    'BTU_preheat': (0, 1_000_000_000),
    'BTU_bbp': (0, 1_000_000_000),
    'BTU_cooling': (0, 1_000_000_000),
    'BTU_batch': (0, 1_000_000_000),
    'CO2_roast': (0, 1_000_000_000),
    'CO2_preheat': (0, 1_000_000_000),
    'CO2_bbp': (0, 1_000_000_000),
    'CO2_cooling': (0, 1_000_000_000),
    'CO2_batch': (0, 1_000_000_000),
}
_PINNED_AROAST_KEYS: Final[frozenset[str]] = frozenset(
    {
        'roast_id',
        'modified_at',
        'date',
        'end_weight_est',
        'location',
        'coffee',
        'blend',
    }
    | _PINNED_STRING_MAX.keys()
    | _PINNED_INTEGER_RANGES.keys()
    | _PINNED_FLOAT_RANGES.keys()
)


def _validate_pinned_reference(value: object) -> None:
    assert isinstance(value, dict)
    reference = cast(dict[str, object], value)
    assert set(reference) <= {'hr_id', 'label'}
    hr_id = reference.get('hr_id')
    label = reference.get('label')
    assert isinstance(hr_id, str) and 1 <= len(hr_id) <= 100
    assert isinstance(label, str) and 1 <= len(label) <= 255


def _validate_pinned_aroast(payload: dict[str, object]) -> None:
    """Repeat the pinned schema shape closely enough to reject connector drift."""

    assert set(payload) <= _PINNED_AROAST_KEYS
    roast_id = payload.get('roast_id')
    assert isinstance(roast_id, str)
    UUID(roast_id)
    for field in ('modified_at', 'date'):
        value = payload.get(field)
        if value is None:
            continue
        assert isinstance(value, str)
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        assert parsed.tzinfo is not None and parsed.utcoffset() is not None
    for field, string_maximum in _PINNED_STRING_MAX.items():
        value = payload.get(field)
        if value is not None:
            assert isinstance(value, str) and len(value) <= string_maximum
    for field, (integer_minimum, integer_maximum) in _PINNED_INTEGER_RANGES.items():
        value = payload.get(field)
        if value is not None:
            assert isinstance(value, int) and not isinstance(value, bool)
            assert integer_minimum <= value <= integer_maximum
    for field, (float_minimum, float_maximum) in _PINNED_FLOAT_RANGES.items():
        value = payload.get(field)
        if value is not None:
            assert isinstance(value, int | float) and not isinstance(value, bool)
            assert math.isfinite(value) and float_minimum <= value <= float_maximum
    if 'end_weight_est' in payload:
        assert isinstance(payload['end_weight_est'], bool)
    for field in ('location', 'coffee'):
        if field in payload:
            _validate_pinned_reference(payload[field])
    if 'blend' in payload:
        raw_blend = payload['blend']
        assert isinstance(raw_blend, dict)
        blend = cast(dict[str, object], raw_blend)
        assert set(blend) <= {'label', 'ingredients'}
        label = blend.get('label')
        raw_ingredients = blend.get('ingredients')
        assert isinstance(label, str) and 1 <= len(label) <= 255
        assert isinstance(raw_ingredients, list)
        ingredients = cast(list[object], raw_ingredients)
        assert 1 <= len(ingredients) <= 32
        for raw_ingredient in ingredients:
            assert isinstance(raw_ingredient, dict)
            ingredient = cast(dict[str, object], raw_ingredient)
            assert set(ingredient) <= {'coffee', 'ratio', 'ratio_num', 'ratio_denom'}
            _validate_pinned_reference(ingredient.get('coffee'))
            ratio = ingredient.get('ratio')
            assert isinstance(ratio, int | float) and not isinstance(ratio, bool)
            assert math.isfinite(ratio) and 0 <= ratio <= 1.0001
            for field in ('ratio_num', 'ratio_denom'):
                value = ingredient.get(field)
                if value is not None:
                    assert isinstance(value, int) and not isinstance(value, bool)
                    assert 1 <= value <= 10_000


def _aroast(profile: ProfileData) -> dict[str, object]:
    payload = cast(dict[str, object], json.loads(project_profile(profile, MODIFIED).aroast_json))
    _validate_pinned_aroast(payload)
    return payload


def minimal_profile() -> ProfileData:
    return cast(
        ProfileData,
        {
            'mode': 'C',
            'roastUUID': '11111111111141118111111111111111',
            'roastepoch': 1785578400,
            'roasttzoffset': 7200,
        },
    )


@pytest.fixture
def sample_profile() -> ProfileData:
    return cast(
        ProfileData,
        {
            'mode': 'C',
            'roastUUID': '11111111111141118111111111111111',
            'roastepoch': 1785578400,
            'roasttzoffset': 7200,
            'title': 'Summer Roast',
            'beans': 'Washed Ethiopia',
            'weight': [1000.0, 845.0, 'g'],
            'end_weight_est': 1,
            'defects_weight': 25.0,
            'operator': 'Roaster One',
            'roastbatchprefix': 'B',
            'roastbatchnr': 12,
            'roastbatchpos': 1,
            'roastertype': 'Sample Roaster',
            'machinesetup': '12 kg drum',
            'whole_color': 65.0,
            'ground_color': 70.0,
            'color_system': 'Agtron',
            'ambientTemp': 22.5,
            'ambient_humidity': 45.0,
            'ambient_pressure': 1013.2,
            'moisture_greens': 11.5,
            'moisture_roasted': 2.1,
            'density': [0.72, 'g', 1, 'l'],
            'density_roasted': [0.41, 'g', 1, 'l'],
            'plus_store': 'secret-store',
            'plus_coffee': 'secret-coffee',
            'scheduleID': 'schedule-id',
            'scheduleDate': '2026-08-02',
            'signature': 'signature-data',
            'roastingnotes': 'omit me',
            'cuppingnotes': 'omit me too',
            'unknown_field': 'ignored',
            'computed': {
                'CHARGE_time': 0.0,
                'CHARGE_ET': 200.0,
                'CHARGE_BT': 190.0,
                'TP_time': 90.0,
                'TP_BT': 120.0,
                'DRY_time': 300.0,
                'DRY_BT': 160.0,
                'FCs_time': 480.0,
                'FCs_BT': 185.0,
                'FCe_time': 540.0,
                'FCe_BT': 195.0,
                'DROP_time': 600.0,
                'DROP_ET': 215.0,
                'DROP_BT': 205.0,
                'fcs_ror': 10.0,
                'finishphasetime': 120.0,
                'totaltime': 600.0,
                'BTU_batch': 15000.0,
                'BTU_roast': 14000.0,
                'BTU_preheat': 5000.0,
                'BTU_bbp': 1000.0,
                'BTU_cooling': 300.0,
                'BTU_ELEC': 100.0,
                'BTU_LPG': 200.0,
                'BTU_NG': 300.0,
                'CO2_batch': 2500.0,
                'CO2_roast': 1500.0,
                'CO2_preheat': 500.0,
                'CO2_bbp': 250.0,
                'CO2_cooling': 100.0,
            },
        },
    )


def test_projection_is_deterministic_and_matches_aroast_names(sample_profile: ProfileData) -> None:
    first = project_profile(sample_profile, MODIFIED)
    second = project_profile(copy.deepcopy(sample_profile), MODIFIED)

    assert first == second
    assert len(first.aroast_json) <= MAX_METADATA_BYTES
    aroast = json.loads(first.aroast_json)
    assert aroast['roast_id'] == '11111111111141118111111111111111'
    assert aroast['modified_at'] == '2026-08-01T12:34:56.123456+00:00'
    assert aroast['date'] == '2026-08-01T10:00:00+00:00'
    assert aroast['amount'] == pytest.approx(1.0)
    assert aroast['charge_temp'] == pytest.approx(190.0)
    assert aroast['charge_temp_ET'] == pytest.approx(200.0)
    assert aroast['charge_time'] == pytest.approx(0.0)
    assert aroast['drop_temp'] == pytest.approx(205.0)
    assert aroast['drop_temp_ET'] == pytest.approx(215.0)
    assert aroast['FCs_RoR'] == pytest.approx(10.0)
    assert aroast['DEV_time'] == pytest.approx(120.0)
    assert aroast['DEV_ratio'] == pytest.approx(20.0)
    assert aroast['end_weight_est'] is True
    _validate_pinned_aroast(aroast)


def test_revision_hints_include_operator_units_and_events(sample_profile: ProfileData) -> None:
    projected = project_profile(sample_profile, MODIFIED)

    assert len(projected.revision_json) <= MAX_METADATA_BYTES
    hints = json.loads(projected.revision_json)
    assert hints['operator'] == 'Roaster One'
    assert hints['temperature_unit'] == 'C'
    assert hints['green_weight_kg'] == pytest.approx(1.0)
    assert hints['roasted_weight_kg'] == pytest.approx(0.845)
    assert hints['defect_weight_kg'] == pytest.approx(0.025)
    assert hints['roast_at'] == '2026-08-01T10:00:00+00:00'
    assert hints['roast_timezone_offset_seconds'] == 7200
    assert 'defects_weight_kg' not in hints
    assert 'roast_at_utc' not in hints
    assert 'roast_tz_offset_seconds' not in hints
    assert hints['events']['charge']['time_seconds'] == pytest.approx(0.0)
    assert hints['events']['charge']['environment_temp_c'] == pytest.approx(200.0)
    assert hints['events']['first_crack_start']['time_seconds'] == pytest.approx(480.0)
    assert hints['events']['drop']['bean_temp_c'] == pytest.approx(205.0)


@pytest.mark.parametrize(
    ('unit', 'raw_value', 'expected_kg'),
    (
        ('g', 1000.0, 1.0),
        ('Kg', 1.0, 1.0),
        ('kg', 1.0, 1.0),
        ('lb', 2.20462262185, 1.0),
        ('oz', 35.2739619496, 1.0),
    ),
)
def test_weight_units_convert_to_kilograms(unit: str, raw_value: float, expected_kg: float) -> None:
    profile = minimal_profile()
    profile['weight'] = [raw_value, raw_value / 2, unit]

    aroast = json.loads(project_profile(profile, MODIFIED).aroast_json)

    assert aroast['amount'] == pytest.approx(expected_kg)
    assert aroast['end_weight'] == pytest.approx(expected_kg / 2)


def test_fahrenheit_temperatures_and_ror_convert_to_celsius() -> None:
    profile = minimal_profile()
    profile['mode'] = 'F'
    profile['ambientTemp'] = 68.0
    profile['computed'] = {
        'CHARGE_ET': 392.0,
        'CHARGE_BT': 374.0,
        'TP_time': 30.0,
        'TP_BT': 302.0,
        'DRY_time': 300.0,
        'DRY_BT': 320.0,
        'FCs_time': 480.0,
        'FCs_BT': 365.0,
        'FCe_time': 540.0,
        'FCe_BT': 383.0,
        'DROP_time': 600.0,
        'DROP_ET': 410.0,
        'DROP_BT': 401.0,
        'fcs_ror': 18.0,
        'finishphasetime': 120.0,
        'totaltime': 600.0,
    }

    projected = project_profile(profile, MODIFIED)
    aroast = json.loads(projected.aroast_json)
    hints = json.loads(projected.revision_json)

    assert aroast['charge_temp_ET'] == pytest.approx(200.0)
    assert aroast['charge_temp'] == pytest.approx(190.0)
    assert aroast['TP_temp'] == pytest.approx(150.0)
    assert aroast['DRY_temp'] == pytest.approx(160.0)
    assert aroast['FCs_temp'] == pytest.approx(185.0)
    assert aroast['FCe_temp'] == pytest.approx(195.0)
    assert aroast['drop_temp_ET'] == pytest.approx(210.0)
    assert aroast['drop_temp'] == pytest.approx(205.0)
    assert aroast['FCs_RoR'] == pytest.approx(10.0)
    assert aroast['temperature'] == pytest.approx(20.0)
    assert hints['ambient_temp_c'] == pytest.approx(20.0)
    assert hints['first_crack_ror_c_per_min'] == pytest.approx(10.0)


def test_batch_descriptor_environment_and_energy_fields_are_projected(sample_profile: ProfileData) -> None:
    aroast = json.loads(project_profile(sample_profile, MODIFIED).aroast_json)
    hints = json.loads(project_profile(sample_profile, MODIFIED).revision_json)

    assert aroast['batch_prefix'] == 'B'
    assert aroast['batch_number'] == 12
    assert aroast['batch_pos'] == 1
    assert aroast['machine'] == 'Sample Roaster'
    assert aroast['setup'] == '12 kg drum'
    assert aroast['moisture'] == pytest.approx(2.1)
    assert aroast['density_roasted'] == pytest.approx(0.41)
    assert aroast['whole_color'] == pytest.approx(65.0)
    assert aroast['ground_color'] == pytest.approx(70.0)
    assert aroast['color_system'] == 'Agtron'
    assert aroast['temperature'] == pytest.approx(22.5)
    assert aroast['humidity'] == pytest.approx(45.0)
    assert aroast['pressure'] == pytest.approx(1013.2)
    assert aroast['BTU_batch'] == pytest.approx(15000.0)
    assert aroast['CO2_batch'] == pytest.approx(2.5)
    assert aroast['CO2_roast'] == pytest.approx(1.5)
    assert hints['green_moisture_percent'] == pytest.approx(11.5)
    assert hints['roasted_moisture_percent'] == pytest.approx(2.1)
    assert hints['green_density'] == pytest.approx(0.72)
    assert hints['roasted_density'] == pytest.approx(0.41)
    assert hints['batch_position'] == 1
    assert hints['machine_setup'] == '12 kg drum'


def test_unknown_nonfinite_unsafe_and_oversized_values_are_omitted() -> None:
    profile = minimal_profile()
    profile['operator'] = 'x\x00y'
    profile['ambientTemp'] = math.inf
    profile['roastbatchnr'] = JS_SAFE_INTEGER_MAX + 1
    profile['end_weight_est'] = JS_SAFE_INTEGER_MAX + 1
    profile['roastUUID'] = 'not-a-uuid'

    projected = project_profile(profile, MODIFIED)
    aroast = json.loads(projected.aroast_json)
    hints = json.loads(projected.revision_json)

    assert 'roast_id' not in aroast
    assert 'end_weight_est' not in aroast
    assert 'ambient_temp_c' not in hints
    assert 'operator' not in hints
    assert 'batch_number' not in aroast
    assert b'Infinity' not in projected.aroast_json
    assert b'Infinity' not in projected.revision_json


def test_projection_omits_free_form_paths_credentials_plus_and_unknown_keys(sample_profile: ProfileData) -> None:
    projected = project_profile(sample_profile, MODIFIED)
    aroast = json.loads(projected.aroast_json)
    hints = json.loads(projected.revision_json)

    for forbidden in (
        'plus_store',
        'plus_coffee',
        'scheduleID',
        'scheduleDate',
        'signature',
        'roastingnotes',
        'cuppingnotes',
        'unknown_field',
    ):
        assert forbidden not in aroast
        assert forbidden not in hints
        assert forbidden.encode('utf-8') not in projected.aroast_json
        assert forbidden.encode('utf-8') not in projected.revision_json


def test_revision_metadata_drops_low_priority_descriptors_in_fixed_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(metadata_module, 'MAX_METADATA_BYTES', 650)
    profile = minimal_profile()
    profile['title'] = 'T' * 240
    profile['operator'] = 'O' * 240
    profile['roastertype'] = 'M' * 240
    profile['machinesetup'] = 'S' * 240
    profile['beans'] = 'B' * 240

    projected = project_profile(profile, MODIFIED)
    hints = json.loads(projected.revision_json)

    assert len(projected.revision_json) <= 650
    assert hints['roast_id'] == '11111111111141118111111111111111'
    assert hints['modified_at'] == '2026-08-01T12:34:56.123456+00:00'
    assert hints['roast_at'] == '2026-08-01T10:00:00+00:00'
    assert 'beans' not in hints
    assert 'machine_setup' not in hints
    assert 'machine' in hints
    assert 'operator' in hints
    assert 'title' in hints


@pytest.mark.parametrize(
    ('value', 'expected'),
    ((False, False), (True, True), (0, False), (1, True)),
)
def test_end_weight_est_is_normalized_to_exact_json_boolean(
    value: bool | int,
    expected: bool,
) -> None:
    profile = minimal_profile()
    profile['end_weight_est'] = value

    aroast = _aroast(profile)

    assert aroast['end_weight_est'] is expected


@pytest.mark.parametrize('value', (-1, 2, 0.0, 1.0, '1', None))
def test_invalid_end_weight_est_is_omitted(value: object) -> None:
    profile = minimal_profile()
    profile['end_weight_est'] = cast(int, value)

    aroast = _aroast(profile)

    assert 'end_weight_est' not in aroast


def test_exact_aroast_maxima_are_accepted() -> None:
    profile = minimal_profile()
    profile.update(
        {
            'title': 'L' * 255,
            'weight': [65_534.0, 65_534.0, 'kg'],
            'defects_weight': 65_534.0,
            'roastbatchprefix': 'B' * 50,
            'roastbatchnr': 65_534,
            'roastbatchpos': 255,
            'roastertype': 'M' * 50,
            'machinesetup': 'S' * 50,
            'moisture_roasted': 100.0,
            'density_roasted': [1_000.0, 'g', 1, 'l'],
            'whole_color': 255.0,
            'ground_color': 255.0,
            'color_system': 'C' * 25,
            'ambientTemp': 1_000.0,
            'ambient_humidity': 100.0,
            'ambient_pressure': 1_200.0,
            'computed': {
                'CHARGE_time': 86_400.0,
                'CHARGE_ET': 1_000.0,
                'CHARGE_BT': 1_000.0,
                'TP_time': 86_400.0,
                'TP_BT': 1_000.0,
                'DRY_time': 86_400.0,
                'DRY_BT': 1_000.0,
                'FCs_time': 86_400.0,
                'FCs_BT': 1_000.0,
                'FCe_time': 86_400.0,
                'FCe_BT': 1_000.0,
                'DROP_time': 86_400.0,
                'DROP_ET': 1_000.0,
                'DROP_BT': 1_000.0,
                'fcs_ror': 1_000.0,
                'finishphasetime': 86_400.0,
                'totaltime': 86_400.0,
                'BTU_batch': 1_000_000_000.0,
                'BTU_roast': 1_000_000_000.0,
                'BTU_preheat': 1_000_000_000.0,
                'BTU_bbp': 1_000_000_000.0,
                'BTU_cooling': 1_000_000_000.0,
                'BTU_ELEC': 1_000_000_000.0,
                'BTU_LPG': 1_000_000_000.0,
                'BTU_NG': 1_000_000_000.0,
                'CO2_batch': 1_000_000_000_000.0,
                'CO2_roast': 1_000_000_000_000.0,
                'CO2_preheat': 1_000_000_000_000.0,
                'CO2_bbp': 1_000_000_000_000.0,
                'CO2_cooling': 1_000_000_000_000.0,
            },
        }
    )

    aroast = _aroast(profile)

    assert aroast['label'] == 'L' * 255
    assert aroast['batch_prefix'] == 'B' * 50
    assert aroast['machine'] == 'M' * 50
    assert aroast['setup'] == 'S' * 50
    for field, (_integer_minimum, integer_maximum) in _PINNED_INTEGER_RANGES.items():
        assert aroast[field] == integer_maximum
    for field, (_float_minimum, float_maximum) in _PINNED_FLOAT_RANGES.items():
        assert aroast[field] == pytest.approx(float_maximum)


def test_aroast_max_plus_one_is_omitted_and_strings_are_truncated() -> None:
    profile = minimal_profile()
    profile.update(
        {
            'title': 'L' * 256,
            'weight': [65_535.0, 65_535.0, 'kg'],
            'defects_weight': 65_535.0,
            'roastbatchprefix': 'B' * 51,
            'roastbatchnr': 65_535,
            'roastbatchpos': 256,
            'roastertype': 'M' * 51,
            'machinesetup': 'S' * 51,
            'moisture_roasted': 101.0,
            'density_roasted': [1_001.0, 'g', 1, 'l'],
            'whole_color': 256.0,
            'ground_color': 256.0,
            'color_system': 'C' * 26,
            'ambientTemp': 1_001.0,
            'ambient_humidity': 101.0,
            'ambient_pressure': 1_201.0,
            'computed': {
                'CHARGE_time': 86_401.0,
                'CHARGE_ET': 1_001.0,
                'CHARGE_BT': 1_001.0,
                'TP_time': 86_401.0,
                'TP_BT': 1_001.0,
                'DRY_time': 86_401.0,
                'DRY_BT': 1_001.0,
                'FCs_time': 86_401.0,
                'FCs_BT': 1_001.0,
                'FCe_time': 86_401.0,
                'FCe_BT': 1_001.0,
                'DROP_time': 86_401.0,
                'DROP_ET': 1_001.0,
                'DROP_BT': 1_001.0,
                'fcs_ror': 1_001.0,
                'finishphasetime': 86_401.0,
                'totaltime': 86_400.0,
                'BTU_batch': 1_000_000_001.0,
                'BTU_roast': 1_000_000_001.0,
                'BTU_preheat': 1_000_000_001.0,
                'BTU_bbp': 1_000_000_001.0,
                'BTU_cooling': 1_000_000_001.0,
                'BTU_ELEC': 1_000_000_001.0,
                'BTU_LPG': 1_000_000_001.0,
                'BTU_NG': 1_000_000_001.0,
                'CO2_batch': 1_000_000_001_000.0,
                'CO2_roast': 1_000_000_001_000.0,
                'CO2_preheat': 1_000_000_001_000.0,
                'CO2_bbp': 1_000_000_001_000.0,
                'CO2_cooling': 1_000_000_001_000.0,
            },
        }
    )

    aroast = _aroast(profile)

    assert aroast['label'] == 'L' * 255
    assert aroast['batch_prefix'] == 'B' * 50
    assert aroast['machine'] == 'M' * 50
    assert aroast['setup'] == 'S' * 50
    assert aroast['color_system'] == 'C' * 25
    for field in _PINNED_INTEGER_RANGES.keys() | _PINNED_FLOAT_RANGES.keys():
        assert field not in aroast


def test_exact_aroast_minima_are_accepted_and_below_minima_are_omitted() -> None:
    profile = minimal_profile()
    profile.update(
        {
            'weight': [0.0, 0.0, 'kg'],
            'defects_weight': 0.0,
            'roastbatchnr': 0,
            'roastbatchpos': 0,
            'moisture_roasted': 0.0,
            'density_roasted': [0.0, 'g', 1, 'l'],
            'whole_color': 0.0,
            'ground_color': 0.0,
            'ambientTemp': -1_000.0,
            'ambient_humidity': 0.0,
            'ambient_pressure': 800.0,
            'computed': {
                'CHARGE_time': 0.0,
                'CHARGE_ET': -1_000.0,
                'CHARGE_BT': -1_000.0,
                'TP_time': 0.0,
                'TP_BT': -1_000.0,
                'DRY_time': 0.0,
                'DRY_BT': -1_000.0,
                'FCs_time': 0.0,
                'FCs_BT': -1_000.0,
                'FCe_time': 0.0,
                'FCe_BT': -1_000.0,
                'DROP_time': 0.0,
                'DROP_ET': -1_000.0,
                'DROP_BT': -1_000.0,
                'fcs_ror': -1_000.0,
                'finishphasetime': 0.0,
                'totaltime': 1.0,
                'BTU_batch': 0.0,
                'CO2_batch': 0.0,
            },
        }
    )

    aroast = _aroast(profile)

    for field, (integer_minimum, _integer_maximum) in _PINNED_INTEGER_RANGES.items():
        assert aroast[field] == integer_minimum
    for field, (float_minimum, _float_maximum) in _PINNED_FLOAT_RANGES.items():
        if field in aroast:
            assert aroast[field] == pytest.approx(float_minimum)

    profile.update(
        {
            'weight': [-0.001, -0.001, 'kg'],
            'defects_weight': -0.001,
            'roastbatchnr': -1,
            'roastbatchpos': -1,
            'moisture_roasted': -0.001,
            'density_roasted': [-0.001, 'g', 1, 'l'],
            'whole_color': -0.001,
            'ground_color': -0.001,
            'ambientTemp': -1_000.001,
            'ambient_humidity': -0.001,
            'ambient_pressure': 799.999,
            'computed': {
                'CHARGE_time': -0.001,
                'CHARGE_ET': -1_000.001,
                'CHARGE_BT': -1_000.001,
                'TP_time': -0.001,
                'TP_BT': -1_000.001,
                'DRY_time': -0.001,
                'DRY_BT': -1_000.001,
                'FCs_time': -0.001,
                'FCs_BT': -1_000.001,
                'FCe_time': -0.001,
                'FCe_BT': -1_000.001,
                'DROP_time': -0.001,
                'DROP_ET': -1_000.001,
                'DROP_BT': -1_000.001,
                'fcs_ror': -1_000.001,
                'finishphasetime': -0.001,
                'totaltime': 1.0,
                'BTU_batch': -0.001,
                'CO2_batch': -0.001,
            },
        }
    )

    aroast = _aroast(profile)
    for field in _PINNED_INTEGER_RANGES.keys() | _PINNED_FLOAT_RANGES.keys():
        if field not in {'BTU_ELEC', 'BTU_LPG', 'BTU_NG', 'BTU_roast', 'BTU_preheat', 'BTU_bbp', 'BTU_cooling', 'CO2_roast', 'CO2_preheat', 'CO2_bbp', 'CO2_cooling'}:
            assert field not in aroast


def test_pinned_reference_and_blend_constraints_are_exact() -> None:
    payload = _aroast(minimal_profile())
    reference = {'hr_id': 'h' * 100, 'label': 'L' * 255}
    payload['location'] = reference
    payload['coffee'] = reference
    payload['blend'] = {
        'label': 'B' * 255,
        'ingredients': [
            {
                'coffee': reference,
                'ratio': 1.0001,
                'ratio_num': 10_000,
                'ratio_denom': 10_000,
            }
            for _index in range(32)
        ],
    }
    _validate_pinned_aroast(payload)

    invalid_payload = copy.deepcopy(payload)
    invalid_location = cast(dict[str, object], invalid_payload['location'])
    invalid_location['hr_id'] = 'h' * 101
    with pytest.raises(AssertionError):
        _validate_pinned_aroast(invalid_payload)

    invalid_payload = copy.deepcopy(payload)
    invalid_location = cast(dict[str, object], invalid_payload['location'])
    invalid_location['label'] = 'L' * 256
    with pytest.raises(AssertionError):
        _validate_pinned_aroast(invalid_payload)

    invalid_payload = copy.deepcopy(payload)
    invalid_blend = cast(dict[str, object], invalid_payload['blend'])
    invalid_blend['label'] = 'B' * 256
    with pytest.raises(AssertionError):
        _validate_pinned_aroast(invalid_payload)

    invalid_payload = copy.deepcopy(payload)
    invalid_blend = cast(dict[str, object], invalid_payload['blend'])
    invalid_ingredients = cast(list[object], invalid_blend['ingredients'])
    invalid_ingredients.append(invalid_ingredients[0])
    with pytest.raises(AssertionError):
        _validate_pinned_aroast(invalid_payload)

    invalid_payload = copy.deepcopy(payload)
    invalid_blend = cast(dict[str, object], invalid_payload['blend'])
    invalid_ingredients = cast(list[object], invalid_blend['ingredients'])
    invalid_ingredient = cast(dict[str, object], invalid_ingredients[0])
    invalid_ingredient['ratio'] = 1.0002
    with pytest.raises(AssertionError):
        _validate_pinned_aroast(invalid_payload)

    for ratio_field in ('ratio_num', 'ratio_denom'):
        invalid_payload = copy.deepcopy(payload)
        invalid_blend = cast(dict[str, object], invalid_payload['blend'])
        invalid_ingredients = cast(list[object], invalid_blend['ingredients'])
        invalid_ingredient = cast(dict[str, object], invalid_ingredients[0])
        invalid_ingredient[ratio_field] = 10_001
        with pytest.raises(AssertionError):
            _validate_pinned_aroast(invalid_payload)


@pytest.mark.parametrize('mode', (None, '', 'K', 0))
def test_unknown_temperature_mode_omits_all_temperatures_and_ror(mode: object) -> None:
    profile = minimal_profile()
    profile['mode'] = cast(str, mode)
    profile['ambientTemp'] = 20.0
    profile['computed'] = {
        'CHARGE_time': 0.0,
        'CHARGE_ET': 200.0,
        'CHARGE_BT': 190.0,
        'TP_time': 90.0,
        'TP_BT': 120.0,
        'DRY_time': 300.0,
        'DRY_BT': 160.0,
        'FCs_time': 480.0,
        'FCs_BT': 185.0,
        'FCe_time': 540.0,
        'FCe_BT': 195.0,
        'DROP_time': 600.0,
        'DROP_ET': 215.0,
        'DROP_BT': 205.0,
        'fcs_ror': 10.0,
    }

    projected = project_profile(profile, MODIFIED)
    aroast = cast(dict[str, object], json.loads(projected.aroast_json))
    hints = cast(dict[str, object], json.loads(projected.revision_json))
    _validate_pinned_aroast(aroast)

    for field in (
        'temperature',
        'charge_temp_ET',
        'charge_temp',
        'TP_temp',
        'DRY_temp',
        'FCs_temp',
        'FCe_temp',
        'drop_temp',
        'drop_temp_ET',
        'FCs_RoR',
    ):
        assert field not in aroast
    assert 'temperature_unit' not in hints
    assert 'ambient_temp_c' not in hints
    assert 'first_crack_ror_c_per_min' not in hints
    events = cast(dict[str, object], hints['events'])
    for event in events.values():
        assert isinstance(event, dict)
        event_fields = cast(dict[str, object], event)
        assert set(event_fields) == {'time_seconds'}


def test_huge_numeric_epoch_and_timezone_values_are_omitted_without_error() -> None:
    huge = 10**1_000
    profile = minimal_profile()
    profile['roastepoch'] = JS_SAFE_INTEGER_MAX
    profile['roasttzoffset'] = huge
    profile['ambientTemp'] = huge
    profile['ambient_pressure'] = huge
    profile['weight'] = [huge, huge, 'kg']
    profile['computed'] = {'CHARGE_time': huge, 'BTU_batch': huge, 'CO2_batch': huge}

    projected = project_profile(profile, MODIFIED)
    aroast = cast(dict[str, object], json.loads(projected.aroast_json))
    hints = cast(dict[str, object], json.loads(projected.revision_json))
    _validate_pinned_aroast(aroast)

    assert 'date' not in aroast
    assert 'amount' not in aroast
    assert 'temperature' not in aroast
    assert 'pressure' not in aroast
    assert 'charge_time' not in aroast
    assert 'BTU_batch' not in aroast
    assert 'CO2_batch' not in aroast
    assert 'roast_at' not in hints
    assert 'roast_timezone_offset_seconds' not in hints


@pytest.mark.parametrize('exception_type', (OverflowError, OSError, ValueError))
def test_invalid_optional_epoch_conversion_is_omitted(
    monkeypatch: pytest.MonkeyPatch,
    exception_type: type[Exception],
) -> None:
    class RaisingDateTime:
        @staticmethod
        def fromtimestamp(_value: int, *, tz: object) -> datetime:
            del tz
            raise exception_type

    monkeypatch.setattr(metadata_module, 'datetime', RaisingDateTime)

    aroast = _aroast(minimal_profile())

    assert 'date' not in aroast


@pytest.mark.parametrize('offset', (-64_800, 64_800))
def test_valid_extreme_timezone_offsets_are_preserved(offset: int) -> None:
    profile = minimal_profile()
    profile['roasttzoffset'] = offset

    hints = cast(dict[str, object], json.loads(project_profile(profile, MODIFIED).revision_json))

    assert hints['roast_timezone_offset_seconds'] == offset


@pytest.mark.parametrize('offset', (-64_801, 64_801, 10**1_000))
def test_invalid_timezone_offsets_are_omitted(offset: int) -> None:
    profile = minimal_profile()
    profile['roasttzoffset'] = offset

    hints = cast(dict[str, object], json.loads(project_profile(profile, MODIFIED).revision_json))

    assert 'roast_timezone_offset_seconds' not in hints


@pytest.mark.parametrize(
    ('unit', 'container'),
    (
        ('g', list),
        ('Kg', list),
        ('kg', tuple),
        ('lb', tuple),
        ('oz', list),
    ),
)
def test_documented_weight_lists_and_tuples_are_accepted(
    unit: str,
    container: type[list[object]] | type[tuple[object, ...]],
) -> None:
    raw_weight = container((1.0, 0.5, unit))
    profile = cast(ProfileData, {**minimal_profile(), 'weight': raw_weight})

    aroast = _aroast(profile)

    assert 'amount' in aroast
    assert 'end_weight' in aroast


def test_unknown_weight_unit_is_omitted() -> None:
    profile = minimal_profile()
    profile['weight'] = [1.0, 0.5, 'stone']

    aroast = _aroast(profile)

    assert 'amount' not in aroast
    assert 'end_weight' not in aroast


def test_valid_minimal_initial_create_has_required_date_and_amount() -> None:
    profile = cast(
        ProfileData,
        {
            'roastUUID': '11111111111141118111111111111111',
            'roastepoch': 1_785_578_400,
            'weight': (1.0, 0.8, 'kg'),
        },
    )

    aroast = _aroast(profile)

    assert {'roast_id', 'modified_at', 'date', 'amount'} <= aroast.keys()
    assert aroast['amount'] == pytest.approx(1.0)


def test_projection_does_not_mutate_input(sample_profile: ProfileData) -> None:
    original = copy.deepcopy(sample_profile)

    project_profile(sample_profile, MODIFIED)

    assert sample_profile == original
