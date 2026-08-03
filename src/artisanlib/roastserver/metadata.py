#
# ABOUT
# Artisan Roast Server deterministic metadata projection
#
# COPYRIGHT (C) 2010-2026 The Artisan team represented by
#   Marko Luther <marko.luther@gmx.net> (maintainer) and all contributors
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# MAINTAINER
# Marko Luther, 2026
#
# AUTHOR
# OpenAI, 2026

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
from typing import Final, cast
from uuid import UUID

from artisanlib.atypes import ComputedProfileInformation, ProfileData
from artisanlib.roastserver.contract import JS_SAFE_INTEGER_MAX, MAX_METADATA_BYTES
from artisanlib.util import convertWeight, fromFtoCstrict

_WEIGHT_UNIT_TO_INDEX: Final[dict[str, int]] = {
    'g': 0,
    'Kg': 1,
    'kg': 1,
    'lb': 2,
    'oz': 3,
}
_MAX_WEIGHT_KG: Final[float] = 65_534.0
_MAX_TIME_SECONDS: Final[float] = 86_400.0
_MAX_TEMPERATURE: Final[float] = 1_000.0
_MAX_ENERGY_OR_EMISSIONS: Final[float] = 1_000_000_000.0
_STRING_LIMITS: Final[dict[str, int]] = {
    'label': 255,
    'title': 255,
    'operator': 100,
    'machine': 50,
    'machine_setup': 50,
    'setup': 50,
    'batch_prefix': 50,
    'beans': 2048,
    'color_system': 25,
}
_AROAST_REMOVABLE_KEYS: Final[tuple[str, ...]] = (
    'beans',
    'setup',
    'machine',
    'operator',
    'label',
    'batch_pos',
    'batch_number',
    'batch_prefix',
    'defects_weight',
    'end_weight_est',
    'end_weight',
    'amount',
    'color_system',
    'ground_color',
    'whole_color',
    'density_roasted',
    'moisture',
    'pressure',
    'humidity',
    'temperature',
    'CO2_cooling',
    'CO2_bbp',
    'CO2_preheat',
    'CO2_roast',
    'CO2_batch',
    'BTU_ELEC',
    'BTU_NG',
    'BTU_LPG',
    'BTU_cooling',
    'BTU_bbp',
    'BTU_preheat',
    'BTU_roast',
    'BTU_batch',
    'DEV_ratio',
    'DEV_time',
    'FCs_RoR',
    'drop_temp_ET',
    'drop_temp',
    'drop_time',
    'FCe_temp',
    'FCe_time',
    'FCs_temp',
    'FCs_time',
    'DRY_temp',
    'DRY_time',
    'TP_temp',
    'TP_time',
    'charge_temp',
    'charge_temp_ET',
    'charge_time',
)
_REVISION_REMOVABLE_KEYS: Final[tuple[str, ...]] = (
    'beans',
    'machine_setup',
    'machine',
    'operator',
    'title',
    'batch_prefix',
    'batch_position',
    'batch_number',
    'defect_weight_kg',
    'roasted_weight_estimate_flag',
    'roasted_weight_kg',
    'green_weight_kg',
    'color_system',
    'ground_color',
    'whole_color',
    'roasted_density',
    'green_density',
    'roasted_moisture_percent',
    'green_moisture_percent',
    'ambient_pressure_hpa',
    'ambient_humidity_percent',
    'ambient_temp_c',
    'co2',
    'energy',
    'development_ratio_percent',
    'development_time_seconds',
    'first_crack_ror_c_per_min',
    'events',
)
_EVENT_MAP: Final[tuple[tuple[str, str, str | None, str], ...]] = (
    ('charge', 'CHARGE_time', 'CHARGE_ET', 'CHARGE_BT'),
    ('turning_point', 'TP_time', None, 'TP_BT'),
    ('dry_end', 'DRY_time', None, 'DRY_BT'),
    ('first_crack_start', 'FCs_time', None, 'FCs_BT'),
    ('first_crack_end', 'FCe_time', None, 'FCe_BT'),
    ('drop', 'DROP_time', 'DROP_ET', 'DROP_BT'),
)
_BTU_KEYS: Final[tuple[str, ...]] = (
    'BTU_batch',
    'BTU_roast',
    'BTU_preheat',
    'BTU_bbp',
    'BTU_cooling',
    'BTU_ELEC',
    'BTU_LPG',
    'BTU_NG',
)
_CO2_KEYS: Final[tuple[str, ...]] = (
    'CO2_batch',
    'CO2_roast',
    'CO2_preheat',
    'CO2_bbp',
    'CO2_cooling',
)


