import os
from flask import Flask, render_template_string, request, flash
import folium
import pandas as pd
from folium.plugins import BeautifyIcon
from concurrent.futures import ThreadPoolExecutor, as_completed
from helper import update_and_get_dataset, haversine, get_coordinates_from_postal, get_route, build_tracked_gmaps_link

# Fetch and initialize dataset
aac_df = update_and_get_dataset()
chp_df = aac_df[~(aac_df['Category'] == 'AAC')].copy()

app = Flask(__name__)
app.secret_key = "secret_key_123"

@app.after_request
def allow_embedding(response):
    # Remove older header if Vercel injected it
    response.headers.pop('X-Frame-Options', None)
    
    # Modern approach: Allow any site to embed via CSP
    response.headers['Content-Security-Policy'] = "frame-ancestors *;"
    
    return response

HTML_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>CHP and AAC Finder</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        html, body {
            height: 100%;
            width: 100%;
            margin: 0;
            padding: 0;
            overflow: hidden;
        }

        #map-container {
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            height: 100vh;
            width: 100vw;
        }

        iframe {
            position: absolute;
            top: 0;
            left: 0;
            height: 100vh !important;
            width: 100vw !important;
            border: none;
        }

        .form-box {
            position: fixed;
            top: 12px;
            left: 50%;
            transform: translateX(-50%);
            z-index: 9999;
            background: white;
            padding: 8px 12px;
            border-radius: 6px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.2);
        }
        .error-msg { 
            color: #721c24; 
            background-color: #f8d7da; 
            border: 1px solid #f5c6cb; 
            padding: 5px; 
            border-radius: 4px; 
            margin-top: 5px; 
            font-size: 13px; 
        }
    </style>
</head>
<body>
    <div class="form-box">
        <form method="POST">
            <input
                type="text"
                name="postal"
                placeholder="Enter postal code"
                value="{{ postal or '' }}"
                style="padding:6px;"
            >
            <button type="submit">Find nearby CHPs</button>
            {% with messages = get_flashed_messages() %}
                {% if messages %}
                    {% for message in messages %}
                        <div class="error-msg">{{ message }}</div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
        </form>
    </div>

    <div id="map-container">
        {{ map_html|safe }}
    </div>
