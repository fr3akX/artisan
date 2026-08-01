from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime
from typing import cast

import pytest

from artisanlib.atypes import ProfileData
from artisanlib.roastserver import metadata as metadata_module
from artisanlib.roastserver.contract import JS_SAFE_INTEGER_MAX, MAX_METADATA_BYTES
from artisanlib.roastserver.metadata import project_profile

MODIFIED = datetime(2026, 8, 1, 12, 34, 56, 123456, tzinfo=UTC)


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


def test_revision_hints_include_operator_units_and_events(sample_profile: ProfileData) -> None:
    projected = project_profile(sample_profile, MODIFIED)

    assert len(projected.revision_json) <= MAX_METADATA_BYTES
    hints = json.loads(projected.revision_json)
    assert hints['operator'] == 'Roaster One'
    assert hints['temperature_unit'] == 'C'
    assert hints['green_weight_kg'] == pytest.approx(1.0)
    assert hints['roasted_weight_kg'] == pytest.approx(0.845)
    assert hints['roast_at_utc'] == '2026-08-01T10:00:00+00:00'
    assert hints['roast_tz_offset_seconds'] == 7200
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
    monkeypatch.setattr(metadata_module, 'MAX_METADATA_BYTES', 850)
    profile = minimal_profile()
    profile['title'] = 'T' * 240
    profile['operator'] = 'O' * 240
    profile['roastertype'] = 'M' * 240
    profile['machinesetup'] = 'S' * 240
    profile['beans'] = 'B' * 240

    projected = project_profile(profile, MODIFIED)
    hints = json.loads(projected.revision_json)

    assert len(projected.revision_json) <= 850
    assert hints['roast_id'] == '11111111111141118111111111111111'
    assert hints['modified_at'] == '2026-08-01T12:34:56.123456+00:00'
    assert hints['roast_at_utc'] == '2026-08-01T10:00:00+00:00'
    assert 'beans' not in hints
    assert 'machine_setup' not in hints
    assert 'machine' in hints
    assert 'operator' in hints
    assert 'title' in hints
