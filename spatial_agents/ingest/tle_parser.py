"""
TLE Parser — Two-Line Element set decoder for satellite orbit tracking.

Parses TLE data from CelesTrak or Space-Track into structured records
for overpass prediction and satellite metadata.

Version History:
    0.1.0  2026-03-28  Initial TLE parser
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


@dataclass
class TLERecord:
    """Parsed Two-Line Element record."""
    norad_id: str
    name: str
    classification: str  # U=unclassified, C=classified, S=secret
    intl_designator: str
    epoch: datetime
    mean_motion: float        # revolutions per day
    eccentricity: float
    inclination_deg: float
    raan_deg: float           # right ascension of ascending node
    arg_perigee_deg: float
    mean_anomaly_deg: float
    bstar: float              # drag term
    line1: str
    line2: str

    @property
    def period_minutes(self) -> float:
        """Orbital period in minutes."""
        if self.mean_motion <= 0:
            return 0.0
        return 1440.0 / self.mean_motion

    @property
    def is_leo(self) -> bool:
        """Low Earth Orbit: period < 128 minutes."""
        return 0 < self.period_minutes < 128


class TLEParser:
    """
    Parse TLE data in standard 3-line format (name + line1 + line2).

    Usage:
        parser = TLEParser()
        records = parser.parse_text(tle_text)
        for r in records:
            print(r.name, r.norad_id, r.period_minutes)
    """

    def __init__(self) -> None:
        self._parse_count = 0
        self._error_count = 0

    @property
    def stats(self) -> dict[str, int]:
        return {"parsed": self._parse_count, "errors": self._error_count}

    def parse_text(self, text: str) -> list[TLERecord]:
        """Parse a block of TLE text into records."""
        lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
        records: list[TLERecord] = []

        i = 0
        while i < len(lines):
            # Detect whether this is 3-line (name + L1 + L2) or 2-line (L1 + L2)
            if i + 2 < len(lines) and lines[i + 1].startswith("1 ") and lines[i + 2].startswith("2 "):
                record = self._parse_three_line(lines[i], lines[i + 1], lines[i + 2])
                i += 3
            elif lines[i].startswith("1 ") and i + 1 < len(lines) and lines[i + 1].startswith("2 "):
                record = self._parse_three_line("UNKNOWN", lines[i], lines[i + 1])
                i += 2
            else:
                i += 1
                continue

            if record is not None:
                records.append(record)
                self._parse_count += 1

        return records

    def _parse_three_line(self, name: str, line1: str, line2: str) -> TLERecord | None:
        """Parse a single 3-line TLE entry."""
        try:
            # Line 1 fields
            norad_id = line1[2:7].strip()
            classification = line1[7:8]
            intl_designator = line1[9:17].strip()

            # Epoch: year (2-digit) + day of year (fractional)
            epoch_year = int(line1[18:20])
            epoch_day = float(line1[20:32])
            full_year = 2000 + epoch_year if epoch_year < 57 else 1900 + epoch_year
            epoch = datetime(full_year, 1, 1, tzinfo=timezone.utc)
            from datetime import timedelta
            epoch += timedelta(days=epoch_day - 1)

            # B* drag term (formatted as ±NNNNN±N → ±0.NNNNN × 10^±N)
            bstar = self._parse_tle_float(line1[53:61])

            # Line 2 fields
            inclination = float(line2[8:16])
            raan = float(line2[17:25])
            eccentricity = float(f"0.{line2[26:33].strip()}")
            arg_perigee = float(line2[34:42])
            mean_anomaly = float(line2[43:51])
            mean_motion = float(line2[52:63])

            return TLERecord(
                norad_id=norad_id,
                name=name.strip(),
                classification=classification,
                intl_designator=intl_designator,
                epoch=epoch,
                mean_motion=mean_motion,
                eccentricity=eccentricity,
                inclination_deg=inclination,
                raan_deg=raan,
                arg_perigee_deg=arg_perigee,
                mean_anomaly_deg=mean_anomaly,
                bstar=bstar,
                line1=line1,
                line2=line2,
            )
        except (ValueError, IndexError) as exc:
            self._error_count += 1
            logger.debug("TLE parse error: %s", exc)
            return None

    @staticmethod
    def _parse_tle_float(field: str) -> float:
        """Parse TLE implicit-decimal-point float like ' 12345-3' → 0.12345e-3."""
        field = field.strip()
        if not field or field == "00000-0":
            return 0.0
        # Insert decimal point
        match = re.match(r"([+-]?)(\d{5})([+-])(\d)", field)
        if match:
            sign, mantissa, exp_sign, exp = match.groups()
            return float(f"{sign}0.{mantissa}e{exp_sign}{exp}")
        return 0.0
