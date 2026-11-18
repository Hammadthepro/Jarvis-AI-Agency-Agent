"""Compliance gates. Google Places API (New) prospect selection is disabled until an approval
from Google or qualified legal counsel covering the exact intended use is recorded."""

from __future__ import annotations

from uuid import UUID

import asyncpg

from jarvis.core.errors import ComplianceBlocked

INTENDED_USE = {
    "google_places_prospect_selection": (1, (
        "Use Google Places API (New) Text Search, with minRating and website-listing data, to find and filter "
        "US businesses by Google rating and website availability, in order to select potential "
        "website-development clients for commercial email outreach.")),
    "google_places_listing_display": (1, (
        "Display a live, attributed Google Places listing (name, address, status, rating, review count, "
        "Google Maps link) for a prospect to the authenticated operator, fetched on demand and never stored.")),
    "google_places_technical_testing": (1, (
        "Capped technical verification of the Google Places API (New) adapter (field masks, SKUs, minRating "
        "behaviour). No prospect records are created and no results are retained beyond test assertions.")),
}


async def gate_status(conn: asyncpg.Connection, workspace_id: UUID, scope: str) -> dict:
    row = await conn.fetchrow(
        """select * from compliance_approvals where workspace_id = $1 and scope = $2 and revoked_at is null""",
        workspace_id, scope)
    if row is None:
        return {"scope": scope, "open": False,
                "reason": f"No approval recorded for '{scope}'. Required: written confirmation from Google or "
                          "a written opinion from qualified legal counsel covering the intended use.",
                "intended_use": INTENDED_USE.get(scope, (None, None))[1]}
    if await conn.fetchval("select $1::timestamptz < now()", row["review_by"]):
        return {"scope": scope, "open": False, "reason": f"Approval expired on {row['review_by'].date()} (review_by passed)",
                "intended_use": row["intended_use_text"]}
    version, text = INTENDED_USE.get(scope, (None, None))
    if version is not None and (row["intended_use_version"] != version or row["intended_use_text"] != text):
        return {"scope": scope, "open": False,
                "reason": "Recorded approval covers a different intended-use statement version; re-approval required",
                "intended_use": text}
    return {"scope": scope, "open": True, "reason": None, "approver_type": row["approver_type"],
            "approver_name": row["approver_name"], "document_ref": row["document_ref"],
            "review_by": row["review_by"].isoformat(), "intended_use": row["intended_use_text"]}


async def require_gate(conn: asyncpg.Connection, workspace_id: UUID, scope: str) -> None:
    st = await gate_status(conn, workspace_id, scope)
    if not st["open"]:
        raise ComplianceBlocked(st["reason"], details={"scope": scope})
