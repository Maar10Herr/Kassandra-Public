"""Portfolio management — import and maintain root company lists."""

import logging
import sqlite3
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

# Pinned Euro Stoxx 50 as of June 2025 (reproducibly pinned per goals)
# ISINs + names + domains from index composition and public data
EURO_STOXX_50 = [
    ("ASML Holding", "NL0010273215", "Technology", "NL", "asml.com", "https://www.asml.com/en/investors"),
    ("LVMH", "FR0000121014", "Consumer Cyclical", "FR", "lvmh.com", "https://www.lvmh.com/investors"),
    ("SAP", "DE0007164600", "Technology", "DE", "sap.com", "https://www.sap.com/investors.html"),
    ("TotalEnergies", "FR0000120271", "Energy", "FR", "totalenergies.com", "https://totalenergies.com/investors"),
    ("Siemens", "DE0007236101", "Technology", "DE", "siemens.com", "https://www.siemens.com/investor"),
    ("Schneider Electric", "FR0000121972", "Industrials", "FR", "se.com", "https://www.se.com/investor-relations"),
    ("Allianz", "DE0008404005", "Financial Services", "DE", "allianz.com", "https://www.allianz.com/en/investor_relations.html"),
    ("Sanofi", "FR0000120578", "Healthcare", "FR", "sanofi.com", "https://www.sanofi.com/en/investors"),
    ("L'Oréal", "FR0000120321", "Consumer Defensive", "FR", "loreal.com", "https://www.loreal.com/en/investors"),
    ("Air Liquide", "FR0000120073", "Basic Materials", "FR", "airliquide.com", "https://www.airliquide.com/investors"),
    ("Deutsche Telekom", "DE0005557508", "Communication Services", "DE", "telekom.com", "https://www.telekom.com/en/investor-relations"),
    ("Hermès International", "FR0000052292", "Consumer Cyclical", "FR", "hermes.com", "https://finance.hermes.com/en/"),
    ("Iberdrola", "ES0144580Y14", "Utilities", "ES", "iberdrola.com", "https://www.iberdrola.com/shareholders-investors"),
    ("Banco Santander", "ES0113900J37", "Financial Services", "ES", "santander.com", "https://www.santander.com/en/shareholders-and-investors"),
    ("AB InBev", "BE0974293251", "Consumer Defensive", "BE", "ab-inbev.com", "https://www.ab-inbev.com/investors"),
    ("AXA", "FR0000120628", "Financial Services", "FR", "axa.com", "https://www.axa.com/en/investor"),
    ("BNP Paribas", "FR0000131104", "Financial Services", "FR", "bnpparibas.com", "https://invest.bnpparibas.com/"),
    ("Airbus", "NL0000235190", "Industrials", "NL", "airbus.com", "https://www.airbus.com/en/investors"),
    ("EssilorLuxottica", "FR0000121667", "Healthcare", "FR", "essilorluxottica.com", "https://www.essilorluxottica.com/en/investors/"),
    ("Intesa Sanpaolo", "IT0000072618", "Financial Services", "IT", "intesasanpaolo.com", "https://group.intesasanpaolo.com/en/investor-relations"),
    ("UniCredit", "IT0005239360", "Financial Services", "IT", "unicreditgroup.eu", "https://www.unicreditgroup.eu/en/investors.html"),
    ("Enel", "IT0003128367", "Utilities", "IT", "enel.com", "https://www.enel.com/investors"),
    ("Vinci", "FR0000125486", "Industrials", "FR", "vinci.com", "https://www.vinci.com/vinci.nsf/en/investors"),
    ("DHL Group", "DE0005552004", "Industrials", "DE", "group.dhl.com", "https://group.dhl.com/en/investors.html"),
    ("ING Groep", "NL0011821202", "Financial Services", "NL", "ing.com", "https://www.ing.com/Investor-relations.htm"),
    ("Prosus", "NL0013654783", "Technology", "NL", "prosus.com", "https://www.prosus.com/investors"),
    ("BBVA", "ES0113211835", "Financial Services", "ES", "bbva.com", "https://shareholdersandinvestors.bbva.com/"),
    ("BASF", "DE000BASF111", "Basic Materials", "DE", "basf.com", "https://www.basf.com/global/en/investors.html"),
    ("Adidas", "DE000A1EWWW0", "Consumer Cyclical", "DE", "adidas-group.com", "https://www.adidas-group.com/en/investors/"),
    ("Mercedes-Benz Group", "DE0007100000", "Consumer Cyclical", "DE", "group.mercedes-benz.com", "https://group.mercedes-benz.com/investors/"),
    ("BMW", "DE0005190003", "Consumer Cyclical", "DE", "bmwgroup.com", "https://www.bmwgroup.com/en/investor-relations.html"),
    ("Bayer", "DE000BAY0017", "Healthcare", "DE", "bayer.com", "https://www.bayer.com/en/investors"),
    ("Deutsche Börse", "DE0005810055", "Financial Services", "DE", "deutsche-boerse.com", "https://www.deutsche-boerse.com/dbg-en/investor-relations"),
    ("Danone", "FR0000120644", "Consumer Defensive", "FR", "danone.com", "https://www.danone.com/investors.html"),
    ("Inditex", "ES0148396007", "Consumer Cyclical", "ES", "inditex.com", "https://www.inditex.com/itxcomweb/en/investors"),
    ("Ferrari", "NL0011585146", "Consumer Cyclical", "IT", "ferrari.com", "https://www.ferrari.com/en-EN/corporate/investors"),
    ("Munich Re", "DE0008430026", "Financial Services", "DE", "munichre.com", "https://www.munichre.com/en/investors.html"),
    ("Pernod Ricard", "FR0000120693", "Consumer Defensive", "FR", "pernod-ricard.com", "https://www.pernod-ricard.com/en/investors"),
    ("Safran", "FR0000073272", "Industrials", "FR", "safran-group.com", "https://www.safran-group.com/investors"),
    ("Stellantis", "NL00150001Q9", "Consumer Cyclical", "NL", "stellantis.com", "https://www.stellantis.com/en/investors"),
    ("Saint-Gobain", "FR0000125007", "Industrials", "FR", "saint-gobain.com", "https://www.saint-gobain.com/en/investors"),
    ("Eni", "IT0003132476", "Energy", "IT", "eni.com", "https://www.eni.com/en-IT/investors.html"),
    ("Infineon Technologies", "DE0006231004", "Technology", "DE", "infineon.com", "https://www.infineon.com/cms/en/about-infineon/investor/"),
    ("Nordea Bank", "FI4000297767", "Financial Services", "FI", "nordea.com", "https://www.nordea.com/en/investors"),
    ("Kering", "FR0000121485", "Consumer Cyclical", "FR", "kering.com", "https://www.kering.com/en/investors/"),
    ("Philips", "NL0000009538", "Healthcare", "NL", "philips.com", "https://www.philips.com/a-w/about/investor.html"),
    ("Wolters Kluwer", "NL0000395903", "Technology", "NL", "wolterskluwer.com", "https://www.wolterskluwer.com/en/investors"),
    ("Flutter Entertainment", "IE00BWT6H894", "Consumer Cyclical", "IE", "flutter.com", "https://www.flutter.com/investors"),
    ("Adyen", "NL0012969182", "Technology", "NL", "adyen.com", "https://www.adyen.com/investor-relations"),
    ("Vonovia", "DE000A1ML7J1", "Real Estate", "DE", "vonovia.de", "https://investoren.vonovia.de/"),
]


