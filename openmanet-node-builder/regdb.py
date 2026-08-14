"""regdb.py - loads the Morse Micro HaLow regulatory table (channels.csv,
copied verbatim from openmanetd's own test fixture, which is itself
derived from the real morse-regdb channel database) so the node builder
can offer only valid country/bandwidth/channel combinations instead of
letting an operator type a combination the radio will silently misbehave
on.

Never hand-edit channels.csv - if it needs updating, re-copy it from
openmanetd/testfixtures/setup-wizard/channels.csv (or wherever the
authoritative morse-regdb source lives) so this stays a faithful mirror
of what the firmware itself enforces.
"""
import csv
import os

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "channels.csv")


class RegDB:
    def __init__(self, csv_path=CSV_PATH):
        # country_code -> country_name
        self.names = {}
        # country_code -> bandwidth_mhz(int) -> sorted list of channel ints
        self.table = {}
        self._load(csv_path)

    def _load(self, csv_path):
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                code = row["country_code"].strip()
                if not code:
                    continue
                bw = int(float(row["bw"]))
                chan = int(float(row["s1g_chan"]))
                name = row["country"].strip()
                self.names[code] = name
                self.table.setdefault(code, {}).setdefault(bw, set()).add(chan)

        # Freeze to sorted lists for stable menu ordering.
        for code, bwmap in self.table.items():
            for bw in bwmap:
                bwmap[bw] = sorted(bwmap[bw])

    def countries(self):
        """Sorted list of (code, name) tuples."""
        return sorted(self.names.items(), key=lambda kv: kv[1])

    def bandwidths(self, country_code):
        """Sorted list of valid bandwidth_mhz values for a country."""
        return sorted(self.table.get(country_code, {}).keys())

    def channels(self, country_code, bandwidth_mhz):
        """Sorted list of valid channel numbers for country+bandwidth."""
        return self.table.get(country_code, {}).get(bandwidth_mhz, [])

    def is_valid(self, country_code, bandwidth_mhz, channel):
        return channel in self.channels(country_code, bandwidth_mhz)

    def default_country(self):
        """AU is this project's standing default (see setup wizard /
        onboard-radio uci-defaults, which both default to Australia)."""
        return "AU" if "AU" in self.names else next(iter(sorted(self.names)), None)

    def narrowest_bandwidth(self, country_code):
        bws = self.bandwidths(country_code)
        return bws[0] if bws else None
