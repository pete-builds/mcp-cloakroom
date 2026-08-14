"""Bulk ingest CLI.

The server runs this automatically when the database is empty, so a plain
``docker compose up`` is enough to get a working install. Run it by hand to
refresh with newly published data:

    python ingest.py            # load if empty, refresh if already loaded
    python ingest.py --if-needed  # load only when empty, otherwise exit 0
    python ingest.py --status     # report what is loaded, fetch nothing

Every load is keyed on natural primary keys and written with INSERT OR REPLACE,
so re-running is safe and converges on the published data.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from dotenv import load_dotenv

from clients import db, loaders
from clients.config import VERSION, CloakroomSettings


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Load Senate roll call data.")
    parser.add_argument(
        "--if-needed",
        action="store_true",
        help="Only ingest when the database is empty; otherwise exit successfully.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print what is currently loaded and exit. Makes no network requests.",
    )
    args = parser.parse_args(argv)

    settings = CloakroomSettings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    conn = db.connect(settings.cloakroom_db_path)
    db.init_schema(conn)

    if args.status:
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM rollcalls) AS rollcalls, "
            "(SELECT COUNT(*) FROM votes) AS votes, "
            "(SELECT COUNT(*) FROM members) AS members, "
            "(SELECT COUNT(*) FROM member_ids) AS member_ids"
        ).fetchone()
        print(
            json.dumps(
                {
                    "database": settings.cloakroom_db_path,
                    "populated": loaders.is_populated(conn),
                    "last_ingest_completed": loaders.get_meta(conn, "last_ingest_completed"),
                    "counts": dict(row),
                },
                indent=2,
            )
        )
        return 0

    if args.if_needed and loaders.is_populated(conn):
        logging.info("Database already populated; nothing to do.")
        return 0

    result = loaders.run_ingest(conn, version=VERSION, contact_url=settings.cloakroom_contact_url)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
