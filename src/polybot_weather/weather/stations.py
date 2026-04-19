"""Station code → coordinate + timezone mapping.

Coordinates are the official ASOS/ICAO sites used by NWS for daily climate
records. Polymarket weather markets resolve against specific stations (KLGA vs
KNYC, KORD vs KMDW), so we *must* use the exact site lat/lon, not the city
center.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Station:
    code: str           # ICAO, e.g. "KLGA" / "EGLL" / "SBGR"
    name: str
    lat: float
    lon: float
    timezone: str       # IANA tz
    default_unit: str = "F"   # "F" for US, "C" for the rest of the world


# Hand-curated. Add as new markets show up. US stations resolve in °F by NWS
# convention; everywhere else uses °C.
STATIONS: dict[str, Station] = {
    # === United States (°F) ===
    "KLGA": Station("KLGA", "New York LaGuardia", 40.7794, -73.8803, "America/New_York", "F"),
    "KJFK": Station("KJFK", "New York JFK",       40.6398, -73.7789, "America/New_York", "F"),
    "KEWR": Station("KEWR", "Newark Liberty",     40.6925, -74.1687, "America/New_York", "F"),
    "KNYC": Station("KNYC", "NYC Central Park",   40.7794, -73.9692, "America/New_York", "F"),
    "KORD": Station("KORD", "Chicago O'Hare",     41.9742, -87.9073, "America/Chicago",  "F"),
    "KMDW": Station("KMDW", "Chicago Midway",     41.7868, -87.7522, "America/Chicago",  "F"),
    "KDCA": Station("KDCA", "Washington Reagan",  38.8512, -77.0402, "America/New_York", "F"),
    "KIAD": Station("KIAD", "Washington Dulles",  38.9531, -77.4565, "America/New_York", "F"),
    "KBWI": Station("KBWI", "Baltimore BWI",      39.1754, -76.6683, "America/New_York", "F"),
    "KBOS": Station("KBOS", "Boston Logan",       42.3656, -71.0096, "America/New_York", "F"),
    "KATL": Station("KATL", "Atlanta",            33.6407, -84.4277, "America/New_York", "F"),
    "KMIA": Station("KMIA", "Miami",              25.7959, -80.2870, "America/New_York", "F"),
    "KFLL": Station("KFLL", "Fort Lauderdale",    26.0726, -80.1527, "America/New_York", "F"),
    "KLAX": Station("KLAX", "Los Angeles",        33.9425, -118.4081, "America/Los_Angeles", "F"),
    "KBUR": Station("KBUR", "Burbank",            34.2007, -118.3585, "America/Los_Angeles", "F"),
    "KSFO": Station("KSFO", "San Francisco",      37.6188, -122.3756, "America/Los_Angeles", "F"),
    "KOAK": Station("KOAK", "Oakland",            37.7213, -122.2208, "America/Los_Angeles", "F"),
    "KSEA": Station("KSEA", "Seattle-Tacoma",     47.4502, -122.3088, "America/Los_Angeles", "F"),
    "KDEN": Station("KDEN", "Denver",             39.8561, -104.6737, "America/Denver",     "F"),
    "KPHX": Station("KPHX", "Phoenix",            33.4342, -112.0116, "America/Phoenix",    "F"),
    "KLAS": Station("KLAS", "Las Vegas",          36.0840, -115.1537, "America/Los_Angeles", "F"),
    "KIAH": Station("KIAH", "Houston Bush",       29.9844, -95.3414, "America/Chicago",    "F"),
    "KHOU": Station("KHOU", "Houston Hobby",      29.6454, -95.2789, "America/Chicago",    "F"),
    "KDFW": Station("KDFW", "Dallas-Fort Worth",  32.8998, -97.0403, "America/Chicago",    "F"),

    # === Europe (°C) ===
    "EGLL": Station("EGLL", "London Heathrow",    51.4700, -0.4543,  "Europe/London",      "C"),
    "EGLC": Station("EGLC", "London City",        51.5053, 0.0553,   "Europe/London",      "C"),
    "LFPG": Station("LFPG", "Paris CDG",          49.0097, 2.5479,   "Europe/Paris",       "C"),
    "LFPO": Station("LFPO", "Paris Orly",         48.7233, 2.3795,   "Europe/Paris",       "C"),
    "EDDB": Station("EDDB", "Berlin Brandenburg", 52.3667, 13.5033,  "Europe/Berlin",      "C"),
    "EDDF": Station("EDDF", "Frankfurt",          50.0379, 8.5622,   "Europe/Berlin",      "C"),
    "LEMD": Station("LEMD", "Madrid Barajas",     40.4983, -3.5676,  "Europe/Madrid",      "C"),
    "LEBL": Station("LEBL", "Barcelona El Prat",  41.2974, 2.0833,   "Europe/Madrid",      "C"),
    "LIRF": Station("LIRF", "Rome Fiumicino",     41.8003, 12.2389,  "Europe/Rome",        "C"),
    "EHAM": Station("EHAM", "Amsterdam Schiphol", 52.3086, 4.7639,   "Europe/Amsterdam",   "C"),
    "LSZH": Station("LSZH", "Zurich",             47.4647, 8.5492,   "Europe/Zurich",      "C"),
    "LOWW": Station("LOWW", "Vienna",             48.1103, 16.5697,  "Europe/Vienna",      "C"),
    "EPWA": Station("EPWA", "Warsaw Chopin",      52.1657, 20.9671,  "Europe/Warsaw",      "C"),
    "LTBA": Station("LTBA", "Istanbul Atatürk",   40.9769, 28.8146,  "Europe/Istanbul",    "C"),
    "UUEE": Station("UUEE", "Moscow Sheremetyevo", 55.9726, 37.4146, "Europe/Moscow",      "C"),
    "EKCH": Station("EKCH", "Copenhagen",         55.6180, 12.6561,  "Europe/Copenhagen",  "C"),
    "ESSA": Station("ESSA", "Stockholm Arlanda",  59.6519, 17.9186,  "Europe/Stockholm",   "C"),

    # === Asia / Pacific (°C) ===
    "RJTT": Station("RJTT", "Tokyo Haneda",       35.5494, 139.7798, "Asia/Tokyo",         "C"),
    "RJAA": Station("RJAA", "Tokyo Narita",       35.7647, 140.3863, "Asia/Tokyo",         "C"),
    "RKSI": Station("RKSI", "Seoul Incheon",      37.4602, 126.4407, "Asia/Seoul",         "C"),
    "ZBAA": Station("ZBAA", "Beijing Capital",    40.0801, 116.5846, "Asia/Shanghai",      "C"),
    "ZSPD": Station("ZSPD", "Shanghai Pudong",    31.1443, 121.8083, "Asia/Shanghai",      "C"),
    "VHHH": Station("VHHH", "Hong Kong",          22.3080, 113.9185, "Asia/Hong_Kong",     "C"),
    "ZGSZ": Station("ZGSZ", "Shenzhen Bao'an",    22.6393, 113.8108, "Asia/Shanghai",      "C"),
    "ZGGG": Station("ZGGG", "Guangzhou Baiyun",   23.3924, 113.2988, "Asia/Shanghai",      "C"),
    "RCTP": Station("RCTP", "Taipei Taoyuan",     25.0797, 121.2342, "Asia/Taipei",        "C"),
    "VTBS": Station("VTBS", "Bangkok Suvarnabhumi", 13.6900, 100.7501, "Asia/Bangkok",     "C"),
    "VVNB": Station("VVNB", "Hanoi Noi Bai",      21.2187, 105.8048, "Asia/Ho_Chi_Minh",   "C"),
    "VVTS": Station("VVTS", "Ho Chi Minh City",   10.8188, 106.6520, "Asia/Ho_Chi_Minh",   "C"),
    "RPLL": Station("RPLL", "Manila Ninoy Aquino", 14.5086, 121.0194, "Asia/Manila",        "C"),
    "WMKK": Station("WMKK", "Kuala Lumpur",        2.7456, 101.7099, "Asia/Kuala_Lumpur",  "C"),
    "WIII": Station("WIII", "Jakarta Soekarno",  -6.1256, 106.6558, "Asia/Jakarta",        "C"),
    "WSSS": Station("WSSS", "Singapore Changi",    1.3644, 103.9915, "Asia/Singapore",     "C"),
    "VABB": Station("VABB", "Mumbai",             19.0887, 72.8679,  "Asia/Kolkata",       "C"),
    "VIDP": Station("VIDP", "Delhi",              28.5562, 77.1000,  "Asia/Kolkata",       "C"),
    "OMDB": Station("OMDB", "Dubai",              25.2532, 55.3657,  "Asia/Dubai",         "C"),
    "OTHH": Station("OTHH", "Doha Hamad",         25.2731, 51.6080,  "Asia/Qatar",         "C"),
    "YSSY": Station("YSSY", "Sydney",            -33.9461, 151.1772, "Australia/Sydney",   "C"),
    "YMML": Station("YMML", "Melbourne",         -37.6690, 144.8410, "Australia/Melbourne", "C"),

    # === Canada (°C) ===
    "CYYZ": Station("CYYZ", "Toronto Pearson",    43.6777, -79.6248, "America/Toronto",    "C"),
    "CYUL": Station("CYUL", "Montreal Trudeau",   45.4706, -73.7408, "America/Toronto",    "C"),
    "CYVR": Station("CYVR", "Vancouver",          49.1939, -123.1844, "America/Vancouver", "C"),

    # === Latin America (°C) ===
    "SBGR": Station("SBGR", "São Paulo Guarulhos", -23.4356, -46.4731, "America/Sao_Paulo", "C"),
    "SBSP": Station("SBSP", "São Paulo Congonhas", -23.6262, -46.6553, "America/Sao_Paulo", "C"),
    "SBRJ": Station("SBRJ", "Rio Santos Dumont",  -22.9105, -43.1631, "America/Sao_Paulo",  "C"),
    "SBGL": Station("SBGL", "Rio Galeão",         -22.8089, -43.2436, "America/Sao_Paulo",  "C"),
    "SBBR": Station("SBBR", "Brasília",           -15.8711, -47.9186, "America/Sao_Paulo",  "C"),
    "MMMX": Station("MMMX", "Mexico City Benito Juárez", 19.4361, -99.0719, "America/Mexico_City", "C"),
    "SABE": Station("SABE", "Buenos Aires Aeroparque", -34.5592, -58.4156, "America/Argentina/Buenos_Aires", "C"),
    "SCEL": Station("SCEL", "Santiago",           -33.3930, -70.7858, "America/Santiago",   "C"),
    "SKBO": Station("SKBO", "Bogotá El Dorado",     4.7016, -74.1469, "America/Bogota",     "C"),
    "SPJC": Station("SPJC", "Lima Jorge Chávez",  -12.0219, -77.1143, "America/Lima",       "C"),

    # === Africa (°C) ===
    "FACT": Station("FACT", "Cape Town",          -33.9648, 18.6017,  "Africa/Johannesburg", "C"),
    "FAJS": Station("FAJS", "Johannesburg O.R. Tambo", -26.1392, 28.2460, "Africa/Johannesburg", "C"),
    "HECA": Station("HECA", "Cairo",              30.1219, 31.4056,  "Africa/Cairo",       "C"),
    "DNMM": Station("DNMM", "Lagos",               6.5774, 3.3210,   "Africa/Lagos",       "C"),
}


def get_station(code: str) -> Station | None:
    return STATIONS.get(code.upper())