def import_euro_stoxx_50(db: sqlite3.Connection) -> int:
    """Import the pinned Euro Stoxx 50 portfolio with domain data.

    Returns count of imported companies.
    """
    now = datetime.now(timezone.utc).isoformat()

    db.execute("DELETE FROM portfolio_items WHERE portfolio_id IN (SELECT id FROM portfolios WHERE name = 'Euro Stoxx 50')")
    db.execute("DELETE FROM portfolios WHERE name = 'Euro Stoxx 50'")
    db.execute("INSERT INTO portfolios (name, created_at) VALUES (?, ?)", ("Euro Stoxx 50", now))
    portfolio_id = db.execute("SELECT id FROM portfolios WHERE name = ?", ("Euro Stoxx 50",)).fetchone()["id"]

    count = 0
    for name, isin, sector, country, domain, ir_url in EURO_STOXX_50:
        ticker = name.split(" (")[0].upper().replace(" ", "-")[:20]
        db.execute(
            """INSERT INTO portfolio_items
               (portfolio_id, ticker, isin, name, sector, country, weight, source, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (portfolio_id, ticker, isin, name, sector, country, 1.0 / len(EURO_STOXX_50), "pinned_list", now),
        )
        count += 1

    db.commit()
    logger.info(f"Imported {count} Euro Stoxx 50 companies with domain data")
    return count


# Lookup table: ISIN → (domain, ir_url)
def get_domain_data() -> dict[str, tuple[str, str]]:
    """Return ISIN → (domain, ir_url) mapping for Euro Stoxx 50."""
    return {isin: (domain, ir_url) for _, isin, _, _, domain, ir_url in EURO_STOXX_50}