@dataclass(frozen=True, slots=True)
class ProjectedMetadata:
    aroast_json: bytes
    revision_json: bytes


def project_profile(profile: ProfileData, modified_at: datetime) -> ProjectedMetadata:
    modified_text = _aware_datetime_text(modified_at)
    roast_id = _uuid_hex(profile.get('roastUUID'))
    roast_at, roast_timezone_offset_seconds = _roast_datetime_parts(profile)
    mode = _temperature_unit(profile.get('mode'))
    computed = _computed_profile(profile)

    aroast: dict[str, object] = {}
    revision: dict[str, object] = {'modified_at': modified_text}

    if roast_id is not None:
        aroast['roast_id'] = roast_id
        revision['roast_id'] = roast_id
    aroast['modified_at'] = modified_text

    if roast_at is not None:
        aroast['date'] = roast_at
        revision['roast_at'] = roast_at
    if roast_timezone_offset_seconds is not None:
        revision['roast_timezone_offset_seconds'] = roast_timezone_offset_seconds
    if mode is not None:
        revision['temperature_unit'] = mode

    _add_text(aroast, 'label', profile.get('title'), limit_key='label')
    _add_text(revision, 'title', profile.get('title'), limit_key='title')
    _add_text(revision, 'beans', profile.get('beans'), limit_key='beans')
    _add_text(revision, 'operator', profile.get('operator'), limit_key='operator')
    _add_text(revision, 'machine', profile.get('roastertype'), limit_key='machine')
    _add_text(revision, 'machine_setup', profile.get('machinesetup'), limit_key='machine_setup')
    _add_text(aroast, 'machine', profile.get('roastertype'), limit_key='machine')
    _add_text(aroast, 'setup', profile.get('machinesetup'), limit_key='setup')

    batch_prefix = _bounded_text(profile.get('roastbatchprefix'), limit_key='batch_prefix')
    if batch_prefix is not None:
        aroast['batch_prefix'] = batch_prefix
        revision['batch_prefix'] = batch_prefix

    batch_number = _safe_int(profile.get('roastbatchnr'), minimum=0, maximum=65_534)
    if batch_number is not None:
        aroast['batch_number'] = batch_number
        revision['batch_number'] = batch_number

    batch_position = _safe_int(profile.get('roastbatchpos'), minimum=0, maximum=255)
    if batch_position is not None:
        aroast['batch_pos'] = batch_position
        revision['batch_position'] = batch_position

    green_weight_kg, roasted_weight_kg, weight_unit_index = _weight_pair_kg(profile)
    if green_weight_kg is not None:
        aroast['amount'] = green_weight_kg
        revision['green_weight_kg'] = green_weight_kg
    if roasted_weight_kg is not None:
        aroast['end_weight'] = roasted_weight_kg
        revision['roasted_weight_kg'] = roasted_weight_kg

    end_weight_est = _profile_bool(profile.get('end_weight_est'))
    if end_weight_est is not None:
        aroast['end_weight_est'] = end_weight_est
        revision['roasted_weight_estimate_flag'] = end_weight_est

    defect_weight_kg = _weight_to_kg(profile.get('defects_weight'), weight_unit_index)
    if defect_weight_kg is not None:
        aroast['defects_weight'] = defect_weight_kg
        revision['defect_weight_kg'] = defect_weight_kg

    moisture_roasted = _bounded_float(profile.get('moisture_roasted'), minimum=0.0, maximum=100.0, digits=3)
    if moisture_roasted is not None:
        aroast['moisture'] = moisture_roasted
        revision['roasted_moisture_percent'] = moisture_roasted

    moisture_greens = _bounded_float(profile.get('moisture_greens'), minimum=0.0, maximum=100.0, digits=3)
    if moisture_greens is not None:
        revision['green_moisture_percent'] = moisture_greens

    density_green = _density_value(profile.get('density'))
    if density_green is not None:
        revision['green_density'] = density_green

    density_roasted = _density_value(profile.get('density_roasted'))
    if density_roasted is not None:
        aroast['density_roasted'] = density_roasted
        revision['roasted_density'] = density_roasted

    whole_color = _bounded_float(profile.get('whole_color'), minimum=0.0, maximum=255.0, digits=3)
    if whole_color is not None:
        aroast['whole_color'] = whole_color
        revision['whole_color'] = whole_color

    ground_color = _bounded_float(profile.get('ground_color'), minimum=0.0, maximum=255.0, digits=3)
    if ground_color is not None:
        aroast['ground_color'] = ground_color
        revision['ground_color'] = ground_color

    color_system = _bounded_text(profile.get('color_system'), limit_key='color_system')
    if color_system is not None:
        aroast['color_system'] = color_system
        revision['color_system'] = color_system

    ambient_temp_c = _temperature_c(profile.get('ambientTemp'), mode)
    if ambient_temp_c is not None:
        aroast['temperature'] = ambient_temp_c
        revision['ambient_temp_c'] = ambient_temp_c

    ambient_humidity = _bounded_float(profile.get('ambient_humidity'), minimum=0.0, maximum=100.0, digits=3)
    if ambient_humidity is not None:
        aroast['humidity'] = ambient_humidity
        revision['ambient_humidity_percent'] = ambient_humidity

    ambient_pressure = _bounded_float(
        profile.get('ambient_pressure'), minimum=800.0, maximum=1_200.0, digits=3
    )
    if ambient_pressure is not None:
        aroast['pressure'] = ambient_pressure
        revision['ambient_pressure_hpa'] = ambient_pressure

    _project_events(aroast, revision, computed, mode)

    first_crack_ror = _ror_c(computed.get('fcs_ror'), mode)
    if first_crack_ror is not None:
        aroast['FCs_RoR'] = first_crack_ror
        revision['first_crack_ror_c_per_min'] = first_crack_ror

    development_time = _bounded_float(
        computed.get('finishphasetime'),
        minimum=0.0,
        maximum=_MAX_TIME_SECONDS,
        digits=3,
    )
    if development_time is not None:
        aroast['DEV_time'] = development_time
        revision['development_time_seconds'] = development_time
        total_time = _finite_float(computed.get('totaltime'))
        if total_time is not None and total_time > 0:
            development_ratio = _bounded_float(
                development_time / total_time * 100.0,
                minimum=0.0,
                maximum=100.0,
                digits=3,
            )
            if development_ratio is not None:
                aroast['DEV_ratio'] = development_ratio
                revision['development_ratio_percent'] = development_ratio

    energy = _energy_projection(computed)
    if energy:
        revision['energy'] = energy
        aroast.update(energy)

    co2 = _co2_projection(computed)
    if co2:
        revision['co2'] = co2
        aroast.update(co2)

    aroast_json = _fit_json_object(aroast, _AROAST_REMOVABLE_KEYS)
    revision_json = _fit_json_object(revision, _REVISION_REMOVABLE_KEYS)
    return ProjectedMetadata(aroast_json=aroast_json, revision_json=revision_json)


