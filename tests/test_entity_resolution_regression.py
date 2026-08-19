"""Regression tests for P0 legal-event entity-resolution hardening.

False cases that MUST NOT match (all three sources):
  BODACC: DELPHINE SAS ≠ LIMOUSIN, Delphine, LIMOUSIN (EI)
  BODACC: HERMES INTERNATIONAL ≠ SCI HERMES / HERMES GASTRONOMIE
  Gazette: K B TECH LIMITED ≠ CORENET TECH (UK) PVT LTD

Valid fixtures that MUST continue to match (regression guard):
  - Exact name matches (LVMH, TotalEnergies, INDITEX, etc.)
  - Single-word distinctive brand names (Airbus, Hermes alone)
  - Hyphenated brands (SAINT-GOBAIN)
  - Multi-token subset matches (Moet Chandon)
"""

import sqlite3
from unittest.mock import MagicMock, patch

import httpx
import pytest


# ── BODACC false-case fixtures ────────────────────────────────────────────────

def test_bodacc_delphine_sas_must_not_match_limousin_delphine_limousin_ei():
    """DELPHINE SAS must not match 'LIMOUSIN, Delphine, LIMOUSIN (EI)'."""
    from kassandra.sources.bodacc import _notice_matches_company

    # The false match: after stripping "sas", company has only ["delphine"].
    # Single-token "delphine" (len=8 >= 4) is in notice tokens → false match.
    assert not _notice_matches_company(
        "DELPHINE SAS",
        "LIMOUSIN, Delphine, LIMOUSIN (EI)",
    ), "DELPHINE SAS must not match an individual entrepreneur named Delphine Limousin"


def test_bodacc_hermes_international_must_not_match_sci_hermes():
    """HERMES INTERNATIONAL must not match SCI HERMES."""
    from kassandra.sources.bodacc import _notice_matches_company

    assert not _notice_matches_company(
        "HERMES INTERNATIONAL",
        "SOCIETE CIVILE IMMOBILIERE HERMES",
    ), "HERMES INTERNATIONAL must not match a real-estate SCI in Yvetot"


def test_bodacc_hermes_international_must_not_match_hermes_gastronomie():
    """HERMES INTERNATIONAL must not match HERMES GASTRONOMIE."""
    from kassandra.sources.bodacc import _notice_matches_company

    assert not _notice_matches_company(
        "HERMES INTERNATIONAL",
        "HERMES GASTRONOMIE",
    ), "HERMES INTERNATIONAL must not match a food wholesale company"


# ── Gazette false-case fixture ─────────────────────────────────────────────────

def test_gazette_k_b_tech_limited_must_not_match_corenet_tech():
    """K B TECH LIMITED must not match CORENET TECH (UK) PVT LTD."""
    from kassandra.sources.gazette import _notice_matches_company

    assert not _notice_matches_company(
        "K B TECH LIMITED",
        "CORENET TECH (UK) PVT LTD — Winding-Up Order",
        "Winding-up order for CORENET TECH (UK) PVT LTD",
    ), "K B TECH LIMITED must not match CORENET TECH (UK) PVT LTD via 'tech' overlap"


# ── Valid fixtures that MUST continue to match ─────────────────────────────────

def test_bodacc_lvmh_still_matches():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company("LVMH", "LVMH MOET HENNESSY LOUIS VUITTON")


def test_bodacc_totalenergies_still_matches():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company("TotalEnergies", "TOTALENERGIES SE")


def test_bodacc_airbus_still_matches():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company("Airbus", "AIRBUS SAS")


def test_bodacc_saint_gobain_still_matches():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company(
        "SAINT-GOBAIN", "SAINT GOBAIN DISTRIBUTION BATIMENT"
    )


def test_bodacc_moet_chandon_still_matches():
    from kassandra.sources.bodacc import _notice_matches_company

    assert _notice_matches_company(
        "Moet Chandon", "LVMH MOET HENNESSY CHANDON"
    )


def test_bodacc_no_match_different_company_still_works():
    from kassandra.sources.bodacc import _notice_matches_company

    assert not _notice_matches_company("LVMH", "SOCIETE GENERALE SA")
    assert not _notice_matches_company("TotalEnergies", "RENAULT SAS")


def test_gazette_invensys_limited_still_matches():
    from kassandra.sources.gazette import _notice_matches_company

    assert _notice_matches_company(
        "INVENSYS LIMITED",
        "INVENSYS LIMITED — Administration Order",
        "administration order",
    )


def test_gazette_british_gas_still_matches():
    from kassandra.sources.gazette import _notice_matches_company

    assert _notice_matches_company(
        "British Gas PLC",
        "British Gas PLC — Administration Order",
        "administration order for British Gas",
    )


def test_gazette_no_match_different_company_still_works():
    from kassandra.sources.gazette import _notice_matches_company

    assert not _notice_matches_company(
        "INVENSYS LIMITED",
        "WELCOME HOMES (SCOTLAND) LIMITED — Winding-Up",
        "winding-up",
    )


# ── BORME: no single-token confirmed direct matches with generic tokens ────────

def test_borme_conservative_single_token_suppression():
    """A single-token generic match should not create a confirmed direct event."""
    from kassandra.sources.borme import _notice_matches_company

    # "IBERDROLA" alone (1 word, distinctive) should still match
    assert _notice_matches_company(
        "IBERDROLA", "IBERDROLA S.A. — Convocatoria de junta", ""
    )
    # But something that reduces to a generic token should not match
    # "TECH SA" → after stripping SA: ["tech"]. Generic.
    # Against "SCHNEIDER ELECTRIC TECH SL" → after stripping: tokens include "tech".
    # This should be suppressed.
    assert not _notice_matches_company(
        "TECH SA",
        "SCHNEIDER ELECTRIC TECH S.L. — Fusión",
        "",
    ), "Generic single-token 'tech' must not match SCHNEIDER subsidiary"


def test_borme_inditex_still_matches():
    from kassandra.sources.borme import _notice_matches_company

    assert _notice_matches_company("INDITEX SA", "INDITEX S.A. — Reducción de capital", "")


def test_borme_telefonica_still_matches():
    from kassandra.sources.borme import _notice_matches_company

    assert _notice_matches_company("TELEFONICA SA", "TELEFONICA S.A. — Fusión por absorción", "")