</body>
</html>
"""

@app.route("/health", methods=["GET"])
def health_check():
    return {"status": "healthy"}, 200

@app.route("/", methods=["GET", "POST"])
def index():
    postal = request.form.get("postal")

    WR_center = [1.345428, 103.7508]
    folium_map = folium.Map(
        location=WR_center,
        zoom_start=13,
        scrollWheelZoom=True,
        dragging=True,
        zoomControl=True
    )

    folium_map.get_root().html.add_child(folium.Element("""
    <script>
    document.addEventListener("DOMContentLoaded", function() {
        const map = document.querySelector(".leaflet-container");
        if (map) {
            map.addEventListener('touchmove', function(e) {}, { passive: true });
        }
    });
    </script>
    """))

    # Add markers for all facilities
    for _, row in aac_df.iterrows():
        if pd.isna(row['Forsg']):
            tracked_gmaps_link = build_tracked_gmaps_link(row["latitude"], row["longitude"])
            popup_html = f"""
            <b>{row['Centre Name']}</b><br>
            <b>Address:</b> {row['Address']}<br>
            <a href="{tracked_gmaps_link}" target="_blank">📍 Open in Google Maps</a><br>
            """
        else:
            popup_html = f"""
            <b>{row['Centre Name']}</b><br>
            <b>Address:</b> {row['Address']}<br>
            <a href="{row['Forsg']}" target="_blank">📍 Open in Google Maps</a><br>
            """
        
        category = row["Category"]
        if category == "CHP":
            marker_color = "#003D7C"
            popup_html += f"<b>CHP Opening Hours:</b> {row['CHP Operating Hours']}"
        elif category == "AAC":
            marker_color = "gray"
            popup_html += f"<b>AAC Opening Hours:</b> {row['AAC Operating Hours']}"
        elif category == "AAC & CHP":
            marker_color = "#003D7C"
            popup_html += f"<b>CHP Opening Hours:</b> {row['CHP Operating Hours']}<br>"
            popup_html += f"<b>AAC Opening Hours:</b> {row['AAC Operating Hours']}"

        folium.Marker(
            location=[row["latitude"], row["longitude"]],
            popup=folium.Popup(popup_html, max_width=300),
            icon=BeautifyIcon(
                icon_shape='marker',
                background_color=marker_color,
                border_color='white',
                border_width=1,
                inner_icon_style="display:none;",
                icon_size=[25, 25]
            )
        ).add_to(folium_map)

    if postal:
        try:
            all_coords = []
            coord_result = get_coordinates_from_postal(postal)
            
            if coord_result is None or not isinstance(coord_result, (tuple, list)):
                flash(f"Postal code '{postal}' not found. Please try again.")
            else:
                user_lat = coord_result[0]
                user_lon = coord_result[1]
                
                if user_lat is None or user_lon is None:
                    flash(f"Location coordinates not available for '{postal}'.")
                else:
                    folium.Marker(
                        location=[user_lat, user_lon],
                        popup="You are here",
                        z_index_offset=100,
                        icon=folium.Icon(color="red", icon="home", prefix="fa")
                    ).add_to(folium_map)

                    all_coords.append([user_lat, user_lon])

                    chp_df["dist_km"] = chp_df.apply(
                        lambda x: haversine(user_lat, user_lon, x["latitude"], x["longitude"]),
                        axis=1
                    )

                    nearest = chp_df.nsmallest(3, "dist_km")
                    colors = ["#F37021", "#003D7C", "#41B6E6"]
                    route_results = []

                    # Multi-threaded route fetching across the 3 closest locations
                    with ThreadPoolExecutor(max_workers=3) as executor:
                        future_to_info = {
                            executor.submit(
                                get_route, 
                                (user_lat, user_lon), 
                                (row["latitude"], row["longitude"])
                            ): (i, row)
                            for i, (_, row) in enumerate(nearest.iterrows())
                        }
                        
                        for future in as_completed(future_to_info):
                            i, row = future_to_info[future]
                            try:
                                route = future.result()
                                if route:
                                    route_results.append((i, row, route))
                            except Exception as route_err:
                                print(f"Error fetching route for {row.get('Centre Name')}: {route_err}")

                    # Sort by original proximity index (0, 1, 2)
                    route_results.sort(key=lambda x: x[0])

                    # Draw routes & render markers
                    for i, row, route in route_results:
                        if isinstance(route, dict) and route.get("coords"):
                            if len(route["coords"]) > 1:
                                folium.PolyLine(
                                    route["coords"],
                                    color=colors[i],
                                    weight=6,
                                    opacity=1
                                ).add_to(folium_map)
                            all_coords.extend(route["coords"])

                        category = row["Category"]
                        hours_html = ""
                        if category == "CHP":
                            hours_html = f"<b>CHP Operating Hours:</b> {row['CHP Operating Hours']}<br>"
                        elif category == "AAC":
                            hours_html = f"<b>AAC Operating Hours:</b> {row['AAC Operating Hours']}<br>"
                        elif category == "AAC & CHP":
                            hours_html = (f"<b>CHP Operating Hours:</b> {row['CHP Operating Hours']}<br>"
                                        f"<b>AAC Operating Hours:</b> {row['AAC Operating Hours']}<br>")

                        tracked_gmaps_link = build_tracked_gmaps_link(row["latitude"], row["longitude"])
                        gmaps_url = row['Forsg'] if pd.notna(row['Forsg']) else tracked_gmaps_link

                        walk_dist = (route.get('Walk distance', 0) / 1000) if route else 0
                        travel_time = (route.get('time', 0) / 60) if route else 0

                        popup_html = f"""
                        <b>{row['Centre Name']}</b><br>
                        <b>Address:</b> {row['Address']}<br>
                        <a href="{gmaps_url}" target="_blank">📍 Open in Google Maps</a><br>
                        {hours_html}
                        <b>Walk Distance:</b> {walk_dist:.2f} km<br>
                        <b>Time:</b> {travel_time:.1f} min<br><br>
                        <b>Directions:</b><br>
                        """

                        instructions = route.get("Instructions", ["At same location."]) if route else []
                        for step in instructions:
                            popup_html += f"- {step}<br>"

                        folium.Marker(
                            location=[row["latitude"], row["longitude"]],
                            popup=folium.Popup(popup_html, max_width=320),
                            z_index_offset=1000,
                            icon=BeautifyIcon(
                                icon='plus',
                                icon_shape='marker',
                                background_color=colors[i],
                                border_color='white',
                                border_width=1,
                                text_color='white',
                                icon_size=[25, 25],
                                inner_icon_style='font-size:12px; margin-left: 0.5px;'
                            )
                        ).add_to(folium_map)

                        all_coords.append([row["latitude"], row["longitude"]])

                    if all_coords:
                        unique_coords = []
                        for c in all_coords:
                            coord_pair = [float(c[0]), float(c[1])]
                            if coord_pair not in unique_coords:
                                unique_coords.append(coord_pair)

                        if len(unique_coords) == 1:
                            folium_map.location = unique_coords[0]
                            folium_map.zoom_start = 16
                        else:
                            folium_map.fit_bounds(unique_coords)

        except Exception as e:
            flash(f"Error: Could not find location for '{postal}'. Please try another postal code.")
            print(f"Geocoding Error: {e}")

    map_html = folium_map._repr_html_()
    return render_template_string(
        HTML_TEMPLATE,
        map_html=map_html,
        postal=postal
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 7860))
    app.run(host="0.0.0.0", port=port, debug=False)

