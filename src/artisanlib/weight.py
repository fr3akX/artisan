#
# ABOUT
# Artisan weight conversion
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

from typing import Final

weight_units: Final[tuple[str, str, str, str]] = ('g', 'Kg', 'lb', 'oz')

# i/o: 0:g, 1:Kg, 2:lb (pound), 3:oz (ounce)
_WEIGHT_CONVERSION_TABLE: Final[tuple[tuple[float, float, float, float], ...]] = (
    (1.0, 0.001, 2.20462262185 / 1000, (2.20462262185 * 16) / 1000),
    (1000, 1.0, 2.20462262185, 2.20462262185 * 16),
    (1 / (2.20462262185 / 1000), 1 / 2.20462262185, 1.0, 16.0),
    (1000 / (2.20462262185 * 16), 1 / (2.20462262185 * 16), 1 / 16, 1.0),
)


def convertWeight(v: float, i: int, o: int) -> float:
    if 0 <= i < len(_WEIGHT_CONVERSION_TABLE) and 0 <= o < len(
        _WEIGHT_CONVERSION_TABLE
    ):
        return v * _WEIGHT_CONVERSION_TABLE[i][o]
    raise IndexError(f'index error in convertWeight({v},{i},{o})')
