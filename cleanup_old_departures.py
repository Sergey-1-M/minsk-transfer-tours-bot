import os
from datetime import datetime
from zoneinfo import ZoneInfo

from supabase import create_client


def main():
    supabase_url = os.environ["SUPABASE_URL"]
    service_role_key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    db = create_client(supabase_url, service_role_key)

    # Compare dates in Belarus local time, not the GitHub runner's UTC date.
    today_minsk = datetime.now(ZoneInfo("Europe/Minsk")).date().isoformat()

    result = (
        db.table("departures")
        .select("id,departure_date")
        .lt("departure_date", today_minsk)
        .execute()
    )
    old_departures = result.data or []

    if not old_departures:
        print(f"No expired departures to delete. Minsk date: {today_minsk}")
        return

    departure_ids = [item["id"] for item in old_departures]

    # Remove dependent tourist applications and their personal data first.
    for start in range(0, len(departure_ids), 100):
        batch_ids = departure_ids[start:start + 100]
        db.table("bookings").delete().in_("departure_id", batch_ids).execute()

    # Then remove the expired departures themselves.
    for start in range(0, len(departure_ids), 100):
        batch_ids = departure_ids[start:start + 100]
        db.table("departures").delete().in_("id", batch_ids).execute()

    print(
        f"Deleted {len(departure_ids)} expired departures and all linked bookings. "
        f"Minsk date: {today_minsk}"
    )


if __name__ == "__main__":
    main()
