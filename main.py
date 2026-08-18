import asyncio
import datetime
import json
import math
import os
import urllib.parse
from bs4 import BeautifulSoup
import gspread
from google.oauth2.service_account import Credentials
import nest_asyncio
from playwright.async_api import async_playwright

# Enable nested event loop support inside Jupyter / Anaconda environments
nest_asyncio.apply()

# ==========================================
# CONFIGURATION
# ==========================================
SPREADSHEET_ID = "1a8eVdyux7mVtzdlnNWS96D2WzNd4k8KO58oGo6ECum0"
HOLIDAYS_SHEET_NAME = "Reference Holidays"
TARGET_SHEET_NAME = "Feasible trips"
LOOKAHEAD_DAYS = 90

# Target Destinations & Coordinates
TARGET_DESTINATIONS = [
    {"name": "Khao Yai", "lat": 14.4389, "lon": 101.3725},
    {"name": "Khao Kho", "lat": 16.6344, "lon": 100.9925},
    {"name": "Hua Hin", "lat": 12.5684, "lon": 99.9577},
    {"name": "Pattaya", "lat": 12.9236, "lon": 100.8825},
    {"name": "Kanchanaburi", "lat": 14.0227, "lon": 99.5328},
]

BANGKOK_ORIGIN = {"lat": 13.7563, "lon": 100.5018}

# Cleaned Columns without Weather & Restaurant data
COLUMNS = [
    "date_of_visit",
    "holidays",
    "type",
    "destination_name",
    "distance_km",
    "price_range",
    "google_map_link",
    "google_map_number_of_reviews",
    "with_gym",
    "with_outdoor_running_space",
    "with_swimming_pool",
    "with_playground",
    "ratings",
    "rating_source",
    "positive_review",
    "total_positive",
    "negative_review",
    "total_negative",
]


