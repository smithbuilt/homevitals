"""Sync Eufy smart scale body composition data to Garmin Connect and Strava."""

__version__ = "1.0.1"

# Public API for programmatic use
from homevitals.eufy_client import EufyClient, EufyMeasurement
from homevitals.garmin_auth import GarminAuth
from homevitals.strava_client import StravaClient
from homevitals.transform import GarminBodyComposition, transform

__all__ = [
    "GarminAuth",
    "EufyClient",
    "EufyMeasurement",
    "GarminBodyComposition",
    "StravaClient",
    "transform",
]
