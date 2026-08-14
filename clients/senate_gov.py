"""Parsers for the senate.gov XML feeds, and the lazy per-vote fetch.

Three schedule feeds publish in three genuinely different shapes (a flat
meeting list, an attribute-keyed senator roster, and a nested convening
calendar), so ``get_schedule`` normalizing them is real work, not a wrapper.

The per-vote detail fetch is deliberately lazy: one vote, on demand, cached
forever. Nothing in this server ever bulk-downloads roll call files. The
historical record comes from Voteview, so senate.gov is only ever asked for the
handful of votes a caller actually looked at.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

from clients.db import to_int
from clients.http_polite import (
    SENATE_STATIC_FEEDS,
    PoliteError,
    PoliteFetcher,
    senate_menu_url,
    senate_vote_url,
)

log = logging.getLogger("mcp-cloakroom.senate")


def _txt(node: ET.Element | None, tag: str) -> str | None:
    if node is None:
        return None
    el = node.find(tag)
    if el is None or el.text is None:
        return None
    # Several fields carry embedded newlines and heavy indentation, e.g.
    # "<question>On Cloture on the Motion to Proceed\n         </question>".
    return re.sub(r"\s+", " ", el.text).strip() or None


def _parse_xml(body: str, url: str) -> ET.Element:
    try:
        return ET.fromstring(body.encode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise PoliteError(
            f"could not parse XML from {url}: {exc}", "INTERNAL", {"url": url}
        ) from exc


class SenateGovClient:
    def __init__(
        self,
        fetcher: PoliteFetcher,
        conn=None,
        *,
        current_congress: int = 119,
        current_session: int = 2,
    ):
        self._fetch = fetcher
        self._conn = conn
        self.current_congress = current_congress
        self.current_session = current_session

    def _is_closed(self, congress: int, session: int) -> bool:
        """A session that is over can never change, so it caches permanently."""
        return (congress, session) < (self.current_congress, self.current_session)

    # ---------------------------------------------------------------- votes

    async def vote_menu(self, congress: int, session: int) -> list[dict]:
        """The per-session vote index. One small request; the freshness source.

        Returns the session's votes newest-first. Each row carries the feed's
        ``congress_year`` alongside ``vote_date`` so the calendar year travels
        with the row and callers do not have to re-derive it.
        """
        url = senate_menu_url(congress, session)
        body = await self._fetch.get_text(url, immutable=self._is_closed(congress, session))
        root = _parse_xml(body, url)
        congress_year = _txt(root, "congress_year")
        out: list[dict] = []
        for v in root.findall("./votes/vote"):
            tally = v.find("vote_tally")
            out.append(
                {
                    "vote_number": to_int(_txt(v, "vote_number")),
                    "vote_date": _txt(v, "vote_date"),
                    "congress_year": to_int(congress_year),
                    "issue": _txt(v, "issue"),
                    "question": _txt(v, "question"),
                    "result": _txt(v, "result"),
                    "yeas": to_int(_txt(tally, "yeas")) if tally is not None else None,
                    "nays": to_int(_txt(tally, "nays")) if tally is not None else None,
                    "title": _txt(v, "title"),
                }
            )
        return out

    async def vote_detail(self, congress: int, session: int, vote_number: int) -> dict:
        """One roll call's full record, including per-member positions.

        Cached in ``senate_vote_detail`` on first fetch and served from there
        afterwards, so a given vote is requested from senate.gov at most once.
        """
        cached = self._detail_from_db(congress, session, vote_number)
        if cached:
            return cached

        url = senate_vote_url(congress, session, vote_number)
        body = await self._fetch.get_text(url, immutable=self._is_closed(congress, session))
        root = _parse_xml(body, url)
        doc = root.find("document")
        amd = root.find("amendment")
        cnt = root.find("count")
        tie = root.find("tie_breaker")

        detail: dict[str, object] = {
            "congress": to_int(_txt(root, "congress")),
            "session": to_int(_txt(root, "session")),
            "vote_number": to_int(_txt(root, "vote_number")),
            "vote_date": _txt(root, "vote_date"),
            "modify_date": _txt(root, "modify_date"),
            "vote_question_text": _txt(root, "vote_question_text"),
            "vote_document_text": _txt(root, "vote_document_text"),
            "vote_result_text": _txt(root, "vote_result_text"),
            "question": _txt(root, "question"),
            "vote_title": _txt(root, "vote_title"),
            "majority_requirement": _txt(root, "majority_requirement"),
            "vote_result": _txt(root, "vote_result"),
            "document_name": _txt(doc, "document_name"),
            "document_title": _txt(doc, "document_title"),
            "amendment_purpose": _txt(amd, "amendment_purpose"),
            "tie_breaker_by": _txt(tie, "by_whom"),
            "tie_breaker_vote": _txt(tie, "tie_breaker_vote"),
            "count_yeas": to_int(_txt(cnt, "yeas")),
            "count_nays": to_int(_txt(cnt, "nays")),
            "count_present": to_int(_txt(cnt, "present")),
            "count_absent": to_int(_txt(cnt, "absent")),
        }
        detail["positions"] = [
            {
                "lis_member_id": _txt(m, "lis_member_id"),
                "member_full": _txt(m, "member_full"),
                "last_name": _txt(m, "last_name"),
                "first_name": _txt(m, "first_name"),
                "party": _txt(m, "party"),
                "state": _txt(m, "state"),
                "vote_cast": _txt(m, "vote_cast"),
            }
            for m in root.findall("./members/member")
        ]
        self._save_detail(detail)
        return detail

    def _detail_from_db(self, congress: int, session: int, vote_number: int) -> dict | None:
        if self._conn is None:
            return None
        row = self._conn.execute(
            "SELECT * FROM senate_vote_detail WHERE congress=? AND session=? AND vote_number=?",
            (congress, session, vote_number),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d.pop("fetched_at", None)
        # Positions are not stored per-member; the Voteview join supplies those
        # with stable identifiers, which is strictly better than the name blob.
        d["positions"] = []
        d["positions_source"] = "voteview"
        return d

    def _save_detail(self, detail: dict) -> None:
        if self._conn is None:
            return
        cols = [
            "congress",
            "session",
            "vote_number",
            "vote_date",
            "modify_date",
            "vote_question_text",
            "vote_document_text",
            "vote_result_text",
            "question",
            "vote_title",
            "majority_requirement",
            "vote_result",
            "document_name",
            "document_title",
            "amendment_purpose",
            "tie_breaker_by",
            "tie_breaker_vote",
            "count_yeas",
            "count_nays",
            "count_present",
            "count_absent",
        ]
        vals = [detail.get(c) for c in cols] + [datetime.now(UTC).isoformat()]
        self._conn.execute(
            f"INSERT OR REPLACE INTO senate_vote_detail ({', '.join(cols)}, fetched_at) "
            f"VALUES ({', '.join('?' * (len(cols) + 1))})",
            vals,
        )
        self._conn.commit()

    # ------------------------------------------------------------- schedule

    async def hearings(self) -> list[dict]:
        """Scheduled committee meetings.

        Entries without a committee are status rows rather than meetings. They
        are passed through flagged as ``placeholder: true`` rather than
        dropped, so "nothing scheduled" stays distinguishable from "the fetch
        returned nothing".
        """
        url = SENATE_STATIC_FEEDS["hearings"]
        root = _parse_xml(await self._fetch.get_text(url), url)
        out = []
        for m in root.findall("meeting"):
            cmte = _txt(m, "committee")
            out.append(
                {
                    "committee": cmte,
                    "committee_code": _txt(m, "cmte_code"),
                    "type": _txt(m, "type"),
                    "date": _txt(m, "date_iso_8601"),
                    "time": _txt(m, "time_iso_8601") or _txt(m, "time"),
                    "day_of_week": _txt(m, "day_of_week"),
                    "room": _txt(m, "room"),
                    "matter": _txt(m, "matter"),
                    "video_url": _txt(m, "video_url"),
                    "placeholder": cmte is None,
                }
            )
        return out

    async def senators(self) -> list[dict]:
        """Current senators with committee assignments.

        Carries both ``lis_member_id`` and ``bioguide_id``, so it doubles as an
        independent cross-check on the identity bridge built from
        congress-legislators.
        """
        url = SENATE_STATIC_FEEDS["members"]
        root = _parse_xml(await self._fetch.get_text(url), url)
        out = []
        for s in root.findall("senator"):
            name = s.find("name")
            out.append(
                {
                    "lis_member_id": s.get("lis_member_id"),
                    "bioguide_id": _txt(s, "bioguideId"),
                    "first_name": _txt(name, "first"),
                    "last_name": _txt(name, "last"),
                    "party": _txt(s, "party"),
                    "state": _txt(s, "state"),
                    "state_rank": _txt(s, "stateRank"),
                    "office": _txt(s, "office"),
                    "committees": [
                        {"code": c.get("code"), "name": (c.text or "").strip()}
                        for c in s.findall("./committees/committee")
                    ],
                }
            )
        return out

    async def floor_schedule(self) -> dict:
        """Session convening/adjourning calendar.

        Shape differs from the other two feeds: data lives in XML *attributes*
        on ``LegislativeDay`` and in child elements of ``SessionDay``.
        """
        url = SENATE_STATIC_FEEDS["floor"]
        root = _parse_xml(await self._fetch.get_text(url), url)
        days = []
        for d in root.findall("LegislativeDay"):
            sd = d.find("SessionDay")
            days.append(
                {
                    "legislative_day": d.get("LegislativeDayDate"),
                    "convene": _txt(sd, "ConveneDate"),
                    "adjourn": _txt(sd, "AdjournDate"),
                    "adjourn_type": _txt(sd, "AdjournType"),
                    "next_convene": _txt(sd, "NextConveneDate"),
                }
            )
        return {
            "congress": to_int(root.get("Congress")),
            "session": to_int(root.get("SessionNumber")),
            "days": days,
        }
