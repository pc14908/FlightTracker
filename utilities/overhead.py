from fr24sdk.client import Client
from fr24sdk.exceptions import Fr24SdkError, ApiError
from threading import Thread, Lock
from time import sleep, time
from datetime import datetime
import math
import os
import requests

try:
    # Attempt to load config data
    from config import MIN_ALTITUDE

except (ModuleNotFoundError, NameError, ImportError):
    # If there's no config data
    MIN_ALTITUDE = 0  # feet

RATE_LIMIT_DELAY = 1
MAX_FLIGHT_LOOKUP = 5
MAX_ALTITUDE = 10000  # feet
EARTH_RADIUS_KM = 6371
BLANK_FIELDS = ["", "N/A", "NONE"]

def zone_dict_to_bounds_string(zone):
    """Convert zone dict (tl_y, tl_x, br_y, br_x) to bounds string (north,south,west,east)."""
    return f"{zone['tl_y']},{zone['br_y']},{zone['tl_x']},{zone['br_x']}"


def get_aircraft_name(icao24, username, password):
    url = f"https://opensky-network.org/api/metadata/aircraft/icao/{icao24.lower()}"
    response = requests.get(url, auth=(username, password))

    if response.status_code == 200:
        data = response.json()
        mfr = data.get("manufacturerName","")
        model = data.get("model","")
        parts = [p for p in [mfr, model] if p]
        return " ".join(parts) if parts else icao24 # fallback to ICAO24 if no metadata available
    else:
        print(f"Error {response.status_code}: {response.text}")
        return icao24 # fallback to returning the original ICAO24 code if metadata lookup fails


try:
    # Attempt to load config data
    from config import ZONE_HOME, LOCATION_HOME

    ZONE_DEFAULT = ZONE_HOME
    LOCATION_DEFAULT = LOCATION_HOME

except (ModuleNotFoundError, NameError, ImportError):
    # If there's no config data
    ZONE_DEFAULT = {"tl_y": 62.61, "tl_x": -13.07, "br_y": 49.71, "br_x": 3.46}
    LOCATION_DEFAULT = [51.509865, -0.118092, EARTH_RADIUS_KM]


def distance_from_flight_to_home(flight, home=LOCATION_DEFAULT):
    # This works if flight is an object FROM the flights list, but NOT if it is the raw flights list
    def polar_to_cartesian(lat, long, alt):
        DEG2RAD = math.pi / 180
        return [
            alt * math.cos(DEG2RAD * lat) * math.sin(DEG2RAD * long),
            alt * math.sin(DEG2RAD * lat),
            alt * math.cos(DEG2RAD * lat) * math.cos(DEG2RAD * long),
        ]

    def feet_to_meters_plus_earth(altitude_ft):
        altitude_km = 0.0003048 * altitude_ft
        return altitude_km + EARTH_RADIUS_KM

    try:
        (x0, y0, z0) = polar_to_cartesian(
            flight.lat,
            flight.lon,
            feet_to_meters_plus_earth(flight.alt),
        )

        (x1, y1, z1) = polar_to_cartesian(*home)

        dist = math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2 + (z1 - z0) ** 2)

        return dist

    except AttributeError:
        # on error say it's far away
        return 1e6


class Overhead:
    def __init__(self):
        #api_token = os.environ.get("FR24_API_TOKEN") # change for passing token directly
        self._api = Client(api_token="API TOKEN HERE")
        self._lock = Lock()
        self._data = []
        self._new_data = False
        self._processing = False
        self._last_api_call = 0  # Timestamp of last API call for rate limiting

    def _is_active_hour(self):
        """Check if current time is within active hours (12:00 to 22:00)."""
        current_hour = datetime.now().hour
        return 12 <= current_hour < 22
    
    def _should_fetch_data(self):
        """Check if enough time has passed since last API call (15 second rate limit)."""
        return (time() - self._last_api_call) >= 15

    def grab_data(self):
        Thread(target=self._grab_data).start()

    def _grab_data(self):
        # Check if we're outside active hours
        if not self._is_active_hour():
            # Outside active hours, don't fetch data
            with self._lock:
                self._new_data = False
                self._processing = False
            return
        
        # Check rate limit
        if not self._should_fetch_data():
            # Rate limit not met, don't fetch
            with self._lock:
                self._new_data = False
                self._processing = False
            return
        
        # Mark data as old
        with self._lock:
            self._new_data = False
            self._processing = True

        data = []

        # Grab flight details
        try:
            # Update last API call time
            self._last_api_call = time()
            
            # Convert zone dict to bounds string
            bounds_str = zone_dict_to_bounds_string(ZONE_DEFAULT)
            
            # Fetch live flight positions
            flights_response = self._api.live.flight_positions.get_full(bounds=bounds_str)
            flights = flights_response.data if flights_response.data else []

            # Sort flights by closest first
            flights = [
                f
                for f in flights
                if f.alt < MAX_ALTITUDE and f.alt > MIN_ALTITUDE
            ]
            flights = sorted(flights, key=lambda f: distance_from_flight_to_home(f))

            # Rate limit protection
            sleep(RATE_LIMIT_DELAY)

            for flight in flights[:MAX_FLIGHT_LOOKUP]:
                # Get plane information using icao24 identifier, with fallback to icao24 if lookup fails or returns blank
                plane = get_aircraft_name(flight.hex,"username","password") if flight.hex else ""
                plane = plane if not (plane.upper() in BLANK_FIELDS) else ""

                # Extract and clean origin airport
                origin = (
                    flight.orig_iata
                    if flight.orig_iata and not (flight.orig_iata.upper() in BLANK_FIELDS)
                    else ""
                )

                # Extract and clean destination airport
                destination = (
                    flight.dest_iata
                    if flight.dest_iata and not (flight.dest_iata.upper() in BLANK_FIELDS)
                    else ""
                )

                # Extract and clean callsign
                callsign = (
                    flight.callsign
                    if flight.callsign and not (flight.callsign.upper() in BLANK_FIELDS)
                    else ""
                )

                data.append(
                    {
                        "plane": plane,
                        "origin": origin,
                        "destination": destination,
                        "vertical_speed": flight.vspeed,
                        "altitude": flight.alt,
                        "callsign": callsign,
                    }
                )

            with self._lock:
                self._new_data = True
                self._processing = False
                self._data = data

        except (Fr24SdkError, ApiError) as e:
            # Log error but don't crash
            print(f"Error fetching flight data from FR24 API: {e}")
            with self._lock:
                self._new_data = False
                self._processing = False
        except Exception as e:
            # Catch any other unexpected errors
            print(f"Unexpected error in _grab_data: {e}")
            with self._lock:
                self._new_data = False
                self._processing = False

    @property
    def new_data(self):
        with self._lock:
            return self._new_data

    @property
    def processing(self):
        with self._lock:
            return self._processing

    @property
    def data(self):
        with self._lock:
            self._new_data = False
            return self._data

    @property
    def data_is_empty(self):
        return len(self._data) == 0


# Main function
if __name__ == "__main__":

    o = Overhead()
    o.grab_data()
    while not o.new_data:
        print("processing...")
        sleep(1)

    print(o.data)
