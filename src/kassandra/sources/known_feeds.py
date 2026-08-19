"""Known RSS/Atom feed URLs for Euro Stoxx 50 companies.

Verified working feeds as of 2026-06-19.
Corporate sites often don't expose feeds via standard mechanisms.
This module provides manually verified feed URLs for companies
where automated discovery fails.
"""

KNOWN_FEEDS: dict[str, list[str]] = {
    # ISIN → list of verified feed URLs
    "DE0007164600": [  # SAP
        "https://news.sap.com/feed/",
    ],
    "FR0000120628": [  # AXA
        "https://www.axa.com/rss",
    ],
    "NL0000235190": [  # Airbus
        "https://www.airbus.com/rss.xml",
    ],
    "DE0005190003": [  # BMW
        "https://www.press.bmwgroup.com/global/rss",
    ],
    "DE0005557508": [  # Deutsche Telekom — revived P2-2
        "https://www.telekom.com/en/media/rss",
    ],
}

# Companies confirmed to NOT have RSS feeds (monitor via sitemap + page only)
NO_FEED_ISINS: set[str] = {
    "DE0007236101",  # Siemens — JavaScript press page, no RSS
    "DE0008404005",  # Allianz — 403 on RSS endpoint
    "FR0000120271",  # TotalEnergies — no RSS discovered
    "NL0010273215",  # ASML — no RSS discovered
    "FR0000121014",  # LVMH — no RSS discovered
    "DE000BASF111",  # BASF — no RSS discovered
    "FR0000120321",  # L'Oréal — no RSS discovered
    "DE000BAY0017",  # Bayer — 403 on RSS endpoint
    "FR0000120578",  # Sanofi — no RSS discovered
}


def get_known_feeds(isin: str) -> list[str]:
    """Get verified RSS feed URLs for a company by ISIN."""
    return KNOWN_FEEDS.get(isin, [])


def has_no_feed(isin: str) -> bool:
    """Check if company is confirmed to have no RSS feed."""
    return isin in NO_FEED_ISINS
