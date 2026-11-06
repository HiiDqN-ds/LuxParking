"""Public business profile (footer, contact section, structured data).

Defaults are the public details published on luxuryparking.de (Impressum, footer, FAQ).
Override any value with a BIZ_<NAME> environment variable, e.g. BIZ_PHONE or BIZ_INSTAGRAM.
Set BIZ_LOGO=0 to fall back to a plain text brand name instead of the bundled logo.
"""
import os
import re

DEFAULTS = {
    "name": "Luxury Parking",
    "legal_name": "Luxury Parking LP GmbH",
    "street": "Sohlstättenstraße 58",
    "postal_code": "40880",
    "city": "Ratingen",
    "country": "DE",
    "airport": "Düsseldorf Airport (DUS)",
    "phone": "+49 151 55886444",
    "email": "info@luxuryparking.de",
    "maps_url": "https://maps.app.goo.gl/vNBXpdmGuSqMpBLb7",
    "instagram": "https://www.instagram.com/luxuryparkinglp",
    "facebook": "https://www.facebook.com/luxuryparkinglp",
    "website": "https://luxuryparking.de",
}


def business_profile() -> dict:
    profile = {key: os.environ.get(f"BIZ_{key.upper()}", default).strip() for key, default in DEFAULTS.items()}
    profile["phone_href"] = "tel:" + re.sub(r"[^\d+]", "", profile["phone"])
    profile["address_line"] = f'{profile["street"]}, {profile["postal_code"]} {profile["city"]}'
    profile["logo_enabled"] = os.environ.get("BIZ_LOGO", "1").strip().lower() not in {"0", "false", "no", ""}
    profile["same_as"] = [url for url in (profile["instagram"], profile["facebook"]) if url]
    return profile
