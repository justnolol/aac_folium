import os
import json
import math
import requests
import numpy
import pandas as pd
from math import radians, sin, cos, sqrt, atan2
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from urllib.parse import quote_plus
from google.oauth2 import service_account
from googleapiclient.discovery import build
from concurrent.futures import ThreadPoolExecutor, as_completed

# Environment variables setup
load_dotenv()
email = os.getenv("ONEMAP_EMAIL")
password = os.getenv("ONEMAP_EMAIL_PASSWORD")
googlekey = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")

if not email and os.path.exists("/run/secrets/ONEMAP_EMAIL"):
    with open("/run/secrets/ONEMAP_EMAIL", "r") as f:
        email = f.read().strip()

if not password and os.path.exists("/run/secrets/ONEMAP_EMAIL_PASSWORD"):
    with open("/run/secrets/ONEMAP_EMAIL_PASSWORD", "r") as f:
        password = f.read().strip()

if not googlekey and os.path.exists("/run/secrets/GOOGLE_SERVICE_ACCOUNT_JSON"):
    with open("/run/secrets/GOOGLE_SERVICE_ACCOUNT_JSON", "r") as f:
        googlekey = f.read().strip()
        
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
info = json.loads(googlekey)
creds = service_account.Credentials.from_service_account_info(
    info,
    scopes=SCOPES
)

url = "https://www.onemap.gov.sg/api/auth/post/getToken"

def get_token(payload=None):
    clean_email = email.strip() if email else ""
    clean_password = password.strip() if password else ""
    
    auth_payload = payload or {
        "email": clean_email,
        "password": clean_password
    }
    
    try:
        response = requests.post(url, json=auth_payload, timeout=5)
        data = response.json()
        token = data.get("access_token")
        
        if not token:
            print("OneMap Auth Failed:", data)
            raise ValueError("Failed to retrieve OneMap access token.")
            
        return {"Authorization": token}
    except Exception as e:
        print(f"Token Error: {e}")
        raise e

def fetch_postal_task(index, row):
    """Worker task to fetch coordinates for a single postal code."""
    try:
        lat, lon, addr = get_coordinates_from_postal(row['Postal Code'])
        return index, row['Postal Code'], round(lat, 6), round(lon, 6)
    except Exception as e:
        print(f"Skipping postal {row.get('Postal Code')}: {e}")
        return index, row['Postal Code'], None, None

def update_and_get_dataset(creds=creds):
    service = build("sheets", "v4", credentials=creds)

    SPREADSHEET_ID = "109iaREEs4CyjcdO8-4quBiQhuGkuWxUd"
    RANGE_NAME = "CHP_dataset!A:I"
    SHEET_NAME = "CHP_dataset"

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=RANGE_NAME
    ).execute()

    values = result.get("values", [])
    
    if not values:
        return pd.DataFrame()

    aac_df = pd.DataFrame(values[1:], columns=values[0])
    aac_df['latitude'] = pd.to_numeric(aac_df['latitude'], errors='coerce')
    aac_df['longitude'] = pd.to_numeric(aac_df['longitude'], errors='coerce')
    
    missing_mask = aac_df['latitude'].isna() | aac_df['longitude'].isna()
    missing_df = aac_df[missing_mask]

    if missing_df.empty:
        return aac_df  

    cols = list(aac_df.columns)
    lat_idx = cols.index('latitude')
    lon_idx = cols.index('longitude')
    lat_col_letter = chr(65 + lat_idx)
    lon_col_letter = chr(65 + lon_idx)

    updates = []

    # Multi-threaded coordinate lookups for missing postal codes
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [
            executor.submit(fetch_postal_task, index, row)
            for index, row in missing_df.iterrows()
        ]
        for future in as_completed(futures):
            index, postal, lat, lon = future.result()
            if lat is not None and lon is not None:
                aac_df.at[index, 'latitude'] = lat
                aac_df.at[index, 'longitude'] = lon
                
                row_num = index + 2
                updates.append({'range': f"{SHEET_NAME}!{lat_col_letter}{row_num}", 'values': [[lat]]})
                updates.append({'range': f"{SHEET_NAME}!{lon_col_letter}{row_num}", 'values': [[lon]]})
                print(f"Updated {postal} at Row {row_num}")

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={'valueInputOption': 'USER_ENTERED', 'data': updates}
        ).execute()

    return aac_df

def haversine(lat1, lon1, lat2, lon2):
    R = 6371
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))

def get_coordinates_from_postal(postal_code):
    headers = get_token()
    url = f"https://www.onemap.gov.sg/api/common/elastic/search?searchVal={postal_code}&returnGeom=Y&getAddrDetails=Y&pageNum=1"
    r = requests.get(url, headers=headers, timeout=5).json()
    if r.get("found", 0) > 0:
        lat = float(r["results"][0]["LATITUDE"])
        lon = float(r["results"][0]["LONGITUDE"])
        addr = r["results"][0]["ADDRESS"]
        return lat, lon, addr
    else:
        raise ValueError("Postal code not found in OneMap.")

