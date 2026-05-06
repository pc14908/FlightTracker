from fr24sdk.client import Client
from fr24sdk.exceptions import Fr24SdkError, ApiError
from threading import Thread, Lock
from time import sleep
import math
import os

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

# ICAO aircraft type to human-readable name mapping
AIRCRAFT_NAMES = {
    "A20N": "Airbus A220",
    "A21N": "Airbus A220",
    "A225": "Airbus A400M",
    "A300": "Airbus A300",
    "A306": "Airbus A300-600",
    "A310": "Airbus A310",
    "A318": "Airbus A318",
    "A319": "Airbus A319",
    "A320": "Airbus A320",
    "A321": "Airbus A321",
    "A330": "Airbus A330",
    "A339": "Airbus A330-900",
    "A340": "Airbus A340",
    "A342": "Airbus A340-200",
    "A343": "Airbus A340-300",
    "A345": "Airbus A340-500",
    "A346": "Airbus A340-600",
    "A350": "Airbus A350-900",
    "A359": "Airbus A350-900",
    "A35K": "Airbus A350-1000",
    "A380": "Airbus A380-800",
    "A388": "Airbus A380-800",
    "A400": "Airbus A400M",
    "B37M": "Boeing 737 MAX 7",
    "B38M": "Boeing 737 MAX 8",
    "B39M": "Boeing 737 MAX 9",
    "B3XM": "Boeing 737 MAX 10",
    "B712": "Boeing 717-200",
    "B721": "Boeing 727-100",
    "B722": "Boeing 727-200",
    "B732": "Boeing 737-200",
    "B733": "Boeing 737-300",
    "B734": "Boeing 737-400",
    "B735": "Boeing 737-500",
    "B736": "Boeing 737-600",
    "B737": "Boeing 737-700",
    "B738": "Boeing 737-800",
    "B739": "Boeing 737-900",
    "B741": "Boeing 747-100",
    "B742": "Boeing 747-200",
    "B743": "Boeing 747-300",
    "B744": "Boeing 747-400",
    "B745": "Boeing 747-500",
    "B746": "Boeing 747-600",
    "B748": "Boeing 747-8",
    "B752": "Boeing 757-200",
    "B753": "Boeing 757-300",
    "B762": "Boeing 767-200",
    "B763": "Boeing 767-300",
    "B764": "Boeing 767-400",
    "B772": "Boeing 777-200",
    "B773": "Boeing 777-300",
    "B77L": "Boeing 777-200LR",
    "B77W": "Boeing 777-300ER",
    "B778": "Boeing 777-8",
    "B779": "Boeing 777-9",
    "B787": "Boeing 787-8",
    "B788": "Boeing 787-8",
    "B789": "Boeing 787-9",
    "B78X": "Boeing 787-10",
    "BA11": "British Aerospace 146",
    "CRJ2": "Bombardier CRJ-200",
    "CRJ7": "Bombardier CRJ-700",
    "CRJ9": "Bombardier CRJ-900",
    "E145": "Embraer ERJ-145",
    "E170": "Embraer E170",
    "E175": "Embraer E175",
    "E190": "Embraer E190",
    "E195": "Embraer E195",
}


def zone_dict_to_bounds_string(zone):
    """Convert zone dict (tl_y, tl_x, br_y, br_x) to bounds string (north,south,west,east)."""
    return f"{zone['tl_y']},{zone['br_y']},{zone['tl_x']},{zone['br_x']}"


def get_aircraft_name(icao_code):
    """Map ICAO aircraft code to human-readable name. Falls back to code if not found."""
    return AIRCRAFT_NAMES.get(icao_code, icao_code)

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
        api_token = os.environ.get("FR24_API_TOKEN") # change for passing token directly
        self._api = Client(api_token=api_token)
        self._lock = Lock()
        self._data = []
        self._new_data = False
        self._processing = False

    def grab_data(self):
        Thread(target=self._grab_data).start()

    def _grab_data(self):
        # Mark data as old
        with self._lock:
            self._new_data = False
            self._processing = True

        data = []

        # Grab flight details
        try:
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
                # Get plane type and map to human-readable name
                plane = get_aircraft_name(flight.type) if flight.type else ""
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
        sleep(15) # changed tio for sure match query time limit on paid API

    print(o.data)