def _computed_profile(profile: ProfileData) -> ComputedProfileInformation:
    computed = profile.get('computed')
    if isinstance(computed, dict):
        return computed
    return ComputedProfileInformation()


def _aware_datetime_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError('modified must be timezone-aware')
    return value.astimezone(UTC).isoformat()


def _uuid_hex(value: object) -> str | None:
    if not isinstance(value, str) or value == '' or '\x00' in value:
        return None
    try:
        return UUID(value).hex
    except (ValueError, AttributeError, TypeError):
        return None


def _temperature_unit(value: object) -> str | None:
    if isinstance(value, str) and value in {'C', 'F'}:
        return value
    return None


def _roast_datetime_parts(profile: ProfileData) -> tuple[str | None, int | None]:
    epoch = _safe_int(profile.get('roastepoch'))
    if epoch is None:
        return None, None
    try:
        roast_at = datetime.fromtimestamp(epoch, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None, None
    offset = _safe_int(profile.get('roasttzoffset'), minimum=-18 * 3600, maximum=18 * 3600)
    return roast_at, offset


def _weight_pair_kg(profile: ProfileData) -> tuple[float | None, float | None, int | None]:
    weight = profile.get('weight')
    if not isinstance(weight, list | tuple) or len(weight) < 3:
        return None, None, None
    unit_index = _weight_unit_index(weight[2])
    if unit_index is None:
        return None, None, None
    green = _weight_to_kg(weight[0], unit_index)
    roasted = _weight_to_kg(weight[1], unit_index)
    return green, roasted, unit_index


def _weight_unit_index(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    return _WEIGHT_UNIT_TO_INDEX.get(value)


def _weight_to_kg(value: object, unit_index: int | None) -> float | None:
    raw = _finite_float(value)
    if raw is None or unit_index is None:
        return None
    return _bounded_float(
        convertWeight(raw, unit_index, 1), minimum=0.0, maximum=_MAX_WEIGHT_KG, digits=6
    )


def _density_value(value: object) -> float | None:
    if not isinstance(value, list | tuple) or not value:
        return None
    return _bounded_float(cast(object, value[0]), minimum=0.0, maximum=1000.0, digits=6)


def _temperature_c(value: object, mode: str | None) -> float | None:
    if mode not in {'C', 'F'}:
        return None
    numeric = _finite_float(value)
    if numeric is None or numeric == -1:
        return None
    if mode == 'F':
        numeric = fromFtoCstrict(numeric)
    return _bounded_float(
        numeric, minimum=-_MAX_TEMPERATURE, maximum=_MAX_TEMPERATURE, digits=6
    )


def _ror_c(value: object, mode: str | None) -> float | None:
    if mode not in {'C', 'F'}:
        return None
    numeric = _finite_float(value)
    if numeric is None or numeric == -1:
        return None
    if mode == 'F':
        numeric = numeric * (5.0 / 9.0)
    return _bounded_float(
        numeric, minimum=-_MAX_TEMPERATURE, maximum=_MAX_TEMPERATURE, digits=6
    )


def _bounded_float(
    value: object,
    *,
    minimum: float,
    maximum: float,
    digits: int,
) -> float | None:
    numeric = _finite_float(value)
    if numeric is None or numeric < minimum or numeric > maximum:
        return None
    return round(numeric, digits)


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, int) and abs(value) > JS_SAFE_INTEGER_MAX:
        return None
    try:
        numeric = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or abs(numeric) > JS_SAFE_INTEGER_MAX:
        return None
    return numeric


def _profile_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


def _safe_int(value: object, minimum: int | None = None, maximum: int | None = None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if abs(value) > JS_SAFE_INTEGER_MAX:
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return value


def _bounded_text(value: object, *, limit_key: str) -> str | None:
    if not isinstance(value, str) or value == '' or '\x00' in value:
        return None
    bounded = value[: _STRING_LIMITS[limit_key]]
    try:
        bounded.encode('utf-8')
    except UnicodeEncodeError:
        return None
    return bounded


def _add_text(target: dict[str, object], key: str, value: object, *, limit_key: str) -> None:
    bounded = _bounded_text(value, limit_key=limit_key)
    if bounded is not None:
        target[key] = bounded


def _project_events(
    aroast: dict[str, object],
    revision: dict[str, object],
    computed: ComputedProfileInformation,
    mode: str | None,
) -> None:
    events: dict[str, object] = {}
    aroast_field_names = {
        'charge': ('charge_time', 'charge_temp_ET', 'charge_temp'),
        'turning_point': ('TP_time', None, 'TP_temp'),
        'dry_end': ('DRY_time', None, 'DRY_temp'),
        'first_crack_start': ('FCs_time', None, 'FCs_temp'),
        'first_crack_end': ('FCe_time', None, 'FCe_temp'),
        'drop': ('drop_time', 'drop_temp_ET', 'drop_temp'),
    }
    for event_name, time_key, et_key, bt_key in _EVENT_MAP:
        event: dict[str, object] = {}
        time_seconds = _bounded_float(
            computed.get(time_key),
            minimum=0.0,
            maximum=_MAX_TIME_SECONDS,
            digits=6,
        )
        if time_seconds is not None:
            event['time_seconds'] = time_seconds
        if et_key is not None:
            environment_temp_c = _temperature_c(computed.get(et_key), mode)
            if environment_temp_c is not None:
                event['environment_temp_c'] = environment_temp_c
        bean_temp_c = _temperature_c(computed.get(bt_key), mode)
        if bean_temp_c is not None:
            event['bean_temp_c'] = bean_temp_c
        if not event:
            continue
        events[event_name] = event
        aroast_time_key, aroast_et_key, aroast_bt_key = aroast_field_names[event_name]
        time_seconds = cast(float | None, event.get('time_seconds'))
        if time_seconds is not None:
            aroast[aroast_time_key] = time_seconds
        if aroast_et_key is not None:
            environment_temp_c = cast(float | None, event.get('environment_temp_c'))
            if environment_temp_c is not None:
                aroast[aroast_et_key] = environment_temp_c
        if aroast_bt_key is not None:
            bean_temp_c = cast(float | None, event.get('bean_temp_c'))
            if bean_temp_c is not None:
                aroast[aroast_bt_key] = bean_temp_c
    if events:
        revision['events'] = events


def _energy_projection(computed: ComputedProfileInformation) -> dict[str, object]:
    projected: dict[str, object] = {}
    for key in _BTU_KEYS:
        value = _bounded_float(
            computed.get(key), minimum=0.0, maximum=_MAX_ENERGY_OR_EMISSIONS, digits=3
        )
        if value is not None:
            projected[key] = value
    return projected


def _co2_projection(computed: ComputedProfileInformation) -> dict[str, object]:
    projected: dict[str, object] = {}
    for key in _CO2_KEYS:
        raw_value = _finite_float(computed.get(key))
        if raw_value is None:
            continue
        value = _bounded_float(
            raw_value / 1000.0,
            minimum=0.0,
            maximum=_MAX_ENERGY_OR_EMISSIONS,
            digits=6,
        )
        if value is not None:
            projected[key] = value
    return projected


def _fit_json_object(payload: dict[str, object], removable_keys: tuple[str, ...]) -> bytes:
    mutable = dict(payload)
    encoded = _json_bytes(mutable)
    if len(encoded) <= MAX_METADATA_BYTES:
        return encoded
    for key in removable_keys:
        if key not in mutable:
            continue
        mutable.pop(key)
        encoded = _json_bytes(mutable)
        if len(encoded) <= MAX_METADATA_BYTES:
            return encoded
    raise ValueError('metadata exceeds maximum size')


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf-8')


__all__ = ['ProjectedMetadata', 'project_profile']