def route_instructions(legs):
    steps = []
    for leg in legs:
        mode = leg.get("mode", "").upper()
        if mode == "WALK":
            dist = leg.get("distance", 0)
            from_name = leg.get("from", {}).get("name", "starting point")
            to_name = leg.get("to", {}).get("name", "next point")
            steps.append(f"Walk {dist} metres from {from_name} to {to_name}.")
        elif mode == "BUS":
            route = leg.get("route", "")
            from_stop = leg.get("from", {}).get("name", "the bus stop")
            to_stop = leg.get("to", {}).get("name", "the next stop")
            steps.append(f"Take Bus {route} from {from_stop} to {to_stop}.")
        elif mode in ["SUBWAY", "TRAIN"]:
            route = leg.get("route", "")
            from_stop = leg.get("from", {}).get("name", "the station")
            to_stop = leg.get("to", {}).get("name", "your stop")
            steps.append(f"Take the {route} line from {from_stop} to {to_stop}.")
        else:
            steps.append("Continue as directed.")
    return steps

def get_route(start, end, routetype="pt", mode='TRANSIT', max_retries=2):
    """Get route using OneMap Routing API with retries and fallback handling."""
    if round(start[0], 6) == round(end[0], 6) and round(start[1], 6) == round(end[1], 6):
        return {
            "coords": [start, end],
            "time": 0,
            "Walk distance": 0,
            "Instructions": ["Same Location"]
        }
    
    headers = get_token()
    sgt = timezone(timedelta(hours=8))
    now = datetime.now(sgt)
    date_format = now.strftime('%m-%d-%Y')
    
    url = (
        f"https://www.onemap.gov.sg/api/public/routingsvc/route?"
        f"start={round(start[0],6)},{round(start[1],6)}&end={round(end[0],6)},{round(end[1],6)}"
        f"&date={date_format}&time=09:00:00"
        f"&routeType={routetype}&mode={mode}"
    )
    
    r = None
    for attempt in range(1, max_retries + 1):
        try:
            r = requests.get(url, headers=headers, timeout=2.5)
            if r.status_code == 200:
                break
        except Exception as e:
            print(f"Attempt {attempt} failed for {start} -> {end}: {e}")

    # Fallback to walking route if 404 or failed
    if r is not None and r.status_code == 404 and routetype == "pt":
        url_walk = (
            f"https://www.onemap.gov.sg/api/public/routingsvc/route?"
            f"start={round(start[0],6)},{round(start[1],6)}&end={round(end[0],6)},{round(end[1],6)}"
            f"&routeType=walk"
        )
        try:
            r = requests.get(url_walk, headers=headers, timeout=2.5)
        except Exception as e:
            print(f"Fallback walking connection failure: {e}")
            r = None

    # Final check: return straight-line geometry fallback if network calls fail completely
    if r is None or r.status_code != 200:
        dist_km = haversine(start[0], start[1], end[0], end[1])
        return {
            "coords": [[start[0], start[1]], [end[0], end[1]]],
            "time": (dist_km / 4.0) * 3600,
            "Walk distance": dist_km * 1000,
            "Instructions": ["Direct route line (live route calculation unavailable)."]
        }
    
    data = r.json()
    plan = data.get("plan")
    
    if not plan or not plan.get("itineraries"):
        dist_km = haversine(start[0], start[1], end[0], end[1])
        return {
            "coords": [[start[0], start[1]], [end[0], end[1]]],
            "time": (dist_km / 4.0) * 3600,
            "Walk distance": dist_km * 1000,
            "Instructions": ["Direct route line (no itinerary returned)."]
        }
        
    itinerary = plan["itineraries"][0]
    legs = itinerary.get("legs", [])
    coords = []
    
    for leg in legs:
        poly = leg.get("legGeometry", {}).get("points")
        if poly:
            coords.extend(decode_polyline(poly))
            
    instructions = route_instructions(legs)
    
    return {
        "coords": coords if coords else [[start[0], start[1]], [end[0], end[1]]],
        "time": itinerary.get("duration", 0),
        "Walk distance": itinerary.get("walkDistance", 0),
        "Instructions": instructions
    }

def decode_polyline(polyline_str):
    index, lat, lng, coordinates = 0, 0, 0, []
    changes = {"lat": 0, "lng": 0}
    while index < len(polyline_str):
        for unit in ["lat", "lng"]:
            shift, result = 0, 0
            while True:
                b = ord(polyline_str[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            if (result & 1):
                changes[unit] = ~(result >> 1)
            else:
                changes[unit] = (result >> 1)
        lat += changes["lat"]
        lng += changes["lng"]
        coordinates.append([lat / 1e5, lng / 1e5])
    return coordinates

def build_tracked_gmaps_link(lat, lng):
    return f"https://www.google.com/maps/search/?api=1&query={lat},{lng}"