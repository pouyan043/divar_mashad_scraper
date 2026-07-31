import requests
import time
import csv
import os
from typing import Optional, Dict, List, Tuple

URL = "https://api.divar.ir/v8/postlist/w/search"

HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Origin": "https://divar.ir",
    "Referer": "https://divar.ir/s/mashhad/real-estate",
    "Accept": "application/json, text/plain, */*",
}

# Main for-sale real-estate categories
CATEGORIES = {
    "apartment-sell": "Apartment",
    "house-villa-sell": "House/Villa",
    "plot-old": "Land/Old House",
    "office-sell": "Office",
    "shop-sell": "Shop",
}

# Price ranges in Toman (helps bypass pagination limit)
# Adjust if needed based on current market
PRICE_RANGES = [
    (None, 3_000_000_000),          # under 3 billion
    (3_000_000_000, 7_000_000_000), # 3-7B
    (7_000_000_000, 15_000_000_000),# 7-15B
    (15_000_000_000, 30_000_000_000),# 15-30B
    (30_000_000_000, None),         # above 30B
]

def build_payload(
    category: str,
    min_price: Optional[int] = None,
    max_price: Optional[int] = None,
    pagination_data: Optional[Dict] = None
) -> Dict:
    form_data = {
        "category": {"str": {"value": category}}
    }

    # Add price filter if provided
    if min_price is not None or max_price is not None:
        price_range = {}
        if min_price is not None:
            price_range["minimum"] = str(min_price)
        if max_price is not None:
            price_range["maximum"] = str(max_price)
        form_data["price"] = {"number_range": price_range}

    payload = {
        "city_ids": ["3"],  # Mashhad
        "source_view": "CATEGORY",
        "disable_recommendation": False,
        "map_state": {
            "camera_info": {
                "bbox": {
                    "min_latitude": 35.48217,
                    "min_longitude": 59.27478,
                    "max_latitude": 37.664839,
                    "max_longitude": 60.115829,
                },
                "place_hash": f"3||{category}|",
                "zoom": 7.85,
            },
            "page_state": "HALF_STATE",
        },
        "previous_place_ids": [],
        "search_data": {
            "form_data": {
                "data": form_data
            },
            "server_payload": {
                "@type": "type.googleapis.com/widgets.SearchData.ServerPayload",
                "additional_form_data": {
                    "data": {
                        "sort": {"str": {"value": "sort_date"}}
                    }
                },
            },
        },
    }
    if pagination_data:
        payload["pagination_data"] = pagination_data
    return payload


def extract_posts(widgets: List[Dict], category: str) -> List[Dict]:
    posts = []
    for w in widgets:
        if w.get("widget_type") != "POST_ROW":
            continue
        d = w.get("data", {})
        token = d.get("token")
        if not token:
            continue
        posts.append({
            "category": category,
            "title": d.get("title"),
            "price": d.get("middle_description_text"),
            "location": d.get("bottom_description_text"),
            "token": token,
            "link": f"https://divar.ir/v/{token}",
        })
    return posts


def fetch_with_retry(session: requests.Session, payload: Dict, max_retries: int = 5) -> Optional[Dict]:
    for attempt in range(max_retries):
        try:
            resp = session.post(URL, json=payload, timeout=30)
            if resp.status_code == 429:
                wait = 25 * (attempt + 1)
                print(f"  ⚠ Rate limit (429) — waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.exceptions.ReadTimeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError) as e:
            wait = 6 * (attempt + 1)
            print(f"  Network error ({type(e).__name__}) — attempt {attempt+1}/{max_retries}, wait {wait}s")
            time.sleep(wait)
        except requests.exceptions.HTTPError as e:
            print(f"  HTTP Error: {e}")
            if e.response is not None and e.response.status_code in (403, 429):
                time.sleep(40)
            else:
                return None
    return None


def scrape_segment(
    session: requests.Session,
    category: str,
    min_price: Optional[int],
    max_price: Optional[int],
    max_pages: int = 80,
    seen: Optional[set] = None
) -> List[Dict]:
    """Scrape one category + one price range."""
    if seen is None:
        seen = set()

    all_posts = []
    pagination_data = None
    name = CATEGORIES.get(category, category)
    price_label = f"{min_price or 0}-{max_price or '∞'}"

    print(f"\n{'='*55}")
    print(f"Scraping: {name} | Price: {price_label}")
    print(f"{'='*55}")

    for page in range(max_pages):
        payload = build_payload(category, min_price, max_price, pagination_data)
        data = fetch_with_retry(session, payload)
        if data is None:
            print("  Multiple failures → stopping this segment.")
            break

        widgets = data.get("list_widgets", [])
        new_posts = [p for p in extract_posts(widgets, category) if p["token"] not in seen]

        if not new_posts:
            print("  No new posts → end of this segment.")
            break

        for p in new_posts:
            seen.add(p["token"])
        all_posts.extend(new_posts)

        pag = data.get("pagination") or {}
        if not pag.get("has_next_page"):
            print("  Reached last page.")
            break

        pagination_data = pag.get("data")
        if not pagination_data:
            print("  pagination_data became empty.")
            break

        print(f"  Page {page+1}: {len(new_posts)} new | Segment total: {len(all_posts)}")
        time.sleep(1.7)

        # Temporary save every 20 pages
        if (page + 1) % 20 == 0:
            fname = f"partial_{category}_{price_label.replace('∞','max')}.csv"
            with open(fname, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["category", "title", "price", "location", "token", "link"])
                writer.writeheader()
                writer.writerows(all_posts)
            print(f"   Temporary save → {fname}")

    return all_posts


def main():
    session = requests.Session()
    session.headers.update(HEADERS)

    all_data = []
    global_seen = set()

    # Load previously collected tokens (for resume)
    if os.path.exists("all_real_estate_mashhad.csv"):
        with open("all_real_estate_mashhad.csv", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                global_seen.add(row["token"])
                all_data.append(row)
        print(f"Resumed with {len(global_seen)} existing posts.")

    for cat in CATEGORIES.keys():
        for min_p, max_p in PRICE_RANGES:
            posts = scrape_segment(
                session,
                category=cat,
                min_price=min_p,
                max_price=max_p,
                max_pages=70,          # enough for each price slice
                seen=global_seen
            )
            all_data.extend(posts)

            # Save after each segment
            with open("all_real_estate_mashhad.csv", "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=["category", "title", "price", "location", "token", "link"])
                writer.writeheader()
                writer.writerows(all_data)

            print(f"Segment done. Current grand total: {len(all_data)}")

            # Soft stop if we already have enough
            if len(all_data) >= 32000:
                print("Reached ~32k → stopping early.")
                break
        else:
            continue
        break

    # Final category-separated files
    for cat, name in CATEGORIES.items():
        cat_posts = [p for p in all_data if p["category"] == cat]
        fname = f"{cat}.csv"
        with open(fname, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["category", "title", "price", "location", "token", "link"])
            writer.writeheader()
            writer.writerows(cat_posts)
        print(f" {name} → {len(cat_posts)} ads saved to {fname}")

    print(f"\n Finished! Total unique ads: {len(all_data)}")
    print("Main file: all_real_estate_mashhad.csv")


if __name__ == "__main__":
    main()