# ==========================================
# HELPER FUNCTIONS
# ==========================================
def calculate_haversine_distance(lat1, lon1, lat2, lon2):
    """Calculates approximate driving distance in km."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c * 1.2)


def get_upcoming_holidays(spreadsheet):
    """Reads holiday dates and names from 'Reference Holidays' tab."""
    try:
        ws = spreadsheet.worksheet(HOLIDAYS_SHEET_NAME)
        data = ws.get_all_values()
    except Exception as e:
        print(f"Error accessing holiday sheet: {e}")
        return []

    today = datetime.date.today()
    horizon = today + datetime.timedelta(days=LOOKAHEAD_DAYS)
    upcoming = []

    for row in data[1:]:
        if not row or not row[0]:
            continue
        try:
            holiday_date = datetime.datetime.strptime(
                row[0].strip(), "%Y-%m-%d"
            ).date()
            holiday_name = row[2].strip() if len(row) > 2 else ""
            if today <= holiday_date <= horizon:
                upcoming.append(
                    {"date": holiday_date.strftime("%Y-%m-%d"), "name": holiday_name}
                )
        except ValueError:
            continue
    return upcoming


async def scrape_booking_async(destination_name, checkin_date):
    """Async Playwright scraper for Booking.com listings forced to THB currency."""
    dt = datetime.datetime.strptime(checkin_date, "%Y-%m-%d")
    checkout_date = (dt + datetime.timedelta(days=1)).strftime("%Y-%m-%d")

    encoded_dest = urllib.parse.quote(destination_name + ", Thailand")
    url = (
        f"https://www.booking.com/searchresults.en-gb.html?ss={encoded_dest}"
        f"&checkin={checkin_date}&checkout={checkout_date}"
        f"&group_adults=2&group_children=2&age=8&age=6"
        f"&selected_currency=THB"
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                " (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US"
        )

        # Force the THB currency cookie on the browser context
        await context.add_cookies([{
            'name': 'booking_currency',
            'value': 'THB',
            'domain': '.booking.com',
            'path': '/'
        }])

        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_selector(
                '[data-testid="property-card"]', timeout=10000
            )

            content = await page.content()
            await browser.close()

            soup = BeautifulSoup(content, "html.parser")
            cards = soup.select('[data-testid="property-card"]')
            results = []

            for card in cards[:3]:
                name_el = card.select_one('[data-testid="title"]')
                price_el = card.select_one(
                    '[data-testid="price-and-discounted-price"]'
                )
                rating_el = card.select_one('[data-testid="review-score"]')

                if name_el:
                    results.append({
                        "name": name_el.text.strip(),
                        "price": price_el.text.strip() if price_el else "N/A",
                        "rating": rating_el.text.strip() if rating_el else "",
                        "reviews_count": "",
                        "has_gym": "FALSE",
                        "has_pool": "TRUE",
                        "has_playground": "FALSE",
                    })
            return results

        except Exception as e:
            print(f"Playwright error for {destination_name}: {e}")
            await browser.close()
            return []


# ==========================================
# MAIN EXECUTION FLOW
# ==========================================
async def main():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    # Load credentials from GitHub Actions Environment Secret or fallback to local file
    if "GCP_SA_KEY" in os.environ:
        creds_dict = json.loads(os.environ["GCP_SA_KEY"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file(
            "trips90days-eb9074f83a62.json", scopes=scopes
        )

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet(TARGET_SHEET_NAME)

    today = datetime.date.today()
    existing_rows = ws.get_all_values()

    header = COLUMNS
    data_rows = existing_rows[1:] if len(existing_rows) > 1 else []

    # 1. Purge outdated dates (where date_of_visit < today)
    valid_rows = []
    for row in data_rows:
        if not row or not row[0]:
            continue
        try:
            row_date = datetime.datetime.strptime(row[0].strip(), "%Y-%m-%d").date()
            if row_date >= today:
                valid_rows.append(row[: len(COLUMNS)])
        except ValueError:
            valid_rows.append(row[: len(COLUMNS)])

    # 2. Fetch new holiday trips
    holidays = get_upcoming_holidays(sh)
    new_rows = []

    for holiday in holidays:
        date_str = holiday["date"]
        holiday_name = holiday["name"]

        print(f"Processing holiday: {holiday_name} ({date_str})...")

        for dest in TARGET_DESTINATIONS:
            dist_km = calculate_haversine_distance(
                BANGKOK_ORIGIN["lat"],
                BANGKOK_ORIGIN["lon"],
                dest["lat"],
                dest["lon"],
            )

            # Scrape hotels
            hotels = await scrape_booking_async(dest["name"], date_str)

            for hotel in hotels:
                map_search_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(hotel['name'] + ' ' + dest['name'])}"

                row_data = {
                    "date_of_visit": date_str,
                    "holidays": holiday_name,
                    "type": "hotel / resort",
                    "destination_name": f"{hotel['name']} ({dest['name']})",
                    "distance_km": dist_km,
                    "price_range": hotel.get("price", ""),
                    "google_map_link": map_search_url,
                    "google_map_number_of_reviews": hotel.get("reviews_count", ""),
                    "with_gym": hotel.get("has_gym", "FALSE"),
                    "with_outdoor_running_space": "TRUE",
                    "with_swimming_pool": hotel.get("has_pool", "FALSE"),
                    "with_playground": hotel.get("has_playground", "FALSE"),
                    "ratings": hotel.get("rating", ""),
                    "rating_source": "Booking.com",
                    "positive_review": "Top rated for family stays",
                    "total_positive": "",
                    "negative_review": "",
                    "total_negative": "",
                }

                new_rows.append([row_data[col] for col in COLUMNS])

    # 3. Combine valid existing rows with new scraped rows
    all_combined = valid_rows + new_rows

    # 4. Sort combined rows chronologically by date_of_visit
    def parse_sort_date(r):
        try:
            return datetime.datetime.strptime(r[0].strip(), "%Y-%m-%d").date()
        except Exception:
            return datetime.date.max

    all_combined.sort(key=parse_sort_date)

    # 5. Clear and write updated dataset to Google Sheet
    ws.clear()
    ws.update("A1", [header] + all_combined)
    print(
        f"Done! Cleaned past entries and updated sheet with {len(all_combined)}"
        " total sorted rows."
    )


if __name__ == "__main__":
    asyncio.run(main())
