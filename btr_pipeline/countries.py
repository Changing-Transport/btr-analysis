"""
Canonical country naming.

The GIZ-SLOCAT Tracker database is the naming authority for this project:
every country name stored in `documents.country` must match the Tracker's
spelling, so BTR and NDC data join without reconciliation.

TRACKER_COUNTRY_NAMES maps raw names (as derived from BTR cache filenames)
to the Tracker convention. Applied at ingestion time; unknown names pass
through unchanged. Note the U+2019 apostrophe in the Lao entry — that is
the Tracker's spelling.
"""

TRACKER_COUNTRY_NAMES = {
    "Czechia": "Czech Republic",
    "Côte d Ivoire": "Côte D'Ivoire",
    "Holy See": "Vatican",
    "Lao PDR": "Lao People’s Democratic Republic",
    "Venezuela": "Venezuela, Bolivarian Republic of",
}

# EU member states (Tracker naming): used by the matching step to fall back
# to the joint EU NDC when the Tracker has no national entry.
EU_MEMBERS = {
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech Republic",
    "Denmark", "Estonia", "Finland", "France", "Germany", "Greece",
    "Hungary", "Ireland", "Italy", "Latvia", "Lithuania", "Luxembourg",
    "Malta", "Netherlands", "Poland", "Portugal", "Romania", "Slovakia",
    "Slovenia", "Spain", "Sweden",
}


def to_tracker_name(country: str) -> str:
    return TRACKER_COUNTRY_NAMES.get(country, country)
