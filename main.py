import asyncio
import datetime
import json
import math
import os
import urllib.parse
from zoneinfo import ZoneInfo
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
BANGKOK_TZ = ZoneInfo("Asia/Bangkok")

# Target Destinations & Coordinates
TARGET_DESTINATIONS = [
    {"name": "Khao Yai", "lat": 14.4389, "lon": 101.3725},
    {"name": "Khao Kho", "lat": 16.6344, "lon": 100.9925},
    {"name": "Hua Hin", "lat": 12.5684, "lon": 99.9577},
    {"name": "Pattaya", "lat": 12.9236, "lon": 100.8825},
    {"name": "Kanchanaburi", "lat": 14.0227, "lon": 99.5328},
    {"name": "Phu Thap Boek", "lat": 16.7000, "lon": 101.0833},
    {"name": "Trat", "lat": 12.2428, "lon": 102.5178},
    {"name": "Chanthaburi", "lat": 12.6112, "lon": 102.1035},
    {"name": "Ratchaburi", "lat": 13.5282, "lon": 99.8134},
    {"name": "Phrae", "lat": 18.1445, "lon": 100.1405},
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
    "booking_link",
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
    "latest_run",
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


def build_booking_search_url(destination_name, checkin_date, checkout_date):
    """Builds the Booking.com search-results URL forced to THB currency."""
    encoded_dest = urllib.parse.quote(destination_name + ", Thailand")
    return (
        f"https://www.booking.com/searchresults.en-gb.html?ss={encoded_dest}"
        f"&checkin={checkin_date}&checkout={checkout_date}"
        f"&group_adults=2&group_children=2&age=8&age=6"
        f"&selected_currency=THB"
    )


def get_all_saturdays(start_date, end_date):
    """Returns every Saturday between start_date and end_date (inclusive)."""
    saturdays = []
    days_until_saturday = (5 - start_date.weekday()) % 7  # Monday=0 ... Saturday=5
    current = start_date + datetime.timedelta(days=days_until_saturday)
    while current <= end_date:
        saturdays.append({"date": current.strftime("%Y-%m-%d"), "name": "Saturday"})
        current += datetime.timedelta(days=7)
    return saturdays


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

    url = build_booking_search_url(destination_name, checkin_date, checkout_date)

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

                # Try to extract the actual property page link; fall back to
                # the search-results URL if the card has no direct href.
                link_el = card.select_one('a[data-testid="title-link"]') or card.select_one("a[href]")
                if link_el and link_el.get("href"):
                    href = link_el["href"]
                    property_link = href if href.startswith("http") else f"https://www.booking.com{href}"
                else:
                    property_link = url

                if name_el:
                    results.append({
                        "name": name_el.text.strip(),
                        "price": price_el.text.strip() if price_el else "N/A",
                        "rating": rating_el.text.strip() if rating_el else "",
                        "booking_link": property_link,
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
    # Timestamp of this automated run, in Bangkok local time.
    run_timestamp = datetime.datetime.now(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S")

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
    header = COLUMNS

    # 1. Fetch new holiday trips, then merge in every Saturday within the
    #    lookahead window. Actual named holidays take precedence over the
    #    generic "Saturday" label if a holiday happens to fall on one.
    horizon = today + datetime.timedelta(days=LOOKAHEAD_DAYS)
    holidays = get_upcoming_holidays(sh)
    saturdays = get_all_saturdays(today, horizon)

    merged_by_date = {h["date"]: h["name"] for h in saturdays}
    merged_by_date.update({h["date"]: h["name"] for h in holidays})
    holidays = [
        {"date": d, "name": n} for d, n in sorted(merged_by_date.items())
    ]

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
                    "booking_link": hotel.get("booking_link", ""),
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
                    "latest_run": run_timestamp,
                }

                new_rows.append([row_data[col] for col in COLUMNS])

    # 2. This run's freshly scraped dataset — no data is carried over from
    #    previous runs, so the sheet always reflects only this run's results.
    all_rows = new_rows

    # Every row is stamped with this run's timestamp for consistency.
    latest_run_idx = COLUMNS.index("latest_run")
    for row in all_rows:
        row[latest_run_idx] = run_timestamp

    # 3. Sort rows chronologically by date_of_visit
    def parse_sort_date(r):
        try:
            return datetime.datetime.strptime(r[0].strip(), "%Y-%m-%d").date()
        except Exception:
            return datetime.date.max

    all_rows.sort(key=parse_sort_date)

    # 4. Fully clear the sheet, then write only this run's fresh dataset —
    #    guarantees anyone reading the sheet always sees the latest run's
    #    results with no stale or duplicated rows left over.
    ws.clear()
    ws.update("A1", [header] + all_rows)
    print(
        f"Done! Cleared sheet and wrote {len(all_rows)} fresh rows."
        f" Run timestamp (BKK): {run_timestamp}"
    )


if __name__ == "__main__":
    asyncio.run(main())
