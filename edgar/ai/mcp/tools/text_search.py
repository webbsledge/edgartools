"""
Text Search Tool

Full-text search across SEC filings using EDGAR EFTS
(Electronic Full-Text Search) API.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from edgar.ai.mcp.tools.base import (
    tool,
    success,
    error,
)

logger = logging.getLogger(__name__)

# EFTS search endpoint
EFTS_BASE_URL = "https://efts.sec.gov/LATEST/search-index"


@tool(
    name="edgar_text_search",
    description="""Full-text search across all SEC filings. Searches the actual text
content of filings — find filings that mention specific topics, products, risks, etc.

This searches EDGAR's EFTS (Electronic Full-Text Search) index, which covers
all filing text content, not just metadata.

Examples:
- Topic search: query="artificial intelligence"
- Scoped to form: query="cybersecurity incident", forms=["8-K"]
- Date range: query="supply chain disruption", start_date="2024-01-01", end_date="2024-12-31"
- Company + topic: query="tariff impact", forms=["10-K"], identifier="AAPL\"""",
    params={
        "query": {
            "type": "string",
            "description": "Full-text search query (searches filing content)"
        },
        "forms": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Filter by form types (e.g., ['10-K', '8-K']). Default: all forms."
        },
        "identifier": {
            "type": "string",
            "description": "Optional company ticker or CIK to scope results"
        },
        "start_date": {
            "type": "string",
            "description": "Start date for filing date range (YYYY-MM-DD)"
        },
        "end_date": {
            "type": "string",
            "description": "End date for filing date range (YYYY-MM-DD)"
        },
        "limit": {
            "type": "integer",
            "description": "Max results to return (default 20, max 50)",
            "default": 20
        }
    },
    required=["query"]
)
async def edgar_text_search(
    query: str,
    forms: Optional[list[str]] = None,
    identifier: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    limit: int = 20,
) -> Any:
    """Full-text search across SEC filings via EFTS."""
    try:
        if not query or not query.strip():
            return error(
                "Search query cannot be empty",
                suggestions=[
                    "Provide a search term like 'artificial intelligence'",
                    "Use forms parameter to filter by filing type",
                ]
            )

        limit = min(max(limit, 1), 50)

        # Build request parameters
        params: dict[str, Any] = {
            "q": query.strip(),
        }

        # Form type filter
        if forms:
            params["forms"] = ",".join(forms)

        # Date range
        if start_date or end_date:
            params["dateRange"] = "custom"
            if start_date:
                params["startdt"] = start_date
            if end_date:
                params["enddt"] = end_date

        # Company filter — resolve to CIK for server-side filtering
        entity_name = None
        if identifier:
            try:
                from edgar.ai.mcp.tools.base import resolve_company
                company = resolve_company(identifier)
                entity_name = company.name
                # EFTS requires 10-digit zero-padded CIK
                params["ciks"] = str(company.cik).zfill(10)
            except ValueError:
                # If it looks like a CIK, pad and use directly
                cleaned = identifier.strip()
                if cleaned.isdigit():
                    params["ciks"] = cleaned.zfill(10)
                else:
                    return error(
                        f"Could not find company: '{identifier}'",
                        suggestions=[
                            "Try a ticker (AAPL) or CIK number",
                            "Remove the identifier to search all filings",
                        ]
                    )

        # Make the EFTS API request
        import httpx

        headers = {"User-Agent": _get_user_agent()}

        async with httpx.AsyncClient() as client:
            resp = await client.get(EFTS_BASE_URL, params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()

        # Parse results
        hits = data.get("hits", {})
        total = hits.get("total", {}).get("value", 0)
        results = []

        for hit in hits.get("hits", []):
            source = hit.get("_source", {})

            filing_result = {
                "accession_number": _format_accession(source.get("adsh", "")),
                "form": source.get("form", ""),
                "filed": source.get("file_date", ""),
            }

            # Company info
            names = source.get("display_names", [])
            ciks = source.get("ciks", [])
            if names:
                filing_result["company"] = names[0]
            if ciks:
                filing_result["cik"] = str(ciks[0])

            # Period
            period = source.get("period_ending")
            if period:
                filing_result["period"] = period

            results.append(filing_result)

            if len(results) >= limit:
                break

        result = {
            "query": query,
            "total_matches": total,
            "count": len(results),
            "results": results,
        }

        if forms:
            result["forms_filter"] = forms
        if entity_name:
            result["company_filter"] = entity_name
        if start_date or end_date:
            result["date_range"] = {
                "start": start_date,
                "end": end_date,
            }

        if total > limit:
            result["note"] = f"Showing {len(results)} of {total} matches. Increase limit for more."

        next_steps = [
            "Use edgar_filing with an accession_number to read the full filing content",
            "Use edgar_company with a CIK to get company details",
        ]

        return success(result, next_steps=next_steps)

    except Exception as e:
        logger.exception("Error in edgar_text_search")
        return error(
            f"Search error: {e}",
            suggestions=[
                "SEC EFTS may be temporarily unavailable",
                "Try a simpler query",
                "Check date format (YYYY-MM-DD)",
            ]
        )


def _format_accession(adsh: str) -> str:
    """Format EFTS accession number (no dashes) to standard format (with dashes)."""
    adsh = adsh.replace("-", "")
    if len(adsh) == 18:
        return f"{adsh[:10]}-{adsh[10:12]}-{adsh[12:]}"
    return adsh


def _get_user_agent() -> str:
    """Get configured user-agent for SEC requests."""
    try:
        from edgar.core import get_identity
        return get_identity()
    except Exception:
        return "EdgarTools/MCP edgartools@example.com"
